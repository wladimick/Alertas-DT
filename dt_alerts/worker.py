from __future__ import annotations

import json
import time
from typing import Any

from . import db
from .config import DT_SOURCES, Settings, get_settings
from .dt_scraper import ScrapedDocument, content_hash, enrich_document_detail, fetch_listing
from .notifier import dispatch_alert
from .summarizer import summarize_document


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
        try:
            for source in DT_SOURCES:
                try:
                    docs = fetch_listing(
                        source, limit=settings.max_listing_documents_per_source
                    )
                except Exception as exc:
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
                    sent_count += sent

            status = "success" if not source_errors else "partial"
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
    if detail_error and summary.status == "ready":
        summary.status = "pending_review"
        summary.ai_error = detail_error
    elif detail_error:
        summary.ai_error = f"{detail_error} | {summary.ai_error or ''}".strip(" |")

    alert_id = db.create_or_update_alert(
        conn,
        document_id,
        summary=summary.summary,
        key_points=summary.key_points,
        practical_impacts=summary.practical_impacts,
        relevance=summary.relevance,
        status=summary.status,
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

    sent = 0
    if summary.status == "ready":
        sent = dispatch_alert(conn, alert_id, settings)
    return alert_id, sent


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
