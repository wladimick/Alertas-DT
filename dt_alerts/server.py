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


# TEMPORAL: desactiva el login por token del panel admin para esta ocasión.
# Volver a False (o eliminar el bypass en is_admin) para reactivar la autenticación.
DISABLE_ADMIN_AUTH = True


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
            if DISABLE_ADMIN_AUTH:
                self.redirect("/admin")
                return
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
        if DISABLE_ADMIN_AUTH:
            return True
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
        body = (
            '<section class="eg-container eg-feedback"><div class="eg-card eg-feedback__card" data-eg-theme="light">'
            '<p class="eg-eyebrow">Error 404</p><h1>No encontrado</h1>'
            '<p class="eg-feedback__lead">La página que buscas no existe o fue movida.</p>'
            '<a class="eg-btn eg-btn--secondary" href="/">Volver al inicio</a></div></section>'
        )
        self.respond_html(render_page("No encontrado", body), status=HTTPStatus.NOT_FOUND)

    def render_error(self, exc: Exception) -> None:
        if self.wants_json() or self.path.startswith("/api/"):
            self.respond_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        else:
            body = (
                '<section class="eg-container eg-feedback"><div class="eg-card eg-feedback__card" data-eg-theme="light" data-eg-accent="blue">'
                f'<p class="eg-eyebrow">Ocurrió un problema</p><h1>Error</h1><p class="eg-feedback__lead">{h(exc)}</p>'
                '<a class="eg-btn eg-btn--secondary" href="/">Volver al inicio</a></div></section>'
            )
            self.respond_html(
                render_page("Error", body),
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
    # Tarjeta de formulario: tema claro forzado para lectura y orden (guía EG §5/§19).
    form = f"""
  <form class="eg-card eg-form" data-eg-theme="light" method="post" action="/api/subscribe">
    <input type="hidden" name="source_page" value="{h(source_page)}">
    <input type="hidden" name="embed" value="{int(embed)}">
    <p class="eg-eyebrow">Suscripción</p>
    <h2 class="eg-form__title">Activa tus alertas</h2>
    <div class="eg-field">
      <label class="eg-label" for="eg-email">Correo electrónico</label>
      <input class="eg-input" id="eg-email" name="email" type="email" required placeholder="nombre@empresa.cl">
    </div>
    <div class="eg-field">
      <label class="eg-label" for="eg-wa">WhatsApp <span class="eg-label__hint">opcional</span></label>
      <input class="eg-input" id="eg-wa" name="whatsapp_optional" type="tel" placeholder="+56912345678">
    </div>
    <div class="eg-checks">
      <label class="eg-check"><input type="checkbox" name="notify_email" checked> Email</label>
      <label class="eg-check"><input type="checkbox" name="notify_whatsapp"> WhatsApp</label>
    </div>
    <label class="eg-check eg-check--consent">
      <input type="checkbox" name="consent" required>
      <span>Acepto recibir alertas sobre normativa DT y comunicaciones asociadas a esta suscripción.</span>
    </label>
    <button class="eg-btn eg-btn--primary eg-btn--block" type="submit">Suscribirme</button>
    <p class="eg-fineprint">WhatsApp requiere consentimiento y activación posterior de API Business.</p>
  </form>
"""
    if embed:
        # En el iframe no mostramos hero ni header: solo la tarjeta funcional.
        body = f'<div class="eg-embed">{form}</div>'
        return render_page("Alertas DT", body, compact=embed)

    body = f"""
<section class="eg-hero" data-eg-theme="dark" data-eg-accent="green">
  <span class="eg-glow eg-glow--a" aria-hidden="true"></span>
  <span class="eg-glow eg-glow--b" aria-hidden="true"></span>
  <div class="eg-container eg-hero__grid">
    <div class="eg-hero__copy eg-fade-up">
      <p class="eg-eyebrow">Alertas Dirección del Trabajo</p>
      <h1 class="eg-hero__title">Novedades laborales <span>relevantes</span> para tu gestión contable</h1>
      <p class="eg-hero__lead">Avisos automáticos cuando la DT publique dictámenes, ordinarios, circulares, resoluciones u otros documentos normativos.</p>
      <ul class="eg-hero__points">
        <li class="eg-chip">Monitoreo continuo</li>
        <li class="eg-chip">Resumen orientado a contadores</li>
        <li class="eg-chip">Email y WhatsApp</li>
      </ul>
    </div>
    {form}
  </div>
</section>
"""
    return render_page("Alertas DT", body, compact=embed)


def render_thanks(*, embed: bool) -> str:
    body = """
<section class="eg-container eg-feedback">
  <div class="eg-card eg-feedback__card" data-eg-theme="light" data-eg-accent="green">
    <span class="eg-feedback__icon" aria-hidden="true">&#10003;</span>
    <p class="eg-eyebrow">Suscripción registrada</p>
    <h1>Listo, quedaste inscrito en Alertas DT.</h1>
    <p class="eg-feedback__lead">Cuando se detecte nueva normativa relevante, recibirás la alerta según tus preferencias.</p>
  </div>
</section>
"""
    return render_page("Suscripción registrada", body, compact=embed)


def render_login(error: str | None = None) -> str:
    error_html = f'<p class="eg-error">{h(error)}</p>' if error else ""
    body = f"""
<section class="eg-container eg-auth">
  <div class="eg-card eg-auth__card" data-eg-theme="light">
    <p class="eg-eyebrow">Panel admin</p>
    <h1>Ingresar</h1>
    {error_html}
    <form class="eg-form" method="post" action="/admin/login">
      <div class="eg-field">
        <label class="eg-label" for="eg-token">Token</label>
        <input class="eg-input" id="eg-token" name="token" type="password" required autocomplete="current-password">
      </div>
      <button class="eg-btn eg-btn--primary eg-btn--block" type="submit">Entrar</button>
    </form>
  </div>
</section>
"""
    return render_page("Admin", body, theme="dark")


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
<header class="eg-admin-header">
  <div>
    <p class="eg-eyebrow">Alertas DT</p>
    <h1>Panel de administración</h1>
  </div>
  <form method="post" action="/api/jobs/check-dt">
    <input type="hidden" name="manual" value="1">
    <button class="eg-btn eg-btn--primary" type="submit" formmethod="post" formaction="/api/jobs/check-dt" data-job-token>Ejecutar monitoreo</button>
  </form>
</header>
<section class="eg-metrics">
  <div class="eg-metric"><strong>{len(subscribers)}</strong><span>Suscriptores</span></div>
  <div class="eg-metric"><strong>{active_count}</strong><span>Activos</span></div>
  <div class="eg-metric"><strong>{len(documents)}</strong><span>Documentos</span></div>
  <div class="eg-metric"><strong>{ready_count}</strong><span>Alertas listas</span></div>
  <div class="eg-metric"><strong>{pending_count}</strong><span>Por revisar</span></div>
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
    return render_page("Admin Alertas DT", body, density="compact")


def render_nav(path: str) -> str:
    links = [
        ("/admin", "Resumen"),
        ("/admin/subscribers", "Suscriptores"),
        ("/admin/alerts", "Alertas"),
        ("/admin/documents", "Documentos"),
    ]
    items = "".join(
        f'<a class="eg-tab{ " is-active" if path == href else "" }" href="{href}">{label}</a>'
        for href, label in links
    )
    return f'<nav class="eg-tabs">{items}</nav>'


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
        return '<section class="eg-card eg-panel"><h2>Últimos jobs</h2><p class="eg-muted">Sin ejecuciones todavía.</p></section>'
    rows = "".join(
        f"""
<tr>
  <td>{h(job['started_at'])}</td>
  <td><span class="eg-pill">{h(job['status'])}</span></td>
  <td>{h(job['discovered_count'])}</td>
  <td>{h(job['processed_count'])}</td>
  <td>{h(job['sent_count'])}</td>
  <td>{h(job.get('error'))}</td>
</tr>
"""
        for job in jobs
    )
    return f"""
<section class="eg-card eg-panel">
  <h2>Últimos jobs</h2>
  <div class="eg-table-wrap">
    <table class="eg-table">
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
  <td><span class="eg-pill">{h(item['status'])}</span></td>
  <td>{h(item['premium_status'])}</td>
  <td>
    <form method="post" action="/admin/subscribers/{item['id']}/{'pause' if item['status'] == 'active' else 'reactivate'}">
      <button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">{'Pausar' if item['status'] == 'active' else 'Reactivar'}</button>
    </form>
  </td>
</tr>
"""
        for item in subscribers
    )
    return f"""
<section class="eg-card eg-panel">
  <h2>Suscriptores</h2>
  <div class="eg-table-wrap">
    <table class="eg-table">
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
    <p class="eg-muted">{h(item['summary'])}</p>
  </td>
  <td>{h(item['category'])}</td>
  <td><span class="eg-pill">{h(item['relevance'])}</span></td>
  <td><span class="eg-pill">{h(item['status'])}</span></td>
  <td><a href="{h(item['canonical_url'])}" target="_blank" rel="noreferrer">DT</a></td>
  <td>
    <form method="post" action="/admin/alerts/{item['id']}/resend">
      <button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Reenviar</button>
    </form>
  </td>
</tr>
"""
        for item in alerts
    )
    return f"""
<section class="eg-card eg-panel">
  <h2>Alertas</h2>
  <div class="eg-table-wrap">
    <table class="eg-table">
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
  <td><strong>{h(item['title'])}</strong><p class="eg-muted">{h(item.get('abstract'))}</p></td>
  <td>{h(item['category'])}</td>
  <td>{h(item.get('publication_date'))}</td>
  <td><span class="eg-pill">{h(item['status'])}</span></td>
  <td><a href="{h(item['canonical_url'])}" target="_blank" rel="noreferrer">DT</a></td>
  <td>
    <form method="post" action="/admin/documents/{item['id']}/ignore">
      <button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Ignorar</button>
    </form>
  </td>
</tr>
"""
        for item in documents
    )
    return f"""
<section class="eg-card eg-panel">
  <h2>Documentos detectados</h2>
  <div class="eg-table-wrap">
    <table class="eg-table">
      <thead><tr><th>Documento</th><th>Categoría</th><th>Fecha</th><th>Estado</th><th>Link</th><th></th></tr></thead>
      <tbody>{rows or '<tr><td colspan="6">Sin documentos.</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


# Logos oficiales External Group (claro para fondos oscuros, oscuro para fondos claros).
EG_LOGO_DARK = "https://externalgroup.cl/sitioweb/wp-content/uploads/2025/09/external-group.png"
EG_LOGO_LIGHT = "https://externalgroup.cl/sitioweb/wp-content/uploads/2022/07/external-group-blanco.png"


def render_header() -> str:
    # Header claro con logo oscuro (guía EG §14).
    return f"""
