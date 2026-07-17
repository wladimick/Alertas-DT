from __future__ import annotations

from typing import Any


def source_kind(document: dict[str, Any]) -> str:
    url = " ".join(
        str(document.get(key) or "").lower()
        for key in ("canonical_url", "source_url", "url")
    )
    doc_id = str(document.get("dt_article_id") or "").lower()
    category = str(document.get("category") or "").lower()

    if "sii.cl" in url or doc_id.startswith("sii") or category.startswith("sii"):
        return "sii"
    return "dt"


def source_context(document: dict[str, Any]) -> dict[str, str]:
    """Return display/prompt metadata for the document source institution."""
    if source_kind(document) == "sii":
        return {
            "short": "SII",
            "institution": "Servicio de Impuestos Internos",
            "domain": "tributaria",
            "audience": "contadores, asesores tributarios, contribuyentes y empresas chilenas",
            "analysis_focus": (
                "Explicar impactos prácticos en cumplimiento tributario, declaraciones, "
                "contabilidad, fiscalización, plazos, multas y obligaciones documentales."
            ),
            "impact_field": "impacto_tributario",
            "impact_label": "Impacto tributario",
            "tags": "Servicio de Impuestos Internos, Normativa tributaria, Contadores",
        }

    return {
        "short": "DT",
        "institution": "Dirección del Trabajo",
        "domain": "laboral",
        "audience": "contadores, asesores laborales, jefes de RRHH y empresas chilenas",
        "analysis_focus": (
            "Explicar impactos prácticos en cumplimiento laboral, remuneraciones, "
            "contratos, fiscalización, registros y obligaciones documentales."
        ),
        "impact_field": "impacto_laboral",
        "impact_label": "Impacto laboral",
        "tags": "Dirección del Trabajo, Normativa laboral, Contadores",
    }


def source_prefix(document: dict[str, Any]) -> str:
    return source_context(document)["short"]
