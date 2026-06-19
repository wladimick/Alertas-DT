from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import db
from .config import Settings, get_settings


@dataclass
class SummaryResult:
    summary: str
    key_points: list[str]
    practical_impacts: list[str]
    relevance: str
    status: str
    ai_error: str | None = None
    email_subject: str | None = None


# --------------------------------------------------------------------------
# Construcción de texto de entrada
# --------------------------------------------------------------------------

def build_source_text(doc: dict[str, Any], max_chars: int = 45_000) -> str:
    lines = [
        f"Título: {doc.get('title') or ''}",
        f"Categoría: {doc.get('category') or ''}",
        f"Fecha: {doc.get('publication_date') or 'sin fecha'}",
        f"URL oficial: {doc.get('canonical_url') or ''}",
        f"Abstract: {doc.get('abstract') or ''}",
        "",
        doc.get("detail_text") or "",
    ]
    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def compute_input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Construcción de prompt
# --------------------------------------------------------------------------

def build_ai_prompt(
    document: dict[str, Any],
    settings: Settings,
    app_settings: dict[str, str],
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt). Input is truncated to AI_MAX_INPUT_CHARS."""
    source_text = build_source_text(document, settings.ai_max_input_chars)

    style = (
        app_settings.get("ai_summary_style")
        or "Profesional, claro, orientado a contadores y empresas chilenas."
    )
    extra = app_settings.get("ai_system_prompt") or ""

    system_prompt = (
        "Eres un analista legal-laboral chileno especializado en normativa de la "
        "Dirección del Trabajo. "
        f"Estilo editorial: {style} "
        "No inventes obligaciones, fechas, artículos ni efectos que no estén en el texto. "
        "Si falta información, indica 'no informado en el documento'. "
        "Responde SOLO con JSON válido, sin markdown, sin comentarios, sin texto adicional. "
        + (extra.strip() + " " if extra.strip() else "")
    ).strip()

    user_prompt = f"""Analiza este documento de la Dirección del Trabajo de Chile.
Audiencia: contadores, administradores y empresas.

Explica:
- Qué informa el documento.
- A quién afecta.
- Qué debería revisar el contador.
- Qué acciones prácticas conviene tomar.

Responde ÚNICAMENTE con este JSON (sin markdown, sin texto extra):

{{
  "title": "Título corregido del documento",
  "category": "Categoría normativa",
  "official_date": "Fecha oficial si existe o null",
  "source_institution": "Dirección del Trabajo",
  "relevance": "bajo|medio|alto",
  "email_subject": "Nueva normativa DT: ...",
  "email_summary": "Resumen breve de 2 a 4 párrafos para el correo.",
  "key_points": ["Punto clave 1", "Punto clave 2", "Punto clave 3"],
  "practical_impacts": [
    {{"title": "Impacto 1", "description": "Descripción práctica para contadores/empresas."}}
  ],
  "recommended_actions": ["Acción recomendada 1", "Acción recomendada 2"],
  "executive_summary": {{"title": "Resumen ejecutivo", "body": "Resumen ejecutivo claro, breve y accionable."}},
  "detailed_summary": {{
    "title": "Resumen detallado",
    "sections": [{{"heading": "Sección 1", "body": "Desarrollo detallado."}}]
  }},
  "tags": ["Dirección del Trabajo", "Normativa laboral", "Contadores"],
  "legal_disclaimer": "Este resumen es informativo y no reemplaza la lectura del documento oficial ni asesoría profesional."
}}

