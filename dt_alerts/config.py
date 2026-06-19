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
    disable_admin_auth: bool
    database_path: Path
    run_worker: bool
    run_on_startup: bool
    check_interval_hours: float
    max_listing_documents_per_source: int
    alert_on_first_run: bool
    openai_api_key: str
    openai_model: str
    email_provider: str
    sendgrid_api_key: str
    resend_api_key: str
    email_from: str
    email_from_name: str
    email_reply_to: str
    test_email_to: str
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
    # WordPress sync
    wordpress_sync_enabled: bool
    wordpress_api_url: str
    wordpress_api_token: str
    wordpress_sync_interval_minutes: int
    wordpress_sync_limit: int
    # IA (desactivada por defecto; no rompe nada si no está configurada)
    ai_provider: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str
    ai_summary_temperature: float
    ai_timeout_seconds: int
    ai_max_input_chars: int
    ai_attachments_enabled: bool


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
        # Seguridad: el bypass de login solo se activa explícitamente (modo desarrollo).
        # Por defecto False -> el admin siempre exige ADMIN_TOKEN.
        disable_admin_auth=env_bool("DISABLE_ADMIN_AUTH", False),
        database_path=db_path,
        run_worker=env_bool("RUN_WORKER", True),
        run_on_startup=env_bool("RUN_ON_STARTUP", False),
        check_interval_hours=env_float("CHECK_INTERVAL_HOURS", 6.0),
        max_listing_documents_per_source=env_int("MAX_LISTING_DOCUMENTS_PER_SOURCE", 25),
        alert_on_first_run=env_bool("ALERT_ON_FIRST_RUN", False),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        # Proveedor de email recomendado: sendgrid. "console" = modo simulado para pruebas.
        # Se mantiene compatibilidad con resend/smtp si ya estaban configurados.
        email_provider=os.getenv("EMAIL_PROVIDER", "console").strip().lower(),
        sendgrid_api_key=os.getenv("SENDGRID_API_KEY", ""),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        email_from=os.getenv("EMAIL_FROM", "alertas@example.com"),
        email_from_name=os.getenv("EMAIL_FROM_NAME", "Alertas DT"),
        email_reply_to=os.getenv("EMAIL_REPLY_TO", ""),
        test_email_to=os.getenv("TEST_EMAIL_TO", ""),
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
        # WordPress sync (desactivado por defecto; no rompe si no está configurado)
        wordpress_sync_enabled=env_bool("WORDPRESS_SYNC_ENABLED", False),
        wordpress_api_url=os.getenv("WORDPRESS_API_URL", "").rstrip("/"),
        wordpress_api_token=os.getenv("WORDPRESS_API_TOKEN", ""),
        wordpress_sync_interval_minutes=env_int("WORDPRESS_SYNC_INTERVAL_MINUTES", 15),
        wordpress_sync_limit=env_int("WORDPRESS_SYNC_LIMIT", 100),
        # IA (desactivada por defecto; listo para integración)
        ai_provider=os.getenv("AI_PROVIDER", "disabled").strip().lower(),
        ai_api_key=os.getenv("AI_API_KEY", ""),
        ai_model=os.getenv("AI_MODEL", ""),
        ai_base_url=os.getenv("AI_BASE_URL", "").rstrip("/"),
        ai_summary_temperature=env_float("AI_SUMMARY_TEMPERATURE", 0.2),
        ai_timeout_seconds=env_int("AI_TIMEOUT_SECONDS", 60),
        ai_max_input_chars=env_int("AI_MAX_INPUT_CHARS", 45000),
        ai_attachments_enabled=env_bool("AI_ATTACHMENTS_ENABLED", True),
    )
