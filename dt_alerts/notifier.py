from __future__ import annotations

import base64
import html
import json
import smtplib
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from . import db
from .config import Settings


# Paleta External Group para el email (inline styles para compatibilidad).
EG_DEEP = "#0A2231"
EG_PRIMARY = "#243743"
EG_CTA = "#167A5F"
EG_TEXT = "#0A2231"
EG_MUTED = "#3C4A52"
EG_BG = "#F6F8F9"
EG_BORDER = "#E2E8EC"
EG_LOGO_LIGHT = "https://externalgroup.cl/sitioweb/wp-content/uploads/2022/07/external-group-blanco.png"

SUBJECT_MAX = 120

AI_DISCLAIMER = (
    "Contenido generado con apoyo de IA. "
    "Debe ser revisado antes de su envío. "
    "Este resumen es informativo y no reemplaza la lectura del documento oficial "
    "ni asesoría profesional."
)


# --------------------------------------------------------------------------
# Helpers de asunto y contenido
# --------------------------------------------------------------------------

def truncate_subject(title: str, max_chars: int = SUBJECT_MAX) -> str:
    title = " ".join(str(title or "").split())
    if len(title) <= max_chars:
        return title
    return title[: max_chars - 1].rstrip() + "…"


def subject_for(alert: dict[str, Any]) -> str:
    """
    Priority:
    1. AI-generated email_subject (ai_email_subject from joined query)
    2. Fallback: "Nueva normativa DT: {title}"
    """
    ai_subject = (alert.get("ai_email_subject") or "").strip()
    if ai_subject:
        return truncate_subject(ai_subject)
    return f"Nueva normativa DT: {truncate_subject(alert.get('title') or 'documento')}"


