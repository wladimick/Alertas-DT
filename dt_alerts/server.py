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
    # None -> "" (no mostramos "None"); pero 0 / valores falsy válidos se conservan
    # (antes "value or ''" convertía 0 en cadena vacía y ocultaba métricas en cero).
    return html.escape("" if value is None else str(value), quote=True)


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
                self.respond_html(render_login(settings=self.settings))
        elif path in {"/admin", "/admin/subscribers", "/admin/alerts", "/admin/documents", "/admin/jobs"}:
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
                self.respond_html(
                    render_login(error="Token inválido. Verifica e inténtalo de nuevo.", settings=self.settings),
                    status=HTTPStatus.UNAUTHORIZED,
                )
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
            if count:
                msg = f"Alerta procesada para {count} suscriptor(es) activo(s)."
            else:
                msg = "Sin envíos: no hay suscriptores activos o ya habían recibido la alerta."
            self.redirect_flash("/admin/alerts", msg)
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
            self.redirect_flash("/admin/alerts", flash_for_email_result(result))
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
      <span>Acepto recibir alertas informativas por email sobre nuevas publicaciones de la Dirección del Trabajo.</span>
    </label>
    <button class="eg-btn eg-btn--primary eg-btn--block" type="submit">Suscribirme a las alertas</button>
    <p class="eg-fineprint">Podrás solicitar la baja cuando quieras. Los resúmenes son informativos y no reemplazan la revisión del documento oficial.</p>
  </form>
"""
    if embed:
        # En el iframe no mostramos hero ni header: solo la tarjeta funcional.
        body = f'<div class="eg-embed">{form}</div>'
        return render_page("Alertas DT", body, compact=embed)

    benefits = [
        ("🔎", "Monitoreo periódico", "Revisamos las publicaciones de la Dirección del Trabajo por ti."),
        ("📝", "Resumen práctico", "Un resumen claro, sin jerga, listo para tomar decisiones."),
        ("⚖️", "Impacto laboral", "Qué significa para contadores, remuneraciones y empresas."),
        ("🔗", "Documento oficial", "Enlace directo a la fuente original de la DT."),
    ]
    benefit_cards = "".join(
        f'<article class="eg-card eg-benefit"><span class="eg-benefit__icon" aria-hidden="true">{ic}</span>'
        f'<h3>{h(t)}</h3><p>{h(d)}</p></article>'
        for ic, t, d in benefits
    )
    steps = [
        ("1", "Te suscribes con tu email", "Sin instalar nada. Solo tu correo y aceptar recibir alertas."),
        ("2", "Monitoreamos la DT", "El sistema revisa periódicamente las nuevas publicaciones oficiales."),
        ("3", "Generamos un resumen", "Cada documento nuevo se resume con su impacto práctico."),
        ("4", "Recibes la alerta por email", "Te llega un correo claro con el resumen y el enlace oficial."),
    ]
    step_cards = "".join(
        f'<li class="eg-step"><span class="eg-step__num" aria-hidden="true">{n}</span>'
        f'<div><h3>{h(t)}</h3><p>{h(d)}</p></div></li>'
        for n, t, d in steps
    )

    body = f"""
<section class="eg-hero" data-eg-theme="dark" data-eg-accent="green">
  <span class="eg-glow eg-glow--a" aria-hidden="true"></span>
  <span class="eg-glow eg-glow--b" aria-hidden="true"></span>
  <div class="eg-container eg-hero__grid">
    <div class="eg-hero__copy eg-fade-up">
      <p class="eg-eyebrow">Alertas Dirección del Trabajo</p>
      <h1 class="eg-hero__title">Alertas DT para <span>contadores y empresas</span></h1>
      <p class="eg-hero__lead">Monitoreamos nuevas publicaciones de la Dirección del Trabajo y te enviamos un resumen práctico por email, pensado para gestión laboral, contabilidad y empresas.</p>
      <ul class="eg-hero__points">
        <li class="eg-chip">Monitoreo continuo</li>
        <li class="eg-chip">Resumen orientado a contadores</li>
        <li class="eg-chip">Alertas por email</li>
      </ul>
    </div>
    {form}
  </div>
</section>

<section class="eg-section eg-section--light" data-eg-theme="light" data-eg-density="compact">
  <div class="eg-container">
    <p class="eg-eyebrow">Beneficios</p>
    <h2 class="eg-section__title">Normativa laboral, sin perderte nada</h2>
    <div class="eg-grid-4 eg-benefits">{benefit_cards}</div>
  </div>
</section>

<section class="eg-section eg-section--soft" data-eg-theme="light" data-eg-density="compact">
  <div class="eg-container">
    <p class="eg-eyebrow">Cómo funciona</p>
    <h2 class="eg-section__title">De la publicación oficial a tu correo, en cuatro pasos</h2>
    <ol class="eg-steps">{step_cards}</ol>
  </div>
