from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import db
from .config import DT_SOURCES, Settings, get_settings
from .dt_scraper import ScrapedDocument, content_hash, enrich_document_detail, fetch_listing
from .summarizer import summarize_document


log = logging.getLogger("dt_alerts.worker")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def run_check(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    db.init_db(settings.database_path)

    discovered_count = 0
    processed_count = 0
    sent_count = 0
    source_errors: list[str] = []

    with db.connect(settings.database_path) as conn:
        job_id = db.start_job(conn, "check-dt")
        baseline_run = db.count_documents(conn) == 0 and not settings.alert_on_first_run
        log.info(
            "Job check-dt iniciado (id=%s, baseline=%s, fuentes=%s)",
            job_id, baseline_run, len(DT_SOURCES),
        )
        try:
            for source in DT_SOURCES:
                try:
                    docs = fetch_listing(
                        source, limit=settings.max_listing_documents_per_source
                    )
                    log.info("Fuente '%s': %s documentos en listado", source["category"], len(docs))
                except Exception as exc:
                    # Un error en una fuente no rompe el resto del job.
                    log.warning("Fuente '%s' falló: %s", source["category"], exc)
                    source_errors.append(f"{source['category']}: {exc}")
                    continue

                for doc in docs:
                    document_id, is_new = db.upsert_document(
                        conn, doc.to_db_dict(), baseline=baseline_run
                    )
                    if not is_new:
                        continue

                    discovered_count += 1
                    if baseline_run:
                        continue

                    alert_id, sent = process_new_document(conn, document_id, doc, settings)
                    if alert_id:
                        processed_count += 1
                        log.info("Documento nuevo procesado: %s (alerta %s, pendiente revisión)", doc.title, alert_id)
                    sent_count += sent

            status = "success" if not source_errors else "partial"
            log.info(
                "Job check-dt %s: nuevos=%s procesados=%s envios=%s errores=%s",
                status, discovered_count, processed_count, sent_count, len(source_errors),
            )
            db.finish_job(
                conn,
                job_id,
                status=status,
                discovered_count=discovered_count,
                processed_count=processed_count,
                sent_count=sent_count,
                error=" | ".join(source_errors) if source_errors else None,
            )
            return {
                "status": status,
                "baseline_run": baseline_run,
                "discovered_count": discovered_count,
                "processed_count": processed_count,
                "sent_count": sent_count,
                "source_errors": source_errors,
            }
        except Exception as exc:
            log.error("Job check-dt falló: %s", exc)
            db.finish_job(
                conn,
                job_id,
                status="failed",
                discovered_count=discovered_count,
                processed_count=processed_count,
                sent_count=sent_count,
                error=str(exc),
            )
            raise


def process_new_document(
    conn, document_id: int, doc: ScrapedDocument, settings: Settings
) -> tuple[int | None, int]:
    detail_error: str | None = None
    try:
        enriched = enrich_document_detail(doc)
    except Exception as exc:
        detail_error = f"No se pudo extraer detalle: {exc}"
        doc.detail_text = doc.abstract or ""
        doc.content_hash = content_hash(
            [doc.dt_article_id, doc.title, doc.publication_date or "", doc.abstract or ""]
        )
        enriched = doc

    doc_dict = enriched.to_db_dict()
    summary = summarize_document(doc_dict, settings)
    if detail_error:
        summary.ai_error = f"{detail_error} | {summary.ai_error or ''}".strip(" |")

    # Flujo de revisión: toda alerta nueva nace 'pending_review'.
    # El envío a suscriptores es manual desde el admin (etapa 11), nunca automático.
    alert_id = db.create_or_update_alert(
        conn,
        document_id,
        summary=summary.summary,
        key_points=summary.key_points,
        practical_impacts=summary.practical_impacts,
        relevance=summary.relevance,
        status="pending_review",
        ai_error=summary.ai_error,
    )
    db.update_document_processed(
        conn,
        document_id,
        status="processed" if not detail_error else "error",
        detail_text=enriched.detail_text,
        pdf_url=enriched.pdf_url,
        content_hash=enriched.content_hash,
        last_error=detail_error,
    )

    # No se envía nada en el job: las alertas quedan listas para revisión.
    return alert_id, 0


def regenerate_alert(conn, document_id: int, settings: Settings | None = None) -> int | None:
    """
    Regenera el resumen/alerta de un documento ya almacenado, sin volver a scrapear.
    Útil desde el admin. La alerta vuelve a 'pending_review'.
    """
    settings = settings or get_settings()
    document = db.get_document(conn, document_id)
    if not document:
        return None
    summary = summarize_document(document, settings)
    alert_id = db.create_or_update_alert(
        conn,
        document_id,
        summary=summary.summary,
        key_points=summary.key_points,
        practical_impacts=summary.practical_impacts,
        relevance=summary.relevance,
        status="pending_review",
        ai_error=summary.ai_error,
    )
    log.info("Resumen regenerado para documento %s (alerta %s)", document_id, alert_id)
    return alert_id


def scheduler_loop(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    delay = max(settings.check_interval_hours, 0.1) * 60 * 60
    if settings.run_on_startup:
        run_check(settings)
    while True:
        time.sleep(delay)
        run_check(settings)


def main() -> None:
    result = run_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