Documento:
{source_text}""".strip()

    return system_prompt, user_prompt


# --------------------------------------------------------------------------
# Clientes IA
# --------------------------------------------------------------------------

def _call_openai_api(system_prompt: str, user_prompt: str, settings: Settings) -> str:
    payload = {
        "model": settings.ai_model or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": settings.ai_summary_temperature,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.ai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.ai_timeout_seconds) as resp:  # noqa: S310
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _call_azure_api(system_prompt: str, user_prompt: str, settings: Settings) -> str:
    base_url = settings.ai_base_url.rstrip("/")
    deployment = settings.ai_model
    url = (
        f"{base_url}/openai/deployments/{deployment}"
        "/chat/completions?api-version=2024-02-15-preview"
    )
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": settings.ai_summary_temperature,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": settings.ai_api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.ai_timeout_seconds) as resp:  # noqa: S310
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def call_ai(system_prompt: str, user_prompt: str, settings: Settings) -> str:
    provider = settings.ai_provider.lower()
    if provider == "openai":
        return _call_openai_api(system_prompt, user_prompt, settings)
    if provider == "azure":
        return _call_azure_api(system_prompt, user_prompt, settings)
    raise ValueError(f"Proveedor IA no soportado: {provider!r}")


# --------------------------------------------------------------------------
# Parser y validador de respuesta IA
# --------------------------------------------------------------------------

def parse_ai_response(raw_text: str) -> dict[str, Any]:
    """Parse AI JSON response. Returns empty dict on failure — never raises."""
    text = (raw_text or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def _clean_str(val: Any, max_len: int = 2000) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", str(val)).strip()[:max_len]


def _clean_list_str(val: Any, fallback: list[str] | None = None) -> list[str]:
    if isinstance(val, list):
        cleaned = [_clean_str(item) for item in val if _clean_str(item)]
        if cleaned:
            return cleaned[:10]
    return fallback or []


def _clean_impacts(val: Any) -> list[dict[str, str]]:
    if not isinstance(val, list):
        return []
    result = []
    for item in val:
        if isinstance(item, dict):
            result.append({
                "title": _clean_str(item.get("title", "")),
                "description": _clean_str(item.get("description", "")),
            })
        elif isinstance(item, str) and _clean_str(item):
            result.append({"title": _clean_str(item), "description": ""})
    return result[:10]


def validate_ai_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize and fill missing fields with safe defaults."""
    relevance = _clean_str(data.get("relevance", "medio")).lower()
    if relevance not in {"bajo", "medio", "alto"}:
        relevance = "medio"

    executive = data.get("executive_summary", {})
    if isinstance(executive, str):
        executive = {"title": "Resumen ejecutivo", "body": executive}
    elif not isinstance(executive, dict):
        executive = {"title": "Resumen ejecutivo", "body": ""}

    detailed = data.get("detailed_summary", {})
    if isinstance(detailed, str):
        detailed = {"title": "Resumen detallado", "sections": [{"heading": "", "body": detailed}]}
    elif not isinstance(detailed, dict):
        detailed = {"title": "Resumen detallado", "sections": []}

    practical = data.get("practical_impacts", [])
    if isinstance(practical, list) and practical and isinstance(practical[0], str):
        practical = [{"title": s, "description": ""} for s in practical]

    return {
        "title": _clean_str(data.get("title", "")) or None,
        "category": _clean_str(data.get("category", "")) or None,
        "official_date": _clean_str(data.get("official_date") or ""),
        "source_institution": _clean_str(data.get("source_institution", "Dirección del Trabajo")),
        "relevance": relevance,
        "email_subject": _clean_str(data.get("email_subject", ""))[:200],
        "email_summary": _clean_str(data.get("email_summary", ""), max_len=5000),
        "key_points": _clean_list_str(data.get("key_points", [])),
        "practical_impacts": _clean_impacts(practical),
        "recommended_actions": _clean_list_str(data.get("recommended_actions", [])),
        "executive_summary": executive,
        "detailed_summary": detailed,
        "tags": _clean_list_str(data.get("tags", [])),
        "legal_disclaimer": (
            _clean_str(data.get("legal_disclaimer", ""))
            or "Este resumen es informativo y no reemplaza la lectura del documento oficial ni asesoría profesional."
        ),
    }


# --------------------------------------------------------------------------
# Fallback local
# --------------------------------------------------------------------------

