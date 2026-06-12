from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings


@dataclass
class SummaryResult:
    summary: str
    key_points: list[str]
    practical_impacts: list[str]
    relevance: str
    status: str
    ai_error: str | None = None


def summarize_document(doc: dict[str, Any], settings: Settings) -> SummaryResult:
    if settings.openai_api_key:
        try:
            return summarize_with_openai(doc, settings)
        except Exception as exc:
            fallback = fallback_summary(doc)
            fallback.status = "pending_review"
            fallback.ai_error = f"Fallo IA: {exc}"
            return fallback

    fallback = fallback_summary(doc)
    fallback.status = "pending_review"
    fallback.ai_error = "OPENAI_API_KEY no configurada; resumen local requiere revisión."
    return fallback


def summarize_with_openai(doc: dict[str, Any], settings: Settings) -> SummaryResult:
    source_text = build_source_text(doc, max_chars=18_000)
    prompt = f"""
Analiza este documento de la Dirección del Trabajo de Chile para una audiencia de
contadores, asesores laborales, contribuyentes y empresas.

Devuelve solo JSON válido con esta forma:
{{
  "summary": "resumen breve en español claro, máximo 90 palabras",
  "key_points": ["3 a 5 puntos clave"],
  "practical_impacts": ["3 a 5 impactos prácticos para contadores/empresas"],
  "relevance": "bajo|medio|alto"
}}

Documento:
{source_text}
""".strip()
    payload = {
        "model": settings.openai_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Eres un analista legal-laboral chileno. No inventes obligaciones. "
                    "Si el documento no entrega suficiente información, dilo con cautela."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read().decode("utf-8"))

    content = body["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    return sanitize_summary(parsed, status="ready")


def build_source_text(doc: dict[str, Any], max_chars: int = 18_000) -> str:
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


def sanitize_summary(value: dict[str, Any], status: str) -> SummaryResult:
    summary = clean_sentence(value.get("summary")) or "Documento DT detectado."
    key_points = clean_list(value.get("key_points"), fallback=["Revisar el texto oficial."])
    impacts = clean_list(
        value.get("practical_impacts"),
        fallback=["Evaluar si afecta contratos, remuneraciones, registros o cumplimiento laboral."],
    )
    relevance = str(value.get("relevance") or "medio").lower().strip()
    if relevance not in {"bajo", "medio", "alto"}:
        relevance = "medio"
    return SummaryResult(
        summary=summary,
        key_points=key_points[:5],
        practical_impacts=impacts[:5],
        relevance=relevance,
        status=status,
    )


def fallback_summary(doc: dict[str, Any]) -> SummaryResult:
    text = " ".join(
        part
        for part in [
            doc.get("abstract") or "",
            doc.get("detail_text") or "",
        ]
        if part
    )
    sentences = split_sentences(text)
    summary = clean_sentence(" ".join(sentences[:2]))
    if not summary:
        summary = f"La DT publicó un nuevo documento en la categoría {doc.get('category', 'normativa')}."

    key_points = sentences[:4] or [
        "Documento publicado por la Dirección del Trabajo.",
        "Debe revisarse el texto oficial para confirmar alcance y vigencia.",
    ]
    impacts = infer_impacts(" ".join([doc.get("title") or "", text]))
    relevance = infer_relevance(" ".join([doc.get("title") or "", text]))
    return SummaryResult(
        summary=summary,
        key_points=[clean_sentence(item) for item in key_points if clean_sentence(item)][:5],
        practical_impacts=impacts,
        relevance=relevance,
        status="ready",
    )


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ0-9])", text)
    return [clean_sentence(part) for part in parts if clean_sentence(part)][:8]


def infer_impacts(text: str) -> list[str]:
    lower = text.lower()
    impacts: list[str] = []
    if any(word in lower for word in ["remuner", "sueldo", "gratificación", "cotización"]):
        impacts.append("Revisar efectos en liquidaciones, remuneraciones, cotizaciones o cálculos laborales.")
    if any(word in lower for word in ["contrato", "jornada", "turno", "teletrabajo"]):
        impacts.append("Evaluar ajustes en contratos, anexos, jornadas o políticas internas.")
    if any(word in lower for word in ["registro", "libro", "electrónico", "fiscalización"]):
        impacts.append("Verificar obligaciones de registro, respaldo documental y preparación ante fiscalizaciones.")
    if any(word in lower for word in ["multa", "sanción", "cumplimiento", "infracción"]):
        impacts.append("Revisar controles de cumplimiento para reducir riesgo de multas o sanciones.")
    if any(word in lower for word in ["licencia", "feriado", "permiso", "descanso"]):
        impacts.append("Confirmar tratamiento administrativo de ausencias, descansos, permisos o beneficios.")
    if not impacts:
        impacts.append("Determinar si el criterio aplica a clientes, trabajadores, proveedores o empresas asesoradas.")
        impacts.append("Guardar el documento oficial como respaldo para futuras revisiones laborales o tributarias.")
    return impacts[:5]


def infer_relevance(text: str) -> str:
    lower = text.lower()
    high_terms = ["multa", "sanción", "cotización", "remuner", "jornada", "registro electrónico", "ley n°"]
    medium_terms = ["contrato", "fiscalización", "dictamen", "resolución", "circular", "ordinario"]
    if any(term in lower for term in high_terms):
        return "alto"
    if any(term in lower for term in medium_terms):
        return "medio"
    return "bajo"


def clean_list(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        cleaned = [clean_sentence(str(item)) for item in value]
        cleaned = [item for item in cleaned if item]
        if cleaned:
            return cleaned
    if isinstance(value, str) and value.strip():
        return [clean_sentence(value)]
    return fallback


def clean_sentence(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:700]
