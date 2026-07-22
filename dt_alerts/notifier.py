from __future__ import annotations

import base64
import html
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from . import db, tls
from .config import Settings
from .sources import source_context


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
    2. Fallback: "Nueva normativa {DT|SII}: {title}"
    """
    ai_subject = (alert.get("ai_email_subject") or "").strip()
    if ai_subject:
        return truncate_subject(ai_subject)
    return (
        f"Nueva normativa {source_context(alert)['short']}: "
        f"{truncate_subject(alert.get('title') or 'documento')}"
    )


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

    ctx = source_context(alert)
    lines = [
        "EXTERNAL GROUP · ALERTAS DT + SII",
        f"Nueva publicación de {ctx['institution']}",
        "",
        alert.get("title") or "Documento",
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


# Estilos del email. Solo propiedades que Outlook soporta vía <style>.
# Layout (flex/grid) se maneja con tablas HTML en el código de generación.
_EMAIL_CSS = (
    "* { box-sizing: border-box; margin: 0; padding: 0; }"
    "body { font-family: Arial, sans-serif; background: #F4F7F8; color: #0A2231; }"
    ".email-wrapper { max-width: 640px; margin: 0 auto; background: #F4F7F8; }"
    ".header-logo { height: 24px; border: 0; display: block; }"
    ".header-tag { font-size: 10px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: #29B78D; }"
    ".header-label { font-size: 11px; color: #7ca0b4; font-style: italic; }"
    ".accent-bar { height: 4px; background: #29B78D; }"
    ".doc-card { background: #0A2231; padding: 24px 32px 0; }"
    ".doc-type { font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: #29B78D; margin-bottom: 8px; }"
    ".doc-title { font-size: 26px; font-weight: 700; color: #ffffff; margin-bottom: 10px; line-height: 1.2; }"
    ".doc-meta { font-size: 12px; color: #7ca0b4; margin-top: 4px; }"
    ".relevancia-badge { font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; background: #29B78D; color: #fff; padding: 2px 9px; border-radius: 20px; }"
    ".body { background: #ffffff; padding: 28px 32px 24px; }"
    ".summary-text { font-size: 14px; color: #3C4A52; line-height: 1.75; }"
    ".section { padding: 22px 32px; border-top: 1px solid #eef2f4; background: #ffffff; }"
    ".section-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: #0A2231; margin-bottom: 14px; }"
    ".tags-section { padding: 16px 32px; background: #ffffff; border-top: 1px solid #eef2f4; }"
    ".tag { display: inline-block; font-size: 11px; font-weight: 600; background: #F4F7F8; color: #5a7080; border: 1px solid #dde4e8; padding: 4px 10px; border-radius: 20px; margin: 3px 4px 3px 0; }"
    ".cta-section { background: #0A2231; padding: 20px 32px; text-align: center; }"
    ".cta-btn { display: inline-block; padding: 12px 28px; background: #29B78D; color: #ffffff; text-decoration: none; font-size: 13px; font-weight: 700; border-radius: 6px; letter-spacing: 0.03em; }"
    ".cta-sub { font-size: 11px; color: #7ca0b4; margin-top: 10px; }"
    ".footer { padding: 20px 32px; background: #F4F7F8; border-top: 1px solid #dde4e8; }"
    ".disclaimer { font-size: 11px; color: #8EA1AA; line-height: 1.6; margin-bottom: 12px; }"
    ".footer-brand { font-size: 11px; color: #B0BEC5; }"
    ".footer-brand strong { color: #7ca0b4; }"
)

_ATTACHMENT_CSS = (
    "* { box-sizing: border-box; margin: 0; padding: 0; }"
    "body { font-family: Arial, sans-serif; background: #F4F7F8; color: #0A2231; }"
    ".header { background: #0A2231; padding: 24px 40px; display: flex; align-items: center; justify-content: space-between; }"
    ".header-brand { display: flex; align-items: center; gap: 14px; }"
    ".header-logo { height: 26px; }"
    ".header-divider { width: 1px; height: 26px; background: #29B78D; opacity: 0.5; }"
    ".header-tag { font-size: 11px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: #29B78D; }"
    ".header-badge { font-size: 11px; background: rgba(41,183,141,0.15); color: #29B78D; border: 1px solid rgba(41,183,141,0.3); padding: 4px 10px; border-radius: 20px; font-weight: 600; letter-spacing: 0.04em; }"
    ".accent-bar { height: 4px; background: #29B78D; }"
    ".wrapper { max-width: 720px; margin: 32px auto; padding: 0 24px 48px; }"
    ".doc-card { background: #ffffff; border: 1px solid #dde4e8; border-radius: 10px; overflow: hidden; margin-bottom: 20px; }"
    ".doc-card-header { background: #0A2231; padding: 20px 28px; position: relative; }"
    ".doc-card-header::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 3px; background: #29B78D; }"
    ".doc-card-body { padding: 24px 28px; }"
    ".doc-type { font-size: 11px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #29B78D; margin-bottom: 6px; }"
    ".doc-title { font-size: 22px; font-weight: 700; color: #ffffff; margin-bottom: 8px; }"
    ".doc-meta { font-size: 12px; color: #7ca0b4; display: flex; gap: 16px; flex-wrap: wrap; }"
    ".relevancia-badge { display: inline-block; font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; background: #29B78D; color: #ffffff; padding: 2px 8px; border-radius: 20px; }"
    ".ai-notice { display: flex; align-items: center; gap: 8px; background: #f0faf6; border: 1px solid #b8e8d6; border-radius: 6px; padding: 10px 14px; margin-bottom: 20px; font-size: 12px; color: #0f6e56; font-weight: 500; }"
    ".ai-dot { width: 6px; height: 6px; border-radius: 50%; background: #29B78D; flex-shrink: 0; }"
    ".summary-text { font-size: 14px; color: #3C4A52; line-height: 1.75; }"
    ".section { background: #ffffff; border: 1px solid #dde4e8; border-radius: 10px; padding: 22px 28px; margin-bottom: 14px; }"
    ".section-header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px solid #eef2f4; }"
    ".section-icon { width: 28px; height: 28px; border-radius: 6px; background: #0A2231; display: flex; align-items: center; justify-content: center; font-size: 13px; flex-shrink: 0; }"
    ".section-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #0A2231; }"
    ".section p, .section li { font-size: 14px; color: #3C4A52; line-height: 1.75; }"
    ".section ul { padding-left: 18px; }"
    ".section li { margin-bottom: 6px; }"
    ".plazos-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }"
    ".plazo-item { background: #F4F7F8; border: 1px solid #dde4e8; border-radius: 8px; padding: 14px 16px; }"
    ".plazo-fecha { font-size: 13px; font-weight: 700; color: #29B78D; margin-bottom: 4px; }"
    ".plazo-desc { font-size: 13px; color: #3C4A52; line-height: 1.5; }"
    ".puntos-item { display: flex; gap: 10px; align-items: flex-start; padding: 10px 0; border-bottom: 1px solid #f0f4f6; }"
    ".puntos-item:last-child { border-bottom: none; padding-bottom: 0; }"
    ".punto-dot { width: 6px; height: 6px; border-radius: 50%; background: #29B78D; flex-shrink: 0; margin-top: 8px; }"
    ".punto-text { font-size: 14px; color: #3C4A52; line-height: 1.7; }"
    ".link-btn { display: inline-block; margin-top: 4px; padding: 10px 20px; background: #0A2231; color: #ffffff; text-decoration: none; font-size: 13px; font-weight: 600; border-radius: 6px; letter-spacing: 0.02em; }"
    ".disclaimer { background: #ffffff; border: 1px solid #dde4e8; border-radius: 10px; padding: 16px 28px; font-size: 12px; color: #8EA1AA; line-height: 1.6; margin-bottom: 16px; }"
    ".footer { text-align: center; font-size: 11px; color: #B0BEC5; padding-top: 8px; }"
    ".footer strong { color: #7ca0b4; }"
)


def _build_puntos_html(items: list[Any], css_item: str = "punto", css_dot: str = "punto-dot", css_text: str = "punto-text") -> str:
    """Genera filas de puntos con tabla HTML — compatible con Outlook."""
    parts = []
    for item in items:
        text = html.escape(str(item.get("title") if isinstance(item, dict) else item))
        parts.append(
            '<table width="100%" cellpadding="0" cellspacing="0" border="0"'
            ' style="border-bottom:1px solid #f4f7f8;">'
            '<tr>'
            '<td width="16" style="padding:8px 10px 8px 0;vertical-align:top;">'
            '<div style="width:6px;height:6px;border-radius:50%;background:#29B78D;margin-top:6px;"></div>'
            '</td>'
            f'<td style="font-size:14px;color:#3C4A52;line-height:1.7;padding:8px 0;">{text}</td>'
            '</tr>'
            '</table>'
        )
    return "".join(parts)


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

    title = html.escape(alert.get("title") or "Documento")
    category = html.escape(alert.get("category") or "Normativa")
    pub_date = html.escape(alert.get("publication_date") or "sin fecha informada")
    relevance = html.escape(alert.get("relevance") or "media")
    url = html.escape(alert.get("canonical_url") or "#")
    preheader = html.escape(truncate_subject(summary_text or title, 120))
    summary_esc = html.escape(summary_text)
    disclaimer_esc = html.escape(disclaimer)

    # AI notice — tabla para compatibilidad con Outlook (no flex)
    ai_notice = ""
    if ai.get("status") == "success":
        ai_notice = (
            '<table width="100%" cellpadding="0" cellspacing="0" border="0"'
            ' style="background:#f0faf6;border:1px solid #b8e8d6;border-radius:6px;margin-bottom:20px;">'
            '<tr>'
            '<td width="20" style="padding:9px 0 9px 14px;vertical-align:middle;">'
            '<div style="width:6px;height:6px;border-radius:50%;background:#29B78D;"></div>'
            '</td>'
            '<td style="padding:9px 14px 9px 8px;font-size:12px;color:#0f6e56;font-weight:500;">'
            'Resumen generado con inteligencia artificial · Revisar antes de tomar decisiones'
            '</td>'
            '</tr>'
            '</table>'
        )

    # Puntos clave — usa _build_puntos_html (tabla interna por punto)
    puntos_html = ""
    if key_points:
        puntos_html = (
            '<div class="section">'
            '<div class="section-title">Puntos clave</div>'
            + _build_puntos_html(key_points)
            + '</div>'
        )

    # Impacto grid — tabla 2 columnas, compatible con Outlook
    impacto_html = ""
    if impacts:
        cells: list[str] = []
        for imp in impacts:
            if isinstance(imp, dict):
                lbl = html.escape(str(imp.get("title") or ""))
                txt = html.escape(str(imp.get("description") or imp.get("text") or ""))
            else:
                lbl = html.escape(str(imp))
                txt = ""
            cells.append(
                f'<div style="background:#F4F7F8;border:1px solid #dde4e8;border-radius:8px;'
                f'padding:14px 16px;border-left:3px solid #29B78D;">'
                f'<div style="font-size:11px;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.06em;color:#0A2231;margin-bottom:6px;">{lbl}</div>'
                f'<div style="font-size:13px;color:#3C4A52;line-height:1.6;">{txt}</div>'
                f'</div>'
            )
        # Agrupar en filas de 2 celdas
        rows = ""
        for i in range(0, len(cells), 2):
            left = cells[i]
            right = cells[i + 1] if i + 1 < len(cells) else ""
            pad_top = "0" if i == 0 else "6px"
            rows += (
                f'<tr>'
                f'<td width="50%" style="padding:{pad_top} 6px 0 0;vertical-align:top;">{left}</td>'
                f'<td width="50%" style="padding:{pad_top} 0 0 6px;vertical-align:top;">{right}</td>'
                f'</tr>'
            )
        impacto_html = (
            '<div class="section">'
            '<div class="section-title">Impacto práctico para contadores y empresas</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'
            '</div>'
        )

    # Acciones — tabla para compatibilidad con Outlook (no flex)
    acciones_html = ""
    if rec_actions:
        rows_html = ""
        for i, act in enumerate(rec_actions, 1):
            txt = html.escape(str(act.get("title") if isinstance(act, dict) else act))
            rows_html += (
                '<table width="100%" cellpadding="0" cellspacing="0" border="0"'
                ' style="border-bottom:1px solid #f4f7f8;">'
                '<tr>'
                '<td width="30" style="padding:8px 10px 8px 0;vertical-align:top;">'
                f'<div style="width:20px;height:20px;border-radius:50%;background:#0A2231;'
                f'color:#29B78D;font-size:10px;font-weight:700;text-align:center;line-height:20px;">{i}</div>'
                '</td>'
                f'<td style="font-size:14px;color:#3C4A52;line-height:1.7;padding:8px 0;">{txt}</td>'
                '</tr>'
                '</table>'
            )
        acciones_html = (
            '<div class="section">'
            '<div class="section-title">Acciones recomendadas</div>'
            + rows_html
            + '</div>'
        )

    # Tags
    tags_html = ""
    if tags:
        spans = "".join(
            f'<span class="tag">{html.escape(str(t))}</span>' for t in tags
        )
        tags_html = f'<div class="tags-section">{spans}</div>'

    # Header — tabla para compatibilidad con Outlook (no flex)
    header_html = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0"'
        f' style="background:#0A2231;padding:22px 32px;">'
        f'<tr>'
        f'<td style="vertical-align:middle;">'
        f'<table cellpadding="0" cellspacing="0" border="0">'
        f'<tr>'
        f'<td style="vertical-align:middle;padding-right:14px;">'
        f'<img src="{EG_LOGO_LIGHT}" alt="External Group" height="24"'
        f' style="height:24px;display:block;border:0;"></td>'
        f'<td style="vertical-align:middle;padding-right:14px;">'
        f'<div style="width:1px;height:24px;background:#29B78D;opacity:0.4;"></div></td>'
        f'<td style="vertical-align:middle;">'
        f'<span style="font-size:10px;font-weight:700;letter-spacing:0.14em;'
        f'text-transform:uppercase;color:#29B78D;">Alertas DT + SII</span></td>'
        f'</tr>'
        f'</table>'
        f'</td>'
        f'<td align="right" style="vertical-align:middle;font-size:11px;color:#7ca0b4;font-style:italic;">'
        f'Nueva normativa</td>'
        f'</tr>'
        f'</table>'
    )

    return (
        f'<!doctype html>\n<html lang="es">\n<head>'
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<style>{_EMAIL_CSS}</style></head>\n<body>\n'
        f'<div class="email-wrapper">\n'
        f'<span style="display:none;visibility:hidden;opacity:0;height:0;width:0;overflow:hidden;">{preheader}</span>\n'
        f'{header_html}\n'
        f'<div class="accent-bar"></div>\n'
        f'<div class="doc-card">'
        f'<div class="doc-type">Nueva publicación de {html.escape(source_context(alert)["institution"])} · {category}</div>'
        f'<div class="doc-title">{title}</div>'
        f'<div class="doc-meta">{pub_date} · <span class="relevancia-badge">{relevance}</span></div>'
        f'<div style="height:3px;background:#29B78D;margin-top:22px;"></div>'
        f'</div>\n'
        f'<div class="body">'
        f'{ai_notice}'
        f'<p class="summary-text">{summary_esc}</p>'
        f'</div>\n'
        f'{puntos_html}'
        f'{impacto_html}'
        f'{acciones_html}'
        f'{tags_html}'
        f'<div class="cta-section">'
        f'<a class="cta-btn" href="{url}" target="_blank">Ver documento oficial en {html.escape(source_context(alert)["short"])} →</a>'
        f'<div class="cta-sub">Se adjuntan resúmenes en este correo</div>'
        f'</div>\n'
        f'<div class="footer">'
        f'<div class="disclaimer">{disclaimer_esc}</div>'
        f'<div class="footer-brand"><strong>External Group</strong> · Alertas DT + SII</div>'
        f'</div>\n'
        f'</div>\n</body>\n</html>'
    )


# --------------------------------------------------------------------------
# Generación de adjuntos HTML
# --------------------------------------------------------------------------

def _attachment_css() -> str:
    return _ATTACHMENT_CSS


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

    ctx = source_context(alert)
    doc_title = html.escape(alert.get("title") or "Documento")
    category = html.escape(alert.get("category") or "Normativa")
    pub_date = html.escape(alert.get("publication_date") or "sin fecha")
    relevance = html.escape(alert.get("relevance") or "")
    url = html.escape(alert.get("canonical_url") or "#")
    disclaimer = html.escape(
        alert.get("ai_legal_disclaimer")
        or "Este resumen es informativo y no reemplaza la lectura del documento oficial ni asesoría profesional."
    )

    body_raw = executive.get("body") or alert.get("summary") or ""
    paragraphs_html = "".join(
        f'<p class="summary-text">{html.escape(p.strip())}</p>'
        for p in body_raw.split("\n\n") if p.strip()
    ) or f'<p class="summary-text">{html.escape(body_raw)}</p>'

    relevance_badge = (
        f'<span class="relevancia-badge">{relevance}</span>' if relevance else ""
    )

    return (
        f'<!doctype html>\n<html lang="es">\n<head>'
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>Resumen ejecutivo</title>'
        f'<style>{_ATTACHMENT_CSS}</style></head>\n<body>\n'
        f'<div class="header">'
        f'<div class="header-brand">'
        f'<img class="header-logo" src="{EG_LOGO_LIGHT}" alt="External Group">'
        f'<div class="header-divider"></div>'
        f'<span class="header-tag">Alertas DT + SII</span>'
        f'</div>'
        f'<span class="header-badge">Resumen ejecutivo</span>'
        f'</div>\n'
        f'<div class="accent-bar"></div>\n'
        f'<div class="wrapper">\n'
        f'<div class="doc-card">'
        f'<div class="doc-card-header">'
        f'<div class="doc-type">{category} · {html.escape(ctx["institution"])}</div>'
        f'<div class="doc-title">{doc_title}</div>'
        f'<div class="doc-meta"><span>{pub_date}</span>'
        f'{"<span>&middot;</span>" + relevance_badge if relevance_badge else ""}'
        f'</div>'
        f'</div>'
        f'<div class="doc-card-body">'
        f'<div class="ai-notice"><div class="ai-dot"></div>'
        f'Contenido generado con apoyo de inteligencia artificial. Revisar antes de tomar decisiones.'
        f'</div>'
        f'{paragraphs_html}'
        f'<a class="link-btn" href="{url}" target="_blank">Ver documento oficial en {html.escape(ctx["short"])} →</a>'
        f'</div>'
        f'</div>\n'
        f'<div class="disclaimer">{disclaimer}</div>\n'
        f'<div class="footer"><strong>External Group</strong> · Alertas DT + SII · Documento #{document_id}</div>\n'
        f'</div>\n</body>\n</html>'
    )


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

    ctx = source_context(alert)
    doc_title = html.escape(alert.get("title") or "Documento")
    category = html.escape(alert.get("category") or "Normativa")
    pub_date = html.escape(alert.get("publication_date") or "sin fecha")
    relevance = html.escape(alert.get("relevance") or "")
    url = html.escape(alert.get("canonical_url") or "#")
    disclaimer = html.escape(
        alert.get("ai_legal_disclaimer")
        or "Este resumen es informativo y no reemplaza la lectura del documento oficial ni asesoría profesional."
    )

    def _section(icon: str, label: str, body_html: str) -> str:
        return (
            f'<div class="section">'
            f'<div class="section-header">'
            f'<div class="section-icon">{icon}</div>'
            f'<div class="section-title">{label}</div>'
            f'</div>'
            f'{body_html}'
            f'</div>\n'
        )

    sections_html = ""

    if "sections" in detailed:
        # Formato antiguo: {"title": "...", "sections": [{"heading": "...", "body": "..."}]}
        ICON_MAP = {
            "descripci": "📄", "impacto contable": "📊", "impacto laboral": "⚖️",
            "plazos": "📅", "puntos clave": "🎯", "acciones": "✅", "riesgo": "⚠️",
        }
        for section in detailed.get("sections") or []:
            h_text = str(section.get("heading") or "")
            b_text = html.escape(str(section.get("body") or ""))
            if not h_text and not b_text:
                continue
            icon = next(
                (v for k, v in ICON_MAP.items() if k in h_text.lower()), "📄"
            )
            body_part = f"<p>{b_text}</p>" if b_text else ""
            sections_html += _section(icon, html.escape(h_text), body_part)
    else:
        # Formato nuevo: dict plano con claves específicas
        _FLAT_SECTIONS = [
            ("descripcion", "📄", "Descripción del documento"),
            ("impacto_contable", "📊", "Impacto contable"),
            ("impacto_tributario", "🧾", "Impacto tributario"),
            ("impacto_laboral", "⚖️", "Impacto laboral"),
        ]
        for key, icon, label in _FLAT_SECTIONS:
            val = str(detailed.get(key) or "").strip()
            if val and val != "no informado en el documento":
                sections_html += _section(icon, label, f"<p>{html.escape(val)}</p>")

        # Plazos (puede ser lista o texto)
        plazos_raw = detailed.get("plazos")
        if plazos_raw:
            if isinstance(plazos_raw, list):
                items = "".join(
                    f'<div class="plazo-item">'
                    f'<div class="plazo-fecha">{html.escape(str(p.get("fecha", "")))}</div>'
                    f'<div class="plazo-desc">{html.escape(str(p.get("descripcion", "")))}</div>'
                    f'</div>'
                    for p in plazos_raw if isinstance(p, dict)
                )
                plazos_body = f'<div class="plazos-grid">{items}</div>' if items else ""
            else:
                plazos_body = f"<p>{html.escape(str(plazos_raw))}</p>"
            if plazos_body:
                sections_html += _section("📅", "Plazos clave", plazos_body)

    # Puntos clave (de ai_key_points_json)
    kp_raw = _parse_json_field(alert.get("ai_key_points_json"), [])
    if kp_raw:
        body_part = "".join(
            f'<div class="puntos-item"><div class="punto-dot"></div>'
            f'<div class="punto-text">{html.escape(str(p))}</div></div>'
            for p in kp_raw if p
        )
        sections_html += _section("🎯", "Puntos clave", body_part)

    # Acciones recomendadas (de ai_recommended_actions_json o campo plano)
    rec_raw = _parse_json_field(alert.get("ai_recommended_actions_json"), [])
    acc_field = str(detailed.get("acciones_recomendadas") or "").strip()
    if rec_raw:
        body_part = "".join(
            f'<div class="puntos-item"><div class="punto-dot"></div>'
            f'<div class="punto-text">{html.escape(str(a))}</div></div>'
            for a in rec_raw if a
        )
        sections_html += _section("✅", "Acciones recomendadas", body_part)
    elif acc_field and acc_field != "no informado en el documento":
        sections_html += _section("✅", "Acciones recomendadas", f"<p>{html.escape(acc_field)}</p>")

    # Riesgos
    riesgos_val = str(detailed.get("riesgos") or "").strip()
    if riesgos_val and riesgos_val != "no informado en el documento":
        sections_html += _section("⚠️", "Riesgos", f"<p>{html.escape(riesgos_val)}</p>")

    if not sections_html:
        fallback = html.escape(alert.get("summary") or "Sin contenido disponible.")
        sections_html = f'<div class="section"><p>{fallback}</p></div>'

    relevance_badge = (
        f'<span class="relevancia-badge">{relevance}</span>' if relevance else ""
    )

    return (
        f'<!doctype html>\n<html lang="es">\n<head>'
        f'<meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>Resumen detallado</title>'
        f'<style>{_ATTACHMENT_CSS}</style></head>\n<body>\n'
        f'<div class="header">'
        f'<div class="header-brand">'
        f'<img class="header-logo" src="{EG_LOGO_LIGHT}" alt="External Group">'
        f'<div class="header-divider"></div>'
        f'<span class="header-tag">Alertas DT + SII</span>'
        f'</div>'
        f'<span class="header-badge">Resumen detallado</span>'
        f'</div>\n'
        f'<div class="accent-bar"></div>\n'
        f'<div class="wrapper">\n'
        f'<div class="doc-card">'
        f'<div class="doc-card-header">'
        f'<div class="doc-type">{category} · {html.escape(ctx["institution"])}</div>'
        f'<div class="doc-title">{doc_title}</div>'
        f'<div class="doc-meta"><span>{pub_date}</span>'
        f'{"<span>&middot;</span>" + relevance_badge if relevance_badge else ""}'
        f'</div>'
        f'</div>'
        f'</div>\n'
        f'{sections_html}'
        f'<a class="link-btn" href="{url}" target="_blank">Ver documento oficial en {html.escape(ctx["short"])} →</a>\n'
        f'<div class="disclaimer">{disclaimer}</div>\n'
        f'<div class="footer"><strong>External Group</strong> · Alertas DT + SII · Documento #{document_id}</div>\n'
        f'</div>\n</body>\n</html>'
    )


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


def _redact_secret(text: str, secret: str | None) -> str:
    """Reemplaza cualquier ocurrencia literal de `secret` en `text` por [REDACTED]."""
    if secret and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text


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
                "type": att.get("type", "text/html").split(";")[0].strip(),
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
    ssl_context, _tls_info = tls.build_ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl_context) as response:  # noqa: S310
            message_id = response.headers.get("X-Message-Id")
        return {
            "ok": True, "provider": "sendgrid", "status": "sent",
            "provider_message_id": message_id, "error": None,
            "message": "Email enviado con SendGrid.",
        }
    except urllib.error.HTTPError as exc:
        body = _redact_secret(exc.read().decode("utf-8", errors="replace"), settings.sendgrid_api_key)
        return {
            "ok": False, "provider": "sendgrid", "status": "failed",
            "provider_message_id": None, "error": f"HTTP {exc.code}: {body}",
            "message": f"Error al enviar con SendGrid: HTTP {exc.code}: {body}",
        }
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            return {
                "ok": False, "provider": "sendgrid", "status": "failed",
                "provider_message_id": None,
                "error": f"Error de verificación TLS ({_tls_info.backend}): {exc.reason.verify_message}",
                "message": (
                    f"Error TLS al conectar con SendGrid (backend={_tls_info.backend}). "
                    "No es un problema de API key. Revisa 'Certificados TLS en Windows' en el README."
                ),
            }
        error_msg = _redact_secret(str(exc), settings.sendgrid_api_key)
        return {
            "ok": False, "provider": "sendgrid", "status": "failed",
            "provider_message_id": None, "error": error_msg,
            "message": f"Error al enviar con SendGrid: {error_msg}",
        }
    except Exception as exc:
        error_msg = _redact_secret(str(exc), settings.sendgrid_api_key)
        return {
            "ok": False, "provider": "sendgrid", "status": "failed",
            "provider_message_id": None, "error": error_msg,
            "message": f"Error al enviar con SendGrid: {error_msg}",
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

def _slug_from_title(title: str, max_len: int = 40) -> str:
    import re
    slug = re.sub(r"[^\w\s-]", "", (title or "").lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len] or "doc"


def _build_attachments(alert: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """Build HTML attachments for executive and detailed summary."""
    ai_enabled = getattr(settings, "ai_attachments_enabled", True)
    if not ai_enabled:
        return []
    ai_status = alert.get("ai_status")
    if ai_status not in ("success", "fallback"):
        return []
    document_id = alert.get("document_id") or alert.get("id") or 0
    slug = _slug_from_title(alert.get("title") or "")
    attachments = []
    exec_html = generate_executive_summary_html(document_id, alert)
    if exec_html.strip():
        attachments.append({
            "content": exec_html,
            "type": "text/html; charset=utf-8",
            "filename": f"resumen_ejecutivo_{slug}_{document_id}.html",
        })
    detail_html = generate_detailed_summary_html(document_id, alert)
    if detail_html.strip():
        attachments.append({
            "content": detail_html,
            "type": "text/html; charset=utf-8",
            "filename": f"resumen_detallado_{slug}_{document_id}.html",
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
    attachments = _build_attachments(alert, settings)
    return send_email(
        settings,
        to=to_email,
        subject=f"[PRUEBA] {subject_for(alert)}",
        html_body=render_alert_email_html(alert),
        text_body=render_alert_email_text(alert),
        attachments=attachments or None,
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
# WhatsApp Business Cloud API
# --------------------------------------------------------------------------

def send_whatsapp_alert(
    subscriber: dict[str, Any],
    alert: dict[str, Any],
    settings: Settings,
) -> dict[str, str | None]:
    phone = subscriber.get("whatsapp") or subscriber.get("phone")
    if not phone:
        return {
            "status": "skipped",
            "provider_message_id": None,
            "error": "Suscriptor sin número WhatsApp.",
        }
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
        "to": phone,
        "type": "template",
        "template": {
            "name": settings.whatsapp_template_name,
            "language": {"code": settings.whatsapp_language},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": truncate_subject(alert.get("title") or "Documento", 80)},
                        {"type": "text", "text": truncate_subject(alert.get("summary") or "", 180)},
                        {"type": "text", "text": str(alert.get("canonical_url") or "")},
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
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
        message_id = None
        if body.get("messages"):
            message_id = body["messages"][0].get("id")
        return {"status": "sent", "provider_message_id": message_id, "error": None}
    except Exception as exc:
        return {"status": "failed", "provider_message_id": None, "error": str(exc)}