def generate_fallback_summary(document: dict[str, Any]) -> dict[str, Any]:
    """Generate structured summary locally when AI is disabled or fails."""
    text = " ".join(
        part
        for part in [document.get("abstract") or "", document.get("detail_text") or ""]
        if part
    )
    sentences = _split_sentences(text)
    email_summary = _clean_str(" ".join(sentences[:2]))
    if not email_summary:
        email_summary = (
            f"La DT publicó un nuevo documento en la categoría "
            f"{document.get('category', 'normativa')}. "
            "Consulta el texto oficial para confirmar alcance y vigencia."
        )

    key_points = [_clean_str(s) for s in sentences[:4] if _clean_str(s)] or [
        "Documento publicado por la Dirección del Trabajo.",
        "Revisar el texto oficial para confirmar alcance y vigencia.",
    ]

    impacts = _infer_impacts(" ".join([document.get("title") or "", text]))
    relevance = _infer_relevance(" ".join([document.get("title") or "", text]))

    title = document.get("title") or "Documento DT"
    category = document.get("category") or "Normativa"
    subject = f"Nueva normativa DT: {_clean_str(title, 100)}"

    return {
        "title": title,
        "category": category,
        "official_date": document.get("publication_date") or None,
        "source_institution": "Dirección del Trabajo",
        "relevance": relevance,
        "email_subject": subject,
        "email_summary": email_summary,
        "key_points": key_points[:5],
        "practical_impacts": [{"title": i, "description": ""} for i in impacts],
        "recommended_actions": [
            "Revisar el documento oficial en el sitio de la Dirección del Trabajo.",
            "Evaluar impacto en clientes y empresas asesoradas.",
        ],
        "executive_summary": {
            "title": f"Resumen ejecutivo: {_clean_str(title, 60)}",
            "body": email_summary,
        },
        "detailed_summary": {
            "title": f"Resumen detallado: {_clean_str(title, 60)}",
            "sections": [
                {"heading": "Descripción", "body": email_summary},
                *(
                    [{"heading": "Texto extraído", "body": text[:3000]}]
                    if text else []
                ),
            ],
        },
        "tags": [category, "Dirección del Trabajo", "Normativa laboral"],
        "legal_disclaimer": (
            "Este resumen es informativo y no reemplaza la lectura del documento oficial "
            "ni asesoría profesional."
        ),
    }


# --------------------------------------------------------------------------
# Guardar en DB
# --------------------------------------------------------------------------

def _save_to_db(
    conn: Any,
    document_id: int,
    validated: dict[str, Any],
    *,
    provider: str,
    model: str,
    status: str,
    input_hash: str,
    content_quality: str,
    error: str | None,
    raw_response: str | None,
) -> None:
    db.upsert_ai_summary(
        conn,
        document_id,
        provider=provider,
        model=model,
        status=status,
        input_hash=input_hash,
        content_quality=content_quality,
        relevance=validated.get("relevance", "medio"),
        email_subject=validated.get("email_subject"),
        email_summary=validated.get("email_summary"),
        key_points_json=json.dumps(validated.get("key_points", []), ensure_ascii=False),
        practical_impacts_json=json.dumps(validated.get("practical_impacts", []), ensure_ascii=False),
        recommended_actions_json=json.dumps(validated.get("recommended_actions", []), ensure_ascii=False),
        executive_summary=json.dumps(validated.get("executive_summary", {}), ensure_ascii=False),
        detailed_summary_json=json.dumps(validated.get("detailed_summary", {}), ensure_ascii=False),
        tags_json=json.dumps(validated.get("tags", []), ensure_ascii=False),
        legal_disclaimer=validated.get("legal_disclaimer"),
        raw_response_json=raw_response[:10_000] if raw_response else None,
        error=error,
    )


# --------------------------------------------------------------------------
# Conversión a SummaryResult
# --------------------------------------------------------------------------

def _validated_to_result(
    validated: dict[str, Any],
    *,
    error: str | None,
) -> SummaryResult:
    impacts = []
    for item in validated.get("practical_impacts", []):
        if isinstance(item, dict):
            label = item.get("title") or item.get("description") or ""
        else:
            label = str(item)
        if label:
            impacts.append(_clean_str(label))

    return SummaryResult(
        summary=validated.get("email_summary") or "Documento DT detectado.",
        key_points=[_clean_str(k) for k in validated.get("key_points", []) if _clean_str(k)][:5],
        practical_impacts=[i for i in impacts if i][:5],
        relevance=validated.get("relevance") or "medio",
        status="pending_review",
        ai_error=error,
        email_subject=validated.get("email_subject") or None,
    )


