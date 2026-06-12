from __future__ import annotations

import html
import json
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Any

from . import db
from .config import Settings


def dispatch_alert(conn, alert_id: int, settings: Settings) -> int:
    alert = db.get_alert_with_document(conn, alert_id)
    if not alert:
        raise ValueError("Alerta no encontrada.")

    subscribers = db.active_subscribers(conn)
    sent_or_simulated = 0
    failures = 0

    for subscriber in subscribers:
        if subscriber["notify_email"]:
            result = send_email_alert(subscriber, alert, settings)
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
            elif result["status"] == "failed":
                failures += 1

        if subscriber["notify_whatsapp"] and subscriber["whatsapp_opt_in"]:
            result = send_whatsapp_alert(subscriber, alert, settings)
            db.record_delivery(
                conn,
                alert_id=alert_id,
                subscriber_id=subscriber["id"],
                channel="whatsapp",
                status=result["status"],
                provider_message_id=result.get("provider_message_id"),
                error=result.get("error"),
            )
            if result["status"] in {"sent", "simulated"}:
                sent_or_simulated += 1
            elif result["status"] == "failed":
                failures += 1

    if sent_or_simulated:
        db.set_alert_status(conn, alert_id, "sent")
    elif failures:
        db.set_alert_status(conn, alert_id, "failed")
    return sent_or_simulated


def send_email_alert(
    subscriber: dict[str, Any], alert: dict[str, Any], settings: Settings
) -> dict[str, str | None]:
    subject = f"Nueva normativa DT: {alert['title']}"
    text_body = build_text_email(alert)
    html_body = build_html_email(alert)

    if settings.resend_api_key:
        return send_resend(
            settings,
            to=subscriber["email"],
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
    if settings.smtp_host:
        return send_smtp(
            settings,
            to=subscriber["email"],
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
    return {
        "status": "simulated",
        "provider_message_id": None,
        "error": "Sin RESEND_API_KEY ni SMTP_HOST; envío simulado.",
    }


def send_resend(
    settings: Settings,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> dict[str, str | None]:
    payload = {
        "from": settings.email_from,
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
            "status": "sent",
            "provider_message_id": body.get("id"),
            "error": None,
        }
    except Exception as exc:
        return {"status": "failed", "provider_message_id": None, "error": str(exc)}


def send_smtp(
    settings: Settings,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> dict[str, str | None]:
    message = EmailMessage()
    message["From"] = settings.email_from
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
        return {"status": "sent", "provider_message_id": None, "error": None}
    except Exception as exc:
        return {"status": "failed", "provider_message_id": None, "error": str(exc)}


def send_whatsapp_alert(
    subscriber: dict[str, Any], alert: dict[str, Any], settings: Settings
) -> dict[str, str | None]:
    if (
        not settings.whatsapp_enabled
        or not settings.whatsapp_phone_number_id
        or not settings.whatsapp_access_token
    ):
        return {
            "status": "simulated",
            "provider_message_id": None,
            "error": "WhatsApp desactivado o sin credenciales; envío simulado.",
        }

    payload = {
        "messaging_product": "whatsapp",
        "to": subscriber["whatsapp"],
        "type": "template",
        "template": {
            "name": settings.whatsapp_template_name,
            "language": {"code": settings.whatsapp_language},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": alert["title"][:80]},
                        {"type": "text", "text": alert["summary"][:180]},
                        {"type": "text", "text": alert["canonical_url"]},
                    ],
                }
            ],
        },
    }
    endpoint = (
        "https://graph.facebook.com/v20.0/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        message_id = None
        if body.get("messages"):
            message_id = body["messages"][0].get("id")
        return {"status": "sent", "provider_message_id": message_id, "error": None}
    except Exception as exc:
        return {"status": "failed", "provider_message_id": None, "error": str(exc)}


def build_text_email(alert: dict[str, Any]) -> str:
    key_points = json.loads(alert.get("key_points_json") or "[]")
    impacts = json.loads(alert.get("practical_impacts_json") or "[]")
    lines = [
        alert["title"],
        f"Categoría: {alert['category']}",
        f"Fecha: {alert.get('publication_date') or 'sin fecha informada'}",
        f"Relevancia: {alert['relevance']}",
        "",
        alert["summary"],
        "",
        "Puntos clave:",
        *[f"- {item}" for item in key_points],
        "",
        "Impactos prácticos:",
        *[f"- {item}" for item in impacts],
        "",
        f"Documento oficial: {alert['canonical_url']}",
        "",
        "Aviso: Este resumen es informativo y no reemplaza la revisión profesional del documento oficial.",
    ]
    return "\n".join(lines)


def build_html_email(alert: dict[str, Any]) -> str:
    key_points = json.loads(alert.get("key_points_json") or "[]")
    impacts = json.loads(alert.get("practical_impacts_json") or "[]")
    title = html.escape(alert["title"])
    url = html.escape(alert["canonical_url"])
    return f"""
<!doctype html>
<html lang="es">
<body style="margin:0;background:#f5f7fb;font-family:Arial,sans-serif;color:#1f2937;">
  <main style="max-width:680px;margin:0 auto;padding:24px;">
    <section style="background:#ffffff;border:1px solid #d9e2ef;border-radius:8px;padding:24px;">
      <p style="margin:0 0 8px;color:#526173;font-size:13px;">Dirección del Trabajo · {html.escape(alert['category'])}</p>
      <h1 style="font-size:22px;line-height:1.25;margin:0 0 12px;">{title}</h1>
      <p style="margin:0 0 16px;color:#526173;">Fecha: {html.escape(alert.get('publication_date') or 'sin fecha informada')} · Relevancia: {html.escape(alert['relevance'])}</p>
      <p style="font-size:16px;line-height:1.55;">{html.escape(alert['summary'])}</p>
      {render_email_list("Puntos clave", key_points)}
      {render_email_list("Impactos prácticos", impacts)}
      <p style="margin-top:24px;">
        <a href="{url}" style="background:#0b5cab;color:#ffffff;text-decoration:none;padding:12px 16px;border-radius:6px;display:inline-block;">Ver documento oficial</a>
      </p>
      <p style="font-size:12px;line-height:1.5;color:#667085;margin-top:24px;">
        Este resumen es informativo y no reemplaza la revisión profesional del documento oficial.
      </p>
    </section>
  </main>
</body>
</html>
""".strip()


def render_email_list(title: str, items: list[str]) -> str:
    lis = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f"""
<h2 style="font-size:16px;margin:22px 0 8px;">{html.escape(title)}</h2>
<ul style="line-height:1.55;margin:0 0 0 20px;padding:0;">{lis}</ul>
""".strip()