</section>
"""
    return render_page("Alertas DT", body, compact=embed)


def render_thanks(*, embed: bool, updated: bool = False) -> str:
    if updated:
        eyebrow = "Suscripción actualizada"
        title = "¡Listo! Actualizamos tu suscripción"
    else:
        eyebrow = "Suscripción recibida"
        title = "¡Suscripción recibida!"
    back_button = (
        '<a class="eg-btn eg-btn--primary" href="/">Volver al inicio</a>'
        if not embed
        else ""
    )
    body = f"""
<section class="eg-container eg-feedback">
  <div class="eg-card eg-feedback__card" data-eg-theme="light" data-eg-accent="green">
    <span class="eg-feedback__icon" aria-hidden="true">&#10003;</span>
    <p class="eg-eyebrow">{h(eyebrow)}</p>
    <h1>{h(title)}</h1>
    <p class="eg-feedback__lead">Desde ahora recibirás alertas informativas cuando detectemos nuevas publicaciones relevantes de la Dirección del Trabajo.</p>
    {back_button}
    <p class="eg-fineprint" style="margin-top:18px;">Este servicio se encuentra en etapa de prueba interna.</p>
  </div>
</section>
"""
    return render_page("Suscripción recibida", body, compact=embed)


def render_login(error: str | None = None, settings: Settings | None = None) -> str:
    error_html = f'<p class="eg-error" role="alert">{h(error)}</p>' if error else ""
    dev_html = ""
    if settings is not None and settings.disable_admin_auth:
        dev_html = (
            '<p class="eg-flash" style="margin:0 0 14px;">'
            "Modo desarrollo activo: la autenticación admin está desactivada.</p>"
        )
    body = f"""
<section class="eg-container eg-auth">
  <div class="eg-card eg-auth__card" data-eg-theme="light">
    <p class="eg-eyebrow">External Group · Alertas DT</p>
    <h1>Acceso administrativo</h1>
    <p class="eg-auth__help">Ingresa el token de administración para revisar suscriptores, documentos detectados y alertas.</p>
    {dev_html}
    {error_html}
    <form class="eg-form" method="post" action="/admin/login">
      <div class="eg-field">
        <label class="eg-label" for="eg-token">Token de administración</label>
        <input class="eg-input" id="eg-token" name="token" type="password" required
               autocomplete="current-password" placeholder="••••••••">
      </div>
      <button class="eg-btn eg-btn--primary eg-btn--block" type="submit">Entrar al panel</button>
    </form>
  </div>
</section>
"""
    return render_page("Acceso administrativo", body, theme="dark")


# Traducción de estados técnicos a microcopy ejecutivo (solo presentación;
# los valores almacenados en la base NO cambian).
STATUS_LABELS = {
    # Suscriptores
    "active": "Activo",
    "paused": "Pausado",
    # Documentos
    "discovered": "Detectado",
    "baseline": "Línea base",
    "processed": "Procesado",
    "ignored": "Ignorado",
    "error": "Error",
    # Alertas
    "pending_review": "Pendiente de revisión",
    "ready_to_send": "Lista para enviar",
    "ready": "Lista para enviar",
    "sent": "Enviada",
    "failed": "Error",
    # Jobs
    "running": "En curso",
    "success": "Éxito",
    "partial": "Parcial",
    # Envíos / deliveries
    "simulated": "Simulada",
    "skipped_missing_credentials": "No enviada: faltan credenciales",
    # Relevancia
    "alto": "Alta",
    "medio": "Media",
    "bajo": "Baja",
}


def status_label(value: Any) -> str:
    """Etiqueta legible para un estado técnico; si no hay mapeo, devuelve el valor."""
    v = str(value or "").strip()
    return STATUS_LABELS.get(v, v or "—")


def flash_for_email_result(result: dict[str, Any]) -> str:
    """Mensaje flash ejecutivo (no técnico) para el resultado de un envío de prueba."""
    status = result.get("status")
    if status == "sent":
        return "Correo de prueba enviado correctamente."
    if status == "simulated":
        return "Envío simulado registrado. Configura SendGrid en Render para enviar correos reales."
    if status == "skipped_missing_credentials":
        return "No se envió: faltan credenciales de SendGrid. Configúralas en Render (Mail Send)."
    if status == "failed":
        return (
            "No se pudo enviar el correo con SendGrid. Revisa que la API key esté activa "
            "y tenga permiso Mail Send."
        )
    return result.get("message") or "Prueba procesada."


def fmt_dt(value: Any) -> str:
    """Formatea timestamps ISO a algo legible (YYYY-MM-DD HH:MM)."""
    text = str(value or "")
    return text.replace("T", " ")[:16] if text else "—"


def empty_row(colspan: int, title: str, hint: str) -> str:
    """Fila de tabla con estado vacío amigable (en vez de 'Sin datos')."""
    return (
        f'<tr class="eg-empty-row"><td colspan="{colspan}">'
        f'<div class="eg-empty"><strong>{h(title)}</strong><span>{h(hint)}</span></div>'
        "</td></tr>"
    )


def pill(value: Any, label: Any = None) -> str:
    """
    Pill de estado con color semántico via data-status (ver CSS).
    El texto se traduce con status_label; el data-status conserva el valor técnico
    para el color, de modo que la lógica de estados no se ve afectada.
    """
    v = str(value or "")
    text = label if label is not None else status_label(v)
    return f'<span class="eg-pill" data-status="{h(v)}">{h(text)}</span>'


# --------------------------------------------------------------------------
# Íconos SVG inline (estilo lineal, currentColor; sin dependencias externas).
# --------------------------------------------------------------------------
ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "document": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "bell": '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "mail": '<path d="M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/><polyline points="22,6 12,13 2,6"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "send": '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
    "alert": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "back": '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>',
}


def icon(name: str, size: int = 20) -> str:
    return (
        f'<svg class="eg-ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{ICONS.get(name, "")}</svg>'
    )


# Metadatos de cada sección admin (título + subtítulo de la topbar).
SECTION_META = {
    "/admin": ("Resumen operativo", "Estado general del monitoreo DT, suscriptores y alertas por email."),
    "/admin/subscribers": ("Suscriptores", "Personas inscritas para recibir alertas por email."),
    "/admin/documents": ("Documentos detectados", "Publicaciones DT encontradas por el monitoreo."),
    "/admin/alerts": ("Alertas", "Resúmenes generados, listos para revisar y enviar."),
    "/admin/jobs": ("Monitoreo", "Historial de ejecuciones del monitoreo DT."),
}

SIDEBAR_NAV = [
    ("/admin", "Resumen", "dashboard"),
    ("/admin/subscribers", "Suscriptores", "users"),
    ("/admin/documents", "Documentos", "document"),
    ("/admin/alerts", "Alertas", "bell"),
    ("/admin/jobs", "Monitoreo", "activity"),
]


def email_mode(settings: Settings) -> tuple[str, str]:
    """Etiqueta + clave de estado para el proveedor/modo de email."""
    provider = settings.email_provider
    if provider == "sendgrid" and settings.sendgrid_api_key:
        return ("SendGrid · envío real", "active")
    if provider == "sendgrid":
        return ("SendGrid sin credenciales", "pending_review")
    if provider in {"resend", "smtp"} and (settings.resend_api_key or settings.smtp_host):
        return (f"{provider.upper()} · envío real", "active")
    return ("Console · simulado", "simulated")


def auth_mode(settings: Settings) -> tuple[str, str]:
    if settings.disable_admin_auth:
        return ("Modo desarrollo", "error")
    return ("Login por token activo", "active")


def render_sidebar(path: str, settings: Settings) -> str:
    email_label, email_key = email_mode(settings)
    parts = []
    for href, label, ic in SIDEBAR_NAV:
        active = path == href
        cls = " is-active" if active else ""
        aria = ' aria-current="page"' if active else ""
        parts.append(
            f'<a class="eg-side__link{cls}" href="{href}"{aria}>'
            f'<span class="eg-side__ic">{icon(ic)}</span><span>{label}</span></a>'
        )
    items = "".join(parts)
    return f"""
