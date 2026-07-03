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


@dataclass
class AIResponse:
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


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


def is_ai_runtime_enabled(settings: Settings, app_settings: dict[str, str]) -> bool:
    """DB (ai_runtime_enabled) es autoritativa. AI_ENABLED en .env es solo valor inicial."""
    override = (app_settings.get("ai_runtime_enabled") or "").strip().lower()
    if override in {"0", "false", "no", "n", "off"}:
        return False
    if override in {"1", "true", "yes", "y", "on"}:
        return True
    # Sin valor en DB: usar AI_ENABLED del entorno como fallback
    return bool(getattr(settings, "ai_enabled", False))


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
    analysis_focus = (
        app_settings.get("ai_analysis_focus")
        or "Explicar impactos prácticos en cumplimiento laboral, gestión contable, "
           "auditoría, remuneraciones y obligaciones documentales."
    )

    system_prompt = (
        "Eres un analista legal-laboral chileno especializado en normativa de la "
        "Dirección del Trabajo (DT). "
        f"Estilo editorial: {style} "
        "Tu audiencia son contadores, jefes de RRHH, administradores y empresas chilenas. "
        "No inventes obligaciones, fechas, artículos, montos ni sanciones que no estén en el texto. "
        "Si falta información, indica exactamente 'no informado en el documento'. "
        "No reemplaces la lectura del documento oficial. "
        "Mantén tono chileno formal, sin exagerar riesgos. "
        "Responde SOLO con JSON válido, sin markdown, sin comentarios, sin texto adicional. "
        + (extra.strip() + " " if extra.strip() else "")
    ).strip()

    user_prompt = f"""Analiza este documento de la Dirección del Trabajo de Chile.
Audiencia: contadores, administradores, RRHH y empresas.
Enfoque del análisis: {analysis_focus}

El correo debe contener un resumen breve y los impactos en el día a día del contador.
Adjunta resumen ejecutivo y resumen detallado.

Responde ÚNICAMENTE con este JSON (sin markdown, sin texto extra):

{{
  "title": "Título corregido del documento (sin agregar información no presente)",
  "category": "Categoría normativa (ej: Circular, Dictamen, Resolución, Ordinario)",
  "official_date": "Fecha oficial del documento si existe, o null",
  "source_institution": "Dirección del Trabajo",
  "relevance": "bajo|medio|alto",
  "email_subject": "Nueva normativa DT: [título conciso]",
  "email_summary": "Resumen breve de 2 a 3 párrafos para el cuerpo del correo. Lenguaje claro para contadores.",
  "key_points": [
    "Punto clave 1: qué establece el documento",
    "Punto clave 2: a quién afecta",
    "Punto clave 3: fecha o vigencia si aplica"
  ],
  "practical_impacts": [
    {{"title": "Impacto en remuneraciones", "description": "Descripción del impacto en el día a día del contador."}},
    {{"title": "Impacto en cumplimiento laboral", "description": "Qué debe verificar o ajustar la empresa."}}
  ],
  "recommended_actions": [
    "Acción concreta 1 que debe tomar el contador o RRHH",
    "Acción concreta 2 con plazo si corresponde"
  ],
  "executive_summary": {{
    "title": "Resumen ejecutivo",
    "body": "Resumen ejecutivo en 1 a 2 párrafos: qué es, por qué importa, qué hacer."
  }},
  "detailed_summary": {{
    "descripcion": "Descripción detallada del documento y su alcance.",
    "impacto_contable": "Impacto en registros contables, libros o declaraciones.",
    "impacto_laboral": "Impacto en contratos, remuneraciones, finiquitos o licencias.",
    "acciones_recomendadas": "Pasos concretos para cumplir o implementar.",
    "riesgos": "Riesgos de no cumplir, si el documento los menciona.",
    "plazos": "Plazos relevantes mencionados en el documento, o 'no informado'."
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

def _usage_from_dict(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    """Normaliza usage de Chat Completions o Responses API."""
    usage = usage or {}
    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or 0
    )
    total_tokens = int(
        usage.get("total_tokens")
        or (input_tokens + output_tokens)
        or 0
    )
    return input_tokens, output_tokens, total_tokens


def _extract_responses_text(body: dict[str, Any]) -> str:
    """Extrae texto desde OpenAI/Azure Responses API sin depender del SDK."""
    if isinstance(body.get("output_text"), str) and body["output_text"].strip():
        return body["output_text"].strip()

    parts: list[str] = []
    for item in body.get("output") or []:
        for content in item.get("content") or []:
            text = content.get("text") or content.get("output_text")
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


def _call_openai_api(system_prompt: str, user_prompt: str, settings: Settings) -> AIResponse:
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

    content = body["choices"][0]["message"]["content"]
    input_tokens, output_tokens, total_tokens = _usage_from_dict(body.get("usage"))
    return AIResponse(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _read_http_error(exc: urllib.error.HTTPError) -> str:
    """Lee el cuerpo de error HTTP sin exponer secretos."""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    detail = body.strip() or str(exc)
    return f"HTTP {exc.code}: {detail[:1000]}"


def _call_azure_api(system_prompt: str, user_prompt: str, settings: Settings) -> AIResponse:
    """
    Azure v1 Responses API:
    AI_BASE_URL=https://DemoTiboxIA.services.ai.azure.com/openai/v1
    POST {AI_BASE_URL}/responses

    Nota:
    El endpoint v1 usa model=deployment_name y no requiere api-version.
    Para maximizar compatibilidad, no enviamos temperature ni response_format aquí.
    El JSON se exige por prompt fijo.
    """
    base_url = settings.ai_base_url.rstrip("/")
    deployment = settings.ai_model

    if base_url.endswith("/openai/v1"):
        payload = {
            "model": deployment,
            "instructions": system_prompt,
            "input": user_prompt,
        }
        request = urllib.request.Request(
            f"{base_url}/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.ai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.ai_timeout_seconds) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_read_http_error(exc)) from exc

        content = _extract_responses_text(body)
        input_tokens, output_tokens, total_tokens = _usage_from_dict(body.get("usage"))
        return AIResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    # Fallback Azure OpenAI clásico /chat/completions
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
    try:
        with urllib.request.urlopen(request, timeout=settings.ai_timeout_seconds) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_read_http_error(exc)) from exc

    content = body["choices"][0]["message"]["content"]
    input_tokens, output_tokens, total_tokens = _usage_from_dict(body.get("usage"))
    return AIResponse(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def call_ai_with_usage(system_prompt: str, user_prompt: str, settings: Settings) -> AIResponse:
    provider = settings.ai_provider.lower()
    if provider == "openai":
        return _call_openai_api(system_prompt, user_prompt, settings)
    if provider == "azure":
        return _call_azure_api(system_prompt, user_prompt, settings)
    raise ValueError(f"Proveedor IA no soportado: {provider!r}")


def call_ai(system_prompt: str, user_prompt: str, settings: Settings) -> str:
    """Compatibilidad: mantiene la firma anterior devolviendo solo contenido."""
    return call_ai_with_usage(system_prompt, user_prompt, settings).content


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
    """
    Generate and save AI summary for a document.
    Never auto-sends. Alert always stays pending_review.

    Seguridad:
    - AI_ENABLED=false impide llamadas reales.
    - Se registran intentos en ai_usage_logs.
    - Se bloquea por límite diario/mensual.
    """
    document = db.get_document(conn, document_id)
    if not document:
        raise ValueError(f"Documento {document_id} no encontrado.")

    if not force:
        existing = db.get_ai_summary(conn, document_id)
        if existing and existing.get("status") == "success":
            return _stored_to_result(existing)

    provider = settings.ai_provider.lower()
    model = settings.ai_model or ""
    operation = "regenerate_summary" if force else "generate_summary"

    alert_id: int | None = None
    try:
        row = conn.execute(
            "SELECT id FROM alerts WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if row:
            alert_id = int(row["id"])
    except Exception:
        alert_id = None

    source_text = build_source_text(document, settings.ai_max_input_chars)
    ihash = compute_input_hash(source_text)

    raw_response: str | None = None
    error: str | None = None
    validated: dict[str, Any] = {}
    status = "pending"
    content_quality = "limited"

    daily_limit = int(getattr(settings, "ai_daily_token_limit", 50000) or 0)
    monthly_limit = int(getattr(settings, "ai_monthly_token_limit", 500000) or 0)
    usage_status = db.get_ai_usage_status(
        conn,
        daily_limit=daily_limit,
        monthly_limit=monthly_limit,
    )

    def _record(
        log_status: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        log_error: str | None = None,
    ) -> None:
        db.record_ai_usage(
            conn,
            document_id=document_id,
            alert_id=alert_id,
            provider=provider,
            model=model,
            operation=operation,
            status=log_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            daily_limit=daily_limit,
            monthly_limit=monthly_limit,
            error=log_error,
        )

    if not is_ai_runtime_enabled(settings, app_settings):
        validated = generate_fallback_summary(document)
        status = "fallback"
        content_quality = "limited"
        error = "AI_ENABLED=false; resumen generado localmente sin usar API."
        _record("disabled", log_error=error)

    elif provider not in ("openai", "azure"):
        validated = generate_fallback_summary(document)
        status = "fallback"
        content_quality = "limited"
        error = f"AI_PROVIDER={provider or 'disabled'}; resumen generado localmente."
        _record("disabled", log_error=error)

    elif not settings.ai_api_key:
        validated = generate_fallback_summary(document)
        status = "fallback"
        content_quality = "limited"
        error = f"AI_API_KEY no configurada para proveedor {provider!r}."
        _record("missing_key", log_error=error)

    elif provider == "azure" and not settings.ai_base_url:
        validated = generate_fallback_summary(document)
        status = "fallback"
        content_quality = "limited"
        error = "Azure requiere AI_BASE_URL configurado."
        _record("error", log_error=error)

    elif usage_status.get("daily_exceeded") or usage_status.get("monthly_exceeded"):
        validated = generate_fallback_summary(document)
        status = "fallback"
        content_quality = "limited"
        if usage_status.get("daily_exceeded"):
            error = (
                f"Límite diario IA alcanzado: "
                f"{usage_status.get('today_tokens')} / {daily_limit} tokens."
            )
        else:
            error = (
                f"Límite mensual IA alcanzado: "
                f"{usage_status.get('month_tokens')} / {monthly_limit} tokens."
            )
        _record("blocked_limit", log_error=error)

    else:
        try:
            system_prompt, user_prompt = build_ai_prompt(document, settings, app_settings)
            ai_response = call_ai_with_usage(system_prompt, user_prompt, settings)
            raw_response = ai_response.content

            parsed = parse_ai_response(raw_response)
            if parsed:
                validated = validate_ai_summary(parsed)
                status = "success"
                content_quality = "full"
                _record(
                    "success",
                    input_tokens=ai_response.input_tokens,
                    output_tokens=ai_response.output_tokens,
                    total_tokens=ai_response.total_tokens,
                )
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
            _record("error", log_error=error)

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