<header class="eg-header">
  <div class="eg-container eg-header__inner">
    <a class="eg-header__brand" href="/" aria-label="External Group · Inicio">
      <img class="eg-logo" src="{EG_LOGO_DARK}" alt="External Group" />
    </a>
    <span class="eg-header__tag">Alertas DT</span>
  </div>
</header>
"""


def render_footer() -> str:
    # Footer oscuro sobrio con logo blanco (guía EG §15).
    return f"""
<footer class="eg-footer" data-eg-theme="dark">
  <div class="eg-container eg-footer__inner">
    <img class="eg-logo eg-logo--sm" src="{EG_LOGO_LIGHT}" alt="External Group" />
    <p class="eg-footer__note">El resumen es informativo y no reemplaza la lectura del documento oficial de la Dirección del Trabajo.</p>
    <p class="eg-footer__copy">© External Group · Servicios especializados de gestión y tecnología.</p>
  </div>
</footer>
"""


def render_page(
    title: str,
    body: str,
    *,
    compact: bool = False,
    theme: str = "light",
    density: str = "editorial",
) -> str:
    body_class = "eg eg--compact" if compact else "eg"
    # En modo embed (iframe) no incluimos header/footer ni cromo del shell.
    chrome_top = "" if compact else render_header()
    chrome_bottom = "" if compact else render_footer()
    return f"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700&family=Montserrat:wght@500;600;700&display=swap" rel="stylesheet">
  <style>{CSS}</style>
</head>
<body class="{body_class}" data-eg-theme="{h(theme)}" data-eg-density="{h(density)}">
  {chrome_top}
  <main class="eg-app">{body}</main>
  {chrome_bottom}
</body>
</html>
""".strip()


