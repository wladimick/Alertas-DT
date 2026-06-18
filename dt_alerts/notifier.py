from __future__ import annotations

import html
import json
import smtplib
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from . import db
from .config import Settings


# Paleta External Group para el email (inline styles para compatibilidad con clientes).
EG_DEEP = "#0A2231"
EG_PRIMARY = "#243743"
EG_CTA = "#167A5F"          # verde con buen contraste sobre texto blanco
EG_TEXT = "#0A2231"
EG_MUTED = "#3C4A52"
EG_BG = "#F6F8F9"
EG_BORDER = "#E2E8EC"
EG_LOGO_LIGHT = "https://externalgroup.cl/sitioweb/wp-content/uploads/2022/07/external-group-blanco.png"

SUBJECT_MAX = 120


# --------------------------------------------------------------------------
# Composición de contenido (reutilizable)
# --------------------------------------------------------------------------
def truncate_subject(title: str, max_chars: int = SUBJECT_MAX) -> str:
    title = " ".join(str(title or "").split())
    if len(title) <= max_chars:
        return title
    return title[: max_chars - 1].rstrip() + "…"


def subject_for(alert: dict[str, Any]) -> str:
    return f"Nueva normativa DT: {truncate_subject(alert.get('title') or 'documento')}"


def _alert_lists(alert: dict[str, Any]) -> tuple[list[str], list[str]]:
    key_points = json.loads(alert.get("key_points_json") or "[]")
    impacts = json.loads(alert.get("practical_impacts_json") or "[]")
    return key_points, impacts


def render_alert_email_text(alert: dict[str, Any]) -> str:
    """Versión texto plano del email de alerta."""
    key_points, impacts = _alert_lists(alert)
    lines = [
        "EXTERNAL GROUP · ALERTAS DT",
        "Nueva publicación de la Dirección del Trabajo",
        "",
        alert.get("title") or "Documento DT",
        f"Categoría: {alert.get('category') or 'normativa'}",
        f"Fecha: {alert.get('publication_date') or 'sin fecha informada'}",
        f"Relevancia: {alert.get('relevance') or 'media'}",
        "",
        alert.get("summary") or "",
    ]
    if key_points:
        lines += ["", "Puntos clave:", *[f"- {p}" for p in key_points]]
    if impacts:
        lines += ["", "Impacto práctico para contadores y empresas:", *[f"- {i}" for i in impacts]]
    lines += [
        "",
        f"Ver documento oficial: {alert.get('canonical_url') or ''}",
        "",
        "Este resumen es informativo y no reemplaza la lectura del documento oficial "
        "de la Dirección del Trabajo ni asesoría profesional.",
        "",
        "External Group · Servicios especializados de gestión y tecnología.",
    ]
    return "\n".join(line for line in lines if line is not None)