# --------------------------------------------------------------------------
# Funciones principales
# --------------------------------------------------------------------------

def generate_ai_summary(
    document_id: int,
    *,
    settings: Settings | None = None,
    app_settings: dict[str, str] | None = None,
    force: bool = False,
) -> SummaryResult:
    """
    Generate and save AI summary for a document.
    Never auto-sends. Alert always stays pending_review.
    """
    settings = settings or get_settings()
    with db.connect(settings.database_path) as conn:
        if app_settings is None:
            app_settings = db.get_all_settings(conn)
        return _generate_and_save(conn, document_id, settings, app_settings, force=force)


def regenerate_ai_summary(
    document_id: int,
    *,
    settings: Settings | None = None,
    app_settings: dict[str, str] | None = None,
) -> SummaryResult:
    return generate_ai_summary(
        document_id, settings=settings, app_settings=app_settings, force=True
    )


def _generate_and_save(
    conn: Any,
    document_id: int,
    settings: Settings,
    app_settings: dict[str, str],
    *,
    force: bool = False,
) -> SummaryResult:
    document = db.get_document(conn, document_id)
    if not document:
        raise ValueError(f"Documento {document_id} no encontrado.")

    if not force:
        existing = db.get_ai_summary(conn, document_id)
        if existing and existing.get("status") == "success":
            return _stored_to_result(existing)

    provider = settings.ai_provider.lower()
    model = settings.ai_model or ""
    source_text = build_source_text(document, settings.ai_max_input_chars)
    ihash = compute_input_hash(source_text)

    raw_response: str | None = None
    error: str | None = None
    validated: dict[str, Any] = {}
    status = "pending"
    content_quality = "limited"

    if provider in ("openai", "azure"):
        if not settings.ai_api_key:
            error = f"AI_API_KEY no configurada para proveedor {provider!r}."
            validated = generate_fallback_summary(document)
            status = "fallback"
        elif provider == "azure" and not settings.ai_base_url:
            error = "Azure requiere AI_BASE_URL configurado."
            validated = generate_fallback_summary(document)
            status = "fallback"
        else:
            try:
                system_prompt, user_prompt = build_ai_prompt(document, settings, app_settings)
                raw_response = call_ai(system_prompt, user_prompt, settings)
                parsed = parse_ai_response(raw_response)
                if parsed:
                    validated = validate_ai_summary(parsed)
                    status = "success"
                    content_quality = "full"
                else:
                    raise ValueError("Respuesta IA vacía o no parseable.")
            except Exception as exc:
                error_msg = str(exc)
                if settings.ai_api_key and settings.ai_api_key in error_msg:
                    error_msg = error_msg.replace(settings.ai_api_key, "[REDACTED]")
                error = f"Error IA: {error_msg[:500]}"
                validated = generate_fallback_summary(document)
                status = "fallback"
                content_quality = "limited"
    else:
        validated = generate_fallback_summary(document)
        status = "fallback"
        content_quality = "limited"
        error = "AI_PROVIDER=disabled; resumen generado localmente."

    _save_to_db(
        conn, document_id, validated,
        provider=provider, model=model, status=status,
        input_hash=ihash, content_quality=content_quality,
        error=error, raw_response=raw_response,
    )
    return _validated_to_result(validated, error=error)


