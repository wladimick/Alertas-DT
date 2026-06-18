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
from .notifier import (
    dispatch_alert,
    render_alert_email_html,
    render_alert_email_text,
    send_test_alert_email,
    subject_for,
)
from .worker import regenerate_alert, run_check, scheduler_loop


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
            self.respond_html(
                render_thanks(
                    embed=bool_from_form(query.get("embed", ["0"])[0]),
                    updated=bool_from_form(query.get("updated", ["0"])[0]),
                )
            )
        elif path == "/healthz":
            self.respond_json({"ok": True, "service": "dt-alertas"})
        elif path == "/admin/login":
            if self.settings.disable_admin_auth:
                # Modo desarrollo: sin autenticación, vamos directo al panel.
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
            flash = query.get("flash", [""])[0]
            self.respond_html(render_admin(path, self.settings, flash=flash))
        elif match := re.match(r"^/admin/alerts/(\d+)/preview-email$", path):
            if not self.is_admin():
                self.redirect("/admin/login")
                return
            self.respond_html(render_alert_preview(int(match.group(1)), self.settings))
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
        elif match := re.match(r"^/admin/alerts/(\d+)/ready$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                db.set_alert_status(conn, alert_id, "ready_to_send")
            self.redirect_flash("/admin/alerts", "Alerta marcada como lista para enviar.")
        elif match := re.match(r"^/admin/alerts/(\d+)/(send|resend)$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                count = dispatch_alert(conn, alert_id, self.settings)
            self.redirect_flash(
                "/admin/alerts", f"Envío procesado: {count} destinatario(s) activo(s)."
            )
        elif match := re.match(r"^/admin/alerts/(\d+)/test$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            payload = self.read_payload()
            to_email = (payload.get("to") or "").strip() or self.settings.test_email_to
            if not to_email:
                self.redirect_flash(
                    "/admin/alerts",
                    "Indica un correo de prueba o configura TEST_EMAIL_TO.",
                )
                return
            with db.connect(self.settings.database_path) as conn:
                alert = db.get_alert_with_document(conn, alert_id)
            if not alert:
                self.redirect_flash("/admin/alerts", "Alerta no encontrada.")
                return
            result = send_test_alert_email(to_email, alert, self.settings)
            self.redirect_flash("/admin/alerts", result.get("message") or "Prueba procesada.")
        elif match := re.match(r"^/admin/documents/(\d+)/regenerate$", path):
            self.require_admin()
            document_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                alert_id = regenerate_alert(conn, document_id, self.settings)
            msg = "Resumen regenerado (queda pendiente de revisión)." if alert_id else "Documento no encontrado."
            self.redirect_flash("/admin/documents", msg)
        elif match := re.match(r"^/admin/documents/(\d+)/ignore$", path):
            self.require_admin()
            document_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                db.set_document_status(conn, document_id, "ignored")
            self.redirect_flash("/admin/documents", "Documento marcado como ignorado.")
        else:
            self.respond_not_found()

    def handle_subscribe(self) -> None:
        payload = self.read_payload()
        embed = bool_from_form(payload.get("embed"))
        try:
            with db.connect(self.settings.database_path) as conn:
                # Detectamos existencia antes del upsert para distinguir alta vs actualización
                # de forma robusta (los timestamps pueden coincidir en el mismo segundo).
                email_norm = db.normalize_email(payload.get("email", ""))
                updated = bool(
                    conn.execute(
                        "SELECT 1 FROM subscribers WHERE email = ?", (email_norm,)
                    ).fetchone()
                )
                subscriber = db.upsert_subscriber(
                    conn,
                    email=payload.get("email", ""),
                    # WhatsApp reservado para fase futura: no se captura ni notifica.
                    whatsapp=None,
                    notify_email=True,
                    notify_whatsapp=False,
                    source_page=payload.get("source_page"),
                    consent=bool_from_form(payload.get("consent")),
                )
        except ValueError as exc:
            # Email inválido o sin consentimiento: mensaje amigable.
            if self.wants_json():
                self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            query = {"source_page": [payload.get("source_page") or "wordpress"]}
            self.respond_html(
                render_public_form(
                    self.settings,
                    embed=embed,
                    query=query,
                    error=str(exc),
                    email_value=payload.get("email", ""),
                ),
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        if self.wants_json():
            self.respond_json(
                {"ok": True, "updated": updated, "subscriber": public_subscriber(subscriber)}
            )
            return
        self.redirect(f"/thanks?embed={int(embed)}&updated={int(updated)}")

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
        # El bypass solo aplica si DISABLE_ADMIN_AUTH=True (modo desarrollo explícito).
        if self.settings.disable_admin_auth:
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

    def redirect_flash(self, target: str, message: str) -> None:
        sep = "&" if "?" in target else "?"
        self.redirect(f"{target}{sep}flash={urllib.parse.quote(message)}")

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
    settings: Settings,
    *,
    embed: bool,
    query: dict[str, list[str]],
    error: str | None = None,
    email_value: str = "",
) -> str:
    source_page = query.get("source_page", ["wordpress"])[0]
    error_html = f'<p class="eg-error" role="alert">{h(error)}</p>' if error else ""
    # MVP centrado en email: WhatsApp reservado para fase futura (no se muestra).
    # Tarjeta de formulario: tema claro forzado para lectura y orden (guía EG §5/§19).
    form = f"""
  <form class="eg-card eg-form" data-eg-theme="light" method="post" action="/api/subscribe" novalidate>
    <input type="hidden" name="source_page" value="{h(source_page)}">
    <input type="hidden" name="embed" value="{int(embed)}">
    <p class="eg-eyebrow">Suscripción</p>
    <h2 class="eg-form__title">Activa tus alertas</h2>
    {error_html}
    <div class="eg-field">
      <label class="eg-label" for="eg-email">Correo electrónico</label>
      <input class="eg-input" id="eg-email" name="email" type="email" required
             value="{h(email_value)}" placeholder="nombre@empresa.cl" autocomplete="email">
    </div>
    <label class="eg-check eg-check--consent">
      <input type="checkbox" name="consent" required>
      <span>Acepto recibir alertas sobre normativa DT y comunicaciones asociadas a esta suscripción.</span>
    </label>
    <button class="eg-btn eg-btn--primary eg-btn--block" type="submit">Suscribirme</button>
    <p class="eg-fineprint">Recibirás alertas por email cuando la DT publique normativa relevante. Puedes pausar tu suscripción cuando quieras.</p>
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
        <li class="eg-chip">Alertas por email</li>
      </ul>
    </div>
    {form}
  </div>
</section>
"""
    return render_page("Alertas DT", body, compact=embed)


def render_thanks(*, embed: bool, updated: bool = False) -> str:
    if updated:
        eyebrow = "Suscripción actualizada"
        title = "Tu suscripción ya existía y fue actualizada correctamente."
    else:
        eyebrow = "Suscripción registrada"
        title = "Listo, quedaste inscrito en Alertas DT."
    body = f"""
<section class="eg-container eg-feedback">
  <div class="eg-card eg-feedback__card" data-eg-theme="light" data-eg-accent="green">
    <span class="eg-feedback__icon" aria-hidden="true">&#10003;</span>
    <p class="eg-eyebrow">{h(eyebrow)}</p>
    <h1>{h(title)}</h1>
    <p class="eg-feedback__lead">Cuando se detecte nueva normativa relevante, recibirás la alerta por email.</p>
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


def fmt_dt(value: Any) -> str:
    """Formatea timestamps ISO a algo legible (YYYY-MM-DD HH:MM)."""
    text = str(value or "")
    return text.replace("T", " ")[:16] if text else "—"


def pill(value: Any, label: Any = None) -> str:
    """Pill de estado con color semántico via data-status (ver CSS)."""
    v = str(value or "")
    return f'<span class="eg-pill" data-status="{h(v)}">{h(label if label is not None else v)}</span>'


def render_admin(path: str, settings: Settings, *, flash: str = "") -> str:
    with db.connect(settings.database_path) as conn:
        subscribers = db.list_subscribers(conn)
        alerts = db.list_alerts(conn)
        documents = db.list_documents(conn)
        jobs = db.latest_jobs(conn)
        sent_deliveries = db.count_sent_deliveries(conn)

    total_subs = len(subscribers)
    active_count = sum(1 for s in subscribers if s["status"] == "active")
    paused_count = sum(1 for s in subscribers if s["status"] == "paused")
    pending_count = sum(1 for a in alerts if a["status"] == "pending_review")
    # "ready" es el estado legacy; lo tratamos como ready_to_send.
    ready_count = sum(1 for a in alerts if a["status"] in {"ready_to_send", "ready"})
    sent_count = sum(1 for a in alerts if a["status"] == "sent")
    last_job = jobs[0] if jobs else None

    banner = ""
    if settings.disable_admin_auth:
        banner = (
            '<div class="eg-devbanner" role="alert">'
            '⚠ Modo desarrollo: autenticación admin desactivada '
            '(DISABLE_ADMIN_AUTH=True). No usar en producción.</div>'
        )
    if flash:
        banner += f'<div class="eg-flash" role="status">{h(flash)}</div>'

    last_job_html = "Sin ejecuciones"
    if last_job:
        last_job_html = f"{fmt_dt(last_job['started_at'])} · {h(last_job['status'])}"

    body = f"""
{banner}
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
  <div class="eg-metric"><strong>{total_subs}</strong><span>Suscriptores</span></div>
  <div class="eg-metric"><strong>{active_count}</strong><span>Activos</span></div>
  <div class="eg-metric"><strong>{paused_count}</strong><span>Pausados</span></div>
  <div class="eg-metric"><strong>{len(documents)}</strong><span>Documentos</span></div>
  <div class="eg-metric"><strong>{pending_count}</strong><span>Por revisar</span></div>
  <div class="eg-metric"><strong>{ready_count}</strong><span>Listas</span></div>
  <div class="eg-metric"><strong>{sent_count}</strong><span>Enviadas</span></div>
  <div class="eg-metric"><strong>{sent_deliveries}</strong><span>Envíos totales</span></div>
</section>
<p class="eg-muted eg-lastjob">Último job: {last_job_html}</p>
{render_nav(path)}
{render_admin_section(path, subscribers, alerts, documents, jobs)}
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
        ("/admin/documents", "Documentos"),
        ("/admin/alerts", "Alertas"),
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
    jobs: list[dict[str, Any]],
) -> str:
    if path == "/admin/subscribers":
        return render_subscribers(subscribers)
    if path == "/admin/alerts":
        return render_alerts(alerts)
    if path == "/admin/documents":
        return render_documents(documents)
    # Resumen: historial de jobs + extracto de alertas recientes.
    return render_jobs(jobs) + render_alerts(alerts[:8])


def render_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return '<section class="eg-card eg-panel"><h2>Últimos jobs</h2><p class="eg-muted">Sin ejecuciones todavía.</p></section>'
    rows = "".join(
        f"""
<tr>
  <td>{fmt_dt(job['started_at'])}</td>
  <td>{pill(job['status'])}</td>
  <td>{h(job['discovered_count'])}</td>
  <td>{h(job['processed_count'])}</td>
  <td>{h(job['sent_count'])}</td>
  <td class="eg-muted">{h(job.get('error'))}</td>
</tr>
"""
        for job in jobs
    )
    return f"""
<section class="eg-card eg-panel">
  <h2>Historial de jobs</h2>
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
  <td><strong>{h(item['email'])}</strong></td>
  <td>{pill(item['status'])}</td>
  <td class="eg-muted">{fmt_dt(item.get('created_at'))}</td>
  <td class="eg-muted">{fmt_dt(item.get('updated_at'))}</td>
  <td class="eg-muted">{h(item.get('source_page') or '—')}</td>
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
      <thead><tr><th>Email</th><th>Estado</th><th>Registro</th><th>Actualización</th><th>Fuente</th><th></th></tr></thead>
      <tbody>{rows or '<tr><td colspan="6">Sin suscriptores.</td></tr>'}</tbody>
    </table>
  </div>
  <p class="eg-muted">WhatsApp reservado para fase futura: el MVP notifica solo por email.</p>
</section>
"""


def alert_actions(item: dict[str, Any]) -> str:
    alert_id = item["id"]
    status = item["status"]
    actions = [
        f'<a class="eg-btn eg-btn--secondary eg-btn--sm" href="/admin/alerts/{alert_id}/preview-email">Vista previa</a>'
    ]
    if status == "pending_review":
        actions.append(
            f'<form method="post" action="/admin/alerts/{alert_id}/ready">'
            f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Marcar lista</button></form>'
        )
    if status in {"ready_to_send", "ready"}:
        actions.append(
            f'<form method="post" action="/admin/alerts/{alert_id}/send" '
            f'onsubmit="return confirm(\'¿Enviar esta alerta a los suscriptores activos?\');">'
            f'<button class="eg-btn eg-btn--primary eg-btn--sm" type="submit">Enviar</button></form>'
        )
    # Enviar prueba (a TEST_EMAIL_TO o al correo escrito).
    actions.append(
        f'<form method="post" action="/admin/alerts/{alert_id}/test" class="eg-inline-form">'
        f'<input class="eg-input eg-input--sm" type="email" name="to" placeholder="correo prueba (opcional)">'
        f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Enviar prueba</button></form>'
    )
    return '<div class="eg-actions">' + "".join(actions) + "</div>"


def render_alerts(alerts: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
<tr>
  <td>
    <strong>{h(item['title'])}</strong>
    <p class="eg-muted">{h(item['summary'])}</p>
  </td>
  <td>{h(item['category'])}</td>
  <td>{pill(item['relevance'])}</td>
  <td>{pill(item['status'])}</td>
  <td class="eg-muted">{fmt_dt(item.get('created_at'))}</td>
  <td>{alert_actions(item)}</td>
</tr>
"""
        for item in alerts
    )
    return f"""
<section class="eg-card eg-panel">
  <h2>Alertas</h2>
  <div class="eg-table-wrap">
    <table class="eg-table">
      <thead><tr><th>Documento</th><th>Categoría</th><th>Relevancia</th><th>Estado</th><th>Generada</th><th>Acciones</th></tr></thead>
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
  <td class="eg-muted">{h(item.get('publication_date') or '—')}</td>
  <td class="eg-muted">{h(item.get('dt_article_id'))}</td>
  <td>{pill(item['status'])}</td>
  <td><a href="{h(item['canonical_url'])}" target="_blank" rel="noreferrer">DT</a></td>
  <td>
    <div class="eg-actions">
      <form method="post" action="/admin/documents/{item['id']}/regenerate">
        <button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Regenerar resumen</button>
      </form>
      <form method="post" action="/admin/documents/{item['id']}/ignore">
        <button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Ignorar</button>
      </form>
    </div>
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
      <thead><tr><th>Documento</th><th>Categoría</th><th>Fecha</th><th>ID DT</th><th>Estado</th><th>Link</th><th>Acciones</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="7">Sin documentos.</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


def render_alert_preview(alert_id: int, settings: Settings) -> str:
    with db.connect(settings.database_path) as conn:
        alert = db.get_alert_with_document(conn, alert_id)
    if not alert:
        body = (
            '<section class="eg-container eg-feedback"><div class="eg-card eg-feedback__card" data-eg-theme="light">'
            '<p class="eg-eyebrow">Vista previa</p><h1>Alerta no encontrada</h1>'
            '<a class="eg-btn eg-btn--secondary" href="/admin/alerts">Volver al admin</a></div></section>'
        )
        return render_page("Vista previa", body)

    subject = subject_for(alert)
    email_html = render_alert_email_html(alert)
    email_text = render_alert_email_text(alert)
    # El HTML del email se aísla en un iframe srcdoc (escapado) para no afectar el admin.
    srcdoc = h(email_html)
    body = f"""
<section class="eg-container eg-preview">
  <p class="eg-eyebrow">Vista previa de email</p>
  <h1>{h(alert['title'])}</h1>
  <dl class="eg-kv">
    <div><dt>Estado</dt><dd>{pill(alert['status'])}</dd></div>
    <div><dt>Categoría</dt><dd>{h(alert['category'])}</dd></div>
    <div><dt>Relevancia</dt><dd>{pill(alert['relevance'])}</dd></div>
    <div><dt>Fecha doc.</dt><dd>{h(alert.get('publication_date') or '—')}</dd></div>
    <div><dt>Asunto</dt><dd>{h(subject)}</dd></div>
    <div><dt>Documento</dt><dd><a href="{h(alert['canonical_url'])}" target="_blank" rel="noreferrer">Ver en DT</a></dd></div>
  </dl>
  <div class="eg-actions">
    <a class="eg-btn eg-btn--secondary" href="/admin/alerts">Volver al admin</a>
  </div>
  <h2 style="margin-top:24px;">Render HTML</h2>
  <div class="eg-preview__frame">
    <iframe title="Vista previa email" srcdoc="{srcdoc}"></iframe>
  </div>
  <details style="margin-top:18px;">
    <summary>Ver versión texto plano</summary>
    <pre>{h(email_text)}</pre>
  </details>
</section>
"""
    return render_page("Vista previa de email", body, density="compact")


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
  display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 14px; margin-bottom: 16px;
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

/* ---------- Admin operativo (etapas 4/8/10/11) ---------- */
.eg-devbanner {
  width: min(1180px, calc(100% - 40px)); margin: 0 auto 18px;
  background: #FEF3C7; color: #92400E; border: 1px solid #FCD34D;
  border-radius: 12px; padding: 12px 16px; font-size: 14px; font-weight: 700;
}
.eg-lastjob { width: min(1180px, calc(100% - 40px)); margin: 0 auto 18px; }
.eg-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.eg-actions form { margin: 0; }
.eg-inline-form { display: flex; gap: 6px; align-items: center; }
.eg-input--sm { min-height: 36px; padding: 6px 10px; font-size: 13px; max-width: 200px; border-radius: 10px; }

/* Pills con color semántico por estado */
.eg-pill[data-status="active"], .eg-pill[data-status="sent"], .eg-pill[data-status="ready_to_send"],
.eg-pill[data-status="ready"], .eg-pill[data-status="processed"], .eg-pill[data-status="success"],
.eg-pill[data-status="alto"] {
  background: color-mix(in srgb, #167A5F 16%, transparent); color: #0F5E51;
}
.eg-pill[data-status="paused"], .eg-pill[data-status="pending_review"], .eg-pill[data-status="partial"],
.eg-pill[data-status="baseline"], .eg-pill[data-status="discovered"], .eg-pill[data-status="medio"],
.eg-pill[data-status="running"], .eg-pill[data-status="skipped"] {
  background: color-mix(in srgb, #B45309 16%, transparent); color: #92400E;
}
.eg-pill[data-status="error"], .eg-pill[data-status="failed"], .eg-pill[data-status="ignored"],
.eg-pill[data-status="bajo"] {
  background: color-mix(in srgb, #B42318 14%, transparent); color: #B42318;
}

/* Vista previa de email (etapa 8) */
.eg-preview { width: min(1180px, calc(100% - 40px)); margin-inline: auto; }
.eg-preview__frame {
  border: 1px solid var(--eg-border); border-radius: var(--eg-radius);
  background: #fff; overflow: hidden; margin-top: 12px;
}
.eg-preview__frame iframe { width: 100%; min-height: 640px; border: 0; display: block; }
.eg-preview pre {
  white-space: pre-wrap; word-break: break-word; background: var(--eg-surface-2);
  border-radius: 12px; padding: 16px; font-size: 13px; line-height: 1.5; overflow-x: auto;
}
.eg-kv { display: grid; gap: 6px; margin: 0 0 16px; }
.eg-kv div { display: grid; grid-template-columns: 140px 1fr; gap: 12px; font-size: 14px; }
.eg-kv dt { font-weight: 700; color: var(--eg-text); }
.eg-kv dd { margin: 0; color: var(--eg-text-muted); }
.eg-flash {
  width: min(1180px, calc(100% - 40px)); margin: 0 auto 16px;
  border-radius: 12px; padding: 12px 16px; font-size: 14px; font-weight: 600;
  background: color-mix(in srgb, var(--eg-support) 12%, transparent); color: var(--eg-support);
  border: 1px solid var(--eg-border-accent);
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