CSS = """
/* =====================================================================
   External Group · Sistema de diseño (capa de implementación .eg-*)
   Un sistema, dos temas (data-eg-theme), theming por sección.
   ===================================================================== */

/* 3. Tokens oficiales de marca */
:root {
  --eg-brand-primary: #243743;
  --eg-brand-accent: #29B78D;
  --eg-brand-text: #929090;
  --eg-brand-secondary: #E9E9E9;
  --eg-brand-dark: #0E2230;
  --eg-brand-mint: #24EBA1;
  --eg-brand-blue: #06A4F5;
  --eg-brand-deep: #0A2231;

  --eg-font-heading: "Montserrat", system-ui, sans-serif;
  --eg-font-body: "Lato", system-ui, sans-serif;
  --eg-radius: 16px;
  --eg-radius-lg: 24px;
}

/* 4. Modo oscuro (institucional / alto impacto) */
:root,
[data-eg-theme="dark"] {
  color-scheme: dark;
  --eg-bg: #0A2231;
  --eg-surface: #0E2230;
  --eg-surface-2: #243743;
  --eg-border: rgba(255, 255, 255, 0.10);
  --eg-border-accent: rgba(41, 183, 141, 0.32);
  --eg-cta: #29B78D;
  --eg-cta-hover: #24EBA1;
  --eg-text-on-cta: #0A2231;
  --eg-support: #06A4F5;
  --eg-accent: #29B78D;
  --eg-text: #F6FAFC;
  --eg-text-muted: #C7D1D6;
  --eg-text-subtle: #8EA1AA;
  --eg-focus: #24EBA1;
}

/* 5. Modo claro (lectura / contenido denso) */
[data-eg-theme="light"] {
  color-scheme: light;
  --eg-bg: #F6F8F9;
  --eg-surface: #FFFFFF;
  --eg-surface-2: #E9E9E9;
  --eg-border: rgba(36, 55, 67, 0.12);
  --eg-border-accent: rgba(41, 183, 141, 0.32);
  --eg-cta: #29B78D;
  --eg-cta-hover: #167A5F;
  --eg-text-on-cta: #0A2231;
  --eg-support: #0478B4;
  --eg-accent: #167A5F;
  --eg-text: #0A2231;
  --eg-text-muted: #3C4A52;
  --eg-text-subtle: #6D7478;
  --eg-focus: #0478B4;
}

/* 10.2 Acento de sección */
[data-eg-accent="green"] { --eg-accent: #29B78D; --eg-border-accent: rgba(41,183,141,.35); }
[data-eg-accent="mint"]  { --eg-accent: #24EBA1; --eg-border-accent: rgba(36,235,161,.35); }
[data-eg-accent="blue"]  { --eg-accent: #06A4F5; --eg-border-accent: rgba(6,164,245,.35); }
[data-eg-theme="light"][data-eg-accent="green"] { --eg-accent: #167A5F; }
[data-eg-theme="light"][data-eg-accent="blue"]  { --eg-accent: #0478B4; }

/* 10.3 Densidad */
[data-eg-density="editorial"] { --eg-section-pad: clamp(56px, 9vw, 120px); }
[data-eg-density="compact"]   { --eg-section-pad: clamp(32px, 5vw, 56px); }

/* ---------- Base ---------- */
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body.eg {
  margin: 0;
  background: var(--eg-bg);
  color: var(--eg-text);
  font-family: var(--eg-font-body);
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.eg h1, .eg h2, .eg h3, .eg h4 {
  font-family: var(--eg-font-heading);
  font-weight: 700;
  color: var(--eg-text);
  letter-spacing: -0.02em;
  margin: 0 0 .5em;
  line-height: 1.1;
}
.eg h1 { font-size: clamp(1.9rem, 4vw, 2.8rem); }
.eg h2 { font-size: clamp(1.35rem, 2.4vw, 1.85rem); }
.eg p { margin: 0 0 1rem; color: var(--eg-text-muted); }
.eg a { color: var(--eg-support); text-decoration: none; }
.eg a:hover { color: var(--eg-accent); }
.eg img { max-width: 100%; }
.eg-app { min-height: 40vh; }

/* 8. Contenedor */
.eg-container { width: min(1180px, calc(100% - 40px)); margin-inline: auto; }

/* ---------- Header (14) ---------- */
.eg-header {
  position: sticky; top: 0; z-index: 30;
  background: rgba(255, 255, 255, .92);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid rgba(36, 55, 67, .10);
}
.eg-header__inner { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 72px; }
.eg-header__brand { display: inline-flex; align-items: center; }
.eg-logo { height: 46px; width: auto; display: block; }
.eg-logo--sm { height: 40px; }
.eg-header__tag {
  font-family: var(--eg-font-body); font-size: 12px; font-weight: 700;
  letter-spacing: .14em; text-transform: uppercase; color: var(--eg-brand-primary);
  border: 1px solid rgba(36,55,67,.12); border-radius: 999px; padding: 6px 12px;
}

/* ---------- Hero (13) ---------- */
.eg-hero {
  position: relative; overflow: hidden;
  padding: clamp(48px, 8vw, 96px) 0;
  background:
    radial-gradient(circle at 18% 20%, rgba(36,235,161,.16), transparent 32%),
    radial-gradient(circle at 86% 28%, rgba(6,164,245,.13), transparent 30%),
    linear-gradient(135deg, #0A2231 0%, #0E2230 54%, #243743 100%);
  color: #F6FAFC;
}
.eg-hero__grid {
  position: relative; z-index: 2;
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(330px, 440px);
  gap: clamp(28px, 5vw, 56px); align-items: center;
}
.eg-hero__copy { color: #F6FAFC; }
.eg-hero__title {
  font-family: var(--eg-font-heading); font-weight: 700;
  font-size: clamp(2.1rem, 5vw, 3.4rem); line-height: 1.04;
  letter-spacing: -0.03em; color: #F6FAFC; margin: 12px 0 16px;
}
.eg-hero__title span { color: var(--eg-accent); }
.eg-hero__lead { max-width: 560px; color: var(--eg-text-muted); font-size: clamp(1rem, 1.2vw, 1.12rem); }
.eg-hero__points { list-style: none; display: flex; flex-wrap: wrap; gap: 10px; padding: 0; margin: 22px 0 0; }

/* Glow de marca (16) */
.eg-glow {
  position: absolute; pointer-events: none; border-radius: 999px; z-index: 1;
  width: 420px; height: 420px; filter: blur(10px);
  background: radial-gradient(circle, rgba(36,235,161,.18), transparent 66%);
}
.eg-glow--a { top: -120px; left: -80px; }
.eg-glow--b { bottom: -160px; right: -60px; background: radial-gradient(circle, rgba(6,164,245,.16), transparent 66%); }

/* ---------- Eyebrow / Chip (9.4 / 9.5) ---------- */
.eg-eyebrow {
  display: inline-flex; align-items: center; gap: 8px; margin: 0 0 6px;
  font-size: .72rem; font-weight: 700; letter-spacing: .16em;
  text-transform: uppercase; color: var(--eg-accent);
}
.eg-chip {
  display: inline-flex; align-items: center; gap: 8px; border-radius: 999px;
  padding: 8px 13px; font-size: 14px; font-weight: 500; color: var(--eg-accent);
  background: color-mix(in srgb, var(--eg-accent) 12%, transparent);
  border: 1px solid var(--eg-border-accent);
}

/* ---------- Botones (9.1 - 9.3) ---------- */
.eg-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 10px;
  min-height: 48px; padding: 13px 24px; border-radius: 999px;
  font-family: var(--eg-font-body); font-size: 16px; font-weight: 700; line-height: 1;
  text-decoration: none; border: 1px solid transparent; cursor: pointer;
  transition: transform .22s ease, box-shadow .22s ease, background .22s ease, border-color .22s ease, color .22s ease;
}
.eg-btn:focus-visible { outline: none; box-shadow: 0 0 0 4px color-mix(in srgb, var(--eg-focus) 26%, transparent); }
.eg-btn--primary { background: var(--eg-cta); color: var(--eg-text-on-cta); border-color: var(--eg-cta); }
.eg-btn--primary:hover {
  background: var(--eg-cta-hover); border-color: var(--eg-cta-hover);
  transform: translateY(-2px); box-shadow: 0 16px 34px rgba(41,183,141,.26);
}
.eg-btn--secondary { background: transparent; color: var(--eg-text); border-color: var(--eg-border); }
.eg-btn--secondary:hover { color: var(--eg-accent); border-color: var(--eg-border-accent); transform: translateY(-2px); }
.eg-btn--sm { min-height: 38px; padding: 8px 16px; font-size: 14px; }
.eg-btn--block { width: 100%; }

/* ---------- Card / Panel (9.6 / 8) ---------- */
.eg-card {
  background: var(--eg-surface); border: 1px solid var(--eg-border);
  border-radius: var(--eg-radius-lg); padding: clamp(22px, 3vw, 32px);
  box-shadow: 0 18px 50px rgba(10, 34, 49, .10);
}
.eg-panel { margin-bottom: 18px; }
.eg-panel > h2 { margin-bottom: 14px; }

/* ---------- Formularios (19) ---------- */
.eg-form { display: grid; gap: 16px; align-content: start; }
.eg-form__title { margin: 0; }
.eg-field { display: grid; gap: 8px; }
.eg-label { font-size: 14px; font-weight: 700; color: var(--eg-text); }
.eg-label__hint { font-weight: 400; color: var(--eg-text-subtle); }
.eg-input, .eg-textarea, .eg-select {
  width: 100%; min-height: 48px; border-radius: 14px;
  border: 1px solid var(--eg-border); background: var(--eg-surface);
  color: var(--eg-text); padding: 12px 14px; font-family: var(--eg-font-body); font-size: 16px; outline: none;
  transition: border-color .18s ease, box-shadow .18s ease;
}
.eg-input:focus, .eg-textarea:focus, .eg-select:focus {
  border-color: var(--eg-focus);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--eg-focus) 18%, transparent);
}
.eg-checks { display: flex; flex-wrap: wrap; gap: 16px; }
.eg-check { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; color: var(--eg-text); }
.eg-check input { width: 18px; height: 18px; accent-color: var(--eg-cta); }
.eg-check--consent { align-items: flex-start; line-height: 1.45; font-weight: 500; color: var(--eg-text-muted); }
.eg-check--consent input { margin-top: 2px; }
.eg-fineprint { color: var(--eg-text-subtle); font-size: 13px; line-height: 1.45; margin: 0; }
.eg-embed { padding: 18px; }
.eg-embed .eg-card { box-shadow: none; }

/* ---------- Feedback / Auth ---------- */
.eg-feedback, .eg-auth { padding: clamp(40px, 8vw, 90px) 20px; display: grid; justify-items: center; }
.eg-feedback__card, .eg-auth__card { max-width: 560px; width: 100%; text-align: center; }
.eg-auth__card { text-align: left; max-width: 440px; }
.eg-feedback__lead { color: var(--eg-text-muted); }
.eg-feedback__icon {
  display: inline-grid; place-items: center; width: 56px; height: 56px; margin: 0 auto 14px;
  border-radius: 999px; background: color-mix(in srgb, var(--eg-cta) 16%, transparent);
  color: var(--eg-cta); font-size: 26px; font-weight: 700;
}
.eg-feedback__card .eg-btn, .eg-auth__card .eg-btn { margin-top: 10px; }
.eg-error { color: #B42318; font-weight: 700; margin: 0 0 6px; }
[data-eg-theme="dark"] .eg-error { color: #FF8A7A; }

/* ---------- Admin ---------- */
.eg-app { padding: clamp(28px, 4vw, 44px) 0 56px; }
.eg-admin-header,
.eg-metrics, .eg-tabs, .eg-panel, .eg-feedback, .eg-auth { width: min(1180px, calc(100% - 40px)); margin-inline: auto; }
.eg-admin-header {
  display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px;
}
.eg-admin-header h1 { margin: 0; }
.eg-metrics {
  display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; margin-bottom: 22px;
}
.eg-metric {
  background: var(--eg-surface); border: 1px solid var(--eg-border);
  border-radius: var(--eg-radius); padding: 16px 18px;
  box-shadow: 0 10px 30px rgba(10,34,49,.06);
}
.eg-metric strong { display: block; font-family: var(--eg-font-heading); font-size: 26px; line-height: 1.1; color: var(--eg-text); }
.eg-metric span { color: var(--eg-text-subtle); font-size: 13px; }

/* Tabs (10.x interacción) */
.eg-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
.eg-tab {
  border: 1px solid var(--eg-border); border-radius: 999px; color: var(--eg-text);
  padding: 9px 16px; font-size: 14px; font-weight: 600; background: var(--eg-surface);
  transition: all .18s ease;
}
.eg-tab:hover { border-color: var(--eg-border-accent); color: var(--eg-accent); }
.eg-tab.is-active { background: var(--eg-brand-primary); color: #fff; border-color: var(--eg-brand-primary); }

/* 18. Tablas */
.eg-table-wrap { overflow-x: auto; border-radius: var(--eg-radius); }
.eg-table { width: 100%; border-collapse: collapse; background: var(--eg-surface); }
.eg-table th, .eg-table td {
  padding: 13px 16px; border-bottom: 1px solid var(--eg-border); text-align: left; vertical-align: top; font-size: 14px;
}
.eg-table th {
  font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--eg-text); background: var(--eg-surface-2); font-weight: 700; white-space: nowrap;
}
.eg-table td { color: var(--eg-text-muted); }
.eg-table tbody tr:last-child td { border-bottom: 0; }
.eg-table tbody tr:hover td { background: color-mix(in srgb, var(--eg-accent) 5%, transparent); }
.eg-table td strong { color: var(--eg-text); font-weight: 700; }
.eg-table td form { margin: 0; }
.eg-muted { color: var(--eg-text-subtle); font-size: 13px; line-height: 1.45; margin: 4px 0 0; }

/* Pills de estado */
.eg-pill {
  display: inline-block; border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 700;
  background: color-mix(in srgb, var(--eg-support) 14%, transparent); color: var(--eg-support);
  white-space: nowrap;
}

/* 15. Footer */
.eg-footer { background: var(--eg-brand-deep); color: rgba(255,255,255,.72); padding: 56px 0 28px; margin-top: 48px; }
.eg-footer__inner { display: grid; gap: 14px; }
.eg-footer__note { color: rgba(255,255,255,.62); font-size: 13.5px; max-width: 620px; margin: 0; }
.eg-footer__copy { color: rgba(255,255,255,.5); font-size: 12.5px; margin: 0; border-top: 1px solid rgba(255,255,255,.08); padding-top: 16px; }
.eg-footer a { color: rgba(255,255,255,.78); }
.eg-footer a:hover { color: #24EBA1; }

/* 16. Animaciones sutiles */
.eg-fade-up { opacity: 0; transform: translateY(18px); animation: egFadeUp .7s ease forwards; }
@keyframes egFadeUp { to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) {
  .eg-fade-up { animation: none; opacity: 1; transform: none; }
  .eg-btn, .eg-card { transition: none; }
}

/* ---------- Responsive ---------- */
@media (max-width: 900px) {
  .eg-hero__grid { grid-template-columns: 1fr; }
  .eg-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 600px) {
  .eg-header__tag { display: none; }
  .eg-admin-header { flex-direction: column; align-items: stretch; }
  .eg-admin-header form, .eg-admin-header .eg-btn { width: 100%; }
  .eg-metric strong { font-size: 22px; }
}

/* ---------- Ajustes finales (overrides de proyecto) ---------- */
html body img.eg-logo {
    height: 35px;
}

html body main.eg-app {
    background: #f6f6f6;
}

html body .eg h1 {
    font-size: clamp(1.35rem, 2.4vw, 1.85rem);
}
main.eg-app>.eg-hero {
    /* padding: 0; */
    min-height: calc(100vh - 318px);
}

html body footer.eg-footer {
    margin: 0;
}

html body main.eg-app {
    padding: 0;
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