def _parse_json_field(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def _alert_lists(alert: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (key_points, practical_impacts_as_strings) from basic alert fields."""
    key_points = _parse_json_field(alert.get("key_points_json"), [])
    impacts_raw = _parse_json_field(alert.get("practical_impacts_json"), [])
    if impacts_raw and isinstance(impacts_raw[0], dict):
        impacts = [i.get("title") or i.get("description") or "" for i in impacts_raw]
    else:
        impacts = [str(i) for i in impacts_raw]
    return key_points, impacts


def _ai_content(alert: dict[str, Any]) -> dict[str, Any]:
    """Extract AI-generated content fields from alert dict (from LEFT JOIN)."""
    if not alert.get("ai_status"):
        return {}
    kp = _parse_json_field(alert.get("ai_key_points_json"), [])
    impacts_raw = _parse_json_field(alert.get("ai_practical_impacts_json"), [])
    rec = _parse_json_field(alert.get("ai_recommended_actions_json"), [])
    tags = _parse_json_field(alert.get("ai_tags_json"), [])
    return {
        "email_summary": alert.get("ai_email_summary") or "",
        "key_points": kp,
        "practical_impacts": impacts_raw,
        "recommended_actions": rec,
        "tags": tags,
        "legal_disclaimer": alert.get("ai_legal_disclaimer") or AI_DISCLAIMER,
        "status": alert.get("ai_status") or "",
        "content_quality": alert.get("ai_content_quality") or "limited",
    }


# --------------------------------------------------------------------------
# Email text (plain)
# --------------------------------------------------------------------------

def render_alert_email_text(alert: dict[str, Any]) -> str:
    ai = _ai_content(alert)
    key_points, impacts = _alert_lists(alert)

    # Use AI content when available
    summary_text = ai.get("email_summary") or alert.get("summary") or ""
    if ai.get("key_points"):
        key_points = [
            p if isinstance(p, str) else (p.get("title") or "") for p in ai["key_points"]
        ]
    if ai.get("practical_impacts"):
        raw_imp = ai["practical_impacts"]
        impacts = [
            i.get("title") or i.get("description") or "" if isinstance(i, dict) else str(i)
            for i in raw_imp
        ]

    rec_actions = ai.get("recommended_actions") or []
    tags = ai.get("tags") or []
    disclaimer = ai.get("legal_disclaimer") or AI_DISCLAIMER

    lines = [
        "EXTERNAL GROUP · ALERTAS DT",
        "Nueva publicación de la Dirección del Trabajo",
        "",
        alert.get("title") or "Documento DT",
        f"Categoría: {alert.get('category') or 'normativa'}",
        f"Fecha: {alert.get('publication_date') or 'sin fecha informada'}",
        f"Relevancia: {alert.get('relevance') or 'media'}",
        "",
        summary_text,
    ]
    if key_points:
        lines += ["", "Puntos clave:", *[f"- {p}" for p in key_points if p]]
    if impacts:
        lines += ["", "Impacto práctico para contadores y empresas:", *[f"- {i}" for i in impacts if i]]
    if rec_actions:
        lines += ["", "Acciones recomendadas:", *[f"- {a}" for a in rec_actions if a]]
    if tags:
        lines += ["", f"Temas: {', '.join(tags)}"]
    lines += [
        "",
        f"Ver documento oficial: {alert.get('canonical_url') or ''}",
        "",
        disclaimer,
        "",
        "External Group · Servicios especializados de gestión y tecnología.",
    ]
    return "\n".join(line for line in lines if line is not None)


# --------------------------------------------------------------------------
# Email HTML
# --------------------------------------------------------------------------

def _email_list_html(title: str, items: list[Any], *, is_impacts: bool = False) -> str:
    if not items:
        return ""
    if is_impacts:
        lis = ""
        for item in items:
            if isinstance(item, dict):
                t = html.escape(str(item.get("title") or ""))
                d = html.escape(str(item.get("description") or ""))
                if d:
                    lis += f'<li style="margin:0 0 10px;"><strong>{t}</strong><br>{d}</li>'
                else:
                    lis += f'<li style="margin:0 0 6px;">{t}</li>'
            else:
                lis += f'<li style="margin:0 0 6px;">{html.escape(str(item))}</li>'
    else:
        lis = "".join(
            f'<li style="margin:0 0 6px;">{html.escape(str(item))}</li>'
            for item in items
            if item
        )
    if not lis:
        return ""
    return (
        f'<h2 style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        f'color:{EG_TEXT};margin:22px 0 8px;">{html.escape(title)}</h2>'
        f'<ul style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:{EG_MUTED};'
        f'line-height:1.55;margin:0 0 0 18px;padding:0;">{lis}</ul>'
    )


def render_alert_email_html(alert: dict[str, Any]) -> str:
    """Email HTML with AI content when available."""
    ai = _ai_content(alert)
    key_points_basic, impacts_basic = _alert_lists(alert)

    summary_text = ai.get("email_summary") or alert.get("summary") or ""
    key_points = ai.get("key_points") or key_points_basic
    impacts = ai.get("practical_impacts") or [{"title": i} for i in impacts_basic]
    rec_actions = ai.get("recommended_actions") or []
    tags = ai.get("tags") or []
    disclaimer = ai.get("legal_disclaimer") or AI_DISCLAIMER

    title = html.escape(alert.get("title") or "Documento DT")
    category = html.escape(alert.get("category") or "Normativa")
    pub_date = html.escape(alert.get("publication_date") or "sin fecha informada")
    relevance = html.escape(alert.get("relevance") or "media")
    summary_esc = html.escape(summary_text)
    url = html.escape(alert.get("canonical_url") or "#")
    preheader = html.escape(truncate_subject(summary_text or title, 120))

    ai_badge = ""
    if ai.get("status") == "success":
        ai_badge = (
            f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
            f'color:#167A5F;margin:0 0 16px;">Resumen generado con IA</p>'
        )

    tags_html = ""
    if tags:
        tag_spans = "".join(
            f'<span style="display:inline-block;background:#EEF3F5;color:#3C4A52;'
            f'font-size:11px;padding:3px 8px;border-radius:4px;margin:2px;">'
            f'{html.escape(str(t))}</span>'
            for t in tags
        )
        tags_html = (
            f'<div style="margin:20px 0 8px;">'
            f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            f'color:{EG_MUTED};margin:0 0 6px;font-weight:bold;">Temas</p>'
            f'{tag_spans}</div>'
        )

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
          {ai_badge}
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;color:{EG_CTA};margin:0 0 10px;">Nueva publicación de la Dirección del Trabajo</p>
          <h1 style="font-family:Arial,Helvetica,sans-serif;font-size:22px;line-height:1.25;color:{EG_TEXT};margin:0 0 12px;">{title}</h1>
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:{EG_MUTED};margin:0 0 18px;">
            {category} &nbsp;·&nbsp; {pub_date} &nbsp;·&nbsp; Relevancia: {relevance}
          </p>
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:{EG_TEXT};margin:0 0 8px;">{summary_esc}</p>
          {_email_list_html("Puntos clave", key_points)}
          {_email_list_html("Impacto práctico para contadores y empresas", impacts, is_impacts=True)}
          {_email_list_html("Acciones recomendadas", rec_actions)}
          {tags_html}
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:26px 0 8px;"><tr><td style="background:{EG_CTA};border-radius:999px;">
            <a href="{url}" style="display:inline-block;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;color:#ffffff;text-decoration:none;padding:13px 26px;">Ver documento oficial</a>
          </td></tr></table>
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:{EG_MUTED};margin:8px 0 0;word-break:break-all;">
            Enlace directo: <a href="{url}" style="color:{EG_CTA};">{url}</a>
          </p>
          <hr style="border:0;border-top:1px solid {EG_BORDER};margin:24px 0;">
          <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;color:{EG_MUTED};margin:0;">
            {html.escape(disclaimer)}
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
# Generación de adjuntos HTML
# --------------------------------------------------------------------------

def _attachment_css() -> str:
    return (
        "body{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;"
        "padding:20px 24px;color:#0A2231;line-height:1.6;}"
        "h1{color:#0A2231;font-size:1.6rem;border-bottom:2px solid #29B78D;padding-bottom:8px;}"
        "h2{color:#167A5F;font-size:1.1rem;margin-top:24px;}"
        "h3{color:#243743;font-size:1rem;}"
        "p,li{color:#3C4A52;font-size:14px;}"
        "ul{padding-left:18px;}"
        ".meta{font-size:12px;color:#8EA1AA;margin-bottom:20px;}"
        ".disclaimer{font-size:12px;color:#8EA1AA;margin-top:40px;"
        "border-top:1px solid #E2E8EC;padding-top:12px;}"
        ".footer{font-size:11px;color:#C7D1D6;margin-top:16px;}"
    )


def generate_executive_summary_html(document_id: int, alert: dict[str, Any]) -> str:
    """Generate executive summary HTML for attachment."""
    raw = alert.get("ai_executive_summary") or ""
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            executive = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            executive = {"title": "Resumen ejecutivo", "body": raw}
    elif isinstance(raw, dict):
        executive = raw
    else:
        executive = {"title": "Resumen ejecutivo", "body": str(raw)}

    title = html.escape(executive.get("title") or "Resumen ejecutivo")
    body_text = html.escape(executive.get("body") or alert.get("summary") or "")
    doc_title = html.escape(alert.get("title") or "Documento DT")
    category = html.escape(alert.get("category") or "")
    pub_date = html.escape(alert.get("publication_date") or "sin fecha")
    url = html.escape(alert.get("canonical_url") or "#")
    disclaimer = html.escape(
        alert.get("ai_legal_disclaimer")
        or "Este resumen es informativo y no reemplaza la lectura del documento oficial ni asesoría profesional."
    )

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{_attachment_css()}</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">
  <strong>{doc_title}</strong><br>
  {category} · {pub_date}<br>
  <a href="{url}">{url}</a>
</div>
<p>{body_text}</p>
<div class="disclaimer">{disclaimer}</div>
<div class="footer">External Group · Alertas DT · Documento #{document_id}</div>
</body>
</html>"""


def generate_detailed_summary_html(document_id: int, alert: dict[str, Any]) -> str:
    """Generate detailed summary HTML for attachment."""
    raw = alert.get("ai_detailed_summary_json") or ""
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            detailed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            detailed = {"title": "Resumen detallado", "sections": [{"heading": "", "body": raw}]}
    elif isinstance(raw, dict):
        detailed = raw
    else:
        detailed = {"title": "Resumen detallado", "sections": []}

    title = html.escape(detailed.get("title") or "Resumen detallado")
    doc_title = html.escape(alert.get("title") or "Documento DT")
    category = html.escape(alert.get("category") or "")
    pub_date = html.escape(alert.get("publication_date") or "sin fecha")
    url = html.escape(alert.get("canonical_url") or "#")
    disclaimer = html.escape(
        alert.get("ai_legal_disclaimer")
        or "Este resumen es informativo y no reemplaza la lectura del documento oficial ni asesoría profesional."
    )

    sections_html = ""
    for section in detailed.get("sections") or []:
        h_text = html.escape(str(section.get("heading") or ""))
        b_text = html.escape(str(section.get("body") or ""))
        if h_text:
            sections_html += f"<h2>{h_text}</h2>"
        if b_text:
            sections_html += f"<p>{b_text}</p>"

    if not sections_html:
        fallback = html.escape(alert.get("summary") or "Sin contenido disponible.")
        sections_html = f"<p>{fallback}</p>"

    # Also include key points and impacts if available
    kp_raw = _parse_json_field(alert.get("ai_key_points_json"), [])
    if kp_raw:
        lis = "".join(f"<li>{html.escape(str(p))}</li>" for p in kp_raw if p)
        sections_html += f"<h2>Puntos clave</h2><ul>{lis}</ul>"

    rec_raw = _parse_json_field(alert.get("ai_recommended_actions_json"), [])
    if rec_raw:
        lis = "".join(f"<li>{html.escape(str(a))}</li>" for a in rec_raw if a)
        sections_html += f"<h2>Acciones recomendadas</h2><ul>{lis}</ul>"

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{_attachment_css()}</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">
  <strong>{doc_title}</strong><br>
  {category} · {pub_date}<br>
  <a href="{url}">{url}</a>
</div>
{sections_html}
<div class="disclaimer">{disclaimer}</div>
<div class="footer">External Group · Alertas DT · Documento #{document_id}</div>
</body>
</html>"""


def _parse_json_field(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Capa de envío (proveedor seleccionable)
# --------------------------------------------------------------------------

def _from_header(settings: Settings) -> str:
    if settings.email_from_name:
        return formataddr((settings.email_from_name, settings.email_from))
    return settings.email_from


def send_email(
    settings: Settings,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Send (or simulate) an email. Never raises on missing credentials.
    States: sent | simulated | skipped_missing_credentials | failed.
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
                "message": "Email no enviado: faltan credenciales. Se registró como simulación.",
            }
        return _send_sendgrid(
            settings, to=to, subject=subject,
            html_body=html_body, text_body=text_body,
            attachments=attachments,
        )

    if provider == "resend":
        if not settings.resend_api_key:
            return {
                "ok": False, "provider": "resend", "status": "skipped_missing_credentials",
                "provider_message_id": None, "error": "Falta RESEND_API_KEY.",
                "message": "Email no enviado: faltan credenciales. Se registró como simulación.",
            }
        return _send_resend(settings, to=to, subject=subject, html_body=html_body, text_body=text_body)

    if provider == "smtp":
        if not settings.smtp_host:
            return {
                "ok": False, "provider": "smtp", "status": "skipped_missing_credentials",
                "provider_message_id": None, "error": "Falta SMTP_HOST.",
                "message": "Email no enviado: faltan credenciales SMTP. Se registró como simulación.",
            }
        return _send_smtp(
            settings, to=to, subject=subject,
            html_body=html_body, text_body=text_body,
            attachments=attachments,
        )

    print(f"[email:{provider}?] simulado -> {to} | {subject}")
    return {
        "ok": True, "provider": provider, "status": "simulated",
        "provider_message_id": None, "error": f"Proveedor '{provider}' no reconocido.",
        "message": "Email simulado (proveedor no reconocido).",
    }


def _send_sendgrid(
    settings: Settings,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    attachments: list[dict[str, Any]] | None = None,
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
    if attachments:
        payload["attachments"] = []
        for att in attachments:
            content_bytes = att["content"]
            if isinstance(content_bytes, str):
                content_bytes = content_bytes.encode("utf-8")
            payload["attachments"].append({
                "content": base64.b64encode(content_bytes).decode("ascii"),
                "type": att.get("type", "text/html; charset=utf-8"),
                "filename": att["filename"],
                "disposition": "attachment",
            })
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
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
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
    settings: Settings,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
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
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
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
    settings: Settings,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message = EmailMessage()
    message["From"] = _from_header(settings)
    message["To"] = to
    message["Subject"] = subject
    if settings.email_reply_to:
        message["Reply-To"] = settings.email_reply_to
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    if attachments:
        for att in attachments:
            content = att["content"]
            if isinstance(content, str):
                content = content.encode("utf-8")
            message.add_attachment(
                content,
                maintype="text",
                subtype="html",
                filename=att["filename"],
            )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return {
            "ok": True, "provider": "smtp", "status": "sent",
            "provider_message_id": None, "error": None,
            "message": "Email enviado por SMTP.",
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

def _build_attachments(alert: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """Build HTML attachments for executive and detailed summary."""
    ai_enabled = getattr(settings, "ai_attachments_enabled", True)
    if not ai_enabled:
        return []
    if not alert.get("ai_status"):
        return []
    document_id = alert.get("document_id") or alert.get("id") or 0
    attachments = []
    exec_html = generate_executive_summary_html(document_id, alert)
    attachments.append({
        "content": exec_html,
        "type": "text/html; charset=utf-8",
        "filename": f"resumen_ejecutivo_{document_id}.html",
    })
    detail_html = generate_detailed_summary_html(document_id, alert)
    attachments.append({
        "content": detail_html,
        "type": "text/html; charset=utf-8",
        "filename": f"resumen_detallado_{document_id}.html",
    })
    return attachments


def send_alert_email(
    subscriber: dict[str, Any],
    alert: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    attachments = _build_attachments(alert, settings)
    return send_email(
        settings,
        to=subscriber["email"],
        subject=subject_for(alert),
        html_body=render_alert_email_html(alert),
        text_body=render_alert_email_text(alert),
        attachments=attachments or None,
    )


def send_test_alert_email(
    to_email: str,
    alert: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    return send_email(
        settings,
        to=to_email,
        subject=f"[PRUEBA] {subject_for(alert)}",
        html_body=render_alert_email_html(alert),
        text_body=render_alert_email_text(alert),
    )


def dispatch_alert(conn: Any, alert_id: int, settings: Settings) -> int:
    """
    Sends alert to active subscribers by email.
    Does not re-send to subscribers who already received it.
    Never auto-sends based on AI generation.
    """
    alert = db.get_alert_with_document(conn, alert_id)
    if not alert:
        raise ValueError("Alerta no encontrada.")

    subscribers = db.active_subscribers(conn)
    sent_or_simulated = 0
    failures = 0

    existing = {
        row["subscriber_id"]: row["status"]
        for row in conn.execute(
            "SELECT subscriber_id, status FROM deliveries WHERE alert_id = ? AND channel = 'email'",
            (alert_id,),
        ).fetchall()
    }

    for subscriber in subscribers:
        if not subscriber["notify_email"]:
            continue
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
# WhatsApp — reservado para fase futura
# --------------------------------------------------------------------------

def send_whatsapp_alert(
    subscriber: dict[str, Any],
    alert: dict[str, Any],
    settings: Settings,
) -> dict[str, str | None]:
    return {
        "status": "simulated",
        "provider_message_id": None,
        "error": "WhatsApp reservado para fase futura; no se envía en el MVP.",
    }
