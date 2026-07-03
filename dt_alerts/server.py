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
    send_email as send_notifier_email,
    send_test_alert_email,
    subject_for,
)
from .worker import regenerate_alert, run_check, scheduler_loop
from . import wordpress_sync
from .summarizer import _generate_and_save as _ai_generate_direct
from .notifier import generate_executive_summary_html, generate_detailed_summary_html, _build_attachments


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
        elif path == "/admin/api/subscribers/count":
            if not self.is_admin():
                self.respond_json({"error": "No autorizado."}, status=HTTPStatus.UNAUTHORIZED)
                return
            with db.connect(self.settings.database_path) as conn:
                active = len(db.active_subscribers(conn))
            self.respond_json({"active": active})
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
        elif path in {"/admin", "/admin/subscribers", "/admin/alerts", "/admin/documents", "/admin/jobs", "/admin/settings"}:
            if not self.is_admin():
                self.redirect("/admin/login")
                return
            flash = query.get("flash", [""])[0]
            status_filter = query.get("status", [""])[0]
            page = max(1, int(query.get("page", ["1"])[0]) if query.get("page", ["1"])[0].isdigit() else 1)
            self.respond_html(render_admin(path, self.settings, flash=flash, status_filter=status_filter, page=page))
        elif match := re.match(r"^/admin/alerts/(\d+)/preview-email$", path):
            if not self.is_admin():
                self.redirect("/admin/login")
                return
            flash = query.get("flash", [""])[0]
            self.respond_html(render_alert_preview(int(match.group(1)), self.settings, flash=flash))
        elif match := re.match(r"^/admin/alerts/(\d+)/executive-summary$", path):
            if not self.is_admin():
                self.redirect("/admin/login")
                return
            self._serve_summary_attachment(int(match.group(1)), kind="executive")
        elif match := re.match(r"^/admin/alerts/(\d+)/detailed-summary$", path):
            if not self.is_admin():
                self.redirect("/admin/login")
                return
            self._serve_summary_attachment(int(match.group(1)), kind="detailed")
        elif path == "/admin/settings/ai-usage.csv":
            if not self.is_admin():
                self.redirect("/admin/login")
                return
            self._serve_ai_usage_csv()
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
                existing = conn.execute(
                    "SELECT id FROM subscribers WHERE id = ?", (subscriber_id,)
                ).fetchone()
                if not existing:
                    self.respond_json({"error": "Suscriptor no encontrado."}, status=HTTPStatus.NOT_FOUND)
                    return
                db.set_subscriber_status(
                    conn, subscriber_id, "paused" if action == "pause" else "active"
                )
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type or self.wants_json():
                self.respond_json({"success": True})
            else:
                self.redirect("/admin/subscribers")
        elif match := re.match(r"^/admin/subscribers/(\d+)/(activate|delete)$", path):
            self.require_admin()
            subscriber_id = int(match.group(1))
            action = match.group(2)
            with db.connect(self.settings.database_path) as conn:
                existing = conn.execute(
                    "SELECT id FROM subscribers WHERE id = ?", (subscriber_id,)
                ).fetchone()
                if not existing:
                    self.respond_json({"error": "Suscriptor no encontrado."}, status=HTTPStatus.NOT_FOUND)
                    return
                if action == "activate":
                    conn.execute(
                        "UPDATE subscribers SET status='active', updated_at=? WHERE id=?",
                        (db.utcnow(), subscriber_id),
                    )
                else:
                    conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
                conn.commit()
            self.respond_json({"success": True})
        elif match := re.match(r"^/admin/subscribers/(\d+)/plan$", path):
            self.require_admin()
            subscriber_id = int(match.group(1))
            payload = self.read_payload()
            plan_value = (payload.get("plan") or "").strip()
            if plan_value not in SUBSCRIBER_PLANS:
                self.respond_json({"error": "Plan inválido."}, status=HTTPStatus.BAD_REQUEST)
                return
            with db.connect(self.settings.database_path) as conn:
                existing = conn.execute(
                    "SELECT id FROM subscribers WHERE id = ?", (subscriber_id,)
                ).fetchone()
                if not existing:
                    self.respond_json({"error": "Suscriptor no encontrado."}, status=HTTPStatus.NOT_FOUND)
                    return
                conn.execute(
                    "UPDATE subscribers SET plan=?, updated_at=? WHERE id=?",
                    (plan_value, db.utcnow(), subscriber_id),
                )
                conn.commit()
            self.respond_json({"success": True})
        elif match := re.match(r"^/admin/alerts/(\d+)/delete$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                existing = conn.execute("SELECT id FROM alerts WHERE id = ?", (alert_id,)).fetchone()
                if not existing:
                    self.respond_json({"error": "Alerta no encontrada."}, status=HTTPStatus.NOT_FOUND)
                    return
                conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
                conn.commit()
            self.respond_json({"success": True})
        elif match := re.match(r"^/admin/alerts/(\d+)/ready$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                db.set_alert_status(conn, alert_id, "ready_to_send")
            self.redirect_flash("/admin/alerts", "Alerta marcada como lista para enviar.")
        elif match := re.match(r"^/admin/alerts/(\d+)/(send|resend)$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            content_type = self.headers.get("Content-Type", "")
            is_json = "application/json" in content_type or self.wants_json()
            with db.connect(self.settings.database_path) as conn:
                alert_check = conn.execute(
                    "SELECT id FROM alerts WHERE id = ?", (alert_id,)
                ).fetchone()
                if not alert_check:
                    if is_json:
                        self.respond_json({"error": "Alerta no encontrada."}, status=HTTPStatus.NOT_FOUND)
                    else:
                        self.redirect_flash("/admin/alerts", "Alerta no encontrada.")
                    return
                active_subs = db.active_subscribers(conn)
                if is_json and not active_subs:
                    self.respond_json({"error": "No hay suscriptores activos."}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    count = dispatch_alert(conn, alert_id, self.settings)
                except Exception as exc:
                    if is_json:
                        self.respond_json({"success": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    else:
                        self.redirect_flash("/admin/alerts", f"Error al enviar: {exc}")
                    return
            if is_json:
                self.respond_json({"success": True, "sent_count": count, "errors": []})
            else:
                msg = (
                    f"Alerta procesada para {count} suscriptor(es) activo(s)."
                    if count else
                    "Sin envíos: no hay suscriptores activos o ya habían recibido la alerta."
                )
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
                tmpl = db.get_setting(conn, "email_test_subject_template", "")
            if not alert:
                self.redirect_flash("/admin/alerts", "Alerta no encontrada.")
                return
            if tmpl:
                try:
                    subject = tmpl.format(title=alert.get("title", "documento"))
                except Exception:
                    subject = f"[PRUEBA] {subject_for(alert)}"
            else:
                subject = f"[PRUEBA] {subject_for(alert)}"
            attachments = _build_attachments(alert, self.settings)
            result = send_notifier_email(
                self.settings,
                to=to_email,
                subject=subject,
                html_body=render_alert_email_html(alert),
                text_body=render_alert_email_text(alert),
                attachments=attachments or None,
            )
            self.redirect_flash("/admin/alerts", flash_for_email_result(result))
        elif match := re.match(r"^/admin/alerts/(\d+)/generate-ai$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                alert = db.get_alert_with_document(conn, alert_id)
            if not alert:
                self.redirect_flash("/admin/alerts", "Alerta no encontrada.")
                return
            document_id = alert["document_id"]
            try:
                with db.connect(self.settings.database_path) as conn:
                    app_settings = db.get_all_settings(conn)
                    summary = _ai_generate_direct(conn, document_id, self.settings, app_settings, force=False)
                    db.create_or_update_alert(
                        conn, document_id,
                        summary=summary.summary,
                        key_points=summary.key_points,
                        practical_impacts=summary.practical_impacts,
                        relevance=summary.relevance,
                        status="pending_review",
                        ai_error=summary.ai_error,
                    )
                msg = "Resumen IA generado. Alerta pendiente de revisión."
            except Exception as exc:
                msg = f"Error al generar resumen IA: {exc}"
            self.redirect_flash("/admin/alerts", msg)
        elif match := re.match(r"^/admin/alerts/(\d+)/regenerate-ai$", path):
            self.require_admin()
            alert_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                alert = db.get_alert_with_document(conn, alert_id)
            if not alert:
                self.redirect_flash("/admin/alerts", "Alerta no encontrada.")
                return
            document_id = alert["document_id"]
            try:
                with db.connect(self.settings.database_path) as conn:
                    app_settings = db.get_all_settings(conn)
                    summary = _ai_generate_direct(conn, document_id, self.settings, app_settings, force=True)
                    db.create_or_update_alert(
                        conn, document_id,
                        summary=summary.summary,
                        key_points=summary.key_points,
                        practical_impacts=summary.practical_impacts,
                        relevance=summary.relevance,
                        status="pending_review",
                        ai_error=summary.ai_error,
                    )
                msg = "Resumen IA regenerado. Alerta vuelve a pendiente de revisión."
            except Exception as exc:
                msg = f"Error al regenerar resumen IA: {exc}"
            self.redirect_flash("/admin/alerts", msg)
        elif match := re.match(r"^/admin/documents/(\d+)/regenerate$", path):
            self.require_admin()
            document_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                alert_id = regenerate_alert(conn, document_id, self.settings)
            if alert_id:
                self.redirect_flash(
                    f"/admin/alerts/{alert_id}/preview-email",
                    "Resumen regenerado. Revisa la vista previa antes de enviarlo.",
                )
            else:
                self.redirect_flash("/admin/documents", "Documento no encontrado.")
        elif match := re.match(r"^/admin/documents/(\d+)/ignore$", path):
            self.require_admin()
            document_id = int(match.group(1))
            with db.connect(self.settings.database_path) as conn:
                db.set_document_status(conn, document_id, "ignored")
            self.redirect_flash("/admin/documents", "Documento marcado como ignorado.")
        elif path == "/admin/wordpress/sync":
            self.require_admin()
            result = wordpress_sync.sync(self.settings)
            if result["status"] == "ok":
                msg = (
                    f"Sincronización WordPress completada: "
                    f"{result['received']} recibidos, "
                    f"{result['created']} creados, "
                    f"{result['updated']} actualizados."
                )
            elif result["status"] == "disabled":
                msg = "Sincronización WordPress desactivada (WORDPRESS_SYNC_ENABLED=false)."
            elif result["status"] == "misconfigured":
                msg = f"WordPress sin configurar: {result['error']}"
            else:
                msg = f"Error sincronizando WordPress: {result['error']}"
            self.redirect_flash("/admin/subscribers", msg)
        elif path == "/admin/settings":
            self.require_admin()
            payload = self.read_payload()
            editable_keys = {
                "email_from_name", "email_from", "email_reply_to",
                "email_subject_template", "email_test_subject_template", "email_footer_legal",
                # IA editable (no sensible — no API keys)
                "ai_system_prompt", "ai_summary_style", "ai_analysis_focus",
                "ai_review_required", "ai_attachments_enabled",
                "ai_email_intro_template", "ai_footer_disclaimer",
            }
            with db.connect(self.settings.database_path) as conn:
                for key in editable_keys:
                    if key in payload:
                        db.set_setting(conn, key, (payload[key] or "").strip())
            section = payload.get("_section", "email")
            if section == "ai":
                self.redirect_flash("/admin/settings", "Configuracion IA guardada.")
            else:
                self.redirect_flash("/admin/settings", "Configuracion de email guardada.")
        elif path == "/admin/settings/test-sendgrid":
            self.require_admin()
            to_email = self.settings.test_email_to
            if not to_email:
                self.redirect_flash(
                    "/admin/settings",
                    "Configura TEST_EMAIL_TO en .env para enviar la prueba SendGrid.",
                )
                return
            result = send_notifier_email(
                self.settings,
                to=to_email,
                subject="[Prueba SendGrid] Alertas DT - Configuracion verificada",
                html_body=(
                    "<p>Prueba de configuracion SendGrid desde Alertas DT.</p>"
                    "<p>Si recibes este correo, el envio de alertas por email funciona correctamente.</p>"
                ),
                text_body=(
                    "Prueba de configuracion SendGrid desde Alertas DT.\n"
                    "Si recibes este correo, el envio de alertas por email funciona correctamente."
                ),
            )
            self.redirect_flash("/admin/settings", flash_for_email_result(result))
        elif path == "/admin/settings/test-wordpress":
            self.require_admin()
            msg = _test_wordpress_connection(self.settings)
            self.redirect_flash("/admin/settings", msg)
        elif path == "/admin/settings/ai-toggle":
            self.require_admin()
            if not self.settings.ai_api_key:
                self.respond_json({"error": "API key no configurada."}, status=HTTPStatus.BAD_REQUEST)
                return
            payload = self.read_payload()
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
            with db.connect(self.settings.database_path) as conn:
                db.set_setting(conn, "ai_runtime_enabled", "true" if enabled else "false")
            self.respond_json({"success": True, "enabled": enabled})
        elif path == "/admin/settings/ai-enable":
            self.require_admin()
            with db.connect(self.settings.database_path) as conn:
                db.set_setting(conn, "ai_runtime_enabled", "true")
            self.redirect_flash("/admin/settings", "Integración IA activada desde el panel.")
        elif path == "/admin/settings/ai-disable":
            self.require_admin()
            with db.connect(self.settings.database_path) as conn:
                db.set_setting(conn, "ai_runtime_enabled", "false")
            self.redirect_flash("/admin/settings", "Integración IA apagada desde el panel. No se llamará a la API.")
        elif path == "/admin/settings/test-ai":
            self.require_admin()
            with db.connect(self.settings.database_path) as conn:
                app_settings = db.get_all_settings(conn)
                msg = _test_ai_connection(self.settings, app_settings, conn)
                db.set_setting(conn, "ai_last_test", f"{db.utcnow()} · {msg[:100]}")
            self.redirect_flash("/admin/settings", msg)
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
                    whatsapp=None,
                    notify_email=True,
                    notify_whatsapp=False,
                    source_page=payload.get("source_page"),
                    consent=bool_from_form(payload.get("consent")),
                    subscriber_name=payload.get("subscriber_name") or None,
                    phone=payload.get("phone") or None,
                    whatsapp_consent=bool_from_form(payload.get("whatsapp_consent")),
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

    def _serve_summary_attachment(self, alert_id: int, kind: str) -> None:
        with db.connect(self.settings.database_path) as conn:
            alert = db.get_alert_with_document(conn, alert_id)
        if not alert:
            self.respond_not_found()
            return
        document_id = alert.get("document_id") or alert_id
        if kind == "executive":
            content = generate_executive_summary_html(document_id, alert)
            filename = f"resumen_ejecutivo_{document_id}.html"
        else:
            content = generate_detailed_summary_html(document_id, alert)
            filename = f"resumen_detallado_{document_id}.html"
        raw = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_ai_usage_csv(self) -> None:
        import csv
        import io
        DB_COLS = ["id", "created_at", "operation", "status", "provider", "model",
                   "input_tokens", "output_tokens", "total_tokens", "error"]
        with db.connect(self.settings.database_path) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(DB_COLS)} FROM ai_usage_logs ORDER BY id DESC LIMIT 1000"  # noqa: S608
            ).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(DB_COLS + ["estimated_cost_usd", "estimated_cost_clp"])
        for row in rows:
            cost_usd, cost_clp = _calc_ai_cost(
                row["input_tokens"] or 0,
                row["output_tokens"] or 0,
                self.settings,
            )
            writer.writerow([
                row["id"], row["created_at"], row["operation"], row["status"],
                row["provider"] or "", row["model"] or "",
                row["input_tokens"], row["output_tokens"], row["total_tokens"],
                (row["error"] or "")[:500],
                f"{cost_usd:.6f}", f"{cost_clp:.2f}",
            ])
        raw = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="ai_usage.csv"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def public_subscriber(subscriber: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": subscriber["id"],
        "email": subscriber["email"],
        "subscriber_name": subscriber.get("subscriber_name"),
        "phone": subscriber.get("phone"),
        "notify_email": bool(subscriber["notify_email"]),
        "notify_whatsapp": bool(subscriber["notify_whatsapp"]),
        "whatsapp_consent": bool(subscriber.get("whatsapp_consent")),
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
        err = result.get("error") or "desconocido"
        return f"Error SendGrid: {err}. Revisa API key y permisos Mail Send."
    return result.get("message") or "Prueba procesada."


def fmt_dt(value: Any) -> str:
    """Formatea timestamps ISO a algo legible (YYYY-MM-DD HH:MM)."""
    text = str(value or "")
    return text.replace("T", " ")[:16] if text else "—"


def mask_secret(value: str | None, visible_start: int = 6, visible_end: int = 4) -> str:
    """Enmascara un secreto mostrando solo los primeros/últimos caracteres."""
    if not value:
        return "No configurado"
    v = str(value)
    if len(v) < 12:
        return "••••••••"
    dots = "•" * min(len(v) - visible_start - visible_end, 20)
    return v[:visible_start] + dots + v[-visible_end:]


def _calc_ai_cost(
    input_tokens: int,
    output_tokens: int,
    settings: Settings,
) -> tuple[float, float]:
    """Retorna (cost_usd, cost_clp) estimado para los tokens dados."""
    input_price = getattr(settings, "ai_input_price_per_1m_usd", 2.00)
    output_price = getattr(settings, "ai_output_price_per_1m_usd", 8.00)
    rate = getattr(settings, "ai_usd_clp_rate", 921)
    cost_usd = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
    cost_clp = cost_usd * rate
    return cost_usd, cost_clp


# Defaults de plantillas de email (fallback si no hay valor en DB)
EMAIL_SETTINGS_DEFAULTS: dict[str, str] = {
    "email_subject_template": "Nueva normativa DT: {title}",
    "email_test_subject_template": "[PRUEBA] Nueva normativa DT: {title}",
    "email_footer_legal": (
        "Este resumen es informativo y no reemplaza la lectura del documento oficial "
        "ni asesoría profesional."
    ),
}


# --------------------------------------------------------------------------
# Sistema de badges (diseño editorial prototype)
# --------------------------------------------------------------------------
_STATUS_BADGE_CLASS: dict[str, str] = {
    "active": "eg-badge--active", "sent": "eg-badge--sent",
    "ready_to_send": "eg-badge--ready", "ready": "eg-badge--ready",
    "processed": "eg-badge--active", "success": "eg-badge--active",
    "alto": "eg-badge--active", "paused": "eg-badge--paused",
    "pending_review": "eg-badge--pending", "discovered": "eg-badge--pending",
    "running": "eg-badge--pending", "partial": "eg-badge--pending",
    "medio": "eg-badge--pending", "baseline": "eg-badge--baseline",
    "ignored": "eg-badge--paused", "error": "eg-badge--danger",
    "failed": "eg-badge--danger", "bajo": "eg-badge--danger",
    "simulated": "eg-badge--paused", "skipped_missing_credentials": "eg-badge--paused",
}
_RELEVANCE_CLASS: dict[str, str] = {
    "alto": "eg-rel--high", "medio": "eg-rel--mid", "bajo": "eg-rel--low",
}
_TONE_ICO: dict[str, str] = {
    "accent": "eg-ico-green", "info": "eg-ico-blue",
    "warning": "eg-ico-warn", "success": "eg-ico-green", "muted": "eg-ico-slate",
}


def badge(value: Any, label: Any = None, *, no_dot: bool = False) -> str:
    v = str(value or "")
    text = label if label is not None else status_label(v)
    cls = _STATUS_BADGE_CLASS.get(v, "eg-badge--baseline")
    nd = " eg-badge--no-dot" if no_dot else ""
    return f'<span class="eg-badge {cls}{nd}">{h(text)}</span>'


def rel_badge(value: Any) -> str:
    v = str(value or "")
    cls = _RELEVANCE_CLASS.get(v, "eg-rel--mid")
    return f'<span class="eg-rel {cls}">{h(status_label(v))}</span>'


_EG_LOGO_SVG = (
    '<svg width="38" height="38" style="color:#fff" aria-hidden="true">'
    '<use href="#eg-logo"/></svg>'
)
_EG_LOGO_SYMBOL = """<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="eg-logo" viewBox="0 0 44 44">
    <path d="M30 6H14a8 8 0 0 0-8 8v6h9v-4a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v1h6v-4a8 8 0 0 0-8-8Z" fill="currentColor" opacity=".55"/>
    <path d="M14 38h16a8 8 0 0 0 8-8v-6h-9v4a3 3 0 0 1-3 3H14a3 3 0 0 1-3-3v-1H5v4a8 8 0 0 0 8 8Z" fill="currentColor"/>
  </symbol>
</svg>"""


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
    "pause": '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>',
    "play": '<polygon points="5 3 19 12 5 21 5 3"/>',
    "x-circle": '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/>',
    "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
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
    "/admin/settings": ("Configuración", "Estado técnico e integraciones de la aplicación."),
}

SIDEBAR_NAV = [
    ("/admin", "Resumen", "dashboard"),
    ("/admin/subscribers", "Suscriptores", "users"),
    ("/admin/documents", "Documentos", "document"),
    ("/admin/alerts", "Alertas", "bell"),
    ("/admin/jobs", "Monitoreo", "activity"),
    ("/admin/settings", "Configuración", "settings"),
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


def render_sidebar(path: str, settings: Settings, *, pending_alerts: int = 0) -> str:
    email_label, email_key = email_mode(settings)
    parts = []
    for href, label, ic in SIDEBAR_NAV:
        active = path == href
        cls = ' class="active"' if active else ""
        aria = ' aria-current="page"' if active else ""
        badge_html = ""
        if href == "/admin/alerts" and pending_alerts > 0:
            badge_html = (
                f'<span style="margin-left:auto;background:#F59E0B;color:#fff;'
                f'font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px;'
                f'line-height:16px;">{pending_alerts}</span>'
            )
        parts.append(f'<a href="{href}"{cls}{aria}>{icon(ic, 19)}<span>{label}</span>{badge_html}</a>')
    items = "".join(parts)
    return f"""
<div class="eg-brand">
  <div class="eg-brand-logo-wrap">
    <img src="{EG_LOGO_LIGHT}" alt="External Group" style="display:block;width:120px;height:auto;object-fit:contain;">
    <div class="eg-brand-sub" style="margin-top:6px;">ALERTAS DT</div>
  </div>
</div>
<div class="eg-hairline"></div>
<nav class="eg-nav" aria-label="Navegación del panel">{items}</nav>
<div class="eg-side-foot">
  <div class="eg-hairline" style="margin:0 0 16px"></div>
  <div class="eg-side-status">
    <span class="eg-dot" data-status="{h(email_key)}"></span>
    <span>{h(email_label)}</span>
  </div>
  <div class="eg-side-tag">MVP interno · External Group</div>
</div>
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
            '<form method="post" action="/api/jobs/check-dt">'
            '<input type="hidden" name="manual" value="1">'
            '<button class="eg-btn eg-btn--primary eg-btn--sm" type="submit" '
            'formaction="/api/jobs/check-dt" data-job-token>'
            f'{icon("refresh", 18)}<span>Ejecutar monitoreo</span></button></form>'
        )
    neutral_email = " is-neutral" if email_key not in {"active"} else ""
    neutral_auth = " is-neutral" if auth_key not in {"active"} else ""
    return f"""
<header class="eg-topbar">
  <div class="eg-topbar-titles">
    <h1>{h(title)}</h1>
    <p>{h(subtitle)}</p>
  </div>
  <div class="eg-topbar-meta">
    <span class="eg-status-pill{neutral_email}">{h(email_label)}</span>
    <span class="eg-status-pill{neutral_auth}">{h(auth_label)}</span>
    {action}
  </div>
</header>
"""


def metric_card(ic: str, value: Any, label: str, sub: str, tone: str = "muted") -> str:
    ico_cls = _TONE_ICO.get(tone, "eg-ico-slate")
    return (
        f'<div class="eg-metric">'
        f'<div class="eg-metric-ico {ico_cls}">{icon(ic, 19)}</div>'
        f'<div class="eg-metric-num">{h(value)}</div>'
        f'<div class="eg-metric-label">{h(label)}</div>'
        f'<div class="eg-metric-sub">{h(sub)}</div>'
        f'</div>'
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
                f'<dt>Último error</dt><dd class="eg-muted mono">{h(last_job["error"])}</dd>'
            )
    return f"""
<section class="eg-card eg-panel">
  <p class="eg-eyebrow">Sistema</p>
  <h2>Estado del sistema</h2>
  <dl class="eg-kv eg-kv--2col">
    <dt>Email</dt><dd>{badge(email_key, email_label, no_dot=True)}</dd>
    <dt>Modo de envío</dt><dd>{'Correos reales' if real else 'Simulado'}</dd>
    <dt>Acceso admin</dt><dd>{badge(auth_key, auth_label, no_dot=True)}</dd>
    <dt>Último monitoreo</dt><dd class="mono">{h(last_job_html)}</dd>
    {last_error}
  </dl>
</section>
"""


def render_admin(path: str, settings: Settings, *, flash: str = "", status_filter: str = "", page: int = 1) -> str:
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
            '<div role="alert" style="font-size:12px;padding:6px 16px;background:#FFF3CD;'
            'color:#856404;border-bottom:1px solid #FFEEBA;text-align:center;">'
            "⚠ Modo desarrollo: autenticación desactivada (DISABLE_ADMIN_AUTH=True)"
            "</div>"
        )
    if flash:
        banner += f'<div class="eg-flash" role="status">{h(flash)}</div>'

    if path == "/admin/settings":
        title, subtitle = SECTION_META["/admin/settings"]
        sidebar = render_sidebar(path, settings, pending_alerts=pending_count)
        topbar = render_topbar(title, subtitle, settings, show_action=False)
        content = banner + render_settings(settings) + MONITOR_SCRIPT
        return render_page(title, content, sidebar=sidebar, topbar=topbar)
    elif path == "/admin/subscribers":
        section = render_db_info(settings, subscribers) + render_subscribers(subscribers)
    elif path == "/admin/documents":
        section = render_documents(documents)
    elif path == "/admin/alerts":
        section = render_alerts_table(alerts, status_filter=status_filter, page=page)
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
        # --- Bloque "Siguiente acción recomendada" ---
        activity_lines: list[str] = []
        if pending_count:
            activity_lines.append(
                f'<span>Tienes <strong>{pending_count}</strong> alerta{"s" if pending_count != 1 else ""} pendiente{"s" if pending_count != 1 else ""} de revisión.</span>'
            )
        if ready_count:
            activity_lines.append(
                f'<span>Listas para enviar: <strong>{ready_count}</strong>.</span>'
            )
        activity_lines.append(
            f'<span>Suscriptores activos: <strong>{active_count}</strong>.</span>'
        )
        if last_job:
            job_status_label = status_label(last_job.get("status") or "")
            job_new = last_job.get("discovered_count") or 0
            activity_lines.append(
                f'<span>Último monitoreo: {fmt_dt(last_job.get("started_at"))} · {h(job_status_label)}'
                f'{f" · {job_new} nuevos" if job_new else ""}.</span>'
            )
        if pending_count:
            cta_label = "Revisar alerta pendiente"
            cta_href = "/admin/alerts"
            cta_icon = "bell"
        elif ready_count:
            cta_label = "Ver alertas listas para enviar"
            cta_href = "/admin/alerts"
            cta_icon = "send"
        else:
            cta_label = "Ejecutar monitoreo"
            cta_href = "#run-monitor"
            cta_icon = "activity"
        activity_html = (
            '<section class="eg-card eg-panel" style="margin-bottom:16px;">'
            '<p class="eg-eyebrow">Actividad</p>'
            '<h2>Siguiente acción recomendada</h2>'
            '<div style="display:flex;flex-direction:column;gap:5px;margin:10px 0 14px;font-size:13.5px;color:var(--eg-muted);">'
            + "".join(f'<div>{line}</div>' for line in activity_lines)
            + '</div>'
            f'<a class="eg-btn eg-btn--primary eg-btn--sm" href="{cta_href}">'
            f'{icon(cta_icon, 15)}<span>{cta_label}</span></a>'
            '</section>'
        )
        _pending_statuses = {"pending_review", "pending", "ready_to_send", "ready", "fallback"}
        pending_alerts = [a for a in alerts if a.get("status") in _pending_statuses]
        section = (
            f'<div class="eg-metric-grid">{cards}</div>'
            + activity_html
            + render_system_status(settings, last_job)
            + render_jobs(jobs[:5])
            + render_alerts(
                pending_alerts[:6],
                title="Alertas pendientes de acción",
                empty_msg="✓ No hay alertas pendientes de revisión.",
                empty_hint="Todas las alertas han sido enviadas o no hay nuevas.",
                history_link="/admin/alerts",
            )
        )

    title, subtitle = SECTION_META.get(path, SECTION_META["/admin"])
    content = banner + section + MONITOR_SCRIPT
    sidebar = render_sidebar(path, settings, pending_alerts=pending_count)
    topbar = render_topbar(title, subtitle, settings)
    return render_page(title, content, sidebar=sidebar, topbar=topbar)


def render_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return (
            '<section class="eg-card eg-panel">'
            '<div class="eg-card-head"><h2>Historial de monitoreo</h2></div>'
            '<div class="eg-empty"><strong>Aún no se ha ejecutado el monitoreo.</strong>'
            '<span>Usa "Ejecutar monitoreo" para buscar nuevas publicaciones de la DT.</span></div></section>'
        )
    rows = "".join(
        f"""
<tr>
  <td class="mono">{fmt_dt(job['started_at'])}</td>
  <td>{badge(job['status'])}</td>
  <td class="mono">{h(job['discovered_count'])}</td>
  <td class="mono">{h(job['processed_count'])}</td>
  <td class="mono">{h(job['sent_count'])}</td>
  <td class="eg-muted mono">{h(job.get('error') or '—')}</td>
</tr>
"""
        for job in jobs
    )
    return f"""
<section class="eg-card eg-panel">
  <div class="eg-card-head"><h2>Historial de jobs</h2></div>
  <div class="eg-table-wrap">
    <table class="eg-table">
      <thead><tr><th>Inicio</th><th>Estado</th><th>Nuevos</th><th>Procesados</th><th>Envíos</th><th>Error</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>
"""


def render_db_info(settings: Settings, subscribers: list[dict[str, Any]]) -> str:
    """Card compacta de WordPress Sync para la vista de suscriptores."""
    last_update = max((s.get("updated_at") or "" for s in subscribers), default="")
    wp_synced_count = sum(
        1 for s in subscribers
        if "wordpress" in (s.get("source_page") or "").lower()
        or "wp" in (s.get("source_page") or "").lower()
    )

    if settings.wordpress_sync_enabled:
        return f"""
<section class="eg-card eg-panel" style="margin-bottom:16px;">
  <div class="eg-card-head">
    <h2>WordPress Sync</h2>{badge("active", "Activo")}
  </div>
  <dl class="eg-kv eg-kv--2col">
    <dt>Última sincronización</dt><dd class="mono">{fmt_dt(last_update) if last_update else '—'}</dd>
    <dt>Suscriptores sincronizados</dt><dd class="mono">{wp_synced_count}</dd>
  </dl>
  <form method="post" action="/admin/wordpress/sync" style="margin-top:12px;text-align:center;">
    <button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">
      {icon("refresh", 14)}<span>↺ Sincronizar ahora</span>
    </button>
  </form>
</section>"""
    return f"""
<section class="eg-card eg-panel" style="margin-bottom:16px;">
  <div class="eg-card-head">
    <h2>WordPress Sync</h2>{badge("paused", "Desactivado")}
  </div>
  <p class="eg-muted" style="font-size:13px;">
    Activa <code>WORDPRESS_SYNC_ENABLED=true</code> en Configuración para importar suscriptores desde WordPress.
    <a href="/admin/settings" style="color:var(--eg-accent);">Ir a Configuración →</a>
  </p>
</section>"""


def _test_wordpress_connection(settings: Settings) -> str:
    """Prueba de conectividad con la API WordPress. Retorna mensaje para flash."""
    import urllib.request, urllib.error  # noqa: E401
    if not settings.wordpress_sync_enabled:
        return "WordPress sync desactivado (WORDPRESS_SYNC_ENABLED=false)."
    if not settings.wordpress_api_url:
        return "WORDPRESS_API_URL no configurado."
    url = f"{settings.wordpress_api_url}/subscribers?limit=1"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.wordpress_api_token}",
            "Accept": "application/json",
            "User-Agent": "AlertasDT-Sync/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            code = resp.getcode()
        return f"Conexion WordPress OK (HTTP {code})."
    except urllib.error.HTTPError as e:
        return f"Error HTTP {e.code} al conectar con WordPress."
    except Exception as exc:
        return f"Error al conectar con WordPress: {exc}"


def _test_ai_connection(settings: Settings, app_settings: dict, conn=None) -> str:
    """Test AI connectivity. Records usage in ai_usage_logs if conn is provided. Never logs API key."""
    provider = (settings.ai_provider or "disabled").lower()
    model = settings.ai_model or ""
    daily_limit = int(getattr(settings, "ai_daily_token_limit", 50000) or 0)
    monthly_limit = int(getattr(settings, "ai_monthly_token_limit", 500000) or 0)

    def _record(log_status: str, *, input_tokens: int = 0, output_tokens: int = 0,
                total_tokens: int = 0, log_error: str | None = None) -> None:
        if conn is None:
            return
        db.record_ai_usage(
            conn,
            document_id=None,
            alert_id=None,
            provider=provider,
            model=model,
            operation="test_connection",
            status=log_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            daily_limit=daily_limit,
            monthly_limit=monthly_limit,
            error=log_error,
        )

    if not bool(getattr(settings, "ai_enabled", False)):
        msg = "IA bloqueada por AI_ENABLED=false en .env. No se llamará a la API."
        _record("disabled", log_error=msg)
        return msg

    runtime = (app_settings.get("ai_runtime_enabled") or "").strip().lower()
    effective_enabled = False if runtime in {"0", "false", "no", "n", "off"} else True

    if not effective_enabled:
        msg = "IA apagada desde el panel. Actívala antes de probar conexión."
        _record("disabled", log_error=msg)
        return msg

    if provider == "disabled":
        msg = "IA desactivada (AI_PROVIDER=disabled). Configura AI_PROVIDER=openai o azure."
        _record("disabled", log_error=msg)
        return msg

    if not settings.ai_api_key:
        msg = "Sin API key: configura AI_API_KEY en el archivo .env."
        _record("missing_key", log_error=msg)
        return msg

    if provider == "azure" and not settings.ai_base_url:
        msg = "Azure requiere AI_BASE_URL configurado en el archivo .env."
        _record("error", log_error=msg)
        return msg

    if conn is not None:
        usage_status = db.get_ai_usage_status(conn, daily_limit=daily_limit, monthly_limit=monthly_limit)
        if usage_status.get("daily_exceeded") or usage_status.get("monthly_exceeded"):
            if usage_status.get("daily_exceeded"):
                msg = f"Límite diario IA alcanzado ({usage_status.get('today_tokens')} / {daily_limit} tokens). No se ejecutará prueba."
            else:
                msg = f"Límite mensual IA alcanzado ({usage_status.get('month_tokens')} / {monthly_limit} tokens). No se ejecutará prueba."
            _record("blocked_limit", log_error=msg)
            return msg

    try:
        from .summarizer import call_ai_with_usage

        ai_response = call_ai_with_usage(
            "Responde solo con JSON válido.",
            '{"ok": true, "message": "test"}',
            settings,
        )
        if ai_response.content:
            _record(
                "success",
                input_tokens=ai_response.input_tokens,
                output_tokens=ai_response.output_tokens,
                total_tokens=ai_response.total_tokens,
            )
            return f"Conexión IA OK ({provider}, modelo: {settings.ai_model or 'default'})."
        msg = "Conexión IA sin respuesta."
        _record("error", log_error=msg)
        return msg
    except Exception as exc:
        error_msg = str(exc)
        if settings.ai_api_key and settings.ai_api_key in error_msg:
            error_msg = error_msg.replace(settings.ai_api_key, "[REDACTED]")
        msg = f"Error al probar IA: {error_msg[:300]}"
        _record("error", log_error=error_msg[:500])
        return msg


def _render_ai_usage_table(logs: list[dict], settings: "Settings | None" = None) -> str:
    """Tabla compacta 'Últimos usos IA' para el panel de configuración."""
    csv_btn = (
        f'<a class="eg-btn eg-btn--secondary eg-btn--sm" href="/admin/settings/ai-usage.csv" download>'
        f'{icon("document", 14)}<span>Exportar CSV</span></a>'
    )
    if not logs:
        return (
            f'<section class="eg-card eg-panel">'
            f'<div class="eg-card-head"><h2>Últimos usos IA</h2>{csv_btn}</div>'
            f'<div class="eg-empty"><strong>Sin registros de uso IA aún.</strong>'
            f'<span>Los registros aparecen tras generar resúmenes o probar conexión.</span></div>'
            f'</section>'
        )
    def _cost_cell(entry: dict) -> str:
        if settings is None:
            return "—"
        _, clp = _calc_ai_cost(
            entry.get("input_tokens") or 0,
            entry.get("output_tokens") or 0,
            settings,
        )
        return f"CLP {clp:,.0f}".replace(",", ".")

    rows = "".join(
        f"""<tr>
  <td class="mono" style="font-size:11px;">{h(fmt_dt(entry.get("created_at")))}</td>
  <td class="mono" style="font-size:11px;">{h(entry.get("operation") or "—")}</td>
  <td>{badge(entry.get("status") or "")}</td>
  <td class="mono" style="font-size:11px;">{h(entry.get("provider") or "—")}</td>
  <td class="mono" style="font-size:11px;">{h(entry.get("model") or "—")}</td>
  <td class="mono" style="font-size:11px;">{h(entry.get("input_tokens") or 0)}</td>
  <td class="mono" style="font-size:11px;">{h(entry.get("output_tokens") or 0)}</td>
  <td class="mono" style="font-size:11px;">{h(entry.get("total_tokens") or 0)}</td>
  <td class="mono" style="font-size:11px;">{_cost_cell(entry)}</td>
  <td class="eg-muted mono" style="font-size:11px;">{h((entry.get("error") or "")[:60] or "—")}</td>
</tr>"""
        for entry in logs
    )
    return f"""<section class="eg-card eg-panel">
  <div class="eg-card-head"><h2>Últimos usos IA</h2>{csv_btn}</div>
  <div class="eg-table-wrap">
    <table class="eg-table">
      <thead><tr>
        <th>Fecha</th><th>Operación</th><th>Estado</th><th>Proveedor</th>
        <th>Modelo</th><th>Input</th><th>Output</th><th>Total</th><th>Costo est.</th><th>Error</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>"""


def render_settings(settings: Settings) -> str:
    """Renderiza la página de Configuración técnica del admin."""
    import datetime
    import os

    db_path = settings.database_path
    db_exists = db_path.exists()
    db_size = f"{db_path.stat().st_size / 1024:.1f} KB" if db_exists else "—"
    db_mtime = ""
    if db_exists:
        import time
        db_mtime = datetime.datetime.fromtimestamp(db_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    project_root_str = str(db_path.resolve())
    db_in_repo = "data/" in project_root_str or str(db_path).startswith("data/")

    with db.connect(db_path) as conn:
        n_subscribers = db.count_table(conn, "subscribers")
        n_documents = db.count_table(conn, "documents")
        n_alerts = db.count_table(conn, "alerts")
        n_deliveries = db.count_table(conn, "deliveries")
        n_jobs = db.count_table(conn, "job_runs")
        last_delivery = db.last_delivery_sent_at(conn)
        app_cfg = db.get_all_settings(conn)
        wp_subscribers = db.count_table(conn, "subscribers")

    wp_from_wp = sum(
        1 for _ in [None]  # computed below via raw query
    )
    with db.connect(db_path) as conn:
        wp_row = conn.execute(
            "SELECT COUNT(*) AS n FROM subscribers WHERE source_page LIKE '%wordpress%' OR source_page LIKE '%wp%'"
        ).fetchone()
        wp_synced_count = int(wp_row["n"]) if wp_row else 0
        last_wp_sync = conn.execute(
            "SELECT updated_at FROM subscribers WHERE source_page LIKE '%wordpress%' OR source_page LIKE '%wp%' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        last_wp_sync_at = last_wp_sync["updated_at"] if last_wp_sync else None

    # --- Estado general ---
    auth_label, auth_key = auth_mode(settings)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ambiente = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development"))

    section_general = f"""
<section class="eg-card eg-panel">
  <h2>{icon("cpu", 17)} Estado general</h2>
  <dl class="eg-kv eg-kv--2col">
    <dt>Modo de ejecucion</dt><dd>Local</dd>
    <dt>APP_BASE_URL</dt><dd class="mono">{h(settings.app_base_url)}</dd>
    <dt>APP_HOST</dt><dd class="mono">{h(settings.app_host)}</dd>
    <dt>APP_PORT</dt><dd class="mono">{h(settings.app_port)}</dd>
    <dt>Acceso admin</dt><dd>{pill(auth_key, auth_label)}</dd>
    <dt>Fecha/hora sistema</dt><dd class="mono">{h(now_str)}</dd>
    <dt>Ambiente</dt><dd class="mono">{h(ambiente)}</dd>
  </dl>
</section>
"""

    # --- SendGrid ---
    email_label, email_key = email_mode(settings)
    provider = settings.email_provider
    if provider == "sendgrid" and settings.sendgrid_api_key:
        sg_estado = pill("active", "Configurado")
    elif provider == "sendgrid":
        sg_estado = pill("error", "Sin credenciales")
    else:
        sg_estado = pill("simulated", f"{provider} (no SendGrid)")

    sg_test_btn = ""
    if settings.test_email_to:
        sg_test_btn = f"""
  <form method="post" action="/admin/settings/test-sendgrid" style="margin-top:14px">
    <button class="eg-btn eg-btn--primary eg-btn--sm" type="submit">
      {icon("send", 14)}<span>Enviar prueba a {h(settings.test_email_to)}</span>
    </button>
  </form>"""
    else:
        sg_test_btn = (
            '<p class="eg-muted" style="margin-top:10px;font-size:13px;">'
            'Configura TEST_EMAIL_TO en .env para habilitar la prueba de envio.</p>'
        )

    section_sendgrid = f"""
<section class="eg-card eg-panel">
  <h2>{icon("mail", 17)} SendGrid</h2>
  <dl class="eg-kv eg-kv--2col">
    <dt>Estado</dt><dd>{sg_estado}</dd>
    <dt>EMAIL_PROVIDER</dt><dd class="mono">{h(provider)}</dd>
    <dt>SENDGRID_API_KEY</dt><dd class="mono">{h(mask_secret(settings.sendgrid_api_key))}</dd>
    <dt>EMAIL_FROM</dt><dd class="mono">{h(settings.email_from or '—')}</dd>
    <dt>EMAIL_FROM_NAME</dt><dd class="mono">{h(settings.email_from_name or '—')}</dd>
    <dt>EMAIL_REPLY_TO</dt><dd class="mono">{h(settings.email_reply_to or '—')}</dd>
    <dt>TEST_EMAIL_TO</dt><dd class="mono">{h(settings.test_email_to or '—')}</dd>
    <dt>Ultimo envio</dt><dd class="mono">{h(fmt_dt(last_delivery) if last_delivery else '—')}</dd>
  </dl>
  {sg_test_btn}
</section>
"""

    # --- Base de datos ---
    db_warn = ""
    if db_in_repo:
        db_warn = (
            '<div class="eg-settings-warn">'
            'Para produccion local se recomienda mover la base de datos fuera del repositorio, '
            'por ejemplo <code>/Users/Shared/AlertasDT/data/dt_alertas.sqlite3</code>.'
            '</div>'
        )

    section_db = f"""
<section class="eg-card eg-panel">
  <h2>{icon("database", 17)} Base de datos</h2>
  <dl class="eg-kv eg-kv--2col">
    <dt>Motor</dt><dd>SQLite</dd>
    <dt>DATABASE_PATH</dt><dd class="mono eg-muted">{h(str(db_path))}</dd>
    <dt>Archivo existe</dt><dd>{pill('active','Si') if db_exists else pill('error','No')}</dd>
    <dt>Tamano</dt><dd class="mono">{h(db_size)}</dd>
    <dt>Ultima modificacion</dt><dd class="mono">{h(db_mtime or '—')}</dd>
    <dt>Total suscriptores</dt><dd class="mono">{h(n_subscribers)}</dd>
    <dt>Total documentos</dt><dd class="mono">{h(n_documents)}</dd>
    <dt>Total alertas</dt><dd class="mono">{h(n_alerts)}</dd>
    <dt>Total envios</dt><dd class="mono">{h(n_deliveries)}</dd>
    <dt>Total jobs</dt><dd class="mono">{h(n_jobs)}</dd>
  </dl>
  {db_warn}
</section>
"""

    # --- WordPress sync ---
    if settings.wordpress_sync_enabled:
        wp_status = pill("active", "Activo")
    else:
        wp_status = pill("paused", "Desactivado")

    wp_token_masked = mask_secret(settings.wordpress_api_token)
    wp_test_btn = (
        '<form method="post" action="/admin/settings/test-wordpress" style="display:inline">'
        f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">'
        f'{icon("wifi", 14)}<span>Probar conexion</span></button></form>'
        if settings.wordpress_sync_enabled
        else ""
    )
    wp_sync_btn = (
        '<form method="post" action="/admin/wordpress/sync" style="display:inline">'
        f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">'
        f'{icon("refresh", 14)}<span>Sincronizar ahora</span></button></form>'
        if settings.wordpress_sync_enabled
        else ""
    )

    section_wp = f"""
<section class="eg-card eg-panel">
  <h2>{icon("wifi", 17)} WordPress Sync</h2>
  <dl class="eg-kv eg-kv--2col">
    <dt>WORDPRESS_SYNC_ENABLED</dt><dd>{wp_status}</dd>
    <dt>WORDPRESS_API_URL</dt><dd class="mono">{h(settings.wordpress_api_url or '—')}</dd>
    <dt>WORDPRESS_API_TOKEN</dt><dd class="mono">{h(wp_token_masked)}</dd>
    <dt>Intervalo sync</dt><dd class="mono">{h(settings.wordpress_sync_interval_minutes)} min</dd>
    <dt>Limite por sync</dt><dd class="mono">{h(settings.wordpress_sync_limit)}</dd>
    <dt>Ultima sincronizacion</dt><dd class="mono">{h(fmt_dt(last_wp_sync_at) if last_wp_sync_at else '—')}</dd>
    <dt>Suscriptores sincronizados</dt><dd class="mono">{h(wp_synced_count)}</dd>
  </dl>
  <div class="eg-actions" style="margin-top:14px">{wp_test_btn}{wp_sync_btn}</div>
</section>
"""

    # --- Conexión IA ---
    ai_provider = settings.ai_provider
    ai_env_enabled = bool(getattr(settings, "ai_enabled", False))
    ai_runtime_raw = (app_cfg.get("ai_runtime_enabled") or "").strip().lower()
    if ai_runtime_raw in {"0", "false", "no", "n", "off"}:
        ai_runtime_enabled = False
        ai_runtime_source = "panel"
    elif ai_runtime_raw in {"1", "true", "yes", "y", "on"}:
        ai_runtime_enabled = True
        ai_runtime_source = "panel"
    else:
        # Sin valor en DB: usar AI_ENABLED del entorno como fallback
        ai_runtime_enabled = ai_env_enabled
        ai_runtime_source = ".env (inicial)"

    with db.connect(settings.database_path) as ai_conn:
        ai_usage = db.get_ai_usage_status(
            ai_conn,
            daily_limit=settings.ai_daily_token_limit,
            monthly_limit=settings.ai_monthly_token_limit,
        )
        recent_ai_logs = db.get_recent_ai_usage(ai_conn, limit=5)
        ai_token_breakdown = db.get_ai_token_breakdown(ai_conn)

    ai_limit_reached = ai_usage.get("daily_exceeded") or ai_usage.get("monthly_exceeded")
    ai_last_usage = ai_usage.get("last_usage") or {}
    ai_last_error = ai_usage.get("last_error") or {}

    if not ai_runtime_enabled:
        ai_estado = pill("paused", "Apagada")
        ai_estado_key = "paused"
    elif ai_provider == "disabled" or not ai_provider:
        ai_estado = pill("paused", "Desactivada")
        ai_estado_key = "paused"
    elif ai_limit_reached:
        ai_estado = pill("error", "Límite alcanzado")
        ai_estado_key = "error"
    elif settings.ai_api_key:
        ai_estado = pill("active", "Activa")
        ai_estado_key = "active"
    else:
        ai_estado = pill("error", "Sin API key")
        ai_estado_key = "error"

    ai_last_test = app_cfg.get("ai_last_test", "")

    has_api_key = bool(settings.ai_api_key)
    _toggle_checked = "checked" if ai_runtime_enabled else ""
    _toggle_disabled = "disabled" if not has_api_key else ""
    _toggle_label = "Activa" if ai_runtime_enabled else "Inactiva"
    _no_key_note = (
        '<span id="ai-toggle-note" style="font-size:12px;color:#9CA3AF;margin-left:8px;">'
        'Sin API key — configura AI_API_KEY para habilitar</span>'
        if not has_api_key else
        '<span id="ai-toggle-note" style="font-size:12px;color:#9CA3AF;margin-left:8px;display:none;"></span>'
    )
    ai_toggle_btn = f"""<style>
.eg-toggle{{display:flex;align-items:center;gap:10px;cursor:pointer;}}
.eg-toggle input{{display:none;}}
.eg-toggle-slider{{width:44px;height:24px;border-radius:12px;background:#8EA1AA;position:relative;transition:background 0.2s;flex-shrink:0;}}
.eg-toggle-slider::after{{content:'';position:absolute;width:18px;height:18px;border-radius:50%;background:#fff;top:3px;left:3px;transition:transform 0.2s;}}
input:checked+.eg-toggle-slider{{background:#29B78D;}}
input:checked+.eg-toggle-slider::after{{transform:translateX(20px);}}
input:disabled+.eg-toggle-slider{{opacity:0.4;cursor:not-allowed;}}
</style>
<div style="display:flex;align-items:center;gap:0;flex-wrap:wrap;">
<label class="eg-toggle" title="Activar/desactivar IA">
  <input type="checkbox" id="ai-toggle" {_toggle_checked} {_toggle_disabled}
    onchange="toggleAI(this.checked)">
  <span class="eg-toggle-slider"></span>
  <span class="eg-toggle-label" id="ai-toggle-label" style="font-size:13px;font-weight:600;color:var(--eg-text);">{_toggle_label}</span>
</label>
{_no_key_note}
</div>
<script>
async function toggleAI(enabled) {{
  const label = document.getElementById('ai-toggle-label');
  const note = document.getElementById('ai-toggle-note');
  const r = await fetch('/admin/settings/ai-toggle', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{enabled}})
  }});
  const data = await r.json();
  if (data.success) {{
    label.textContent = enabled ? 'Activa' : 'Inactiva';
    if (note) note.style.display = 'none';
  }} else {{
    document.getElementById('ai-toggle').checked = !enabled;
    if (note) {{ note.textContent = data.error || 'Error al cambiar estado IA'; note.style.display = ''; }}
    else alert(data.error || 'Error al cambiar estado IA');
  }}
}}
</script>"""

    ai_test_btn = ""
    if ai_runtime_enabled and ai_provider not in ("disabled", "") and settings.ai_api_key and not ai_limit_reached:
        ai_test_btn = f"""
  <form method="post" action="/admin/settings/test-ai" style="display:inline">
    <button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">
      {icon("cpu", 14)}<span>Probar conexion IA</span>
    </button>
  </form>"""
    else:
        reason = "Activa IA y configura AI_PROVIDER + AI_API_KEY para probar."
        if ai_limit_reached:
            reason = "Límite IA alcanzado. No se ejecutará prueba."
        ai_test_btn = (
            f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="button" disabled>'
            f'{icon("cpu", 14)}<span>Probar conexion IA</span></button>'
            f'<p class="eg-muted" style="margin-top:8px;font-size:13px;">{h(reason)}</p>'
        )

    ai_usage_today = f"{ai_usage.get('today_tokens', 0):,}".replace(",", ".")
    ai_usage_month = f"{ai_usage.get('month_tokens', 0):,}".replace(",", ".")
    ai_daily_limit = f"{settings.ai_daily_token_limit:,}".replace(",", ".")
    ai_monthly_limit = f"{settings.ai_monthly_token_limit:,}".replace(",", ".")

    # Costos estimados (Fase 1)
    last_u = ai_usage.get("last_usage") or {}
    cost_last_usd, cost_last_clp = _calc_ai_cost(
        last_u.get("input_tokens") or 0,
        last_u.get("output_tokens") or 0,
        settings,
    )
    cost_today_usd, cost_today_clp = _calc_ai_cost(
        ai_token_breakdown.get("today_input", 0),
        ai_token_breakdown.get("today_output", 0),
        settings,
    )
    cost_month_usd, cost_month_clp = _calc_ai_cost(
        ai_token_breakdown.get("month_input", 0),
        ai_token_breakdown.get("month_output", 0),
        settings,
    )

    def _fmt_cost(usd: float, clp: float) -> str:
        return f"USD {usd:.4f} · CLP {clp:,.0f}".replace(",", ".")

    # Advertencia de consumo (Fase 5)
    _warn_pct = settings.ai_warning_percent
    _daily_pct = ai_usage.get("daily_percent", 0)
    _monthly_pct = ai_usage.get("monthly_percent", 0)
    _ai_warning_banner = ""
    if ai_usage.get("daily_exceeded") or ai_usage.get("monthly_exceeded"):
        _ai_warning_banner = (
            '<div class="eg-devbanner" style="background:var(--eg-danger,#D32F2F);color:#fff;'
            'padding:10px 14px;border-radius:6px;margin-bottom:12px;font-size:13px;font-weight:600;">'
            f'{icon("warning", 14)} Límite de tokens IA alcanzado. No se realizarán llamadas a la API. '
            'Apaga IA o espera que se renueve el límite.'
            '</div>'
        )
    elif _daily_pct >= _warn_pct or _monthly_pct >= _warn_pct:
        _ai_warning_banner = (
            '<div class="eg-devbanner" style="background:var(--eg-warning,#F59E0B);'
            'padding:10px 14px;border-radius:6px;margin-bottom:12px;font-size:13px;">'
            f'{icon("warning", 14)} Consumo IA cercano al límite configurado ({_warn_pct}%). '
            'Para evitar consumo accidental, mantén IA apagada cuando no estés probando.'
            '</div>'
        )

    # Defaults para campos IA editables
    AI_DEFAULTS = {
        "ai_summary_style": "Profesional, claro, orientado a contadores y empresas chilenas.",
        "ai_analysis_focus": (
            "Explicar impactos prácticos en cumplimiento laboral, gestión contable, "
            "auditoría, remuneraciones y obligaciones documentales."
        ),
        "ai_review_required": "true",
        "ai_attachments_enabled": "true",
        "ai_email_intro_template": (
            "Hemos detectado una nueva publicación normativa relevante. "
            "A continuación encontrarás un resumen breve y los impactos prácticos "
            "para la gestión contable."
        ),
        "ai_footer_disclaimer": (
            "Este resumen es informativo y no reemplaza la lectura del documento oficial "
            "ni asesoría profesional."
        ),
    }

    def _ai_field(
        key: str,
        label: str,
        multiline: bool = False,
        rows: int = 2,
        help_text: str = "",
    ) -> str:
        stored = app_cfg.get(key, "")
        val = stored if stored else AI_DEFAULTS.get(key, "")
        hint = "guardado" if stored else "por defecto"
        help_el = f'<p class="eg-muted" style="font-size:12px;margin:2px 0 6px;">{h(help_text)}</p>' if help_text else ""
        if multiline:
            return (
                f'<div class="eg-field">'
                f'<label class="eg-label" for="cfg-{h(key)}">{h(label)}'
                f'<span class="eg-label-hint">{hint}</span></label>'
                f'{help_el}'
                f'<textarea class="eg-input eg-input--mono" id="cfg-{h(key)}" name="{h(key)}" '
                f'rows="{rows}">{h(val)}</textarea>'
                f'</div>'
            )
        return (
            f'<div class="eg-field">'
            f'<label class="eg-label" for="cfg-{h(key)}">{h(label)}'
            f'<span class="eg-label-hint">{hint}</span></label>'
            f'{help_el}'
            f'<input class="eg-input eg-input--mono" id="cfg-{h(key)}" name="{h(key)}" '
            f'type="text" value="{h(val)}">'
            f'</div>'
        )

    ai_editable_fields = (
        _ai_field(
            "ai_summary_style", "Estilo editorial", multiline=True, rows=2,
            help_text="Tono y voz del resumen. Ej: 'Profesional, claro, orientado a contadores chilenos.'",
        )
        + _ai_field(
            "ai_analysis_focus", "Enfoque del análisis", multiline=True, rows=2,
            help_text="Áreas que la IA debe priorizar. Ej: cumplimiento laboral, remuneraciones, auditoría.",
        )
        + _ai_field(
            "ai_system_prompt", "Instrucciones adicionales al sistema", multiline=True, rows=3,
            help_text="Reglas internas adicionales para la IA. No incluir API keys ni datos sensibles.",
        )
        + _ai_field(
            "ai_email_intro_template", "Intro del correo", multiline=True, rows=2,
            help_text="Texto que aparece al inicio del correo, antes del resumen generado.",
        )
        + _ai_field(
            "ai_footer_disclaimer", "Aviso legal / footer", multiline=True, rows=2,
            help_text="Disclaimer que aparece al final de los adjuntos y correos.",
        )
        + _ai_field(
            "ai_review_required", "Revisión requerida antes de envío",
            help_text="true = la alerta queda en revisión manual antes de enviarse. Recomendado: true.",
        )
        + _ai_field(
            "ai_attachments_enabled", "Adjuntos habilitados",
            help_text="true = se adjuntan resumen ejecutivo y detallado al correo cuando IA genera correctamente.",
        )
    )

    # IA core (conexión + uso) — ancho completo
    section_ai_core = f"""
{_ai_warning_banner}
<section class="eg-card eg-panel">
  <h2>{icon("cpu", 17)} Conexion IA</h2>
  <div style="margin-bottom:16px;">{ai_toggle_btn}</div>
  <dl class="eg-kv eg-kv--2col">
    <dt>Estado</dt><dd>{ai_estado}</dd>
    <dt>Fuente interruptor</dt><dd class="mono">{h(ai_runtime_source)}</dd>
    <dt>AI_ENABLED (.env, inicial)</dt><dd class="mono">{h(str(settings.ai_enabled).lower())}</dd>
    <dt>AI_PROVIDER</dt><dd class="mono">{h(ai_provider or 'disabled')}</dd>
    <dt>AI_MODEL</dt><dd class="mono">{h(settings.ai_model or '—')}</dd>
    <dt>AI_BASE_URL</dt><dd class="mono">{h(settings.ai_base_url or '—')}</dd>
    <dt>AI_API_KEY</dt><dd class="mono">{h(mask_secret(settings.ai_api_key))}</dd>
    <dt>AI_TIMEOUT_SECONDS</dt><dd class="mono">{h(settings.ai_timeout_seconds)}</dd>
    <dt>AI_MAX_INPUT_CHARS</dt><dd class="mono">{h(settings.ai_max_input_chars)}</dd>
    <dt>AI_ATTACHMENTS_ENABLED</dt><dd class="mono">{h(str(settings.ai_attachments_enabled).lower())}</dd>
    <dt>Uso hoy</dt><dd class="mono">{h(ai_usage_today)} / {h(ai_daily_limit)} tokens · {h(ai_usage.get('daily_percent', 0))}%</dd>
    <dt>Uso mes</dt><dd class="mono">{h(ai_usage_month)} / {h(ai_monthly_limit)} tokens · {h(ai_usage.get('monthly_percent', 0))}%</dd>
    <dt>Advertencia límite</dt><dd class="mono">{h(settings.ai_warning_percent)}%</dd>
    <dt>Última llamada IA</dt><dd class="mono">{h((ai_last_usage or {}).get('created_at') or '—')} · {h((ai_last_usage or {}).get('status') or '—')} · {h((ai_last_usage or {}).get('total_tokens') or 0)} tokens</dd>
    <dt>Último error IA</dt><dd class="mono">{h((ai_last_error or {}).get('error') or '—')}</dd>
    <dt>Ultima prueba IA</dt><dd class="mono">{h(ai_last_test or '—')}</dd>
  </dl>
  <h3 style="margin:16px 0 8px;font-size:13px;color:var(--eg-subtle);">Costo estimado referencial</h3>
  <dl class="eg-kv eg-kv--2col">
    <dt>Precio input / 1M tokens</dt><dd class="mono">USD {h(f"{settings.ai_input_price_per_1m_usd:.2f}")}</dd>
    <dt>Precio output / 1M tokens</dt><dd class="mono">USD {h(f"{settings.ai_output_price_per_1m_usd:.2f}")}</dd>
    <dt>Tipo de cambio USD/CLP</dt><dd class="mono">{h(settings.ai_usd_clp_rate)}</dd>
    <dt>Costo última llamada</dt><dd class="mono">{h(_fmt_cost(cost_last_usd, cost_last_clp))}</dd>
    <dt>Costo estimado hoy</dt><dd class="mono">{h(_fmt_cost(cost_today_usd, cost_today_clp))}</dd>
    <dt>Costo estimado mes</dt><dd class="mono">{h(_fmt_cost(cost_month_usd, cost_month_clp))}</dd>
  </dl>
  <p class="eg-muted" style="font-size:12px;margin-top:6px;">
    Costo estimado referencial. Puede variar según contrato Azure, modelo y tipo de cambio.
    Configurable con AI_INPUT_PRICE_PER_1M_USD, AI_OUTPUT_PRICE_PER_1M_USD, AI_USD_CLP_RATE.
  </p>
  <div class="eg-actions" style="margin-top:14px">{ai_test_btn}</div>
</section>

{_render_ai_usage_table(recent_ai_logs, settings)}
"""

    # IA editorial — colapsable
    section_ai_editorial = f"""
<details class="eg-card eg-panel" style="padding:0;">
  <summary style="padding:20px 24px;font-size:14px;font-weight:700;color:var(--eg-text);
    cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;border-radius:10px;">
    {icon("cpu", 16)} ✏️ Configuracion editorial IA
    <span style="margin-left:auto;font-size:12px;color:var(--eg-muted);font-weight:400;">▼ Expandir</span>
  </summary>
  <div style="padding:0 24px 20px;">
    <p class="eg-muted" style="margin-bottom:14px;">
      Controlan el comportamiento editorial de la IA. No incluyas API keys aquí.
      Los cambios se guardan en la base de datos y anulan los valores por defecto.
    </p>
    <form method="post" action="/admin/settings">
      <input type="hidden" name="_section" value="ai">
      {ai_editable_fields}
      <button class="eg-btn eg-btn--primary" type="submit">
        {icon("check", 15)}<span>Guardar configuracion IA</span>
      </button>
    </form>
  </div>
</details>
"""

    section_ai = section_ai_core

    # --- Email y plantillas (editable) ---
    def _field(key: str, label: str, placeholder: str, env_val: str, multiline: bool = False) -> str:
        stored = app_cfg.get(key, "")
        val = stored if stored else env_val
        if multiline:
            return (
                f'<div class="eg-field">'
                f'<label class="eg-label" for="cfg-{h(key)}">{h(label)}'
                f'<span class="eg-label-hint">{h("desde .env" if not stored else "guardado")}</span>'
                f'</label>'
                f'<textarea class="eg-input eg-input--mono" id="cfg-{h(key)}" name="{h(key)}" rows="2"'
                f' placeholder="{h(placeholder)}">{h(val)}</textarea>'
                f'</div>'
            )
        return (
            f'<div class="eg-field">'
            f'<label class="eg-label" for="cfg-{h(key)}">{h(label)}'
            f'<span class="eg-label-hint">{h("desde .env" if not stored else "guardado")}</span>'
            f'</label>'
            f'<input class="eg-input eg-input--mono" id="cfg-{h(key)}" name="{h(key)}" type="text"'
            f' value="{h(val)}" placeholder="{h(placeholder)}">'
            f'</div>'
        )

    tmpl_default = EMAIL_SETTINGS_DEFAULTS["email_subject_template"]
    test_tmpl_default = EMAIL_SETTINGS_DEFAULTS["email_test_subject_template"]
    footer_default = EMAIL_SETTINGS_DEFAULTS["email_footer_legal"]

    fields_html = (
        _field("email_from_name", "Nombre del remitente", "Alertas DT", settings.email_from_name)
        + _field("email_from", "Email remitente", "alertas@example.com", settings.email_from)
        + _field("email_reply_to", "Email responder a", "soporte@example.com", settings.email_reply_to)
        + _field("email_subject_template", "Asunto (correo real)", tmpl_default, tmpl_default)
        + _field("email_test_subject_template", "Asunto (correo de prueba)", test_tmpl_default, test_tmpl_default)
        + _field("email_footer_legal", "Texto legal del footer", footer_default, footer_default, multiline=True)
    )

    section_email = f"""
<details class="eg-card eg-panel" style="padding:0;">
  <summary style="padding:20px 24px;font-size:14px;font-weight:700;color:var(--eg-text);
    cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;border-radius:10px;">
    {icon("mail", 16)} 📨 Email y plantillas
    <span style="margin-left:auto;font-size:12px;color:var(--eg-muted);font-weight:400;">▼ Expandir</span>
  </summary>
  <div style="padding:0 24px 20px;">
    <p class="eg-muted" style="margin-bottom:14px;">
      Anulan el <code>.env</code> para el envio de emails. Si se dejan en blanco, se usan las variables de entorno.
      No guardes API keys en este formulario.
    </p>
    <form method="post" action="/admin/settings">
      {fields_html}
      <button class="eg-btn eg-btn--primary" type="submit">
        {icon("check", 15)}<span>Guardar configuracion</span>
      </button>
    </form>
  </div>
</details>
"""

    _grid2 = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:0;">'
    _grid2_end = '</div>'

    return (
        _grid2 + section_general + section_sendgrid + _grid2_end
        + _grid2 + section_db + section_wp + _grid2_end
        + section_ai
        + section_ai_editorial
        + section_email
    )


_SUBSCRIBERS_JS = """
<script>
function _planLabel(plan) {
  const labels = {sin_suscripcion:'Sin suscripción',prueba:'Prueba gratuita',basico:'Básico',empresarial:'Empresarial'};
  return labels[plan] || plan;
}
function _planColor(plan) {
  const colors = {sin_suscripcion:'#8EA1AA',prueba:'#2563EB',basico:'#29B78D',empresarial:'#0A2231'};
  return colors[plan] || '#8EA1AA';
}
function updateSubscriberRow(id, status) {
  const row = document.getElementById('sub-row-' + id);
  if (!row) return;
  const statusCell = row.querySelector('[data-status]');
  const actionsCell = row.querySelector('[data-actions]');
  const email = row.dataset.email || '';
  if (statusCell) {
    const isActive = status === 'active';
    statusCell.innerHTML = isActive
      ? '<span style="color:#29B78D;font-weight:600;">&#9679; Activo</span>'
      : '<span style="color:#F59E0B;font-weight:600;">&#9646; Pausado</span>';
    statusCell.dataset.status = status;
  }
  if (actionsCell) {
    actionsCell.innerHTML = _buildActions(id, email, status);
  }
}
function _buildActions(id, email, status) {
  const del = '<button onclick="deleteSubscriber(' + id + ',\\'' + email.replace(/'/g,"\\'") + '\\')" style="padding:3px 8px;border-radius:4px;font-size:12px;font-weight:600;background:#FEE2E2;color:#991B1B;border:1px solid #FECACA;cursor:pointer;">&#x1F5D1; Eliminar</button>';
  if (status === 'active') {
    return '<div style="display:flex;gap:6px;flex-wrap:wrap;">'
      + '<button onclick="pauseSubscriber(' + id + ',\\'' + email.replace(/'/g,"\\'") + '\\')" style="padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600;background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;cursor:pointer;">&#9646; Pausar</button>'
      + del + '</div>';
  }
  return '<div style="display:flex;gap:6px;flex-wrap:wrap;">'
    + '<button onclick="activateSubscriber(' + id + ')" style="padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600;background:#D1FAE5;color:#065F46;border:1px solid #6EE7B7;cursor:pointer;">&#9654; Reactivar</button>'
    + del + '</div>';
}
function updatePlanBadge(id, plan) {
  const badge = document.getElementById('plan-badge-' + id);
  const sel = document.getElementById('plan-select-' + id);
  if (badge) { badge.textContent = _planLabel(plan); badge.style.background = _planColor(plan); }
  if (sel) sel.value = plan;
}
async function pauseSubscriber(id, email) {
  const r = await fetch('/admin/subscribers/' + id + '/pause', {method:'POST'});
  if (r.ok) updateSubscriberRow(id, 'paused');
  else alert('Error al pausar');
}
async function activateSubscriber(id) {
  const r = await fetch('/admin/subscribers/' + id + '/activate', {method:'POST'});
  if (r.ok) updateSubscriberRow(id, 'active');
  else alert('Error al reactivar');
}
async function deleteSubscriber(id, email) {
  if (!confirm('\\u00BFEliminar a ' + email + '? No se puede deshacer.')) return;
  const r = await fetch('/admin/subscribers/' + id + '/delete', {method:'POST'});
  if (r.ok) {
    const row = document.getElementById('sub-row-' + id);
    if (row) {
      row.remove();
      const tbody = document.querySelector('#subscribers-table tbody');
      if (tbody && tbody.querySelectorAll('tr').length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:32px;color:#6B7280;">No hay suscriptores registrados a\\u00FAn.</td></tr>';
      }
    }
  } else alert('Error al eliminar');
}
async function changePlan(id, plan) {
  const r = await fetch('/admin/subscribers/' + id + '/plan', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({plan})
  });
  if (r.ok) updatePlanBadge(id, plan);
  else alert('Error al cambiar plan');
}
</script>
"""


def _subscriber_plan_badge(item: dict[str, Any]) -> str:
    sub_id = item["id"]
    plan = item.get("plan") or "prueba"
    label = SUBSCRIBER_PLANS.get(plan, plan)
    color = _PLAN_COLORS.get(plan, "#8EA1AA")
    options = "".join(
        f'<option value="{k}"{" selected" if k == plan else ""}>{v}</option>'
        for k, v in SUBSCRIBER_PLANS.items()
    )
    return (
        f'<span id="plan-badge-{sub_id}" '
        f'style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;'
        f'font-weight:600;background:{color};color:#fff;cursor:pointer;" '
        f'onclick="this.style.display=\'none\';document.getElementById(\'plan-select-{sub_id}\').style.display=\'inline\';">'
        f'{h(label)}</span>'
        f'<select id="plan-select-{sub_id}" style="display:none;font-size:11px;padding:2px 4px;border-radius:4px;border:1px solid #D1D5DB;" '
        f'onchange="changePlan({sub_id},this.value);this.style.display=\'none\';document.getElementById(\'plan-badge-{sub_id}\').style.display=\'inline-block\';">'
        f'{options}'
        f'</select>'
    )


def _subscriber_actions(item: dict[str, Any]) -> str:
    sub_id = item["id"]
    email_js = h(item["email"]).replace("'", "\\'")
    status = item["status"]
    del_btn = (
        f'<button onclick="deleteSubscriber({sub_id},\'{email_js}\')" '
        f'style="padding:3px 8px;border-radius:4px;font-size:12px;font-weight:600;'
        f'background:#FEE2E2;color:#991B1B;border:1px solid #FECACA;cursor:pointer;">🗑 Eliminar</button>'
    )
    if status == "active":
        toggle_btn = (
            f'<button onclick="pauseSubscriber({sub_id},\'{email_js}\')" '
            f'style="padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600;'
            f'background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;cursor:pointer;">⏸ Pausar</button>'
        )
    else:
        toggle_btn = (
            f'<button onclick="activateSubscriber({sub_id})" '
            f'style="padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600;'
            f'background:#D1FAE5;color:#065F46;border:1px solid #6EE7B7;cursor:pointer;">▶ Reactivar</button>'
        )
    return f'<div style="display:flex;gap:6px;flex-wrap:wrap;">{toggle_btn}{del_btn}</div>'


def render_subscribers(subscribers: list[dict[str, Any]]) -> str:
    def _row(item: dict[str, Any]) -> str:
        status = item["status"]
        wa_ok = bool(item.get("whatsapp_consent"))
        if status == "active":
            status_html = '<span style="color:#29B78D;font-weight:600;">&#9679; Activo</span>'
        else:
            status_html = '<span style="color:#F59E0B;font-weight:600;">&#9646; Pausado</span>'
        wa_html = (
            '<span style="color:#29B78D;font-weight:600;">✓</span>'
            if wa_ok else
            '<span style="color:#9CA3AF;">—</span>'
        )
        return (
            f'<tr id="sub-row-{item["id"]}" data-email="{h(item["email"])}">'
            f'<td style="padding:10px 8px;font-size:13px;"><strong>{h(item["email"])}</strong></td>'
            f'<td style="padding:10px 8px;font-size:13px;">{h(item.get("subscriber_name") or "—")}</td>'
            f'<td style="padding:10px 8px;font-size:12px;font-family:monospace;">{h(item.get("phone") or "—")}</td>'
            f'<td style="padding:10px 8px;">{_subscriber_plan_badge(item)}</td>'
            f'<td style="padding:10px 8px;text-align:center;">{wa_html}</td>'
            f'<td style="padding:10px 8px;" data-status="{h(status)}">{status_html}</td>'
            f'<td style="padding:10px 8px;font-size:12px;font-family:monospace;">{_fmt_short_date(item.get("created_at"))}</td>'
            f'<td style="padding:10px 8px;" data-actions="1">{_subscriber_actions(item)}</td>'
            f'</tr>'
        )

    rows = "".join(_row(item) for item in subscribers)
    empty = (
        '<tr><td colspan="8" style="text-align:center;padding:32px;color:#6B7280;">'
        'No hay suscriptores registrados aún.</td></tr>'
    )
    return f"""
<section class="eg-card eg-panel">
  <div class="eg-card-head"><h2>Suscriptores</h2></div>
  <div class="eg-table-wrap">
    <table class="eg-table" id="subscribers-table" style="table-layout:fixed;width:100%;">
      <thead><tr>
        <th>Email</th><th>Nombre</th><th style="width:110px;">Teléfono</th>
        <th style="width:120px;">Plan</th><th style="width:60px;">WhatsApp</th>
        <th style="width:90px;">Estado</th><th style="width:70px;">Registro</th>
        <th style="width:200px;">Acciones</th>
      </tr></thead>
      <tbody>{rows or empty}</tbody>
    </table>
  </div>
  <details style="margin-top:12px;">
    <summary style="font-size:12px;color:var(--eg-muted);cursor:pointer;list-style:none;">
      ℹ️ Sobre notificaciones WhatsApp
    </summary>
    <p class="eg-note" style="margin-top:6px;">WhatsApp reservado para fase futura: el MVP notifica solo por email.</p>
  </details>
</section>
{_SUBSCRIBERS_JS}
"""


SUBSCRIBER_PLANS: dict[str, str] = {
    "sin_suscripcion": "Sin suscripción",
    "prueba":          "Prueba gratuita",
    "basico":          "Básico",
    "empresarial":     "Empresarial",
}

_PLAN_COLORS: dict[str, str] = {
    "sin_suscripcion": "#8EA1AA",
    "prueba":          "#2563EB",
    "basico":          "#29B78D",
    "empresarial":     "#0A2231",
}

AI_STATUS_LABELS = {
    "success": ("IA generada", "eg-badge--active"),
    "fallback": ("Fallback local", "eg-badge--paused"),
    "error": ("Fallback local", "eg-badge--paused"),   # old data compat — error uses fallback, no red badge
    "disabled": ("IA desactivada", "eg-badge--paused"),
    "pending": ("IA pendiente", "eg-badge--pending"),
}


def ai_status_badge(item: dict[str, Any]) -> str:
    ai_st = item.get("ai_status") or ""
    if not ai_st:
        return ""
    label, cls = AI_STATUS_LABELS.get(ai_st, (ai_st, "eg-badge--baseline"))
    return f'<span class="eg-badge {cls} eg-badge--no-dot">{h(label)}</span>'


_ALERT_STATUS_LABELS: dict[str, tuple[str, str]] = {
    "pending_review": ("Pendiente revisión", "#FEF3C7;color:#92400E"),
    "ready_to_send":  ("Lista p/enviar",     "#D1FAE5;color:#065F46"),
    "ready":          ("Lista p/enviar",     "#D1FAE5;color:#065F46"),
    "sent":           ("Enviada",            "#29B78D;color:#fff"),
    "fallback":       ("Fallback",           "#F3F4F6;color:#6B7280"),
    "error":          ("Error",              "#FEE2E2;color:#991B1B"),
}

_ALERT_FILTER_TABS: list[tuple[str, str]] = [
    ("", "Todas"),
    ("pending_review", "Pendiente"),
    ("ready_to_send", "Lista p/enviar"),
    ("sent", "Enviadas"),
    ("fallback", "Fallback"),
    ("error", "Error"),
]

_PAGE_SIZE = 20


def _alert_status_badge(status: str) -> str:
    label, style = _ALERT_STATUS_LABELS.get(status, (h(status), "#F3F4F6;color:#6B7280"))
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'font-size:11px;font-weight:600;background:{style};">{label}</span>'
    )


def _alert_ia_badge(item: dict[str, Any]) -> str:
    if (item.get("ai_status") or "") == "success":
        return (
            '<span style="display:inline-block;padding:2px 7px;border-radius:10px;'
            'font-size:11px;font-weight:600;background:#D1FAE5;color:#065F46;" '
            'title="Resumen IA generado — clic para ver preview">✓ IA</span>'
        )
    return (
        '<span style="display:inline-block;padding:2px 7px;border-radius:10px;'
        'font-size:11px;font-weight:600;background:#F3F4F6;color:#6B7280;" '
        'title="Sin resumen IA — usar Generar con IA en el preview">− Sin IA</span>'
    )


def _fmt_short_date(val: str | None) -> str:
    if not val:
        return "—"
    try:
        from datetime import datetime
        _MONTHS = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"]
        dt = datetime.fromisoformat(val[:19])
        return f"{dt.day} {_MONTHS[dt.month - 1]}"
    except Exception:
        return val[:10] if val else "—"


def _alert_table_row(item: dict[str, Any]) -> str:
    alert_id = item["id"]
    status = item["status"]
    title_raw = item.get("title") or ""
    title_trunc = (title_raw[:45] + "…") if len(title_raw) > 45 else title_raw
    cat_raw = item.get("category") or ""
    cat_trunc = (cat_raw[:20] + "…") if len(cat_raw) > 20 else cat_raw

    title_js = h(title_raw).replace("'", "\\'")

    # Botón Ver
    ver_btn = (
        f'<a href="/admin/alerts/{alert_id}/preview-email" '
        f'style="display:inline-block;padding:3px 10px;border-radius:4px;font-size:12px;'
        f'font-weight:600;background:#29B78D;color:#fff;text-decoration:none;">Ver</a>'
    )

    # Botón Prueba ▾ (inline form colapsable)
    prueba_btn = (
        f'<span style="display:inline-block;">'
        f'<button onclick="this.parentNode.querySelector(\'form\').style.display=\'block\';this.style.display=\'none\';" '
        f'style="padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600;'
        f'background:#F3F4F6;color:#374151;border:1px solid #D1D5DB;cursor:pointer;">Prueba ▾</button>'
        f'<form method="post" action="/admin/alerts/{alert_id}/test" '
        f'style="display:none;margin-top:4px;">'
        f'<input type="email" name="to" placeholder="correo@ejemplo.com" '
        f'style="font-size:11px;padding:2px 6px;border:1px solid #D1D5DB;border-radius:3px;width:130px;">'
        f'<button type="submit" '
        f'style="margin-left:4px;padding:2px 8px;border-radius:3px;font-size:11px;'
        f'background:#29B78D;color:#fff;border:none;cursor:pointer;">OK</button>'
        f'</form></span>'
    )

    # Botón Enviar a todos / badge Enviada
    if status == "sent":
        send_all_btn = (
            f'<span id="send-status-{alert_id}" '
            f'style="font-size:12px;color:#29B78D;font-weight:600;">✓ Enviada</span>'
        )
    else:
        send_all_btn = (
            f'<span id="send-status-{alert_id}">'
            f'<button onclick="confirmSend({alert_id},\'{title_js}\')" '
            f'style="padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600;'
            f'background:#EFF6FF;color:#1D4ED8;border:1px solid #BFDBFE;cursor:pointer;">'
            f'✉ Enviar a todos</button>'
            f'</span>'
        )

    # Botón Eliminar
    delete_btn = (
        f'<button onclick="deleteAlert({alert_id})" '
        f'style="padding:3px 8px;border-radius:4px;font-size:12px;font-weight:600;'
        f'background:#FEE2E2;color:#991B1B;border:1px solid #FECACA;cursor:pointer;" '
        f'title="Eliminar alerta">🗑</button>'
    )

    return (
        f'<tr id="alert-row-{alert_id}">'
        f'<td style="font-family:monospace;font-size:12px;color:#6B7280;width:50px;padding:10px 8px;">{alert_id}</td>'
        f'<td style="padding:10px 8px;font-size:13px;" title="{h(title_raw)}">{h(title_trunc)}</td>'
        f'<td style="padding:10px 8px;font-size:12px;color:#6B7280;" title="{h(cat_raw)}">{h(cat_trunc)}</td>'
        f'<td style="padding:10px 8px;" id="status-cell-{alert_id}">{_alert_status_badge(status)}</td>'
        f'<td style="padding:10px 8px;">{_alert_ia_badge(item)}</td>'
        f'<td style="padding:10px 8px;font-size:12px;color:#6B7280;white-space:nowrap;">{_fmt_short_date(item.get("created_at"))}</td>'
        f'<td style="padding:10px 8px;">'
        f'<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">'
        f'{ver_btn}{prueba_btn}{send_all_btn}{delete_btn}'
        f'</div></td>'
        f'</tr>'
    )


_ALERTS_TABLE_JS = """
<div id="send-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:12px;padding:28px 32px;max-width:420px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.18);">
    <h3 id="modal-alert-title" style="font-size:16px;font-weight:700;color:#0A2231;margin-bottom:8px;"></h3>
    <p style="font-size:14px;color:#374151;margin-bottom:6px;">
      Se enviará a <strong id="modal-count">…</strong> suscriptores activos.
    </p>
    <p style="font-size:13px;color:#9CA3AF;margin-bottom:24px;">Esta acción no se puede deshacer.</p>
    <div style="display:flex;gap:12px;justify-content:flex-end;">
      <button onclick="closeModal()" style="padding:8px 18px;border-radius:6px;border:1px solid #D1D5DB;background:#fff;font-size:14px;cursor:pointer;">Cancelar</button>
      <button id="modal-confirm" style="padding:8px 18px;border-radius:6px;border:none;background:#1D4ED8;color:#fff;font-size:14px;font-weight:600;cursor:pointer;">✉ Enviar ahora</button>
    </div>
  </div>
</div>
<script>
var _activeCount = null;
async function _loadCount() {
  if (_activeCount !== null) return _activeCount;
  try {
    const r = await fetch('/admin/api/subscribers/count');
    const d = await r.json();
    _activeCount = d.active || 0;
  } catch(e) { _activeCount = 0; }
  return _activeCount;
}
function closeModal() {
  document.getElementById('send-modal').style.display = 'none';
}
async function confirmSend(id, title) {
  const count = await _loadCount();
  document.getElementById('modal-alert-title').textContent = title;
  document.getElementById('modal-count').textContent = count;
  document.getElementById('modal-confirm').onclick = function() { sendToSubscribers(id, count); };
  document.getElementById('send-modal').style.display = 'flex';
}
async function sendToSubscribers(id, count) {
  closeModal();
  const r = await fetch('/admin/alerts/' + id + '/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'}
  });
  const data = await r.json();
  if (data.success) {
    const sc = document.getElementById('status-cell-' + id);
    if (sc) sc.innerHTML = '<span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;background:#29B78D;color:#fff;">Enviada</span>';
    const ss = document.getElementById('send-status-' + id);
    if (ss) ss.innerHTML = '<span style="font-size:12px;color:#29B78D;font-weight:600;">✓ Enviada a ' + data.sent_count + ' suscriptores</span>';
  } else {
    alert('Error: ' + (data.error || 'Error desconocido'));
  }
}
async function deleteAlert(id) {
  if (!confirm('\\u00BFEliminar alerta #' + id + '? No se puede deshacer.')) return;
  const r = await fetch('/admin/alerts/' + id + '/delete', {method:'POST'});
  if (r.ok) { const row = document.getElementById('alert-row-' + id); if (row) row.remove(); }
  else alert('Error al eliminar');
}
document.getElementById('send-modal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});
</script>
"""


def render_alerts(
    alerts: list[dict[str, Any]],
    *,
    title: str = "Alertas pendientes de acción",
    empty_msg: str = "✓ No hay alertas pendientes de revisión.",
    empty_hint: str = "Todas las alertas han sido enviadas o no hay nuevas.",
    history_link: str = "/admin/alerts",
) -> str:
    """Vista compacta para el dashboard (cards pendientes). Mantiene compatibilidad."""
    if not alerts:
        history_html = (
            f'<a href="{history_link}" style="font-size:13px;color:var(--eg-accent);margin-top:8px;display:inline-block;">'
            f'Ver historial de alertas →</a>'
            if history_link else ""
        )
        return (
            f'<div class="eg-section-head"><span class="ghost">01</span><h2>{h(title)}</h2></div>'
            '<div class="eg-card eg-panel"><div class="eg-empty">'
            f"<strong>{h(empty_msg)}</strong>"
            f"<span>{h(empty_hint)}</span>"
            f"{history_html}"
            "</div></div>"
        )
    rows = "".join(_alert_table_row(item) for item in alerts)
    table = (
        '<div class="eg-table-wrap">'
        '<table class="eg-table" style="table-layout:fixed;width:100%;">'
        '<thead><tr>'
        '<th style="width:50px;">ID</th>'
        '<th>Documento</th>'
        '<th style="width:120px;">Categoría</th>'
        '<th style="width:130px;">Estado</th>'
        '<th style="width:80px;">IA</th>'
        '<th style="width:70px;">Fecha</th>'
        '<th style="width:200px;">Acciones</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody>'
        '</table></div>'
    )
    return (
        f'<div class="eg-section-head">'
        f'<span class="ghost">01</span><h2>{h(title)}</h2>'
        f'<p>{len(alerts)} alerta{"s" if len(alerts) != 1 else ""}</p>'
        f'</div>'
        f'<section class="eg-card eg-panel">{table}</section>'
        f'{_ALERTS_TABLE_JS}'
    )


def render_alerts_table(
    alerts: list[dict[str, Any]],
    *,
    status_filter: str = "",
    page: int = 1,
) -> str:
    """Vista completa /admin/alerts con filtros, tabla y paginación."""
    # Filtrar
    valid_statuses = {"pending_review", "ready_to_send", "ready", "sent", "fallback", "error"}
    if status_filter and status_filter in valid_statuses:
        filtered = [a for a in alerts if a.get("status") == status_filter]
    else:
        status_filter = ""
        filtered = list(alerts)

    total = len(filtered)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    page_slice = filtered[(page - 1) * _PAGE_SIZE : page * _PAGE_SIZE]

    # Barra de filtros
    filter_tabs = ""
    for tab_val, tab_label in _ALERT_FILTER_TABS:
        is_active = (tab_val == status_filter)
        href = f"/admin/alerts{'?status=' + tab_val if tab_val else ''}"
        active_style = "background:#29B78D;color:#fff;border-color:#29B78D;" if is_active else "background:#fff;color:#374151;border-color:#D1D5DB;"
        filter_tabs += (
            f'<a href="{href}" style="display:inline-block;padding:5px 14px;border-radius:20px;'
            f'font-size:12px;font-weight:600;text-decoration:none;border:1px solid;margin-right:6px;margin-bottom:6px;'
            f'{active_style}">{tab_label}</a>'
        )

    # Tabla
    if page_slice:
        rows = "".join(_alert_table_row(item) for item in page_slice)
        table_body = rows
    else:
        status_label_str = dict(_ALERT_FILTER_TABS).get(status_filter, status_filter)
        empty_text = f"No hay alertas con estado {h(status_label_str)}" if status_filter else "No hay alertas generadas."
        table_body = (
            f'<tr><td colspan="7" style="text-align:center;padding:32px;color:#6B7280;">'
            f'{empty_text}'
            f'{"<br><a href=/admin/alerts style=color:#29B78D;font-size:13px;>Ver todas las alertas →</a>" if status_filter else ""}'
            f'</td></tr>'
        )

    table_html = (
        '<div class="eg-table-wrap">'
        '<table class="eg-table" style="table-layout:fixed;width:100%;">'
        '<thead><tr>'
        '<th style="width:50px;">ID</th>'
        '<th>Documento</th>'
        '<th style="width:120px;">Categoría</th>'
        '<th style="width:130px;">Estado</th>'
        '<th style="width:80px;">IA</th>'
        '<th style="width:70px;">Fecha</th>'
        '<th style="width:220px;">Acciones</th>'
        '</tr></thead>'
        f'<tbody>{table_body}</tbody>'
        '</table></div>'
    )

    # Paginación
    pagination = ""
    if total_pages > 1:
        base = f"/admin/alerts{'?status=' + status_filter if status_filter else '?'}"
        sep = "&" if status_filter else ""
        prev_link = (
            f'<a href="{base}{sep}page={page - 1}" style="padding:5px 14px;border-radius:4px;'
            f'border:1px solid #D1D5DB;text-decoration:none;font-size:13px;color:#374151;">← Anterior</a>'
            if page > 1 else
            '<span style="padding:5px 14px;border-radius:4px;border:1px solid #E5E7EB;'
            'font-size:13px;color:#D1D5DB;">← Anterior</span>'
        )
        next_link = (
            f'<a href="{base}{sep}page={page + 1}" style="padding:5px 14px;border-radius:4px;'
            f'border:1px solid #D1D5DB;text-decoration:none;font-size:13px;color:#374151;">Siguiente →</a>'
            if page < total_pages else
            '<span style="padding:5px 14px;border-radius:4px;border:1px solid #E5E7EB;'
            'font-size:13px;color:#D1D5DB;">Siguiente →</span>'
        )
        pagination = (
            f'<div style="display:flex;align-items:center;gap:16px;justify-content:center;padding:16px 0;">'
            f'{prev_link}'
            f'<span style="font-size:13px;color:#6B7280;">Página {page} de {total_pages}</span>'
            f'{next_link}'
            f'</div>'
        )

    return (
        f'<div class="eg-section-head">'
        f'<span class="ghost">01</span><h2>Alertas generadas</h2>'
        f'<p>{total} alerta{"s" if total != 1 else ""}</p>'
        f'</div>'
        f'<section class="eg-card eg-panel">'
        f'<div style="margin-bottom:16px;">{filter_tabs}</div>'
        f'{table_html}'
        f'{pagination}'
        f'</section>'
        f'{_ALERTS_TABLE_JS}'
    )


def render_documents(documents: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
<tr>
  <td>
    <div class="eg-doc-title">{h(item['title'])}</div>
    <div class="eg-doc-desc eg-muted">{h(item.get('abstract') or '')}</div>
  </td>
  <td class="eg-cell-cat">{h(item['category'])}</td>
  <td class="mono">{h(item.get('publication_date') or '—')}</td>
  <td class="mono">{h(item.get('dt_article_id'))}</td>
  <td>{badge(item['status'])}</td>
  <td>
    <a class="eg-btn eg-btn--primary eg-btn--sm" href="{h(item['canonical_url'])}" target="_blank" rel="noopener noreferrer">
      {icon('external', 14)}<span>Ver en DT</span>
    </a>
  </td>
  <td class="eg-cell-actions">
    <form method="post" action="/admin/documents/{item['id']}/regenerate">
      <button class="eg-btn eg-ghost eg-btn--sm" type="submit">
        {icon('refresh', 14)}<span>Regenerar</span>
      </button>
    </form>
    <form method="post" action="/admin/documents/{item['id']}/ignore">
      <button class="eg-btn eg-ghost eg-btn--sm" type="submit">
        {icon('x-circle', 14)}<span>Ignorar</span>
      </button>
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
      <thead><tr><th>Documento</th><th>Categoría</th><th>Fecha</th><th>ID DT</th><th>Estado</th><th>Link</th><th>Acciones</th></tr></thead>
      <tbody>{rows or empty_row(7, "Aún no hay documentos detectados.", "Ejecuta el monitoreo para buscar nuevas publicaciones de la Dirección del Trabajo.")}</tbody>
    </table>
  </div>
</section>
"""


def render_alert_preview(alert_id: int, settings: Settings, *, flash: str = "") -> str:
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

    ready_btn = ""
    if alert["status"] == "pending_review":
        ready_btn = (
            f'<form method="post" action="/admin/alerts/{alert_id}/ready">'
            f'<button class="eg-btn eg-btn--secondary eg-btn--sm" type="submit">'
            f'{icon("check", 15)}<span>Marcar lista</span></button></form>'
        )

    ai_st = alert.get("ai_status") or ""

    # --- Paneles inferiores ---
    ai_panel = ""
    exec_panel = ""
    detail_panel = ""
    ai_attach_card = ""

    if ai_st:
        ai_label, _ = AI_STATUS_LABELS.get(ai_st, (ai_st, ""))
        ai_error_text = h((alert.get('ai_summary_error') or '—')[:200])

        # Acciones consolidadas
        dl_exec = (
            f'<a class="eg-btn eg-btn--secondary eg-btn--sm" href="/admin/alerts/{alert_id}/executive-summary" download>'
            f'{icon("document", 14)}<span>Descargar ejecutivo</span></a>'
            if ai_st in ("success", "fallback") else ""
        )
        dl_detail = (
            f'<a class="eg-btn eg-btn--secondary eg-btn--sm" href="/admin/alerts/{alert_id}/detailed-summary" download>'
            f'{icon("document", 14)}<span>Descargar detallado</span></a>'
            if ai_st in ("success", "fallback") else ""
        )
        if ai_st == "success":
            regen_btn = (
                f'<form method="post" action="/admin/alerts/{alert_id}/regenerate-ai" style="display:inline;">'
                f'<button class="eg-btn eg-ghost eg-btn--sm" type="submit" title="Volver a generar el resumen con IA">'
                f'{icon("refresh", 14)}<span>↺ Regenerar con IA</span></button></form>'
            )
        else:
            regen_btn = (
                f'<form method="post" action="/admin/alerts/{alert_id}/generate-ai" style="display:inline;">'
                f'<button class="eg-btn eg-ghost eg-btn--sm" type="submit" title="Generar resumen con inteligencia artificial">'
                f'{icon("cpu", 14)}<span>✨ Generar con IA</span></button></form>'
            )
        canon_btn = (
            f'<a class="eg-btn eg-ghost eg-btn--sm" href="{h(alert["canonical_url"])}" target="_blank" rel="noopener noreferrer">'
            f'{icon("external", 14)}<span>Ver documento oficial</span></a>'
        )
        actions_html = f'<div class="eg-actions" style="margin-top:12px;">{dl_exec}{dl_detail}{regen_btn}{canon_btn}</div>'

        ai_panel = f"""<section class="eg-card eg-panel">
  <p class="eg-eyebrow">Inteligencia Artificial</p>
  <h2>Estado del resumen IA</h2>
  <p class="eg-devbanner" style="font-size:13px;font-weight:600;">
    Contenido generado con apoyo de IA. Revisar antes de enviar.
  </p>
  <dl class="eg-kv eg-kv--2col">
    <dt>Estado</dt><dd>{h(ai_label)}</dd>
    <dt>Proveedor</dt><dd class="mono">{h(alert.get('ai_provider') or '—')}</dd>
    <dt>Modelo</dt><dd class="mono">{h(alert.get('ai_model') or '—')}</dd>
    <dt>Calidad</dt><dd class="mono">{h(alert.get('ai_content_quality') or '—')}</dd>
    <dt>Generado</dt><dd class="mono">{fmt_dt(alert.get('ai_updated_at'))}</dd>
    <dt>Error</dt><dd class="eg-muted mono" style="font-size:12px;">{ai_error_text}</dd>
  </dl>
  {actions_html}
</section>"""

        # Estado de adjuntos como tarjeta compacta (junto a IA panel)
        if ai_st in ("success", "fallback") and settings.ai_attachments_enabled:
            ai_attach_card = f"""<section class="eg-card eg-panel">
  <p class="eg-eyebrow">Adjuntos</p>
  <h2>Estado de adjuntos</h2>
  <dl class="eg-kv eg-kv--2col">
    <dt>Resumen ejecutivo</dt><dd>{badge('active', 'Preparado')}</dd>
    <dt>Resumen detallado</dt><dd>{badge('active', 'Preparado')}</dd>
    <dt>Doc. oficial</dt><dd>{badge('simulated', 'Enlace en correo') if not alert.get('pdf_url') else badge('active', 'PDF disponible')}</dd>
  </dl>
  <p class="eg-muted" style="font-size:12px;">Se adjuntan automáticamente si AI_ATTACHMENTS_ENABLED=true.</p>
</section>"""

    # Resumen ejecutivo como <details> colapsable
    raw_exec = alert.get("ai_executive_summary") or ""
    if raw_exec:
        try:
            exec_data = json.loads(raw_exec)
        except Exception:
            exec_data = {"title": "Resumen ejecutivo", "body": raw_exec}
        exec_title = h(exec_data.get('title') or 'Resumen ejecutivo')
        exec_body = h((exec_data.get('body') or '')[:1500])
        exec_panel = f"""<details class="eg-card eg-panel" style="padding:0;">
  <summary style="padding:18px 20px;cursor:pointer;font-size:13px;font-weight:700;color:var(--eg-text);list-style:none;display:flex;align-items:center;gap:8px;">
    {icon("document", 15)}<span>{exec_title}</span>
    <span style="margin-left:auto;font-size:11px;font-weight:400;color:var(--eg-subtle);">Ver contenido</span>
  </summary>
  <div style="padding:0 20px 18px;">
    <p style="font-size:14px;line-height:1.65;color:#3C4A52;margin:0;">{exec_body}</p>
  </div>
</details>"""

    # Resumen detallado como <details> colapsable
    raw_detail = alert.get("ai_detailed_summary_json") or ""
    if raw_detail:
        try:
            detail_data = json.loads(raw_detail)
        except Exception:
            detail_data = {"title": "Resumen detallado", "sections": []}
        detail_title = h(detail_data.get('title') or 'Resumen detallado')
        sections_html = ""
        for s in (detail_data.get("sections") or [])[:6]:
            heading = h(s.get("heading") or "")
            body_t = h((s.get("body") or "")[:800])
            if heading:
                sections_html += f'<h3 style="font-size:14px;color:#0A2231;margin:14px 0 4px;">{heading}</h3>'
            if body_t:
                sections_html += f'<p style="font-size:14px;color:#3C4A52;margin:0 0 10px;">{body_t}</p>'
        detail_panel = f"""<details class="eg-card eg-panel" style="padding:0;">
  <summary style="padding:18px 20px;cursor:pointer;font-size:13px;font-weight:700;color:var(--eg-text);list-style:none;display:flex;align-items:center;gap:8px;">
    {icon("document", 15)}<span>{detail_title}</span>
    <span style="margin-left:auto;font-size:11px;font-weight:400;color:var(--eg-subtle);">Ver contenido</span>
  </summary>
  <div style="padding:0 20px 18px;">{sections_html}</div>
</details>"""

    # Cuando no hay resumen IA, mostrar tarjeta con botón "Generar con IA"
    if not ai_st:
        ai_panel = (
            f'<section class="eg-card eg-panel">'
            f'<p class="eg-eyebrow">Inteligencia Artificial</p>'
            f'<h2>Sin resumen IA</h2>'
            f'<p style="font-size:14px;color:#6B7280;margin-bottom:16px;">Este documento aún no tiene un resumen generado por IA.</p>'
            f'<form method="post" action="/admin/alerts/{alert_id}/generate-ai" style="display:inline;">'
            f'<button class="eg-btn eg-btn--primary eg-btn--sm" type="submit" title="Generar resumen con inteligencia artificial">'
            f'{icon("cpu", 14)}<span>✨ Generar con IA</span></button></form>'
            f'</section>'
        )

    ai_lower_row = ""
    if ai_panel or ai_attach_card:
        if ai_attach_card:
            ai_lower_row = (
                f'<div class="eg-grid-2" style="align-items:start;">{ai_panel}{ai_attach_card}</div>'
            )
        else:
            ai_lower_row = ai_panel

    topbar = render_topbar(
        "Vista previa de email",
        "Revisa el correo tal como lo recibirá el suscriptor antes de enviarlo.",
        settings,
        show_action=False,
    )
    real_send_note = (
        '<div class="eg-banner">Envio real habilitado '
        f"({h(settings.email_provider)}). El boton Enviar prueba envia un correo de verdad.</div>"
        if real_send else
        '<div class="eg-banner eg-banner--info">El envio esta en modo simulado. '
        "Configura SendGrid para habilitar correos reales.</div>"
    )
    flash_html = f'<div class="eg-flash" role="status" style="margin-bottom:14px;">{h(flash)}</div>' if flash else ""
    body = f"""
{flash_html}{real_send_note}
<div class="eg-preview-grid">
  <section class="eg-card eg-card-pad eg-review-panel">
    <p class="eg-eyebrow">Revisión</p>
    <h2>Detalle de la alerta</h2>
    <dl class="eg-kv eg-kv--2col">
      <dt>Estado</dt><dd>{badge(alert['status'])}</dd>
      <dt>Relevancia</dt><dd>{rel_badge(alert['relevance'])}</dd>
      <dt>Categoría</dt><dd>{h(alert['category'])}</dd>
      <dt>Fecha doc.</dt><dd class="mono">{h(alert.get('publication_date') or '—')}</dd>
      <dt>Asunto</dt><dd class="mono">{h(subject)}</dd>
      <dt>Fuente</dt><dd><a class="eg-btn eg-btn--primary eg-btn--sm" href="{h(alert['canonical_url'])}" target="_blank" rel="noopener noreferrer">{icon('external', 14)}<span>Ver en DT</span></a></dd>
    </dl>
    <div class="eg-review-actions">
      <a class="eg-btn eg-ghost eg-btn--sm" href="/admin/alerts">{icon("back", 16)}<span>Volver</span></a>
      {ready_btn}
      <form method="post" action="/admin/alerts/{alert_id}/test" class="eg-inline-form">
        <input class="eg-input eg-input--sm" type="email" name="to" placeholder="correo de prueba" aria-label="Correo de prueba">
        <button class="eg-btn eg-btn--primary eg-btn--sm" type="submit">
          {icon("mail", 15)}<span>Enviar prueba</span>
        </button>
      </form>
    </div>
  </section>
  <section class="eg-card eg-panel">
    <p class="eg-eyebrow">Previsualización</p>
    <h2>Vista previa del email</h2>
    <p class="eg-muted">Asi se vera el correo en la bandeja del suscriptor.</p>
    <div class="eg-email-preview">
      <iframe title="Vista previa del email (HTML)" srcdoc="{srcdoc}"></iframe>
    </div>
    <details style="margin-top:14px;">
      <summary>Ver version en texto plano</summary>
      <pre>{h(email_text)}</pre>
    </details>
  </section>
</div>
{ai_lower_row}
{exec_panel}
{detail_panel}
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
  <link href="https://fonts.googleapis.com/css2?family=Titillium+Web:wght@400;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>{CSS}</style>
</head>
"""
    if sidebar is not None:
        mobile_drawer_js = """
<script>
(function(){
  var scrim=document.querySelector('.eg-scrim');
  var sidebar=document.querySelector('.eg-sidebar');
  var burger=document.querySelector('.eg-burger');
  function open(){sidebar.classList.add('is-open');scrim.classList.add('is-visible');document.body.style.overflow='hidden';}
  function close(){sidebar.classList.remove('is-open');scrim.classList.remove('is-visible');document.body.style.overflow='';}
  if(burger)burger.addEventListener('click',open);
  if(scrim)scrim.addEventListener('click',close);
})();
</script>"""
        return f"""
<!doctype html>
<html lang="es">
{head}
<body class="eg eg--admin" data-eg-theme="light" data-eg-density="compact">
{_EG_LOGO_SYMBOL}
<div class="eg-scrim" aria-hidden="true"></div>
<div class="eg-mobilebar">
  <button class="eg-burger" aria-label="Abrir menu" type="button">
    <span></span><span></span><span></span>
  </button>
  <span class="eg-mobilebar-title">Alertas DT</span>
</div>
<div class="eg-shell">
  <aside class="eg-sidebar">{sidebar}</aside>
  <div class="eg-main">
    {topbar}
    <div class="eg-content">{body}</div>
  </div>
</div>
{mobile_drawer_js}
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
   Alertas DT · Sistema de diseño editorial (prototipo v2)
   ===================================================================== */

/* ----- Tokens -------------------------------------------------------- */
:root {
  --eg-bg: #F4F7F8;
  --eg-surface: #FFFFFF;
  --eg-surface-soft: #EEF3F5;
  --eg-sidebar: #0A2231;
  --eg-sidebar-2: #071B27;
  --eg-text: #0A2231;
  --eg-muted: #5F6E76;
  --eg-subtle: #8EA1AA;
  --eg-border: rgba(36,55,67,0.12);
  --eg-green: #29B78D;
  --eg-green-hover: #24EBA1;
  --eg-green-deep: #1E8E6C;
  --eg-blue: #06A4F5;
  --eg-warning: #C56A14;
  --eg-danger: #B23B3B;
  --sidebar-w: 248px;
  --font-sans: 'Titillium Web', system-ui, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, monospace;
  --radius-sm: 7px;
  --radius: 12px;
  --radius-lg: 18px;
  /* public pages compat */
  --eg-brand-primary: #243743;
  --eg-brand-accent: #29B78D;
  --eg-brand-dark: #0E2230;
  --eg-brand-mint: #24EBA1;
  --eg-brand-blue: #06A4F5;
  --eg-font-heading: 'Titillium Web', system-ui, sans-serif;
  --eg-font-body: 'Titillium Web', system-ui, sans-serif;
  --eg-radius: 16px;
  --eg-radius-lg: 24px;
}

/* ----- Dark theme (public hero, sidebar) ----------------------------- */
:root, [data-eg-theme="dark"] {
  color-scheme: dark;
  --eg-bg-t: #0A2231;
  --eg-surface-t: #0E2230;
  --eg-border-t: rgba(255,255,255,.10);
  --eg-cta: #29B78D;
  --eg-cta-hover: #24EBA1;
  --eg-text-on-cta: #0A2231;
  --eg-support: #06A4F5;
  --eg-accent: #29B78D;
  --eg-text-t: #F6FAFC;
  --eg-text-muted: #C7D1D6;
  --eg-text-subtle: #8EA1AA;
  --eg-focus: #24EBA1;
}

/* ----- Light theme (public content, admin main) ---------------------- */
[data-eg-theme="light"] {
  color-scheme: light;
  --eg-bg-t: #F6F8F9;
  --eg-surface-t: #FFFFFF;
  --eg-border-t: rgba(36,55,67,0.12);
  --eg-border-accent: rgba(41,183,141,0.32);
  --eg-cta: #29B78D;
  --eg-cta-hover: #167A5F;
  --eg-text-on-cta: #0A2231;
  --eg-support: #0478B4;
  --eg-accent: #167A5F;
  --eg-text-t: #0A2231;
  --eg-text-muted: #3C4A52;
  --eg-text-subtle: #6D7478;
  --eg-focus: #0478B4;
}

[data-eg-accent="green"] { --eg-accent: #29B78D; }
[data-eg-accent="blue"]  { --eg-accent: #06A4F5; }
[data-eg-density="editorial"] { --eg-section-pad: clamp(56px,9vw,120px); }
[data-eg-density="compact"]   { --eg-section-pad: clamp(32px,5vw,56px); }

/* ----- Base ---------------------------------------------------------- */
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body.eg {
  margin: 0;
  background: var(--eg-bg);
  color: var(--eg-text);
  font-family: var(--font-sans);
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.eg h1, .eg h2, .eg h3, .eg h4 {
  font-family: var(--font-sans);
  font-weight: 700;
  color: var(--eg-text);
  letter-spacing: -0.02em;
  margin: 0 0 .5em;
  line-height: 1.15;
}
.eg h1 { font-size: clamp(1.9rem,4vw,2.8rem); }
.eg h2 { font-size: clamp(1.2rem,2vw,1.5rem); }
.eg p { margin: 0 0 1rem; color: var(--eg-muted); }
.eg a { color: var(--eg-blue); text-decoration: none; }
.eg a:hover { color: var(--eg-green); }
.mono { font-family: var(--font-mono); font-size: .85em; }

/* ----- Admin shell --------------------------------------------------- */
body.eg--admin {
  background: var(--eg-bg);
  color: var(--eg-text);
}
.eg-shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr;
  min-height: 100vh;
}

/* ----- Sidebar ------------------------------------------------------- */
.eg-sidebar {
  background: var(--eg-sidebar);
  color: rgba(255,255,255,.82);
  position: sticky;
  top: 0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  padding: 20px 16px;
  overflow-y: auto;
  transition: transform .22s cubic-bezier(.4,0,.2,1);
}
.eg-brand {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 2px 4px 4px;
}
.eg-brand-logo-wrap { display: flex; flex-direction: column; }
.eg-brand-sub {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--eg-green);
}
.eg-hairline {
  height: 1px;
  background: rgba(255,255,255,.08);
  margin: 14px 0;
}
.eg-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
}
.eg-nav a {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  color: rgba(255,255,255,.70);
  font-size: 14px;
  font-weight: 600;
  text-decoration: none;
  transition: background .15s, color .15s;
}
.eg-nav a:hover {
  background: rgba(255,255,255,.07);
  color: #fff;
}
.eg-nav a.active {
  background: var(--eg-green);
  color: #0A2231;
}
.eg-nav a .eg-ic { flex-shrink: 0; }
.eg-side-foot { margin-top: auto; }
.eg-side-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: rgba(255,255,255,.5);
  padding: 4px 4px;
}
.eg-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #8EA1AA;
  flex-shrink: 0;
}
.eg-dot[data-status="active"] { background: var(--eg-green); }
.eg-dot[data-status="pending_review"],
.eg-dot[data-status="simulated"] { background: var(--eg-warning); }
.eg-dot[data-status="error"] { background: var(--eg-danger); }
.eg-side-tag {
  font-size: 11px;
  color: rgba(255,255,255,.3);
  padding: 4px 4px 2px;
  letter-spacing: .04em;
}

/* Mobile bar */
.eg-mobilebar {
  display: none;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: var(--eg-sidebar);
  color: #fff;
  position: sticky;
  top: 0;
  z-index: 200;
}
.eg-mobilebar-title { font-size: 14px; font-weight: 700; }
.eg-burger {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  width: 32px;
  height: 32px;
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 4px;
}
.eg-burger span {
  display: block;
  height: 2px;
  background: rgba(255,255,255,.85);
  border-radius: 2px;
  transition: opacity .2s;
}
.eg-scrim {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.5);
  z-index: 299;
  opacity: 0;
  transition: opacity .22s;
}
.eg-scrim.is-visible { opacity: 1; }

/* ----- Topbar -------------------------------------------------------- */
.eg-main { display: flex; flex-direction: column; min-width: 0; }
.eg-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 28px 16px;
  border-bottom: 1px solid var(--eg-border);
  background: var(--eg-surface);
  position: sticky;
  top: 0;
  z-index: 100;
}
.eg-topbar-titles h1 {
  font-size: 1.15rem;
  font-weight: 700;
  margin: 0;
  color: var(--eg-text);
}
.eg-topbar-titles p {
  font-size: 13px;
  color: var(--eg-muted);
  margin: 2px 0 0;
}
.eg-topbar-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.eg-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  background: color-mix(in srgb, var(--eg-green) 12%, transparent);
  color: var(--eg-green-deep);
  border: 1px solid color-mix(in srgb, var(--eg-green) 28%, transparent);
  white-space: nowrap;
}
.eg-status-pill.is-neutral {
  background: color-mix(in srgb, var(--eg-muted) 10%, transparent);
  color: var(--eg-muted);
  border-color: var(--eg-border);
}

/* ----- Content area -------------------------------------------------- */
.eg-content {
  padding: 24px 28px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* ----- Cards --------------------------------------------------------- */
.eg-card {
  background: var(--eg-surface);
  border: 1px solid var(--eg-border);
  border-radius: var(--radius-lg);
}
.eg-panel { padding: 22px 24px; }
.eg-card-pad { padding: 24px; }
.eg-card-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.eg-card-head h2 { margin: 0; font-size: 1rem; }

/* ----- Metric grid --------------------------------------------------- */
.eg-metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 14px;
}
.eg-metric {
  background: var(--eg-surface);
  border: 1px solid var(--eg-border);
  border-radius: var(--radius);
  padding: 18px 18px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.eg-metric-ico {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 8px;
}
.eg-ico-green { background: color-mix(in srgb, var(--eg-green) 14%, transparent); color: var(--eg-green-deep); }
.eg-ico-blue  { background: color-mix(in srgb, var(--eg-blue) 14%, transparent);  color: #0478B4; }
.eg-ico-warn  { background: color-mix(in srgb, var(--eg-warning) 14%, transparent); color: var(--eg-warning); }
.eg-ico-slate { background: color-mix(in srgb, var(--eg-muted) 14%, transparent);  color: var(--eg-muted); }
.eg-metric-num  { font-size: 1.7rem; font-weight: 700; color: var(--eg-text); line-height: 1; }
.eg-metric-label { font-size: 13px; font-weight: 600; color: var(--eg-text); }
.eg-metric-sub   { font-size: 12px; color: var(--eg-subtle); }

/* ----- Badges -------------------------------------------------------- */
.eg-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px 3px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.eg-badge::before {
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.eg-badge--no-dot::before { display: none; }
.eg-badge--active   { background: color-mix(in srgb,#29B78D 14%,#fff); color: #1E8E6C; }
.eg-badge--active::before { background: #29B78D; }
.eg-badge--sent     { background: color-mix(in srgb,#06A4F5 14%,#fff); color: #0478B4; }
.eg-badge--sent::before   { background: #06A4F5; }
.eg-badge--ready    { background: color-mix(in srgb,#06A4F5 14%,#fff); color: #0478B4; }
.eg-badge--ready::before  { background: #06A4F5; }
.eg-badge--pending  { background: color-mix(in srgb,#C56A14 12%,#fff); color: #A05010; }
.eg-badge--pending::before { background: #C56A14; }
.eg-badge--baseline { background: color-mix(in srgb,#8EA1AA 14%,#fff); color: #5F6E76; }
.eg-badge--baseline::before { background: #8EA1AA; }
.eg-badge--paused   { background: color-mix(in srgb,#8EA1AA 12%,#fff); color: #5F6E76; }
.eg-badge--paused::before  { background: #8EA1AA; }
.eg-badge--danger   { background: color-mix(in srgb,#B23B3B 12%,#fff); color: #B23B3B; }
.eg-badge--danger::before  { background: #B23B3B; }

/* Relevance chips */
.eg-rel {
  display: inline-flex;
  align-items: center;
  padding: 3px 9px;
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
  white-space: nowrap;
}
.eg-rel--high { background: #DCF8EE; color: #1E8E6C; }
.eg-rel--mid  { background: #FEF0DC; color: #A05010; }
.eg-rel--low  { background: #FDE8E8; color: #B23B3B; }

/* Legacy pill (public pages, login) */
.eg-pill {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid color-mix(in srgb, currentColor 30%, transparent);
  background: color-mix(in srgb, var(--eg-muted) 10%, transparent);
  color: var(--eg-muted);
}
.eg-pill[data-status="active"], .eg-pill[data-status="sent"],
.eg-pill[data-status="ready_to_send"], .eg-pill[data-status="ready"],
.eg-pill[data-status="processed"], .eg-pill[data-status="success"],
.eg-pill[data-status="alto"] {
  background: color-mix(in srgb,#167A5F 16%,transparent); color: #0F5E51;
}
.eg-pill[data-status="paused"], .eg-pill[data-status="pending_review"],
.eg-pill[data-status="partial"], .eg-pill[data-status="baseline"],
.eg-pill[data-status="discovered"], .eg-pill[data-status="medio"],
.eg-pill[data-status="running"], .eg-pill[data-status="skipped"] {
  background: color-mix(in srgb,#B45309 16%,transparent); color: #92400E;
}
.eg-pill[data-status="error"], .eg-pill[data-status="failed"],
.eg-pill[data-status="ignored"], .eg-pill[data-status="bajo"] {
  background: color-mix(in srgb,#B42318 14%,transparent); color: #B42318;
}

/* ----- Section heads ------------------------------------------------- */
.eg-section-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  margin-bottom: 16px;
}
.eg-section-head .ghost {
  font-size: 2.8rem;
  font-weight: 700;
  color: var(--eg-border);
  line-height: 1;
  letter-spacing: -.04em;
  user-select: none;
}
.eg-section-head h2 { margin: 0; }
.eg-section-head p  { margin: 0; font-size: 13px; color: var(--eg-subtle); }
.eg-eyebrow {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--eg-subtle);
  margin: 0 0 6px;
}

/* ----- Buttons ------------------------------------------------------- */
.eg-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 18px;
  border-radius: var(--radius-sm);
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  border: none;
  text-decoration: none;
  transition: background .15s, color .15s, opacity .15s;
  white-space: nowrap;
}
.eg-btn--primary {
  background: var(--eg-green);
  color: #fff;
}
.eg-btn--primary:hover { background: var(--eg-green-deep); color: #fff; }
/* Forzar color en <a> que hereda color de link */
a.eg-btn--primary,
a.eg-btn--primary:link,
a.eg-btn--primary:visited { color: #fff !important; text-decoration: none; }
a.eg-btn--primary:hover { color: #fff !important; }
.eg-btn--secondary {
  background: var(--eg-surface-soft);
  color: var(--eg-text);
  border: 1px solid var(--eg-border);
}
.eg-btn--secondary:hover { background: var(--eg-border); color: var(--eg-text); }
a.eg-btn--secondary,
a.eg-btn--secondary:link,
a.eg-btn--secondary:visited { color: var(--eg-text) !important; text-decoration: none; }
.eg-ghost {
  background: transparent;
  color: var(--eg-muted);
  border: 1px solid var(--eg-border);
}
.eg-ghost:hover { color: var(--eg-text); border-color: var(--eg-text); background: transparent; }
a.eg-ghost,
a.eg-ghost:link,
a.eg-ghost:visited { color: var(--eg-muted) !important; text-decoration: none; }
a.eg-ghost:hover { color: var(--eg-text) !important; }
.eg-btn--sm { padding: 6px 12px; font-size: 13px; }
.eg-btn--block { width: 100%; justify-content: center; }
.eg-btn:disabled { opacity: .45; cursor: not-allowed; }

/* ----- Tables -------------------------------------------------------- */
.eg-table-wrap { overflow-x: auto; border-radius: var(--radius); }
.eg-table { width: 100%; border-collapse: collapse; background: var(--eg-surface); }
.eg-table th, .eg-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--eg-border);
  text-align: left;
  vertical-align: top;
  font-size: 13.5px;
}
.eg-table th {
  font-size: 11px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--eg-muted);
  background: var(--eg-surface-soft);
  font-weight: 700;
  white-space: nowrap;
}
.eg-table td { color: var(--eg-muted); }
.eg-table tbody tr:last-child td { border-bottom: 0; }
.eg-table tbody tr:hover td { background: color-mix(in srgb, var(--eg-green) 4%, transparent); }
.eg-table td strong { color: var(--eg-text); font-weight: 700; }
.eg-table td form { margin: 0; }
.eg-table td.mono { font-family: var(--font-mono); font-size: 12.5px; }
.eg-doc-title { font-weight: 700; color: var(--eg-text); font-size: 13.5px; }
.eg-doc-desc  {
  font-size: 12px;
  color: var(--eg-subtle);
  margin-top: 3px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  max-width: 380px;
}
.eg-cell-cat  { font-size: 12.5px; color: var(--eg-muted); white-space: nowrap; }
.eg-cell-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.eg-cell-actions form { margin: 0; }

/* ----- Alert grid ---------------------------------------------------- */
.eg-alert-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 16px;
  max-width: 1400px;
}
.eg-alert-card {
  background: var(--eg-surface);
  border: 1px solid var(--eg-border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.eg-alert-card .accent {
  height: 4px;
  width: 100%;
  flex-shrink: 0;
  background: var(--eg-green);
}
.eg-alert-body {
  padding: 16px 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 1;
  min-width: 0;
}
.eg-alert-meta-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.eg-alert-cat { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--eg-subtle); }
.eg-alert-chips { display: flex; gap: 5px; align-items: center; flex-wrap: wrap; }
.eg-alert-title { font-size: .95rem; font-weight: 700; color: var(--eg-text); margin: 0; line-height: 1.3; }
.eg-alert-summary {
  font-size: 13px;
  color: var(--eg-muted);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin: 0;
}
.eg-alert-date { font-family: var(--font-mono); font-size: 11.5px; color: var(--eg-subtle); }
.eg-alert-actions {
  padding: 10px 14px 12px;
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  gap: 7px;
  align-items: center;
  border-top: 1px solid var(--eg-border);
  background: var(--eg-surface-soft);
}
.eg-alert-actions form { margin: 0; }
.eg-action-row { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; }
.eg-test-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; width: 100%; margin-top: 3px; }
.eg-test-row input { flex: 1; min-width: 160px; }

/* ----- KV (key-value) lists ----------------------------------------- */
.eg-kv { display: grid; gap: 6px; margin: 0 0 16px; }
.eg-kv div { display: grid; grid-template-columns: 140px 1fr; gap: 12px; font-size: 14px; }
.eg-kv dt { font-weight: 700; color: var(--eg-text); }
.eg-kv dd { margin: 0; color: var(--eg-muted); }
.eg-kv--2col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
  margin: 0 0 12px;
}
.eg-kv--2col dt, .eg-kv--2col dd {
  padding: 9px 0;
  border-bottom: 1px solid var(--eg-border);
  font-size: 13.5px;
  margin: 0;
}
.eg-kv--2col dt { font-weight: 600; color: var(--eg-text); padding-right: 16px; }
.eg-kv--2col dd { color: var(--eg-muted); }
.eg-kv--2col dt:last-of-type, .eg-kv--2col dd:last-of-type { border-bottom: 0; }

/* ----- Grids --------------------------------------------------------- */
.eg-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

/* ----- Misc admin components ---------------------------------------- */
.eg-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.eg-actions form { margin: 0; }
.eg-inline-form { display: flex; gap: 6px; align-items: center; }
.eg-input--sm { min-height: 36px; padding: 6px 10px; font-size: 13px; max-width: 200px; border-radius: var(--radius-sm); }
.eg-preview-grid { display: grid; grid-template-columns: minmax(280px,360px) 1fr; gap: 16px; align-items: start; }
.eg-review-panel {}
.eg-review-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; align-items: center; }
.eg-email-preview { border: 1px solid var(--eg-border); border-radius: var(--radius); background: #fff; overflow: hidden; margin-top: 12px; }
.eg-email-preview iframe { width: 100%; min-height: 640px; border: 0; display: block; }
.eg-preview pre { white-space: pre-wrap; word-break: break-word; background: var(--eg-surface-soft); border-radius: 10px; padding: 14px; font-size: 12.5px; line-height: 1.5; overflow-x: auto; }
.eg-banner {
  border-radius: var(--radius-sm);
  padding: 11px 16px;
  font-size: 13.5px;
  font-weight: 600;
  background: color-mix(in srgb, var(--eg-green) 10%, transparent);
  color: var(--eg-green-deep);
  border: 1px solid color-mix(in srgb, var(--eg-green) 28%, transparent);
}
.eg-banner--info {
  background: color-mix(in srgb, var(--eg-blue) 10%, transparent);
  color: #0478B4;
  border-color: color-mix(in srgb, var(--eg-blue) 28%, transparent);
}
.eg-flash {
  border-radius: var(--radius-sm);
  padding: 11px 16px;
  font-size: 13.5px;
  font-weight: 600;
  background: color-mix(in srgb, var(--eg-blue) 12%, transparent);
  color: #0478B4;
  border: 1px solid color-mix(in srgb, var(--eg-blue) 28%, transparent);
}
.eg-devbanner {
  border-radius: var(--radius-sm);
  padding: 11px 16px;
  font-size: 13.5px;
  font-weight: 700;
  background: color-mix(in srgb, var(--eg-warning) 12%, transparent);
  color: var(--eg-warning);
  border: 1px solid color-mix(in srgb, var(--eg-warning) 30%, transparent);
}
.eg-empty { display: grid; gap: 4px; padding: 28px 8px; text-align: center; }
.eg-empty strong { color: var(--eg-text); font-size: 15px; }
.eg-empty span { color: var(--eg-subtle); font-size: 13.5px; }
.eg-empty-row td { background: transparent; }
.eg-empty-row:hover td { background: transparent; }
.eg-muted { color: var(--eg-subtle); font-size: 13px; line-height: 1.45; margin: 4px 0 0; }
p.eg-muted { max-height: 75px; overflow: hidden; }
.eg-note { font-size: 12.5px; color: var(--eg-subtle); margin-top: 12px; line-height: 1.5; }
.eg-section-note { font-size: 13px; color: var(--eg-muted); margin: 0; }

/* ----- Settings page specific --------------------------------------- */
.eg-settings-warn {
  margin-top: 12px;
  background: color-mix(in srgb,#C56A14 10%,transparent);
  border: 1px solid color-mix(in srgb,#C56A14 30%,transparent);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  font-size: 13px;
  color: #7A3E06;
  line-height: 1.5;
}
.eg-settings-warn code {
  font-family: var(--font-mono);
  background: rgba(0,0,0,.07);
  padding: 1px 5px;
  border-radius: 4px;
}
.eg-label-hint {
  margin-left: 8px;
  font-size: 11px;
  font-weight: 400;
  color: var(--eg-subtle);
  text-transform: uppercase;
  letter-spacing: .04em;
}
.eg-input--mono { font-family: var(--font-mono); font-size: 13px; }
textarea.eg-input { resize: vertical; min-height: 56px; }

/* ----- Inputs -------------------------------------------------------- */
.eg-input {
  display: block;
  width: 100%;
  padding: 10px 14px;
  border: 1px solid var(--eg-border);
  border-radius: var(--radius-sm);
  font-size: 14px;
  font-family: var(--font-sans);
  color: var(--eg-text);
  background: var(--eg-surface);
  transition: border-color .15s, box-shadow .15s;
}
.eg-input:focus { outline: none; border-color: var(--eg-green); box-shadow: 0 0 0 3px color-mix(in srgb,var(--eg-green) 18%,transparent); }
.eg-label { display: block; font-size: 13px; font-weight: 700; color: var(--eg-text); margin-bottom: 6px; }
.eg-field { display: grid; gap: 4px; margin-bottom: 16px; }

/* ----- Public pages -------------------------------------------------- */
.eg-container { width: min(1140px, calc(100% - 48px)); margin-inline: auto; }
.eg-hero {
  position: relative;
  overflow: hidden;
  background: var(--eg-sidebar);
  padding: var(--eg-section-pad) 0;
}
.eg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  pointer-events: none;
}
.eg-glow--a { width: 560px; height: 560px; background: rgba(41,183,141,.18); top: -120px; right: -80px; }
.eg-glow--b { width: 380px; height: 380px; background: rgba(6,164,245,.12); bottom: -60px; left: -60px; }
.eg-hero__grid { display: grid; grid-template-columns: 1fr 420px; gap: 56px; align-items: center; position: relative; z-index: 1; }
.eg-hero__copy { color: #F6FAFC; }
.eg-hero__title { font-size: clamp(2.2rem,5vw,3.2rem); line-height: 1.08; color: #F6FAFC; margin-bottom: 18px; }
.eg-hero__title span { color: var(--eg-green); }
.eg-hero__lead { font-size: clamp(1rem,1.6vw,1.18rem); color: rgba(255,255,255,.78); margin-bottom: 24px; }
.eg-hero__points { list-style: none; display: flex; flex-wrap: wrap; gap: 10px; padding: 0; margin: 0; }
.eg-chip { background: rgba(255,255,255,.10); color: #fff; border: 1px solid rgba(255,255,255,.2); border-radius: 999px; font-size: 13px; font-weight: 600; padding: 5px 14px; }
.eg-section { padding: var(--eg-section-pad) 0; }
.eg-section--soft { background: var(--eg-surface-soft); }
.eg-section__title { font-size: clamp(1.5rem,2.5vw,2rem); margin-bottom: 32px; }
.eg-grid-4 { display: grid; grid-template-columns: repeat(auto-fill,minmax(220px,1fr)); gap: 20px; }
.eg-benefit { padding: 24px; display: flex; flex-direction: column; gap: 8px; }
.eg-benefit__icon { font-size: 2rem; }
.eg-steps { list-style: none; padding: 0; margin: 0; display: grid; gap: 20px; grid-template-columns: repeat(auto-fill,minmax(240px,1fr)); }
.eg-step { display: flex; align-items: flex-start; gap: 16px; }
.eg-step__num { width: 40px; height: 40px; border-radius: 50%; background: var(--eg-green); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 1.1rem; flex-shrink: 0; }
.eg-feedback { padding: clamp(40px,8vw,100px) 0; }
.eg-feedback__card { max-width: 480px; margin: 0 auto; padding: 40px; text-align: center; }
.eg-feedback__icon { font-size: 3rem; display: block; margin-bottom: 12px; }
.eg-feedback__lead { font-size: 1rem; color: var(--eg-text-muted,#3C4A52); }
.eg-auth { padding: clamp(40px,8vw,100px) 0; }
.eg-auth__card { max-width: 420px; margin: 0 auto; padding: 40px; }
.eg-auth__help { font-size: 14px; color: var(--eg-text-muted,#3C4A52); margin-bottom: 20px; }
.eg-form { display: flex; flex-direction: column; gap: 16px; }
.eg-form__title { font-size: 1.4rem; margin-bottom: 4px; }
.eg-check { display: flex; align-items: flex-start; gap: 10px; cursor: pointer; font-size: 14px; color: var(--eg-text-muted,#3C4A52); }
.eg-check--consent { line-height: 1.4; }
.eg-error { color: var(--eg-danger,#B23B3B); font-size: 14px; margin: 0; }
.eg-fineprint { font-size: 12px; color: var(--eg-text-subtle,#6D7478); margin: 8px 0 0; text-align: center; }
.eg-header { background: #fff; border-bottom: 1px solid rgba(36,55,67,.10); }
.eg-header__inner { display: flex; align-items: center; justify-content: space-between; padding: 14px 0; }
.eg-header__brand { display: flex; align-items: center; gap: 12px; text-decoration: none; }
.eg-header__tag { font-size: 12px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--eg-subtle,#8EA1AA); }
.eg-logo { height: 36px; width: auto; display: block; }
.eg-logo--sm { height: 26px; }
.eg-footer { background: #0A2231; padding: 40px 0; }
.eg-footer__inner { display: flex; flex-direction: column; gap: 14px; }
.eg-footer__note { font-size: 13.5px; color: rgba(255,255,255,.55); margin: 0; }
.eg-footer__copy { font-size: 12px; color: rgba(255,255,255,.35); margin: 0; }
.eg-embed { padding: 24px; min-height: 100vh; background: var(--eg-bg); display: flex; align-items: flex-start; justify-content: center; }
.eg-embed .eg-card { max-width: 480px; width: 100%; }

/* ----- Responsive ---------------------------------------------------- */
@media (max-width: 1280px) {
  .eg-metric-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 1024px) {
  .eg-metric-grid { grid-template-columns: repeat(2, 1fr); }
  .eg-grid-2 { grid-template-columns: 1fr; }
}
@media (max-width: 900px) {
  .eg-preview-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .eg-shell { grid-template-columns: 1fr; }
  .eg-sidebar {
    position: fixed;
    left: 0; top: 0; bottom: 0;
    z-index: 300;
    width: var(--sidebar-w);
    transform: translateX(-100%);
  }
  .eg-sidebar.is-open { transform: translateX(0); }
  .eg-mobilebar { display: flex; }
  .eg-scrim { display: block; }
  .eg-topbar { position: static; padding: 14px 16px; }
  .eg-content { padding: 16px; }
  .eg-hero__grid { grid-template-columns: 1fr; }
  .eg-topbar-meta { flex-wrap: wrap; }
}
@media (max-width: 430px) {
  .eg-metric-grid { grid-template-columns: 1fr; }
  .eg-alert-grid { grid-template-columns: 1fr; }
}

/* ----- Focus accessible --------------------------------------------- */
.eg a:focus-visible, .eg-btn:focus-visible, summary:focus-visible {
  outline: 2px solid var(--eg-green);
  outline-offset: 2px;
  border-radius: 4px;
}
.eg-check input:focus-visible { outline: 2px solid var(--eg-green); outline-offset: 2px; }
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