def _email_list_html(title: str, items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(
        f'<li style="margin:0 0 6px;">{html.escape(str(item))}</li>' for item in items
    )
    return (
        f'<h2 style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        f'color:{EG_TEXT};margin:22px 0 8px;">{html.escape(title)}</h2>'
        f'<ul style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:{EG_MUTED};'
        f'line-height:1.55;margin:0 0 0 18px;padding:0;">{lis}</ul>'
    )


def render_alert_email_html(alert: dict[str, Any]) -> str:
    """
    Versión HTML del email de alerta, con estética External Group.
    Usa tablas + estilos inline para compatibilidad. El logo tiene fallback textual.
    """
    key_points, impacts = _alert_lists(alert)
    title = html.escape(alert.get("title") or "Documento DT")
    category = html.escape(alert.get("category") or "Normativa")
    pub_date = html.escape(alert.get("publication_date") or "sin fecha informada")
    relevance = html.escape(alert.get("relevance") or "media")
    summary = html.escape(alert.get("summary") or "")
    url = html.escape(alert.get("canonical_url") or "#")
    preheader = html.escape(truncate_subject(alert.get("summary") or title, 120))

    return f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:{EG_BG};">
  <span style="display:none!important;visibility:hidden;opacity:0;height:0;width:0;overflow:hidden;">{preheader}</span>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{EG_BG};">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;">
        <!-- Header marca -->
        <tr><td style="background:{EG_DEEP};border-radius:14px 14px 0 0;padding:22px 28px;">
          <img src="{EG_LOGO_LIGHT}" alt="External Group" height="30" style="height:30px;display:block;border:0;">
          <div style="font-family:Arial,Helvetica,sans-serif;color:#9FE7CF;font-size:11px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;margin-top:8px;">Alertas DT</div>
        </td></tr>
        <!-- Cuerpo -->
        <tr><td style="background:#ffffff;border:1px solid {EG_BORDER};border-top:0;border-radius:0 0 14px 14px;padding:28px;">
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;color:{EG_CTA};margin:0 0 10px;">Nueva publicación de la Dirección del Trabajo</p>
          <h1 style="font-family:Arial,Helvetica,sans-serif;font-size:22px;line-height:1.25;color:{EG_TEXT};margin:0 0 12px;">{title}</h1>
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:{EG_MUTED};margin:0 0 18px;">
            {category} &nbsp;·&nbsp; {pub_date} &nbsp;·&nbsp; Relevancia: {relevance}
          </p>
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:{EG_TEXT};margin:0 0 8px;">{summary}</p>
          {_email_list_html("Puntos clave", key_points)}
          {_email_list_html("Impacto práctico para contadores y empresas", impacts)}
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:26px 0 8px;"><tr><td style="background:{EG_CTA};border-radius:999px;">
            <a href="{url}" style="display:inline-block;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#ffffff;text-decoration:none;padding:13px 26px;">Ver documento oficial</a>
          </td></tr></table>
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:{EG_MUTED};margin:8px 0 0;word-break:break-all;">
            Enlace directo: <a href="{url}" style="color:{EG_CTA};">{url}</a>
          </p>
          <hr style="border:0;border-top:1px solid {EG_BORDER};margin:24px 0;">
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;color:{EG_MUTED};margin:0;">
            Este resumen es informativo y no reemplaza la lectura del documento oficial de la Dirección del Trabajo ni asesoría profesional.
          </p>
        </td></tr>
        <!-- Footer -->
        <tr><td style="padding:18px 28px;">
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:{EG_MUTED};margin:0;">External Group · Servicios especializados de gestión y tecnología.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# --------------------------------------------------------------------------
# Capa de envío con proveedor seleccionable (etapa 9)
# --------------------------------------------------------------------------
def _from_header(settings: Settings) -> str:
    if settings.email_from_name:
        return formataddr((settings.email_from_name, settings.email_from))
    return settings.email_from


def send_email(
    settings: Settings, *, to: str, subject: str, html_body: str, text_body: str
) -> dict[str, Any]:
    """
    Envía (o simula) un email según EMAIL_PROVIDER.
    Nunca lanza por falta de credenciales: devuelve un estado descriptivo.
    Estados: sent | simulated | skipped_missing_credentials | failed.
    """
    provider = (settings.email_provider or "console").lower()

    if provider == "console":
        print(f"[email:console] -> {to} | {subject}")
        return {
            "ok": True, "provider": "console", "status": "simulated",
            "provider_message_id": None, "error": None,
            "message": "Email simulado correctamente (modo console).",
        }

    if provider == "sendgrid":
        if not settings.sendgrid_api_key:
            return {
                "ok": False, "provider": "sendgrid", "status": "skipped_missing_credentials",
                "provider_message_id": None, "error": "Falta SENDGRID_API_KEY.",
                "message": "Email no enviado: faltan credenciales transaccionales. Se registró como simulación.",
            }
        return _send_sendgrid(settings, to=to, subject=subject, html_body=html_body, text_body=text_body)

    if provider == "resend":
        if not settings.resend_api_key:
            return {
                "ok": False, "provider": "resend", "status": "skipped_missing_credentials",
                "provider_message_id": None, "error": "Falta RESEND_API_KEY.",
                "message": "Email no enviado: faltan credenciales transaccionales. Se registró como simulación.",
            }
        return _send_resend(settings, to=to, subject=subject, html_body=html_body, text_body=text_body)

    if provider == "smtp":
        if not settings.smtp_host:
            return {
                "ok": False, "provider": "smtp", "status": "skipped_missing_credentials",
                "provider_message_id": None, "error": "Falta SMTP_HOST.",
                "message": "Email no enviado: faltan credenciales SMTP. Se registró como simulación.",
            }
        return _send_smtp(settings, to=to, subject=subject, html_body=html_body, text_body=text_body)

    # Proveedor desconocido: simular para no romper.
    print(f"[email:{provider}?] simulado -> {to} | {subject}")
    return {
        "ok": True, "provider": provider, "status": "simulated",
        "provider_message_id": None, "error": f"Proveedor '{provider}' no reconocido.",
        "message": "Email simulado (proveedor no reconocido).",
    }


def _send_sendgrid(
    settings: Settings, *, to: str, subject: str, html_body: str, text_body: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": settings.email_from, "name": settings.email_from_name or None},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }
    if settings.email_reply_to:
        payload["reply_to"] = {"email": settings.email_reply_to}
    request = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.sendgrid_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            message_id = response.headers.get("X-Message-Id")
        return {
            "ok": True, "provider": "sendgrid", "status": "sent",
            "provider_message_id": message_id, "error": None,
            "message": "Email enviado con SendGrid.",
        }
    except Exception as exc:
        return {
            "ok": False, "provider": "sendgrid", "status": "failed",
            "provider_message_id": None, "error": str(exc),
            "message": f"Error al enviar con SendGrid: {exc}",
        }


def _send_resend(
    settings: Settings, *, to: str, subject: str, html_body: str, text_body: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from": _from_header(settings),
        "to": [to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        return {
            "ok": True, "provider": "resend", "status": "sent",
            "provider_message_id": body.get("id"), "error": None,
            "message": "Email enviado con Resend.",
        }
    except Exception as exc:
        return {
            "ok": False, "provider": "resend", "status": "failed",
            "provider_message_id": None, "error": str(exc),
            "message": f"Error al enviar con Resend: {exc}",
        }


def _send_smtp(
    settings: Settings, *, to: str, subject: str, html_body: str, text_body: str
) -> dict[str, Any]:
    message = EmailMessage()
    message["From"] = _from_header(settings)
    message["To"] = to
    message["Subject"] = subject
    if settings.email_reply_to:
        message["Reply-To"] = settings.email_reply_to
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return {
            "ok": True, "provider": "smtp", "status": "sent",
            "provider_message_id": None, "error": None, "message": "Email enviado por SMTP.",
        }
    except Exception as exc:
        return {
            "ok": False, "provider": "smtp", "status": "failed",
            "provider_message_id": None, "error": str(exc),
            "message": f"Error al enviar por SMTP: {exc}",
        }


# --------------------------------------------------------------------------
# Helpers de alto nivel
# --------------------------------------------------------------------------
def send_alert_email(
    subscriber: dict[str, Any], alert: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    return send_email(
        settings,
        to=subscriber["email"],
        subject=subject_for(alert),
        html_body=render_alert_email_html(alert),
        text_body=render_alert_email_text(alert),
    )


def send_test_alert_email(
    to_email: str, alert: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    return send_email(
        settings,
        to=to_email,
        subject=f"[PRUEBA] {subject_for(alert)}",
        html_body=render_alert_email_html(alert),
        text_body=render_alert_email_text(alert),
    )


def dispatch_alert(conn, alert_id: int, settings: Settings) -> int:
    """
    Envía la alerta a los suscriptores activos por email.
    No envía a pausados. No reenvía a quien ya tiene un envío registrado como sent/simulated.
    WhatsApp queda reservado para fase futura: no se notifica por ese canal.
    """
    alert = db.get_alert_with_document(conn, alert_id)
    if not alert:
        raise ValueError("Alerta no encontrada.")

    subscribers = db.active_subscribers(conn)
    sent_or_simulated = 0
    failures = 0

    existing = {
        (row["subscriber_id"]): row["status"]
        for row in conn.execute(
            "SELECT subscriber_id, status FROM deliveries WHERE alert_id = ? AND channel = 'email'",
            (alert_id,),
        ).fetchall()
    }

    for subscriber in subscribers:
        if not subscriber["notify_email"]:
            continue
        # Evitar reenvío si ya se entregó/simuló a este suscriptor.
        if existing.get(subscriber["id"]) in {"sent", "simulated"}:
            continue

        result = send_alert_email(subscriber, alert, settings)
        db.record_delivery(
            conn,
            alert_id=alert_id,
            subscriber_id=subscriber["id"],
            channel="email",
            status=result["status"],
            provider_message_id=result.get("provider_message_id"),
            error=result.get("error"),
        )
        if result["status"] in {"sent", "simulated"}:
            sent_or_simulated += 1
        elif result["status"] in {"failed"}:
            failures += 1

    if sent_or_simulated:
        db.set_alert_status(conn, alert_id, "sent")
    elif failures:
        db.set_alert_status(conn, alert_id, "error")
    return sent_or_simulated


# --------------------------------------------------------------------------
# WhatsApp — reservado para fase futura (no se invoca en el MVP por email).
# Se mantiene la función para compatibilidad; está desactivada por defecto.
# --------------------------------------------------------------------------
def send_whatsapp_alert(
    subscriber: dict[str, Any], alert: dict[str, Any], settings: Settings
) -> dict[str, str | None]:
    # WhatsApp reservado para fase futura.
    return {
        "status": "simulated",
        "provider_message_id": None,
        "error": "WhatsApp reservado para fase futura; no se envía en el MVP.",
    }