def _stored_to_result(ai_summary: dict[str, Any]) -> SummaryResult:
    key_points = json.loads(ai_summary.get("key_points_json") or "[]")
    impacts_raw = json.loads(ai_summary.get("practical_impacts_json") or "[]")
    if impacts_raw and isinstance(impacts_raw[0], dict):
        impacts = [
            i.get("title") or i.get("description") or ""
            for i in impacts_raw
        ]
    else:
        impacts = [str(i) for i in impacts_raw]
    return SummaryResult(
        summary=ai_summary.get("email_summary") or "Documento DT detectado.",
        key_points=[_clean_str(k) for k in key_points if _clean_str(k)][:5],
        practical_impacts=[_clean_str(i) for i in impacts if _clean_str(i)][:5],
        relevance=ai_summary.get("relevance") or "medio",
        status="pending_review",
        ai_error=ai_summary.get("error"),
        email_subject=ai_summary.get("email_subject") or None,
    )


# --------------------------------------------------------------------------
# Backward compatibility (no DB connection needed)
# --------------------------------------------------------------------------

def summarize_document(doc: dict[str, Any], settings: Settings) -> SummaryResult:
    """
    Backward-compat wrapper. Used by tests and old code paths.
    When AI is enabled and credentials are present, calls the AI (no DB save).
    Otherwise uses local fallback. Always returns status='pending_review'.
    """
    provider = settings.ai_provider.lower()
    ai_api_key = getattr(settings, "ai_api_key", "") or getattr(settings, "openai_api_key", "")

    if provider in ("openai", "azure") and ai_api_key:
        try:
            system_prompt, user_prompt = build_ai_prompt(doc, settings, {})
            raw = call_ai(system_prompt, user_prompt, settings)
            parsed = parse_ai_response(raw)
            if parsed:
                validated = validate_ai_summary(parsed)
                return _validated_to_result(validated, error=None)
        except Exception as exc:
            error_msg = str(exc)
            if ai_api_key and ai_api_key in error_msg:
                error_msg = error_msg.replace(ai_api_key, "[REDACTED]")
            fb = generate_fallback_summary(doc)
            result = _validated_to_result(fb, error=f"Fallo IA: {error_msg[:300]}")
            return result

    fb = generate_fallback_summary(doc)
    result = _validated_to_result(fb, error="AI_PROVIDER=disabled o sin credenciales.")
    return result


def fallback_summary(doc: dict[str, Any]) -> SummaryResult:
    fb = generate_fallback_summary(doc)
    return _validated_to_result(fb, error=None)


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ0-9])", text)
    return [_clean_str(part) for part in parts if _clean_str(part)][:8]


def _infer_impacts(text: str) -> list[str]:
    lower = text.lower()
    impacts: list[str] = []
    if any(w in lower for w in ["remuner", "sueldo", "gratificación", "cotización"]):
        impacts.append(
            "Revisar efectos en liquidaciones, remuneraciones, cotizaciones o cálculos laborales."
        )
    if any(w in lower for w in ["contrato", "jornada", "turno", "teletrabajo"]):
        impacts.append(
            "Evaluar ajustes en contratos, anexos, jornadas o políticas internas."
        )
    if any(w in lower for w in ["registro", "libro", "electrónico", "fiscalización"]):
        impacts.append(
            "Verificar obligaciones de registro, respaldo documental y preparación ante fiscalizaciones."
        )
    if any(w in lower for w in ["multa", "sanción", "cumplimiento", "infracción"]):
        impacts.append(
            "Revisar controles de cumplimiento para reducir riesgo de multas o sanciones."
        )
    if any(w in lower for w in ["licencia", "feriado", "permiso", "descanso"]):
        impacts.append(
            "Confirmar tratamiento administrativo de ausencias, descansos, permisos o beneficios."
        )
    if not impacts:
        impacts.append(
            "Determinar si el criterio aplica a clientes, trabajadores o empresas asesoradas."
        )
        impacts.append(
            "Guardar el documento oficial como respaldo para futuras revisiones laborales."
        )
    return impacts[:5]


def _infer_relevance(text: str) -> str:
    lower = text.lower()
    high = ["multa", "sanción", "cotización", "remuner", "jornada", "registro electrónico", "ley n°"]
    medium = ["contrato", "fiscalización", "dictamen", "resolución", "circular", "ordinario"]
    if any(t in lower for t in high):
        return "alto"
    if any(t in lower for t in medium):
        return "medio"
    return "bajo"