<div class="eg-side__brand">
  <img class="eg-logo eg-logo--sm" src="{EG_LOGO_LIGHT}" alt="External Group" />
  <span class="eg-side__product">Alertas DT</span>
</div>
<nav class="eg-side__nav" aria-label="Navegación del panel">{items}</nav>
<div class="eg-side__status">
  <span class="eg-side__dot" data-status="{h(email_key)}"></span>
  <span>{h(email_label)}</span>
</div>
<p class="eg-side__foot">MVP interno · External Group</p>
"""


# Script (una vez por página admin) para el botón "Ejecutar monitoreo".
MONITOR_SCRIPT = """
<script>
document.querySelectorAll('[data-job-token]').forEach(function(button) {
  button.closest('form').addEventListener('submit', async function(event) {
    event.preventDefault();
    button.disabled = true;
    button.textContent = 'Ejecutando...';
    try {
      const token = prompt('JOB_TOKEN');
      if (!token) { button.disabled = false; button.textContent = 'Ejecutar monitoreo'; return; }
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


def render_topbar(title: str, subtitle: str, settings: Settings, *, show_action: bool = True) -> str:
    email_label, email_key = email_mode(settings)
    auth_label, auth_key = auth_mode(settings)
    action = ""
    if show_action:
        action = (
            '<form method="post" action="/api/jobs/check-dt" class="eg-topbar__action">'
            '<input type="hidden" name="manual" value="1">'
            '<button class="eg-btn eg-btn--primary eg-btn--sm" type="submit" '
            'formaction="/api/jobs/check-dt" data-job-token>'
            f'{icon("refresh", 18)}<span>Ejecutar monitoreo</span></button></form>'
        )
    return f"""
<header class="eg-topbar">
  <div class="eg-topbar__titles">
    <h1>{h(title)}</h1>
    <p>{h(subtitle)}</p>
  </div>
  <div class="eg-topbar__right">
    <span class="eg-topbar__status">{icon("mail", 16)} {pill(email_key, email_label)}</span>
    <span class="eg-topbar__status">{pill(auth_key, auth_label)}</span>
    {action}
  </div>
</header>
"""


def metric_card(ic: str, value: Any, label: str, sub: str, tone: str = "muted") -> str:
    return (
        f'<article class="eg-stat" data-tone="{h(tone)}">'
        f'<span class="eg-stat__ic">{icon(ic, 18)}</span>'
        f'<strong class="eg-stat__num">{h(value)}</strong>'
        f'<span class="eg-stat__label">{h(label)}</span>'
        f'<span class="eg-stat__sub">{h(sub)}</span>'
        "</article>"
    )


def render_system_status(settings: Settings, last_job: dict[str, Any] | None) -> str:
    email_label, email_key = email_mode(settings)
    auth_label, auth_key = auth_mode(settings)
    real = email_key == "active"
    last_job_html = "Sin ejecuciones aún"
    last_error = ""
    if last_job:
        last_job_html = f"{fmt_dt(last_job['started_at'])} · {status_label(last_job['status'])}"
        if last_job.get("error"):
            last_error = (
                f'<div><dt>Último error</dt><dd class="eg-muted">{h(last_job["error"])}</dd></div>'
            )
    return f"""
<section class="eg-card eg-panel">
  <h2>Estado del sistema</h2>
  <dl class="eg-kv">
    <div><dt>Email</dt><dd>{pill(email_key, email_label)}</dd></div>
    <div><dt>Modo de envío</dt><dd>{'Correos reales' if real else 'Simulado (no se envían correos)'}</dd></div>
    <div><dt>Acceso admin</dt><dd>{pill(auth_key, auth_label)}</dd></div>
    <div><dt>Último monitoreo</dt><dd>{h(last_job_html)}</dd></div>
    {last_error}
  </dl>
</section>
"""


def render_admin(path: str, settings: Settings, *, flash: str = "") -> str:
    with db.connect(settings.database_path) as conn:
        subscribers = db.list_subscribers(conn)
        alerts = db.list_alerts(conn)
        documents = db.list_documents(conn)
        jobs = db.latest_jobs(conn)
        sent_deliveries = db.count_sent_deliveries(conn)

    active_count = sum(1 for s in subscribers if s["status"] == "active")
    paused_count = sum(1 for s in subscribers if s["status"] == "paused")
    pending_count = sum(1 for a in alerts if a["status"] == "pending_review")
    ready_count = sum(1 for a in alerts if a["status"] in {"ready_to_send", "ready"})
    sent_count = sum(1 for a in alerts if a["status"] == "sent")
    last_job = jobs[0] if jobs else None

    banner = ""
    if settings.disable_admin_auth:
        banner = (
            '<div class="eg-devbanner" role="alert">'
            "⚠ Modo desarrollo: autenticación admin desactivada "
            "(DISABLE_ADMIN_AUTH=True). No usar en producción.</div>"
        )
    if flash:
        banner += f'<div class="eg-flash" role="status">{h(flash)}</div>'

    if path == "/admin/subscribers":
        section = render_db_info(settings, subscribers) + render_subscribers(subscribers)
    elif path == "/admin/documents":
        section = render_documents(documents)
    elif path == "/admin/alerts":
        section = render_alerts(alerts)
    elif path == "/admin/jobs":
        section = (
            '<p class="eg-section-note">El monitoreo revisa las fuentes configuradas de la '
            "Dirección del Trabajo y registra nuevos documentos sin duplicar URLs ya "
            "detectadas.</p>" + render_jobs(jobs)
        )
    else:  # /admin -> Resumen
        cards = (
            metric_card("users", active_count, "Suscriptores activos", f"{paused_count} pausados", "accent")
            + metric_card("document", len(documents), "Documentos detectados", "desde fuentes DT", "info")
            + metric_card("bell", pending_count, "Pendientes de revisión", "requieren validación", "warning")
            + metric_card("check", ready_count, "Listas para enviar", "revisadas y aprobadas", "info")
            + metric_card("send", sent_count, "Alertas enviadas", "a suscriptores activos", "success")
            + metric_card("mail", sent_deliveries, "Envíos registrados", "incluye simulados", "muted")
        )
        section = (
            f'<section class="eg-stats">{cards}</section>'
            + render_system_status(settings, last_job)
            + render_jobs(jobs[:5])
            + render_alerts(alerts[:6])
        )

    title, subtitle = SECTION_META.get(path, SECTION_META["/admin"])
    content = banner + section + MONITOR_SCRIPT
    sidebar = render_sidebar(path, settings)
    topbar = render_topbar(title, subtitle, settings)
    return render_page(title, content, sidebar=sidebar, topbar=topbar)


def render_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return (
            '<section class="eg-card eg-panel"><h2>Historial de monitoreo</h2>'
            '<div class="eg-empty"><strong>Aún no se ha ejecutado el monitoreo.</strong>'
            '<span>Usa "Ejecutar monitoreo" para buscar nuevas publicaciones de la DT.</span></div></section>'
        )
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


def render_db_info(settings: Settings, subscribers: list[dict[str, Any]]) -> str:
    """Card informativa: motor, ruta parcial y última actualización de suscriptores.
    Ayuda a confirmar que el admin lee la base esperada. No expone datos sensibles."""
    from pathlib import Path

    p = Path(str(settings.database_path))
    partial = "/".join(p.parts[-2:]) if len(p.parts) >= 2 else p.name
    last_update = max((s.get("updated_at") or "" for s in subscribers), default="")
    return f"""
<section class="eg-card eg-panel">
  <h2>Base de datos</h2>
  <dl class="eg-kv">
    <div><dt>Motor</dt><dd>SQLite</dd></div>
    <div><dt>Ruta</dt><dd class="eg-muted">…/{h(partial)}</dd></div>
    <div><dt>Suscriptores</dt><dd>{len(subscribers)}</dd></div>
    <div><dt>Última actualización</dt><dd>{fmt_dt(last_update) if last_update else '—'}</dd></div>
  </dl>
  <p class="eg-muted">En Render, usa un disco persistente o Postgres para que los suscriptores no se reinicien entre despliegues. Ver README.</p>
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
      <tbody>{rows or empty_row(6, "Aún no hay suscriptores.", "Puedes probar el formulario público usando un correo interno.")}</tbody>
    </table>
  </div>
  <p class="eg-muted">WhatsApp reservado para fase futura: el MVP notifica solo por email.</p>
</section>
"""


def alert_actions(item: dict[str, Any]) -> str:
    alert_id = item["id"]
    status = item["status"]
    actions = [
        f'<a class="eg-btn eg-btn--primary eg-btn--sm" href="/admin/alerts/{alert_id}/preview-email">'
        f'{icon("eye", 16)}<span>Vista previa</span></a>'
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
            f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">{icon("send", 16)}<span>Enviar</span></button></form>'
        )
    # Enviar prueba (a TEST_EMAIL_TO o al correo escrito).
    actions.append(
        f'<form method="post" action="/admin/alerts/{alert_id}/test" class="eg-inline-form">'
        f'<input class="eg-input eg-input--sm" type="email" name="to" placeholder="correo de prueba" aria-label="Correo de prueba">'
        f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Enviar prueba</button></form>'
    )
    return '<div class="eg-actions">' + "".join(actions) + "</div>"


def alert_card(item: dict[str, Any]) -> str:
    return f"""
<article class="eg-alert">
  <div class="eg-alert__head">
    <span class="eg-alert__cat">{h(item['category'])}</span>
    <span class="eg-alert__meta">{pill(item['relevance'])} {pill(item['status'])}</span>
  </div>
  <h3 class="eg-alert__title">{h(item['title'])}</h3>
  <p class="eg-alert__summary">{h(item['summary'])}</p>
  <div class="eg-alert__foot">
    <span class="eg-alert__date">{fmt_dt(item.get('created_at'))}</span>
    {alert_actions(item)}
  </div>
</article>
"""


def render_alerts(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return (
            '<h2 class="eg-block-title">Alertas</h2>'
            '<div class="eg-card eg-panel"><div class="eg-empty">'
            "<strong>Aún no hay alertas generadas.</strong>"
            "<span>Las alertas aparecerán cuando se detecten documentos nuevos.</span>"
            "</div></div>"
        )
    cards = "".join(alert_card(item) for item in alerts)
    return f'<h2 class="eg-block-title">Alertas</h2><div class="eg-alert-grid">{cards}</div>'


def render_documents(documents: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
<tr>
  <td><strong>{h(item['title'])}</strong><p class="eg-muted">{h(item.get('abstract'))}</p></td>
  <td>{h(item['category'])}</td>
  <td class="eg-muted">{h(item.get('publication_date') or '—')}</td>
  <td class="eg-muted">{h(item.get('dt_article_id'))}</td>
  <td>{pill(item['status'])}</td>
  <td><a class="eg-link" href="{h(item['canonical_url'])}" target="_blank" rel="noreferrer">Ver en DT ↗</a></td>
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
      <tbody>{rows or empty_row(7, "Aún no hay documentos detectados.", "Ejecuta el monitoreo para buscar nuevas publicaciones de la Dirección del Trabajo.")}</tbody>
    </table>
  </div>
</section>
"""


def render_alert_preview(alert_id: int, settings: Settings) -> str:
    with db.connect(settings.database_path) as conn:
        alert = db.get_alert_with_document(conn, alert_id)
    sidebar = render_sidebar("/admin/alerts", settings)
    if not alert:
        topbar = render_topbar("Vista previa", "Alerta no encontrada.", settings, show_action=False)
        body = (
            '<div class="eg-card eg-panel"><div class="eg-empty">'
            "<strong>Alerta no encontrada.</strong>"
            "<span>Es posible que haya sido eliminada o que el enlace sea incorrecto.</span></div>"
            '<a class="eg-btn eg-btn--secondary" href="/admin/alerts">Volver a Alertas</a></div>'
        )
        return render_page("Vista previa", body, sidebar=sidebar, topbar=topbar)

    subject = subject_for(alert)
    srcdoc = h(render_alert_email_html(alert))
    email_text = render_alert_email_text(alert)

    real_send = (
        (settings.email_provider == "sendgrid" and settings.sendgrid_api_key)
        or (settings.email_provider == "resend" and settings.resend_api_key)
        or (settings.email_provider == "smtp" and settings.smtp_host)
    )
    if real_send:
        send_note = (
            '<div class="eg-flash">Envío real habilitado '
            f"({h(settings.email_provider)}). El botón “Enviar prueba” envía un correo de verdad.</div>"
        )
    else:
        send_note = (
            '<div class="eg-flash">El envío está en modo simulado. '
            "Configura SendGrid en Render para habilitar correos reales.</div>"
        )

    ready_btn = ""
    if alert["status"] == "pending_review":
        ready_btn = (
            f'<form method="post" action="/admin/alerts/{alert_id}/ready">'
            f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">Marcar lista</button></form>'
        )

    topbar = render_topbar(
        "Vista previa de email",
        "Revisa el correo tal como lo recibirá el suscriptor antes de enviarlo.",
        settings,
        show_action=False,
    )
    body = f"""
{send_note}
<div class="eg-preview-grid">
  <section class="eg-card eg-panel">
    <h2>Detalle de la alerta</h2>
    <dl class="eg-kv">
      <div><dt>Documento</dt><dd>{h(alert['title'])}</dd></div>
      <div><dt>Estado</dt><dd>{pill(alert['status'])}</dd></div>
      <div><dt>Categoría</dt><dd>{h(alert['category'])}</dd></div>
      <div><dt>Relevancia</dt><dd>{pill(alert['relevance'])}</dd></div>
      <div><dt>Fecha doc.</dt><dd>{h(alert.get('publication_date') or '—')}</dd></div>
      <div><dt>Asunto</dt><dd>{h(subject)}</dd></div>
      <div><dt>Fuente</dt><dd><a class="eg-link" href="{h(alert['canonical_url'])}" target="_blank" rel="noreferrer">Ver en DT ↗</a></dd></div>
    </dl>
    <div class="eg-actions">
      <a class="eg-btn eg-btn--secondary eg-btn--sm" href="/admin/alerts">{icon("back", 16)}<span>Volver a Alertas</span></a>
      {ready_btn}
      <form method="post" action="/admin/alerts/{alert_id}/test" class="eg-inline-form">
        <input class="eg-input eg-input--sm" type="email" name="to" placeholder="correo de prueba" aria-label="Correo de prueba">
        <button class="eg-btn eg-btn--primary eg-btn--sm" type="submit">Enviar prueba</button>
      </form>
    </div>
  </section>
  <section class="eg-card eg-panel">
    <h2>Vista previa del email</h2>
    <p class="eg-muted">Así se verá el correo en la bandeja del suscriptor.</p>
    <div class="eg-preview__frame">
      <iframe title="Vista previa del email (HTML)" srcdoc="{srcdoc}"></iframe>
    </div>
    <details style="margin-top:14px;">
      <summary>Ver versión en texto plano</summary>
      <pre>{h(email_text)}</pre>
    </details>
  </section>
</div>
"""
    return render_page("Vista previa de email", body, sidebar=sidebar, topbar=topbar)


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
    sidebar: str | None = None,
    topbar: str = "",
) -> str:
    head = f"""
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700&family=Montserrat:wght@500;600;700&display=swap" rel="stylesheet">
  <style>{CSS}</style>
</head>
"""
    if sidebar is not None:
        # Layout administrativo tipo SaaS: sidebar fijo + main con topbar + contenido.
        # Sin header/footer público.
        return f"""
<!doctype html>
<html lang="es">
{head}
<body class="eg eg--admin" data-eg-theme="light" data-eg-density="compact">
  <div class="eg-shell">
    <aside class="eg-sidebar" data-eg-theme="dark">{sidebar}</aside>
    <div class="eg-main">
      {topbar}
      <div class="eg-content">{body}</div>
    </div>
  </div>
</body>
</html>
""".strip()

    body_class = "eg eg--compact" if compact else "eg"
    # En modo embed (iframe) no incluimos header/footer ni cromo del shell.
    chrome_top = "" if compact else render_header()
    chrome_bottom = "" if compact else render_footer()
    return f"""
<!doctype html>
<html lang="es">
{head}
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

/* ---------- Secciones landing (beneficios / cómo funciona) ---------- */
.eg-section--light { background: var(--eg-bg); }
.eg-section--soft { background: var(--eg-surface-2); }
.eg-section__title { margin: 6px 0 26px; }
.eg-benefits { margin-top: 8px; }
.eg-benefit { text-align: left; }
.eg-benefit__icon {
  display: inline-grid; place-items: center; width: 46px; height: 46px; margin-bottom: 12px;
  border-radius: 14px; background: color-mix(in srgb, var(--eg-accent) 14%, transparent);
  font-size: 22px; line-height: 1;
}
.eg-benefit h3 { font-size: 1.05rem; margin: 0 0 6px; }
.eg-benefit p { margin: 0; font-size: 14px; color: var(--eg-text-muted); }
.eg-steps {
  list-style: none; margin: 0; padding: 0;
  display: grid; gap: 14px; grid-template-columns: repeat(2, minmax(0, 1fr));
}
.eg-step {
  display: flex; gap: 16px; align-items: flex-start;
  background: var(--eg-surface); border: 1px solid var(--eg-border);
  border-radius: var(--eg-radius); padding: 18px 20px;
}
.eg-step__num {
  flex: none; display: inline-grid; place-items: center; width: 36px; height: 36px;
  border-radius: 999px; background: var(--eg-cta); color: var(--eg-text-on-cta);
  font-family: var(--eg-font-heading); font-weight: 700; font-size: 16px;
}
.eg-step h3 { font-size: 1rem; margin: 4px 0 4px; }
.eg-step p { margin: 0; font-size: 14px; color: var(--eg-text-muted); }
@media (max-width: 720px) { .eg-steps { grid-template-columns: 1fr; } }

/* ---------- Feedback / Auth ---------- */
.eg-feedback, .eg-auth { padding: clamp(40px, 8vw, 90px) 20px; display: grid; justify-items: center; }
.eg-feedback__card, .eg-auth__card { max-width: 560px; width: 100%; text-align: center; }
.eg-auth__card { text-align: left; max-width: 440px; }
.eg-auth__help { color: var(--eg-text-muted); font-size: 14px; margin: 0 0 16px; }
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

/* ---------- Admin operativo (etapas 4/8/10/11) + pulido UX/UI ---------- */
.eg-devbanner {
  width: min(1180px, calc(100% - 40px)); margin: 0 auto 18px;
  background: #FEF3C7; color: #92400E; border: 1px solid #FCD34D;
  border-radius: 12px; padding: 12px 16px; font-size: 14px; font-weight: 700;
}
.eg-admin-header__sub { color: var(--eg-text-muted); margin: 4px 0 0; font-size: 14px; }

/* Estado del sistema (proveedor email / auth / último job) */
.eg-sysstatus {
  width: min(1180px, calc(100% - 40px)); margin: 0 auto 20px;
  display: flex; flex-wrap: wrap; gap: 10px 22px; align-items: center;
  background: var(--eg-surface); border: 1px solid var(--eg-border);
  border-radius: var(--eg-radius); padding: 12px 18px;
}
.eg-sysstatus__item { display: inline-flex; align-items: center; gap: 8px; font-size: 13.5px; color: var(--eg-text-muted); }
.eg-sysstatus__item b { color: var(--eg-text); font-weight: 700; }

/* Estados vacíos amigables */
.eg-empty { display: grid; gap: 4px; padding: 24px 8px; text-align: center; }
.eg-empty strong { color: var(--eg-text); font-size: 15px; }
.eg-empty span { color: var(--eg-text-subtle); font-size: 13.5px; }
.eg-empty-row td { background: transparent; }
.eg-empty-row:hover td { background: transparent; }

/* Link tabular destacado */
.eg-link { color: var(--eg-support); font-weight: 600; white-space: nowrap; }
.eg-link:hover { color: var(--eg-accent); }

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
/* eg-flash dentro de una card (preview/login) ocupa el ancho del contenedor */
.eg-preview .eg-flash, .eg-auth__card .eg-flash, .eg-form .eg-flash { width: 100%; }

/* ---------- Accesibilidad (etapa 14): foco visible y no depender solo del color ---------- */
.eg a:focus-visible, .eg-link:focus-visible, .eg-tab:focus-visible,
.eg-nav__link:focus-visible, summary:focus-visible {
  outline: 2px solid var(--eg-focus); outline-offset: 2px; border-radius: 6px;
}
.eg-check input:focus-visible { outline: 2px solid var(--eg-focus); outline-offset: 2px; }
.eg-pill { border: 1px solid color-mix(in srgb, currentColor 30%, transparent); }

/* ---------- Responsive del estado del sistema ---------- */
@media (max-width: 600px) {
  .eg-sysstatus { flex-direction: column; align-items: flex-start; gap: 8px; }
  .eg-inline-form { flex-wrap: wrap; }
  .eg-input--sm { max-width: 100%; flex: 1 1 160px; }
}

/* =====================================================================
   Panel administrativo SaaS con sidebar (rediseño)
   ===================================================================== */
.eg--admin { background: var(--eg-bg); }
.eg-shell { display: grid; grid-template-columns: 264px 1fr; min-height: 100vh; }

/* Sidebar */
.eg-sidebar {
  background: #0E2230; color: rgba(255,255,255,.82);
  position: sticky; top: 0; align-self: start; height: 100vh;
  display: flex; flex-direction: column; gap: 16px; padding: 22px 16px;
}
.eg-side__brand {
  display: flex; flex-direction: column; align-items: flex-start; gap: 6px;
  padding: 2px 8px 16px; border-bottom: 1px solid rgba(255,255,255,.08);
}
.eg-side__brand .eg-logo { height: 30px; width: auto; max-width: 160px; }
.eg-side__product {
  font-family: var(--eg-font-body); font-weight: 700; color: rgba(255,255,255,.7);
  font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
}
.eg-side__nav { display: flex; flex-direction: column; gap: 4px; }
.eg-side__link {
  display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 10px;
  color: rgba(255,255,255,.74); font-weight: 600; font-size: 14px; text-decoration: none;
  transition: background .16s ease, color .16s ease;
}
.eg-side__link:hover { background: rgba(255,255,255,.06); color: #fff; }
.eg-side__link.is-active { background: var(--eg-cta); color: var(--eg-text-on-cta); }
.eg-side__link:focus-visible { outline: 2px solid #24EBA1; outline-offset: 2px; }
.eg-side__ic { display: inline-flex; }
.eg-side__status {
  margin-top: auto; display: flex; align-items: center; gap: 8px; font-size: 12.5px;
  color: rgba(255,255,255,.6); padding: 12px; border-top: 1px solid rgba(255,255,255,.08);
}
.eg-side__dot { width: 8px; height: 8px; border-radius: 999px; background: #24EBA1; flex: none; }
.eg-side__dot[data-status="simulated"], .eg-side__dot[data-status="pending_review"] { background: #F79009; }
.eg-side__dot[data-status="error"] { background: #F97066; }
.eg-side__foot { font-size: 11.5px; color: rgba(255,255,255,.4); margin: 0; padding: 0 12px; }
.eg-ic { flex: none; }

/* Main + topbar */
.eg-main { min-width: 0; display: flex; flex-direction: column; }
.eg-topbar {
  position: sticky; top: 0; z-index: 20;
  background: rgba(245,247,250,.92); backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--eg-border);
  display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 28px;
}
.eg-topbar__titles h1 { font-size: 1.4rem; margin: 0; }
.eg-topbar__titles p { margin: 2px 0 0; color: var(--eg-text-muted); font-size: 13.5px; }
.eg-topbar__right { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }
.eg-topbar__status { display: inline-flex; align-items: center; gap: 6px; color: var(--eg-text-muted); font-size: 13px; }
.eg-content { padding: 24px 28px 48px; }
.eg-content .eg-flash, .eg-content .eg-devbanner { width: 100%; margin: 0 0 14px; }

/* Stats (cards de métricas) */
.eg-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 18px; }
.eg-stat {
  background: var(--eg-surface); border: 1px solid var(--eg-border); border-radius: var(--eg-radius);
  padding: 16px 18px; display: grid; gap: 2px; box-shadow: 0 8px 24px rgba(10,34,49,.05);
}
.eg-stat__ic {
  width: 34px; height: 34px; border-radius: 10px; display: inline-grid; place-items: center;
  color: #fff; background: var(--eg-text-subtle); margin-bottom: 6px;
}
.eg-stat[data-tone="accent"] .eg-stat__ic { background: #167A5F; }
.eg-stat[data-tone="info"] .eg-stat__ic { background: #0478B4; }
.eg-stat[data-tone="warning"] .eg-stat__ic { background: #B45309; }
.eg-stat[data-tone="success"] .eg-stat__ic { background: #12B76A; }
.eg-stat__num { font-family: var(--eg-font-heading); font-size: 1.9rem; line-height: 1; color: var(--eg-text); }
.eg-stat__label { font-weight: 700; font-size: 13.5px; color: var(--eg-text); margin-top: 4px; }
.eg-stat__sub { font-size: 12px; color: var(--eg-text-subtle); }

.eg-block-title { margin: 18px 0 12px; font-size: 1.15rem; }
.eg-section-note { color: var(--eg-text-muted); font-size: 14px; margin: 0 0 14px; }

/* Alertas en cards */
.eg-alert-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 14px; }
.eg-alert {
  background: var(--eg-surface); border: 1px solid var(--eg-border); border-radius: var(--eg-radius);
  padding: 18px; display: flex; flex-direction: column; gap: 10px; box-shadow: 0 8px 24px rgba(10,34,49,.05);
}
.eg-alert__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.eg-alert__cat { font-size: 12px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: var(--eg-text-subtle); }
.eg-alert__meta { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.eg-alert__title { font-size: 1rem; margin: 0; line-height: 1.3; }
.eg-alert__summary {
  margin: 0; color: var(--eg-text-muted); font-size: 13.5px; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.eg-alert__foot { margin-top: auto; display: flex; flex-direction: column; gap: 10px; }
.eg-alert__date { font-size: 12px; color: var(--eg-text-subtle); }

/* Vista previa: dos columnas */
.eg-preview-grid { display: grid; grid-template-columns: minmax(280px, 360px) 1fr; gap: 16px; align-items: start; }

.eg-btn .eg-ic { margin-right: -2px; }

/* Responsive del shell: sidebar pasa a barra superior */
@media (max-width: 860px) {
  .eg-shell { grid-template-columns: 1fr; }
  .eg-sidebar {
    position: static; height: auto; flex-direction: row; flex-wrap: wrap;
    align-items: center; gap: 10px; padding: 12px 16px;
  }
  .eg-side__brand { border-bottom: 0; padding: 0; margin-right: auto; }
  .eg-side__nav { flex-direction: row; flex-wrap: wrap; gap: 6px; width: 100%; overflow-x: auto; }
  .eg-side__status, .eg-side__foot { display: none; }
  .eg-topbar { position: static; flex-direction: column; align-items: flex-start; padding: 14px 16px; }
  .eg-topbar__right { width: 100%; justify-content: flex-start; }
  .eg-content { padding: 16px; }
  .eg-preview-grid { grid-template-columns: 1fr; }
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
