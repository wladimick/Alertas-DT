from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb) -> bool:
        result = super().__exit__(exc_type, exc, tb)
        self.close()
        return result


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(database_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(database_path: Path | str) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        migrate(conn)


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            whatsapp TEXT,
            notify_email INTEGER NOT NULL DEFAULT 1,
            notify_whatsapp INTEGER NOT NULL DEFAULT 0,
            whatsapp_opt_in INTEGER NOT NULL DEFAULT 0,
            consent INTEGER NOT NULL DEFAULT 0,
            source_page TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            premium_status TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dt_article_id TEXT NOT NULL UNIQUE,
            canonical_url TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            publication_date TEXT,
            abstract TEXT,
            detail_text TEXT,
            pdf_url TEXT,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'discovered',
            detected_at TEXT NOT NULL,
            processed_at TEXT,
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL UNIQUE,
            summary TEXT NOT NULL,
            key_points_json TEXT NOT NULL DEFAULT '[]',
            practical_impacts_json TEXT NOT NULL DEFAULT '[]',
            relevance TEXT NOT NULL DEFAULT 'medio',
            status TEXT NOT NULL DEFAULT 'ready',
            ai_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            subscriber_id INTEGER NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            provider_message_id TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            FOREIGN KEY(alert_id) REFERENCES alerts(id) ON DELETE CASCADE,
            FOREIGN KEY(subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE,
            UNIQUE(alert_id, subscriber_id, channel)
        );

        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            discovered_count INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL UNIQUE,
            provider TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            input_hash TEXT,
            content_quality TEXT,
            relevance TEXT,
            email_subject TEXT,
            email_summary TEXT,
            key_points_json TEXT,
            practical_impacts_json TEXT,
            recommended_actions_json TEXT,
            executive_summary TEXT,
            detailed_summary_json TEXT,
            tags_json TEXT,
            legal_disclaimer TEXT,
            raw_response_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER,
            alert_id INTEGER,
            provider TEXT,
            model TEXT,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            daily_limit INTEGER,
            monthly_limit INTEGER,
            error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status);
        CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
        CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
        CREATE INDEX IF NOT EXISTS idx_deliveries_alert ON deliveries(alert_id);
        CREATE INDEX IF NOT EXISTS idx_ai_summaries_document ON ai_summaries(document_id);
        CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_ai_usage_status ON ai_usage_logs(status);
        CREATE INDEX IF NOT EXISTS idx_ai_usage_operation ON ai_usage_logs(operation);
        """
    )


def as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(email)))


def normalize_whatsapp(value: str | None) -> str:
    cleaned = re.sub(r"[^\d+]", "", value or "")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


def upsert_subscriber(
    conn: sqlite3.Connection,
    *,
    email: str,
    whatsapp: str | None,
    notify_email: bool,
    notify_whatsapp: bool,
    source_page: str | None,
    consent: bool,
) -> dict[str, Any]:
    email = normalize_email(email)
    if not validate_email(email):
        raise ValueError("Ingresa un correo electrónico válido.")
    if not consent:
        raise ValueError("Debes aceptar recibir alertas para suscribirte.")

    phone = normalize_whatsapp(whatsapp)
    wants_whatsapp = bool(notify_whatsapp and phone)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO subscribers (
            email, whatsapp, notify_email, notify_whatsapp, whatsapp_opt_in,
            consent, source_page, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            whatsapp = excluded.whatsapp,
            notify_email = excluded.notify_email,
            notify_whatsapp = excluded.notify_whatsapp,
            whatsapp_opt_in = excluded.whatsapp_opt_in,
            consent = excluded.consent,
            -- Conserva el origen original si la nueva suscripción no trae source_page.
            source_page = COALESCE(excluded.source_page, subscribers.source_page),
            status = 'active',
            updated_at = excluded.updated_at
        """,
        (
            email,
            phone or None,
            int(notify_email),
            int(wants_whatsapp),
            int(wants_whatsapp),
            int(consent),
            source_page,
            now,
            now,
        ),
    )
    conn.commit()
    return as_dict(
        conn.execute("SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone()
    )


def list_subscribers(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM subscribers ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def active_subscribers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM subscribers WHERE status = 'active' ORDER BY created_at ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def set_subscriber_status(conn: sqlite3.Connection, subscriber_id: int, status: str) -> None:
    if status not in {"active", "paused"}:
        raise ValueError("Estado de suscriptor inválido.")
    conn.execute(
        "UPDATE subscribers SET status = ?, updated_at = ? WHERE id = ?",
        (status, utcnow(), subscriber_id),
    )
    conn.commit()


def count_documents(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS total FROM documents").fetchone()
    return int(row["total"])


def upsert_document(
    conn: sqlite3.Connection, doc: dict[str, Any], *, baseline: bool = False
) -> tuple[int, bool]:
    now = utcnow()
    status = "baseline" if baseline else doc.get("status", "discovered")
    values = (
        doc["dt_article_id"],
        doc["canonical_url"],
        doc["source_url"],
        doc["category"],
        doc["title"],
        doc.get("publication_date"),
        doc.get("abstract"),
        doc.get("detail_text"),
        doc.get("pdf_url"),
        doc.get("content_hash"),
        status,
        now,
        doc.get("processed_at"),
        doc.get("last_error"),
    )
    try:
        cur = conn.execute(
            """
            INSERT INTO documents (
                dt_article_id, canonical_url, source_url, category, title,
                publication_date, abstract, detail_text, pdf_url, content_hash,
                status, detected_at, processed_at, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.commit()
        return int(cur.lastrowid), True
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id, status FROM documents WHERE dt_article_id = ? OR canonical_url = ?",
            (doc["dt_article_id"], doc["canonical_url"]),
        ).fetchone()
        if not row:
            raise
        if row["status"] != "ignored":
            conn.execute(
                """
                UPDATE documents SET
                    source_url = ?, category = ?, title = ?, publication_date = ?,
                    abstract = COALESCE(?, abstract),
                    detail_text = COALESCE(?, detail_text),
                    pdf_url = COALESCE(?, pdf_url),
                    content_hash = COALESCE(?, content_hash)
                WHERE id = ?
                """,
                (
                    doc["source_url"],
                    doc["category"],
                    doc["title"],
                    doc.get("publication_date"),
                    doc.get("abstract"),
                    doc.get("detail_text"),
                    doc.get("pdf_url"),
                    doc.get("content_hash"),
                    row["id"],
                ),
            )
            conn.commit()
        return int(row["id"]), False


def update_document_processed(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    status: str,
    detail_text: str | None = None,
    pdf_url: str | None = None,
    content_hash: str | None = None,
    last_error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE documents SET
            status = ?,
            detail_text = COALESCE(?, detail_text),
            pdf_url = COALESCE(?, pdf_url),
            content_hash = COALESCE(?, content_hash),
            processed_at = ?,
            last_error = ?
        WHERE id = ?
        """,
        (status, detail_text, pdf_url, content_hash, utcnow(), last_error, document_id),
    )
    conn.commit()


def set_document_status(conn: sqlite3.Connection, document_id: int, status: str) -> None:
    if status not in {"discovered", "processed", "baseline", "ignored", "error"}:
        raise ValueError("Estado de documento inválido.")
    conn.execute(
        "UPDATE documents SET status = ?, processed_at = ? WHERE id = ?",
        (status, utcnow(), document_id),
    )
    conn.commit()


def list_documents(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM documents ORDER BY detected_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_document(conn: sqlite3.Connection, document_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    return dict(row) if row else None


def count_sent_deliveries(conn: sqlite3.Connection) -> int:
    """Envíos efectivos o simulados registrados en deliveries."""
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM deliveries WHERE status IN ('sent', 'simulated')"
    ).fetchone()
    return int(row["total"])


def create_or_update_alert(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    summary: str,
    key_points: list[str],
    practical_impacts: list[str],
    relevance: str,
    status: str,
    ai_error: str | None,
) -> int:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO alerts (
            document_id, summary, key_points_json, practical_impacts_json,
            relevance, status, ai_error, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            summary = excluded.summary,
            key_points_json = excluded.key_points_json,
            practical_impacts_json = excluded.practical_impacts_json,
            relevance = excluded.relevance,
            status = excluded.status,
            ai_error = excluded.ai_error,
            updated_at = excluded.updated_at
        """,
        (
            document_id,
            summary,
            json.dumps(key_points, ensure_ascii=False),
            json.dumps(practical_impacts, ensure_ascii=False),
            relevance,
            status,
            ai_error,
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM alerts WHERE document_id = ?", (document_id,)
    ).fetchone()
    return int(row["id"])


def set_alert_status(conn: sqlite3.Connection, alert_id: int, status: str) -> None:
    conn.execute(
        "UPDATE alerts SET status = ?, updated_at = ? WHERE id = ?",
        (status, utcnow(), alert_id),
    )
    conn.commit()


def list_alerts(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            alerts.*,
            documents.title,
            documents.category,
            documents.canonical_url,
            documents.publication_date,
            ai_summaries.status AS ai_status,
            ai_summaries.provider AS ai_provider,
            ai_summaries.content_quality AS ai_content_quality,
            ai_summaries.email_subject AS ai_email_subject,
            ai_summaries.error AS ai_summary_error
        FROM alerts
        JOIN documents ON documents.id = alerts.document_id
        LEFT JOIN ai_summaries ON ai_summaries.document_id = alerts.document_id
        ORDER BY alerts.created_at DESC, alerts.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_alert_with_document(
    conn: sqlite3.Connection, alert_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            alerts.*,
            documents.dt_article_id,
            documents.canonical_url,
            documents.category,
            documents.title,
            documents.publication_date,
            documents.abstract,
            documents.pdf_url,
            ai_summaries.id AS ai_summary_id,
            ai_summaries.provider AS ai_provider,
            ai_summaries.model AS ai_model,
            ai_summaries.status AS ai_status,
            ai_summaries.content_quality AS ai_content_quality,
            ai_summaries.relevance AS ai_relevance,
            ai_summaries.email_subject AS ai_email_subject,
            ai_summaries.email_summary AS ai_email_summary,
            ai_summaries.key_points_json AS ai_key_points_json,
            ai_summaries.practical_impacts_json AS ai_practical_impacts_json,
            ai_summaries.recommended_actions_json AS ai_recommended_actions_json,
            ai_summaries.executive_summary AS ai_executive_summary,
            ai_summaries.detailed_summary_json AS ai_detailed_summary_json,
            ai_summaries.tags_json AS ai_tags_json,
            ai_summaries.legal_disclaimer AS ai_legal_disclaimer,
            ai_summaries.error AS ai_summary_error,
            ai_summaries.updated_at AS ai_updated_at
        FROM alerts
        JOIN documents ON documents.id = alerts.document_id
        LEFT JOIN ai_summaries ON ai_summaries.document_id = alerts.document_id
        WHERE alerts.id = ?
        """,
        (alert_id,),
    ).fetchone()
    return dict(row) if row else None


def record_delivery(
    conn: sqlite3.Connection,
    *,
    alert_id: int,
    subscriber_id: int,
    channel: str,
    status: str,
    provider_message_id: str | None = None,
    error: str | None = None,
) -> None:
    now = utcnow()
    sent_at = now if status in {"sent", "simulated"} else None
    conn.execute(
        """
        INSERT INTO deliveries (
            alert_id, subscriber_id, channel, status, provider_message_id,
            error, created_at, sent_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alert_id, subscriber_id, channel) DO UPDATE SET
            status = excluded.status,
            provider_message_id = excluded.provider_message_id,
            error = excluded.error,
            sent_at = excluded.sent_at
        """,
        (
            alert_id,
            subscriber_id,
            channel,
            status,
            provider_message_id,
            error,
            now,
            sent_at,
        ),
    )
    conn.commit()


def delivery_stats(conn: sqlite3.Connection, alert_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT channel, status, COUNT(*) AS total
        FROM deliveries
        WHERE alert_id = ?
        GROUP BY channel, status
        ORDER BY channel, status
        """,
        (alert_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def start_job(conn: sqlite3.Connection, job_type: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_runs (job_type, status, started_at) VALUES (?, 'running', ?)",
        (job_type, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    discovered_count: int = 0,
    processed_count: int = 0,
    sent_count: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE job_runs SET
            status = ?,
            finished_at = ?,
            discovered_count = ?,
            processed_count = ?,
            sent_count = ?,
            error = ?
        WHERE id = ?
        """,
        (
            status,
            utcnow(),
            discovered_count,
            processed_count,
            sent_count,
            error,
            job_id,
        ),
    )
    conn.commit()


def latest_jobs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM job_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# app_settings — configuración operativa no sensible (no API keys)
# --------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )
    conn.commit()


def get_all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def count_table(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608
    return int(row["n"])


def last_delivery_sent_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sent_at FROM deliveries WHERE status IN ('sent','simulated') ORDER BY sent_at DESC LIMIT 1"
    ).fetchone()
    return row["sent_at"] if row else None


# --------------------------------------------------------------------------
# ai_summaries — resúmenes generados con IA por documento
# --------------------------------------------------------------------------

def get_ai_summary(conn: sqlite3.Connection, document_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM ai_summaries WHERE document_id = ?", (document_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_ai_summary(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    provider: str | None,
    model: str | None,
    status: str,
    input_hash: str | None = None,
    content_quality: str | None = None,
    relevance: str | None = None,
    email_subject: str | None = None,
    email_summary: str | None = None,
    key_points_json: str | None = None,
    practical_impacts_json: str | None = None,
    recommended_actions_json: str | None = None,
    executive_summary: str | None = None,
    detailed_summary_json: str | None = None,
    tags_json: str | None = None,
    legal_disclaimer: str | None = None,
    raw_response_json: str | None = None,
    error: str | None = None,
) -> int:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO ai_summaries (
            document_id, provider, model, status, input_hash, content_quality,
            relevance, email_subject, email_summary, key_points_json,
            practical_impacts_json, recommended_actions_json, executive_summary,
            detailed_summary_json, tags_json, legal_disclaimer, raw_response_json,
            error, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            provider = excluded.provider,
            model = excluded.model,
            status = excluded.status,
            input_hash = excluded.input_hash,
            content_quality = excluded.content_quality,
            relevance = excluded.relevance,
            email_subject = excluded.email_subject,
            email_summary = excluded.email_summary,
            key_points_json = excluded.key_points_json,
            practical_impacts_json = excluded.practical_impacts_json,
            recommended_actions_json = excluded.recommended_actions_json,
            executive_summary = excluded.executive_summary,
            detailed_summary_json = excluded.detailed_summary_json,
            tags_json = excluded.tags_json,
            legal_disclaimer = excluded.legal_disclaimer,
            raw_response_json = excluded.raw_response_json,
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (
            document_id, provider, model, status, input_hash, content_quality,
            relevance, email_subject, email_summary, key_points_json,
            practical_impacts_json, recommended_actions_json, executive_summary,
            detailed_summary_json, tags_json, legal_disclaimer, raw_response_json,
            error, now, now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM ai_summaries WHERE document_id = ?", (document_id,)
    ).fetchone()
    return int(row["id"])



# --------------------------------------------------------------------------
# ai_usage_logs — auditoría de consumo IA y límites internos
# --------------------------------------------------------------------------

def record_ai_usage(
    conn: sqlite3.Connection,
    *,
    document_id: int | None = None,
    alert_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    operation: str,
    status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    daily_limit: int | None = None,
    monthly_limit: int | None = None,
    error: str | None = None,
) -> int:
    """Registra un intento de uso IA. No guarda secretos ni API keys."""
    now = utcnow()
    conn.execute(
        """
        INSERT INTO ai_usage_logs (
            document_id, alert_id, provider, model, operation, status,
            input_tokens, output_tokens, total_tokens,
            daily_limit, monthly_limit, error, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            alert_id,
            provider,
            model,
            operation,
            status,
            int(input_tokens or 0),
            int(output_tokens or 0),
            int(total_tokens or 0),
            daily_limit,
            monthly_limit,
            error,
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
    return int(row["id"])


def _usage_sum_for_period(conn: sqlite3.Connection, prefix: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(total_tokens), 0) AS total
        FROM ai_usage_logs
        WHERE substr(created_at, 1, ?) = ?
          AND status = 'success'
        """,
        (len(prefix), prefix),
    ).fetchone()
    return int(row["total"] or 0)


def get_ai_usage_today(conn: sqlite3.Connection) -> int:
    """Tokens usados hoy UTC, solo llamadas exitosas."""
    return _usage_sum_for_period(conn, utcnow()[:10])


def get_ai_usage_month(conn: sqlite3.Connection) -> int:
    """Tokens usados este mes UTC, solo llamadas exitosas."""
    return _usage_sum_for_period(conn, utcnow()[:7])


def get_last_ai_usage(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ai_usage_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def get_last_ai_error(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ai_usage_logs
        WHERE status IN ('error', 'blocked_limit', 'missing_key')
           OR (error IS NOT NULL AND error != '')
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def get_recent_ai_usage(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    """Últimos N registros de uso IA para auditoría en panel de configuración."""
    rows = conn.execute(
        "SELECT * FROM ai_usage_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_ai_token_breakdown(conn: sqlite3.Connection) -> dict[str, int]:
    """Retorna sumas de input/output tokens para hoy y este mes, para cálculo de costo."""
    today_row = conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens), 0)  AS input_t,
               COALESCE(SUM(output_tokens), 0) AS output_t
        FROM ai_usage_logs
        WHERE status NOT IN ('disabled', 'missing_key', 'blocked_limit')
          AND date(created_at) = date('now')
        """
    ).fetchone()
    month_row = conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens), 0)  AS input_t,
               COALESCE(SUM(output_tokens), 0) AS output_t
        FROM ai_usage_logs
        WHERE status NOT IN ('disabled', 'missing_key', 'blocked_limit')
          AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        """
    ).fetchone()
    return {
        "today_input": today_row["input_t"] if today_row else 0,
        "today_output": today_row["output_t"] if today_row else 0,
        "month_input": month_row["input_t"] if month_row else 0,
        "month_output": month_row["output_t"] if month_row else 0,
    }


def get_ai_usage_status(
    conn: sqlite3.Connection,
    *,
    daily_limit: int,
    monthly_limit: int,
) -> dict[str, Any]:
    """Resumen de uso para mostrar en Configuración y validar límites."""
    today = get_ai_usage_today(conn)
    month = get_ai_usage_month(conn)
    last = get_last_ai_usage(conn)
    last_error = get_last_ai_error(conn)

    daily_pct = round((today / daily_limit) * 100, 1) if daily_limit > 0 else 0
    monthly_pct = round((month / monthly_limit) * 100, 1) if monthly_limit > 0 else 0

    return {
        "today_tokens": today,
        "month_tokens": month,
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "daily_percent": daily_pct,
        "monthly_percent": monthly_pct,
        "daily_exceeded": bool(daily_limit > 0 and today >= daily_limit),
        "monthly_exceeded": bool(monthly_limit > 0 and month >= monthly_limit),
        "last_usage": last,
        "last_error": last_error,
    }
