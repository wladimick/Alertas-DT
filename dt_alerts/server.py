from __future__ import annotations

import html
import json
import re
import threading
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import db
from .config import Settings, get_settings
from .notifier import dispatch_alert
from .worker import run_check, scheduler_loop


def h(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def bool_from_form(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).lower() in {"1", "true", "on", "yes", "si", "sí"}


class AppHandler(BaseHTTPRequestHandler):
    settings: Settings

    def do_GET(self) -> None:
        try:
            self.route_get()
        except Exception as exc:
            self.render_error(exc)

    def do_POST(self) -> None:
        try:
            self.route_post()
        except Exception as exc:
            self.render_error(exc)

    def route_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self.respond_html(render_public_form(self.settings, embed=False, query=query))
        elif path == "/embed":
            self.respond_html(render_public_form(self.settings, embed=True, query=query))
        elif path == "/thanks":
            self.respond_html(render_thanks(embed=bool_from_form(query.get("embed", ["0"])[0])))
        elif path == "/healthz":
            self.respond_json({"ok": True, "service": "dt-alertas"})
        elif path == "/admin/login":
            token = query.get("token", [""])[0]
            if token and token == self.settings.admin_token:
                self.redirect("/admin", set_admin_cookie=True)
            else:
                self.respond_html(render_login())
        elif path in {"/admin", "/admin/subscribers", "/admin/alerts", "/admin/documents"}:
            if not self.is_admin():
                self.redirect("/admin/login")
                return
            self.respond_html(render_admin(path, self.settings))
        else:
            self.respond_not_found()

    def route_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/subscribe":
            self.handle_subscribe()
        elif path == "/admin/login":
            payload = self.read_payload()
            if payload.get("token") == self.settings.admin_token:
                self.redirect("/admin", set_admin_cookie=True)
            else:
                self.respond_html(render_login(error="Token inválido."), status=HTTPStatus.UNAUTHORIZED)
        elif path == "/api/jobs/check-dt":
            if not self.is_job_authorized():
                self.respond_json({"error": "No autorizado."}, status=HTTPStatus.UNAUTHORIZED)
                return
            result = run_check(self.settings)
            self.respond_json(result)
        elif match := re.match(r"^/admin/subscribers/(\d+)/(pause|reactivate)$", path):
            self.require_admin()
            subscriber_id = int(match.group(1))
            action = match.group(2)
            with db.connect(self.settings.database_path) as conn:
                db.set_subscriber_status(
                    conn, subscriber_id, "paused" if action == "pause" else "active"
                )
            self.redirect("/admin/subscribers")
        elif match := re.match(r"^/admin/alerts/(\d+)/resend$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                dispatch_alert(conn, alert_id, self.settings)
            self.redirect("/admin/alerts")
        elif match := re.match(r"^/admin/documents/(\d+)/ignore$", path):
            self.require_admin()
            document_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                db.set_document_status(conn, document_id, "ignored")
            self.redirect("/admin/documents")
        else:
            self.respond_not_found()

    def handle_subscribe(self) -> None:
        payload = self.read_payload()
        with db.connect(self.settings.database_path) as conn:
            subscriber = db.upsert_subscriber(
                conn,
                email=payload.get("email", ""),
                whatsapp=payload.get("whatsapp_optional") or payload.get("whatsapp"),
                notify_email=bool_from_form(payload.get("notify_email", True)),
                notify_whatsapp=bool_from_form(payload.get("notify_whatsapp")),
                source_page=payload.get("source_page"),
                consent=bool_from_form(payload.get("consent")),
            )
        if self.wants_json():
            self.respond_json({"ok": True, "subscriber": public_subscriber(subscriber)})
            return
        embed = bool_from_form(payload.get("embed"))
        self.redirect(f"/thanks?embed={int(embed)}")

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8") or "{}")
        parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def wants_json(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "application/json" in accept

    def is_admin(self) -> bool:
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie(cookie_header)
        token = cookie.get("dt_admin_token")
        return bool(token and token.value == self.settings.admin_token)

    def require_admin(self) -> None:
        if not self.is_admin():
            raise PermissionError("No autorizado.")

    def is_job_authorized(self) -> bool:
        return self.headers.get("X-Job-Token") == self.settings.job_token

    def redirect(self, target: str, *, set_admin_cookie: bool = False) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", target)
        if set_admin_cookie:
            self.send_header(
                "Set-Cookie",
                f"dt_admin_token={self.settings.admin_token}; Path=/; HttpOnly; SameSite=Lax",
            )
        self.end_headers()

    def respond_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def respond_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def respond_not_found(self) -> None:
        self.respond_html(render_page("No encontrado", "<h1>No encontrado</h1>"), status=HTTPStatus.NOT_FOUND)

    def render_error(self, exc: Exception) -> None:
        if self.wants_json() or self.path.startswith("/api/"):
            self.respond_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        else:
            self.respond_html(
                render_page("Error", f"<h1>Error</h1><p>{h(exc)}</p>"),
                status=HTTPStatus.BAD_REQUEST,
            )

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def public_subscriber(subscriber: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": subscriber["id"],
        "email": subscriber["email"],
        "notify_email": bool(subscriber["notify_email"]),
        "notify_whatsapp": bool(subscriber["notify_whatsapp"]),
        "status": subscriber["status"],
    }


def render_public_form(
    settings: Settings, *, embed: bool, query: dict[str, list[str]]
) -> str:
    source_page = query.get("source_page", ["wordpress"])[0]
    body = f"""
<section class="signup-shell">
  <div class="signup-copy">
    <p class="eyebrow">Alertas Dirección del Trabajo</p>
    <h1>Recibe novedades laborales relevantes para tu gestión contable</h1>
    <p class="lead">Avisos automáticos cuando la DT publique dictámenes, ordinarios, circulares, resoluciones u otros documentos normativos.</p>
  </div>
  <form class="panel form-panel" method="post" action="/api/subscribe">
    <input type="hidden" name="source_page" value="{h(source_page)}">
    <input type="hidden" name="embed" value="{int(embed)}">
    <label>
      Correo electrónico
      <input name="email" type="email" required placeholder="nombre@empresa.cl">
    </label>
    <label>
      WhatsApp opcional
      <input name="whatsapp_optional" type="tel" placeholder="+56912345678">
    </label>
    <div class="checks">
      <label><input type="checkbox" name="notify_email" checked> Email</label>
      <label><input type="checkbox" name="notify_whatsapp"> WhatsApp</label>
    </div>
    <label class="consent">
      <input type="checkbox" name="consent" required>
      Acepto recibir alertas sobre normativa DT y comunicaciones asociadas a esta suscripción.
    </label>
    <button type="submit">Suscribirme</button>
    <p class="fineprint">WhatsApp requiere consentimiento y activación posterior de API Business.</p>
  </form>
</section>
"""
    return render_page("Alertas DT", body, compact=embed)


def render_thanks(*, embed: bool) -> str:
    body = """
<section class="panel thanks">
  <p class="eyebrow">Suscripción registrada</p>
  <h1>Listo, quedaste inscrito en Alertas DT.</h1>
  <p>Cuando se detecte nueva normativa relevante, recibirás la alerta según tus preferencias.</p>
</section>
"""
    return render_page("Suscripción registrada", body, compact=embed)


def render_login(error: str | None = None) -> str:
    error_html = f'<p class="error">{h(error)}</p>' if error else ""
    body = f"""
<section class="panel login">
  <p class="eyebrow">Panel admin</p>
  <h1>Ingresar</h1>
  {error_html}
  <form method="post" action="/admin/login">
    <label>
      Token
      <input name="token" type="password" required autocomplete="current-password">
    </label>
    <button type="submit">Entrar</button>
  </form>
</section>
"""
    return render_page("Admin", body)


def render_admin(path: str, settings: Settings) -> str:
    with db.connect(settings.database_path) as conn:
        subscribers = db.list_subscribers(conn)
        alerts = db.list_alerts(conn)
        documents = db.list_documents(conn)
        jobs = db.latest_jobs(conn)

    active_count = sum(1 for item in subscribers if item["status"] == "active")
    ready_count = sum(1 for item in alerts if item["status"] == "ready")
    pending_count = sum(1 for item in alerts if item["status"] == "pending_review")
    body = f"""
<header class="admin-header">
  <div>
    <p class="eyebrow">Alertas DT</p>
    <h1>Panel de administración</h1>
  </div>
  <form method="post" action="/api/jobs/check-dt">
    <input type="hidden" name="manual" value="1">
    <button type="submit" formmethod="post" formaction="/api/jobs/check-dt" data-job-token>Ejecutar monitoreo</button>
  </form>
</header>
<section class="metrics">
  <div><strong>{len(subscribers)}</strong><span>Suscriptores</span></div>
  <div><strong>{active_count}</strong><span>Activos</span></div>
  <div><strong>{len(documents)}</strong><span>Documentos</span></div>
  <div><strong>{ready_count}</strong><span>Alertas listas</span></div>
  <div><strong>{pending_count}</strong><span>Por revisar</span></div>
</section>
{render_nav(path)}
{render_jobs(jobs)}
{render_admin_section(path, subscribers, alerts, documents)}
"""
    body += """
<script>
document.querySelectorAll('[data-job-token]').forEach(function(button) {
  button.closest('form').addEventListener('submit', async function(event) {
    event.preventDefault();
    button.disabled = true;
    button.textContent = 'Ejecutando...';
    try {
      const token = prompt('JOB_TOKEN');
      if (!token) return;
      await fetch('/api/jobs/check-dt', { method: 'POST', headers: { 'X-Job-Token': token } });
      location.reload();
    } finally {
      button.disabled = false;
      button.textContent = 'Ejecutar monitoreo';
    }
  });
});
</script>
"""
    return render_page("Admin Alertas DT", body)


def render_nav(path: str) -> str:
    links = [
        ("/admin", "Resumen"),
        ("/admin/subscribers", "Suscriptores"),
        ("/admin/alerts", "Alertas"),
        ("/admin/documents", "Documentos"),
    ]
    items = "".join(
        f'<a class="{ "active" if path == href else "" }" href="{href}">{label}</a>'
        for href, label in links
    )
    return f'<nav class="tabs">{items}</nav>'


def render_admin_section(
    path: str,
    subscribers: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> str:
    if path == "/admin/subscribers":
        return render_subscribers(subscribers)
    if path == "/admin/alerts":
        return render_alerts(alerts)
    if path == "/admin/documents":
        return render_documents(documents)
    return render_alerts(alerts[:10]) + render_subscribers(subscribers[:10])


def render_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return '<section class="panel"><h2>Últimos jobs</h2><p class="muted">Sin ejecuciones todavía.</p></section>'
    rows = "".join(
        f"""
<tr>
  <td>{h(job['started_at'])}</td>
  <td><span class="pill">{h(job['status'])}</span></td>
  <td>{h(job['discovered_count'])}</td>
  <td>{h(job['processed_count'])}</td>
  <td>{h(job['sent_count'])}</td>
  <td>{h(job.get('error'))}</td>
</tr>
"""
        for job in jobs
    )
    return f"""
<section class="panel">
  <h2>Últimos jobs</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Inicio</th><th>Estado</th><th>Nuevos</th><th>Procesados</th><th>Envíos</th><th>Error</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>
"""


def render_subscribers(subscribers: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
<tr>
  <td>{h(item['email'])}</td>
  <td>{h(item.get('whatsapp'))}</td>
  <td>{'Email' if item['notify_email'] else ''} {'WhatsApp' if item['notify_whatsapp'] else ''}</td>
  <td><span class="pill">{h(item['status'])}</span></td>
  <td>{h(item['premium_status'])}</td>
  <td>
    <form method="post" action="/admin/subscribers/{item['id']}/{'pause' if item['status'] == 'active' else 'reactivate'}">
      <button class="secondary" type="submit">{'Pausar' if item['status'] == 'active' else 'Reactivar'}</button>
    </form>
  </td>
</tr>
"""
        for item in subscribers
    )
    return f"""
<section class="panel">
  <h2>Suscriptores</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Email</th><th>WhatsApp</th><th>Canales</th><th>Estado</th><th>Plan</th><th></th></tr></thead>
      <tbody>{rows or '<tr><td colspan="6">Sin suscriptores.</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


def render_alerts(alerts: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
<tr>
  <td>
    <strong>{h(item['title'])}</strong>
    <p class="muted">{h(item['summary'])}</p>
  </td>
  <td>{h(item['category'])}</td>
  <td><span class="pill">{h(item['relevance'])}</span></td>
  <td><span class="pill">{h(item['status'])}</span></td>
  <td><a href="{h(item['canonical_url'])}" target="_blank" rel="noreferrer">DT</a></td>
  <td>
    <form method="post" action="/admin/alerts/{item['id']}/resend">
      <button class="secondary" type="submit">Reenviar</button>
    </form>
  </td>
</tr>
"""
        for item in alerts
    )
    return f"""
<section class="panel">
  <h2>Alertas</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Documento</th><th>Categoría</th><th>Relevancia</th><th>Estado</th><th>Link</th><th></th></tr></thead>
      <tbody>{rows or '<tr><td colspan="6">Sin alertas.</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


def render_documents(documents: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
<tr>
  <td><strong>{h(item['title'])}</strong><p class="muted">{h(item.get('abstract'))}</p></td>
  <td>{h(item['category'])}</td>
  <td>{h(item.get('publication_date'))}</td>
  <td><span class="pill">{h(item['status'])}</span></td>
  <td><a href="{h(item['canonical_url'])}" target="_blank" rel="noreferrer">DT</a></td>
  <td>
    <form method="post" action="/admin/documents/{item['id']}/ignore">
      <button class="secondary" type="submit">Ignorar</button>
    </form>
  </td>
</tr>
"""
        for item in documents
    )
    return f"""
<section class="panel">
  <h2>Documentos detectados</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Documento</th><th>Categoría</th><th>Fecha</th><th>Estado</th><th>Link</th><th></th></tr></thead>
      <tbody>{rows or '<tr><td colspan="6">Sin documentos.</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


def render_page(title: str, body: str, *, compact: bool = False) -> str:
    shell_class = "compact" if compact else ""
    return f"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <style>{CSS}</style>
</head>
<body class="{shell_class}">
  <main class="app">{body}</main>
</body>
</html>
""".strip()


CSS = """
:root {
  color-scheme: light;
  --ink: #18212f;
  --muted: #667085;
  --line: #d9e2ef;
  --panel: #ffffff;
  --bg: #f4f6f9;
  --brand: #0b5cab;
  --brand-dark: #083e74;
  --accent: #127864;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: Arial, Helvetica, sans-serif;
  letter-spacing: 0;
}
.app {
  width: min(1120px, calc(100% - 32px));
  margin: 0 auto;
  padding: 32px 0;
}
body.compact .app {
  width: 100%;
  padding: 0;
}
.signup-shell {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 440px);
  gap: 24px;
  align-items: start;
}
body.compact .signup-shell {
  grid-template-columns: 1fr;
}
.signup-copy {
  padding: 24px 0;
}
.eyebrow {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--accent);
  font-weight: 700;
  text-transform: uppercase;
}
h1 {
  margin: 0 0 12px;
  font-size: 32px;
  line-height: 1.15;
}
h2 {
  margin: 0 0 16px;
  font-size: 20px;
}
.lead {
  max-width: 620px;
  color: var(--muted);
  font-size: 17px;
  line-height: 1.5;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 18px;
}
.form-panel {
  display: grid;
  gap: 14px;
}
label {
  display: grid;
  gap: 7px;
  font-size: 14px;
  font-weight: 700;
}
input {
  width: 100%;
  border: 1px solid #b9c5d6;
  border-radius: 6px;
  padding: 11px 12px;
  font: inherit;
}
input[type="checkbox"] {
  width: auto;
}
.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
.checks label, .consent {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.consent {
  align-items: flex-start;
  line-height: 1.4;
}
button {
  border: 0;
  border-radius: 6px;
  background: var(--brand);
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-weight: 700;
  padding: 11px 14px;
}
button:hover { background: var(--brand-dark); }
button.secondary {
  background: #eef4fb;
  color: var(--brand-dark);
  padding: 8px 10px;
}
button.secondary:hover { background: #dceafa; }
.fineprint, .muted {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.4;
}
.thanks, .login {
  max-width: 560px;
  margin: 0 auto;
}
.error {
  color: #b42318;
  font-weight: 700;
}
.admin-header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  margin-bottom: 18px;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.metrics div {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px;
}
.metrics strong {
  display: block;
  font-size: 24px;
}
.metrics span {
  color: var(--muted);
  font-size: 13px;
}
.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 18px;
}
.tabs a {
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--ink);
  padding: 9px 12px;
  text-decoration: none;
  background: #fff;
}
.tabs a.active {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
.table-wrap {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  border-bottom: 1px solid var(--line);
  padding: 10px;
  text-align: left;
  vertical-align: top;
  font-size: 14px;
}
th {
  color: #344054;
  font-size: 12px;
  text-transform: uppercase;
}
.pill {
  display: inline-block;
  background: #edf7f5;
  color: #0f5e51;
  border-radius: 999px;
  padding: 4px 8px;
  font-size: 12px;
  font-weight: 700;
}
td form {
  margin: 0;
}
@media (max-width: 760px) {
  .app { width: min(100% - 20px, 1120px); padding: 20px 0; }
  .signup-shell { grid-template-columns: 1fr; }
  h1 { font-size: 26px; }
  .admin-header { display: grid; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
"""


def run_server(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    db.init_db(settings.database_path)
    AppHandler.settings = settings
    if settings.run_worker:
        thread = threading.Thread(target=scheduler_loop, args=(settings,), daemon=True)
        thread.start()
    server = ThreadingHTTPServer((settings.app_host, settings.app_port), AppHandler)
    print(f"Alertas DT escuchando en http://{settings.app_host}:{settings.app_port}")
    server.serve_forever()
