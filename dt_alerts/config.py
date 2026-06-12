from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DT_BASE_URL = "https://www.dt.gob.cl/legislacion/1624/"

DT_SOURCES = [
    {
        "category": "Portada normativa",
        "url": "https://www.dt.gob.cl/legislacion/1624/w3-channel.html",
    },
    {
        "category": "Resoluciones",
        "url": "https://www.dt.gob.cl/legislacion/1624/w3-propertyvalue-24000.html",
    },
    {
        "category": "Dictámenes",
        "url": "https://www.dt.gob.cl/legislacion/1624/w3-propertyvalue-22762.html",
    },
    {
        "category": "Órdenes de Servicio",
        "url": "https://www.dt.gob.cl/legislacion/1624/w3-propertyvalue-28189.html",
    },
    {
        "category": "Circulares",
        "url": "https://www.dt.gob.cl/legislacion/1624/w3-propertyvalue-81218.html",
    },
    {
        "category": "Ordinarios",
        "url": "https://www.dt.gob.cl/legislacion/1624/w3-propertyvalue-147182.html",
    },
    {
        "category": "Resumen de Jurisprudencia Administrativa",
        "url": "https://www.dt.gob.cl/legislacion/1624/w3-propertyvalue-174414.html",
    },
]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    app_host: str
    app_port: int
    admin_token: str
    job_token: str
    database_path: Path
    run_worker: bool
    run_on_startup: bool
    check_interval_hours: float
    max_listing_documents_per_source: int
    alert_on_first_run: bool
    openai_api_key: str
    openai_model: str
    resend_api_key: str
    email_from: str
    email_reply_to: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    whatsapp_enabled: bool
    whatsapp_phone_number_id: str
    whatsapp_access_token: str
    whatsapp_template_name: str
    whatsapp_language: str


def get_settings() -> Settings:
    db_path = Path(os.getenv("DATABASE_PATH", "data/dt_alertas.sqlite3"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Settings(
        app_base_url=os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        app_host=os.getenv("APP_HOST", "127.0.0.1"),
        app_port=env_int("APP_PORT", 8000),
        admin_token=os.getenv("ADMIN_TOKEN", "dev-admin-token"),
        job_token=os.getenv("JOB_TOKEN", os.getenv("ADMIN_TOKEN", "dev-job-token")),
        database_path=db_path,
        run_worker=env_bool("RUN_WORKER", True),
        run_on_startup=env_bool("RUN_ON_STARTUP", False),
        check_interval_hours=env_float("CHECK_INTERVAL_HOURS", 6.0),
        max_listing_documents_per_source=env_int("MAX_LISTING_DOCUMENTS_PER_SOURCE", 25),
        alert_on_first_run=env_bool("ALERT_ON_FIRST_RUN", False),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        email_from=os.getenv("EMAIL_FROM", "Alertas DT <alertas@example.com>"),
        email_reply_to=os.getenv("EMAIL_REPLY_TO", ""),
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=env_int("SMTP_PORT", 587),
        smtp_username=os.getenv("SMTP_USERNAME", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_use_tls=env_bool("SMTP_USE_TLS", True),
        whatsapp_enabled=env_bool("WHATSAPP_ENABLED", False),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_template_name=os.getenv(
            "WHATSAPP_TEMPLATE_NAME", "dt_alerta_normativa"
        ),
        whatsapp_language=os.getenv("WHATSAPP_LANGUAGE", "es"),
    )
