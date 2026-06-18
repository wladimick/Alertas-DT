"""Sincronización de suscriptores desde WordPress.

Si WORDPRESS_SYNC_ENABLED=false (por defecto), este módulo no hace nada
y no rompe el funcionamiento normal de la app.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .db import connect, upsert_subscriber

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fetch_subscribers(settings: Settings, page: int = 1) -> dict[str, Any]:
    """GET {api_url}/subscribers con paginación."""
    url = (
        f"{settings.wordpress_api_url}/subscribers"
        f"?status=active&limit={settings.wordpress_sync_limit}&page={page}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.wordpress_api_token}",
            "Accept": "application/json",
            "User-Agent": "AlertasDT-Sync/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _mark_synced(settings: Settings, ids: list[int], synced_at: str) -> None:
    """POST {api_url}/subscribers/synced."""
    if not ids:
        return
    url = f"{settings.wordpress_api_url}/subscribers/synced"
    body = json.dumps({"ids": ids, "synced_at": synced_at}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.wordpress_api_token}",
            "Content-Type": "application/json",
            "User-Agent": "AlertasDT-Sync/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            resp.read()
    except Exception as exc:
        logger.warning("wordpress_sync: no se pudo marcar sincronizados: %s", exc)


def sync(settings: Settings) -> dict[str, Any]:
    """Sincroniza suscriptores activos de WordPress a SQLite local.

    Retorna un dict con el resultado: status, counts, error.
    Nunca lanza excepciones hacia el caller.
    """
    result: dict[str, Any] = {
        "status": "disabled",
        "received": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "error": None,
        "synced_at": None,
    }

    if not settings.wordpress_sync_enabled:
        return result

    if not settings.wordpress_api_url or not settings.wordpress_api_token:
        result["status"] = "misconfigured"
        result["error"] = "WORDPRESS_API_URL o WORDPRESS_API_TOKEN no configurado."
        logger.warning("wordpress_sync: %s", result["error"])
        return result

    synced_at = _utcnow()
    synced_ids: list[int] = []

    try:
        page = 1
        while True:
            data = _fetch_subscribers(settings, page)
            if not data.get("ok"):
                raise ValueError(f"Respuesta inesperada de WordPress: {data}")

            rows: list[dict] = data.get("subscribers", [])
            result["received"] += len(rows)

            with connect(settings.database_path) as conn:
                for row in rows:
                    email = (row.get("email") or "").strip().lower()
                    if not email:
                        result["skipped"] += 1
                        continue
                    try:
                        sub = upsert_subscriber(
                            conn,
                            email=email,
                            whatsapp=None,
                            notify_email=True,
                            notify_whatsapp=False,
                            source_page=row.get("source_page") or "wordpress",
                            consent=bool(row.get("consent")),
                        )
                        wp_id = row.get("id")
                        if wp_id:
                            synced_ids.append(int(wp_id))
                        # Detect if record was just created (updated_at == created_at)
                        if sub.get("created_at") == sub.get("updated_at"):
                            result["created"] += 1
                        else:
                            result["updated"] += 1
                    except ValueError as exc:
                        logger.debug("wordpress_sync: omitido %s: %s", email, exc)
                        result["skipped"] += 1
                    except Exception as exc:
                        logger.warning("wordpress_sync: error al upsert %s: %s", email, exc)
                        result["skipped"] += 1

            total    = data.get("total", 0)
            per_page = data.get("limit", settings.wordpress_sync_limit)
            if page * per_page >= total or not rows:
                break
            page += 1

        _mark_synced(settings, synced_ids, synced_at)
        result["status"]    = "ok"
        result["synced_at"] = synced_at
        logger.info(
            "wordpress_sync: ok — recibidos=%d creados=%d actualizados=%d omitidos=%d",
            result["received"], result["created"], result["updated"], result["skipped"],
        )

    except urllib.error.HTTPError as exc:
        result["status"] = "error"
        result["error"]  = f"HTTP {exc.code}: {exc.reason}"
        logger.error("wordpress_sync: %s", result["error"])
    except urllib.error.URLError as exc:
        result["status"] = "error"
        result["error"]  = f"URLError: {exc.reason}"
        logger.error("wordpress_sync: %s", result["error"])
    except Exception as exc:
        result["status"] = "error"
        result["error"]  = str(exc)
        logger.error("wordpress_sync: error inesperado: %s", exc, exc_info=True)

    return result
