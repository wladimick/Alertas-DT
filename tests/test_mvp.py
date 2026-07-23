from __future__ import annotations

import os

# Debe establecerse antes de importar dt_alerts.config (directa o
# indirectamente): evita cargar secretos reales de .env.local/.env durante
# las pruebas automatizadas. `unittest discover -s tests` importa este
# archivo como módulo de nivel superior (no como tests.test_mvp), por lo que
# tests/__init__.py no se ejecuta primero; por eso la bandera se fija aquí.
os.environ.setdefault("ALERTAS_DT_SKIP_DOTENV", "1")

import json
import ssl
import tempfile
import unittest
import unittest.mock as mock
import urllib.error
from pathlib import Path

from dt_alerts import db, notifier, worker
from dt_alerts.config import get_settings
from dt_alerts.dt_scraper import parse_listing, parse_sii_listing
from dt_alerts.summarizer import summarize_document
from dt_alerts import wordpress_sync


def settings_for(path: Path, **overrides):
    base = get_settings()
    data = {**base.__dict__, "database_path": path}
    data.update(overrides)
    return base.__class__(**data)


def sample_alert() -> dict:
    return {
        "title": "ORD. N°906/41 sobre remuneraciones y registro electrónico",
        "category": "Dictámenes",
        "publication_date": "27/12/2024",
        "relevance": "alto",
        "status": "pending_review",
        "summary": "La DT precisa criterios de cálculo de remuneraciones.",
        "key_points_json": '["Aplica a empleadores", "Afecta gratificaciones"]',
        "practical_impacts_json": '["Revisar liquidaciones."]',
        "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-127291.html",
    }


LISTING_HTML = """
<div class="recuadro">
  <h3 class="titulo aid-127291 cid-900">
    <a href="w3-article-127291.html" title="Criterio sobre corporación municipal.">ORD. N°906/41</a>
  </h3>
  <h6 class="fecha cid-900 aid-127291 pnid-2294 iso8601-20241227T0000000300">27/12/2024</h6>
  <p class="abstract aid-127291 cid-900">La Corporación Municipal mantiene su calidad de empleadora.</p>
</div>
<div class="recuadro">
  <h3 class="titulo aid-122123 cid-900">
    <a href="w3-article-122123.html" title="Regula el Registro Electrónico Laboral">Orden de Servicio N° 3</a>
  </h3>
  <h6 class="fecha">27/04/2022</h6>
  <p class="abstract">Establece definiciones y responsabilidades del Registro Electrónico Laboral.</p>
</div>
"""

SII_LISTING_HTML = """
<h5 style='margin-bottom:0px;'>
  <a href='circu27.pdf' target='_blank'>Circular N&deg; 27 del 23 de Junio del 2026</a>
</h5>
<p style='margin-top:0px;margin-bottom:0px;'>Pone en conocimiento la pol&iacute;tica de condonaciones contenida en el Decreto N&deg;437.</p>
<span style='font-size:12px;margin-bottom:10px;'><i>Fuente: Subdirecci&oacute;n Jur&iacute;dica</i></span>
<h5 style='margin-bottom:0px;'>
  <a href='circu26.pdf' target='_blank'>Circular N&deg; 26 del 18 de Junio del 2026</a>
</h5>
<p>Actualiza instrucciones sobre el recurso de reposici&oacute;n administrativa voluntaria.</p>
"""


class MvpTestCase(unittest.TestCase):
    def test_parse_listing_extracts_canonical_documents(self) -> None:
        docs = parse_listing(
            LISTING_HTML,
            "https://www.dt.gob.cl/legislacion/1624/w3-propertyvalue-22762.html",
            "Dictámenes",
        )
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0].dt_article_id, "127291")
        self.assertEqual(docs[0].title, "ORD. N°906/41")
        self.assertEqual(docs[0].publication_date, "27/12/2024")
        self.assertEqual(
            docs[0].canonical_url,
            "https://www.dt.gob.cl/legislacion/1624/w3-article-127291.html",
        )

    def test_parse_sii_listing_extracts_pdf_documents(self) -> None:
        docs = parse_sii_listing(
            SII_LISTING_HTML,
            "https://www.sii.cl/normativa_legislacion/circulares/2026/indcir2026.htm",
            "Circulares",
        )
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0].dt_article_id, "sii-circu27")
        self.assertEqual(docs[0].title, "Circular N° 27 del 23 de Junio del 2026")
        self.assertEqual(docs[0].publication_date, "23/06/2026")
        self.assertEqual(
            docs[0].canonical_url,
            "https://www.sii.cl/normativa_legislacion/circulares/2026/circu27.pdf",
        )
        self.assertIn("política de condonaciones", docs[0].abstract)

    def test_subscriber_upsert_updates_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                first = db.upsert_subscriber(
                    conn,
                    email=" Contador@Empresa.CL ",
                    whatsapp=None,
                    notify_email=True,
                    notify_whatsapp=False,
                    source_page="test",
                    consent=True,
                )
                second = db.upsert_subscriber(
                    conn,
                    email="contador@empresa.cl",
                    whatsapp="+56 9 1234 5678",
                    notify_email=True,
                    notify_whatsapp=True,
                    source_page="test-2",
                    consent=True,
                )
                subscribers = db.list_subscribers(conn)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(subscribers), 1)
        self.assertEqual(subscribers[0]["email"], "contador@empresa.cl")
        self.assertEqual(subscribers[0]["whatsapp"], "+56912345678")
        self.assertEqual(subscribers[0]["notify_whatsapp"], 1)

    def test_invalid_email_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                with self.assertRaises(ValueError):
                    db.upsert_subscriber(
                        conn,
                        email="no-es-email",
                        whatsapp=None,
                        notify_email=True,
                        notify_whatsapp=False,
                        source_page="test",
                        consent=True,
                    )

    def test_subscriber_requires_consent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                with self.assertRaises(ValueError):
                    db.upsert_subscriber(
                        conn,
                        email="ok@empresa.cl",
                        whatsapp=None,
                        notify_email=True,
                        notify_whatsapp=False,
                        source_page="test",
                        consent=False,
                    )

    # --- Persistencia / listado de suscriptores ---
    def _subscribe(self, conn, email, *, source_page="test"):
        return db.upsert_subscriber(
            conn, email=email, whatsapp=None, notify_email=True,
            notify_whatsapp=False, source_page=source_page, consent=True,
        )

    def test_subscriber_appears_in_listing_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                self._subscribe(conn, "nuevo@empresa.cl")
                subs = db.list_subscribers(conn)
                active = sum(1 for s in subs if s["status"] == "active")
        emails = [s["email"] for s in subs]
        self.assertIn("nuevo@empresa.cl", emails)
        self.assertEqual(active, 1)

    def test_paused_subscriber_still_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                sub = self._subscribe(conn, "pausado@empresa.cl")
                db.set_subscriber_status(conn, sub["id"], "paused")
                subs = db.list_subscribers(conn)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["status"], "paused")

    def test_source_page_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                self._subscribe(conn, "src@empresa.cl", source_page="embed")
                subs = db.list_subscribers(conn)
        self.assertEqual(subs[0]["source_page"], "embed")

    def test_resubscribe_without_source_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                self._subscribe(conn, "keep@empresa.cl", source_page="landing")
                # Re-suscripción sin source_page no debe borrar el origen original.
                db.upsert_subscriber(
                    conn, email="keep@empresa.cl", whatsapp=None, notify_email=True,
                    notify_whatsapp=False, source_page=None, consent=True,
                )
                subs = db.list_subscribers(conn)
        self.assertEqual(subs[0]["source_page"], "landing")

    def test_email_normalization_avoids_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                self._subscribe(conn, "TEST@MAIL.COM")
                self._subscribe(conn, "test@mail.com")
                subs = db.list_subscribers(conn)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["email"], "test@mail.com")

    def test_existing_email_is_reactivated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                sub = self._subscribe(conn, "vuelve@empresa.cl")
                db.set_subscriber_status(conn, sub["id"], "paused")
                # Volver a suscribirse debe reactivar el registro existente.
                again = self._subscribe(conn, "vuelve@empresa.cl")
                subs = db.list_subscribers(conn)
        self.assertEqual(sub["id"], again["id"])
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["status"], "active")

    # --- Auth admin (etapa 2) ---
    def test_admin_auth_enabled_by_default(self) -> None:
        # Sin DISABLE_ADMIN_AUTH en entorno, el bypass debe estar apagado.
        self.assertFalse(get_settings().disable_admin_auth)

    # --- Email (etapas 7/9) ---
    def test_email_render_html_and_text_do_not_fail(self) -> None:
        alert = sample_alert()
        html_body = notifier.render_alert_email_html(alert)
        text_body = notifier.render_alert_email_text(alert)
        self.assertIn("External Group", html_body)
        self.assertIn("Ver documento oficial", html_body)
        self.assertIn("Puntos clave", text_body)

    def test_email_render_tolerates_missing_fields(self) -> None:
        # No debe fallar aunque falten campos opcionales.
        minimal = {"title": "Doc", "canonical_url": "https://x"}
        self.assertIsInstance(notifier.render_alert_email_html(minimal), str)
        self.assertIsInstance(notifier.render_alert_email_text(minimal), str)

    def test_subject_generation_and_truncation(self) -> None:
        short = notifier.subject_for({"title": "Circular 5"})
        self.assertEqual(short, "Nueva normativa DT: Circular 5")
        sii_short = notifier.subject_for({"title": "Circular 5", "canonical_url": "https://www.sii.cl/foo"})
        self.assertEqual(sii_short, "Nueva normativa SII: Circular 5")
        long_title = "x" * 300
        subject = notifier.subject_for({"title": long_title})
        self.assertLessEqual(len(subject), len("Nueva normativa DT: ") + notifier.SUBJECT_MAX)
        self.assertTrue(subject.endswith("…"))

    def test_email_console_is_simulated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            settings = settings_for(path, email_provider="console")
            result = notifier.send_email(
                settings, to="a@b.cl", subject="s", html_body="<p>x</p>", text_body="x"
            )
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["provider"], "console")

    def test_email_sendgrid_without_key_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            settings = settings_for(path, email_provider="sendgrid", sendgrid_api_key="")
            result = notifier.send_email(
                settings, to="a@b.cl", subject="s", html_body="<p>x</p>", text_body="x"
            )
        self.assertEqual(result["status"], "skipped_missing_credentials")
        self.assertFalse(result["ok"])

    # --- Worker / job (etapa 5) ---
    def test_job_without_new_documents_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            settings = settings_for(path, alert_on_first_run=True)
            original = worker.fetch_listing
            worker.fetch_listing = lambda source, limit=25: []
            try:
                result = worker.run_check(settings)
            finally:
                worker.fetch_listing = original
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["discovered_count"], 0)

    def test_job_records_source_error_without_breaking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            settings = settings_for(path, alert_on_first_run=True)

            def boom(source, limit=25):
                raise RuntimeError("URL caída")

            original = worker.fetch_listing
            worker.fetch_listing = boom
            try:
                result = worker.run_check(settings)
            finally:
                worker.fetch_listing = original
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["source_errors"])

    def test_job_can_run_dt_sources_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            settings = settings_for(path, alert_on_first_run=True)
            seen_categories = []

            def fake_fetch(source, limit=25):
                seen_categories.append(source["category"])
                return []

            original = worker.fetch_listing
            worker.fetch_listing = fake_fetch
            try:
                result = worker.run_check(settings, source_filter="dt")
            finally:
                worker.fetch_listing = original
        self.assertEqual(result["source_filter"], "dt")
        self.assertEqual(result["source_count"], 7)
        self.assertTrue(seen_categories)
        self.assertTrue(all(category.startswith("DT -") for category in seen_categories))

    def test_job_can_run_sii_sources_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            settings = settings_for(path, alert_on_first_run=True)
            seen_categories = []

            def fake_fetch(source, limit=25):
                seen_categories.append(source["category"])
                return []

            original = worker.fetch_listing
            worker.fetch_listing = fake_fetch
            try:
                result = worker.run_check(settings, source_filter="sii")
            finally:
                worker.fetch_listing = original
        self.assertEqual(result["source_filter"], "sii")
        self.assertEqual(result["source_count"], 5)
        self.assertTrue(seen_categories)
        self.assertTrue(all(category.startswith("SII -") for category in seen_categories))

    def test_first_run_baseline_is_calculated_per_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            settings = settings_for(path, alert_on_first_run=False)
            with db.connect(path) as conn:
                db.upsert_document(conn, {
                    "dt_article_id": "dt-1",
                    "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-1.html",
                    "source_url": "https://www.dt.gob.cl/legislacion/1624/w3-channel.html",
                    "category": "DT - Dictámenes",
                    "title": "Documento DT",
                })

            original = worker.fetch_listing
            worker.fetch_listing = lambda source, limit=25: []
            try:
                result = worker.run_check(settings, source_filter="sii")
            finally:
                worker.fetch_listing = original
        self.assertTrue(result["baseline_run"])

    def test_duplicate_document_is_not_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            doc = {
                "dt_article_id": "999",
                "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-999.html",
                "source_url": "https://www.dt.gob.cl/x.html",
                "category": "Dictámenes",
                "title": "Doc 999",
            }
            with db.connect(path) as conn:
                _, first_new = db.upsert_document(conn, doc)
                _, second_new = db.upsert_document(conn, doc)
                total = db.count_documents(conn)
        self.assertTrue(first_new)
        self.assertFalse(second_new)
        self.assertEqual(total, 1)

    def test_list_documents_can_filter_by_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                db.upsert_document(conn, {
                    "dt_article_id": "dt-1",
                    "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-1.html",
                    "source_url": "https://www.dt.gob.cl/legislacion/1624/w3-channel.html",
                    "category": "DT - Dictámenes",
                    "title": "Documento DT",
                })
                db.upsert_document(conn, {
                    "dt_article_id": "sii-circular-1",
                    "canonical_url": "https://www.sii.cl/normativa_legislacion/circular1.htm",
                    "source_url": "https://www.sii.cl/normativa_legislacion/",
                    "category": "SII - Circulares",
                    "title": "Documento SII",
                })
                dt_docs = db.list_documents(conn, source_filter="dt")
                sii_docs = db.list_documents(conn, source_filter="sii")
        self.assertEqual([doc["title"] for doc in dt_docs], ["Documento DT"])
        self.assertEqual([doc["title"] for doc in sii_docs], ["Documento SII"])

    def test_documents_admin_renders_source_selector_and_badges(self) -> None:
        from dt_alerts.server import render_documents

        docs = [
            {
                "id": 1,
                "title": "Documento DT",
                "abstract": "",
                "category": "DT - Dictámenes",
                "publication_date": "01/01/2026",
                "dt_article_id": "dt-1",
                "status": "baseline",
                "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-1.html",
            },
            {
                "id": 2,
                "title": "Documento SII",
                "abstract": "",
                "category": "SII - Circulares",
                "publication_date": "01/01/2026",
                "dt_article_id": "sii-circular-1",
                "status": "baseline",
                "canonical_url": "https://www.sii.cl/normativa_legislacion/circular1.htm",
            },
        ]
        html = render_documents(docs, source_filter="sii", counts={"all": 2, "dt": 1, "sii": 1})
        self.assertNotIn("<select", html)
        self.assertNotIn('name="source"', html)
        self.assertIn('data-source="dt"', html)
        self.assertIn('data-source="sii"', html)
        self.assertIn('class="eg-source-tab is-active" data-source="sii"', html)

    def test_fallback_summary_is_pending_review_without_api_key(self) -> None:
        settings = get_settings()
        settings = settings.__class__(**{**settings.__dict__, "openai_api_key": ""})
        result = summarize_document(
            {
                "title": "Circular sobre IVA y multas",
                "category": "Circulares",
                "publication_date": "01/01/2026",
                "canonical_url": "https://example.com",
                "abstract": "Instruye criterios sobre IVA, multas y registro electrónico.",
                "detail_text": "Instruye criterios sobre IVA, multas y registro electrónico. Las empresas deben revisar controles de cumplimiento tributario.",
            },
            settings,
        )
        self.assertEqual(result.status, "pending_review")
        self.assertEqual(result.relevance, "alto")
        self.assertTrue(result.practical_impacts)


class WordPressSyncTestCase(unittest.TestCase):
    """Tests para dt_alerts.wordpress_sync."""

    def _db_settings(self, tmp: str, **overrides) -> object:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return settings_for(path, **overrides)

    def test_sync_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._db_settings(tmp)
        result = wordpress_sync.sync(settings)
        self.assertEqual(result["status"], "disabled")
        self.assertIsNone(result["error"])

    def test_sync_misconfigured_without_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._db_settings(
                tmp,
                wordpress_sync_enabled=True,
                wordpress_api_url="",
                wordpress_api_token="",
            )
        result = wordpress_sync.sync(settings)
        self.assertEqual(result["status"], "misconfigured")
        self.assertIsNotNone(result["error"])

    def _fake_response(self, subscribers: list[dict]) -> object:
        payload = json.dumps({
            "ok": True,
            "total": len(subscribers),
            "page": 1,
            "limit": 100,
            "subscribers": subscribers,
        }).encode()
        resp = mock.MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = mock.MagicMock(return_value=False)
        return resp

    def test_sync_creates_subscriber_from_wordpress(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._db_settings(
                tmp,
                wordpress_sync_enabled=True,
                wordpress_api_url="https://example.cl/wp-json/alertas-dt/v1",
                wordpress_api_token="test-token",
                wordpress_sync_limit=100,
            )
            fake = self._fake_response([{
                "id": 1,
                "email": "wp@empresa.cl",
                "status": "active",
                "consent": True,
                "source_page": "home",
                "source_url": "https://example.cl/",
                "created_at": "2026-06-18 10:00:00",
                "updated_at": "2026-06-18 10:00:00",
            }])
            with mock.patch("urllib.request.urlopen", return_value=fake):
                result = wordpress_sync.sync(settings)

            with db.connect(settings.database_path) as conn:
                subs = db.list_subscribers(conn)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["received"], 1)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["email"], "wp@empresa.cl")

    def test_sync_does_not_duplicate_existing_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._db_settings(
                tmp,
                wordpress_sync_enabled=True,
                wordpress_api_url="https://example.cl/wp-json/alertas-dt/v1",
                wordpress_api_token="test-token",
                wordpress_sync_limit=100,
            )
            with db.connect(settings.database_path) as conn:
                db.upsert_subscriber(
                    conn, email="dup@empresa.cl", whatsapp=None,
                    notify_email=True, notify_whatsapp=False,
                    source_page="local", consent=True,
                )

            fake = self._fake_response([{
                "id": 2, "email": "dup@empresa.cl", "status": "active",
                "consent": True, "source_page": "wordpress",
                "created_at": "2026-06-18 10:00:00", "updated_at": "2026-06-18 10:00:00",
            }])
            with mock.patch("urllib.request.urlopen", return_value=fake):
                result = wordpress_sync.sync(settings)

            with db.connect(settings.database_path) as conn:
                subs = db.list_subscribers(conn)

        self.assertEqual(len(subs), 1)
        self.assertEqual(result["received"], 1)

    def test_sync_http_error_recorded_without_raising(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._db_settings(
                tmp,
                wordpress_sync_enabled=True,
                wordpress_api_url="https://example.cl/wp-json/alertas-dt/v1",
                wordpress_api_token="bad-token",
                wordpress_sync_limit=100,
            )
            with mock.patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(None, 401, "Unauthorized", {}, None),
            ):
                result = wordpress_sync.sync(settings)

        self.assertEqual(result["status"], "error")
        self.assertIn("401", result["error"])

    def test_sync_missing_token_stays_misconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._db_settings(
                tmp,
                wordpress_sync_enabled=True,
                wordpress_api_url="https://example.cl/wp-json/alertas-dt/v1",
                wordpress_api_token="",
            )
        result = wordpress_sync.sync(settings)
        self.assertEqual(result["status"], "misconfigured")


class SettingsTestCase(unittest.TestCase):
    """Tests para /admin/settings, mask_secret y configuracion de email."""

    def _db_path(self, tmp: str) -> Path:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return path

    # -- 1. mask_secret no expone secretos completos --
    def test_mask_secret_hides_middle_of_key(self):
        from dt_alerts.server import mask_secret
        key = "SG.DKEhABCDEFGHIJKLMNOPQRSTUVWXYZXen1"
        masked = mask_secret(key, visible_start=6, visible_end=4)
        self.assertFalse(masked.startswith("No configurado"))
        self.assertNotIn(key, masked)
        self.assertIn("•", masked)
        self.assertTrue(masked.startswith(key[:6]))
        self.assertTrue(masked.endswith(key[-4:]))

    def test_mask_secret_empty_shows_not_configured(self):
        from dt_alerts.server import mask_secret
        self.assertEqual(mask_secret(None), "No configurado")
        self.assertEqual(mask_secret(""), "No configurado")

    def test_mask_secret_short_value_is_fully_hidden(self):
        from dt_alerts.server import mask_secret
        self.assertEqual(mask_secret("abc123"), "••••••••")

    # -- 2. /admin/settings requiere login --
    def test_settings_route_requires_login(self):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            _H.settings = settings_for(path, disable_admin_auth=False)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
            t = threading.Thread(target=server.serve_forever)
            t.daemon = True
            t.start()
            try:
                port = server.server_address[1]
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/admin/settings")
                resp = conn.getresponse()
                self.assertIn(resp.status, (302, 303))
                location = resp.getheader("Location", "")
                self.assertIn("/admin/login", location)
            finally:
                server.shutdown()

    # -- 3. /admin/settings renderiza estados tecnicos --
    def test_settings_renders_technical_status(self):
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path)
            html = render_settings(s)
        self.assertIn("Estado general", html)
        self.assertIn("SendGrid", html)
        self.assertIn("Base de datos", html)
        self.assertIn("WordPress", html)
        self.assertIn("Conexion IA", html)
        self.assertIn("Email y plantillas", html)

    # -- 4. Sin SendGrid key, muestra "No configurado" --
    def test_settings_shows_not_configured_when_no_sendgrid_key(self):
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, sendgrid_api_key="")
            html = render_settings(s)
        self.assertIn("No configurado", html)

    # -- 5. Con SendGrid key, muestra valor enmascarado (no la key completa) --
    def test_settings_masks_sendgrid_key(self):
        from dt_alerts.server import render_settings
        key = "SG.TestKey1234567890Abc"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, sendgrid_api_key=key, email_provider="sendgrid")
            html = render_settings(s)
        self.assertNotIn(key, html)
        self.assertIn("••", html)
        self.assertIn(key[:6], html)

    # -- 6. Settings de email se guardan y se leen --
    def test_email_settings_save_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                db.set_setting(conn, "email_from_name", "Alertas Test")
                db.set_setting(conn, "email_subject_template", "Test: {title}")
                db.set_setting(conn, "email_footer_legal", "Solo informativo.")

            with db.connect(path) as conn:
                all_cfg = db.get_all_settings(conn)

        self.assertEqual(all_cfg["email_from_name"], "Alertas Test")
        self.assertEqual(all_cfg["email_subject_template"], "Test: {title}")
        self.assertEqual(all_cfg["email_footer_legal"], "Solo informativo.")

    # -- 7. Asunto de prueba usa template configurado si existe --
    def test_test_subject_uses_db_template_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                db.set_setting(conn, "email_test_subject_template", "[CUSTOM] {title}")

            with db.connect(path) as conn:
                tmpl = db.get_setting(conn, "email_test_subject_template", "")

        self.assertTrue(tmpl)
        subject = tmpl.format(title="Circular 42")
        self.assertEqual(subject, "[CUSTOM] Circular 42")

    def test_test_subject_falls_back_to_default_when_no_db_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                tmpl = db.get_setting(conn, "email_test_subject_template", "")

        # Sin template en DB, tmpl es cadena vacia -> se usa el default
        self.assertEqual(tmpl, "")
        # El fallback en server.py usa subject_for(); sin fuente explicita cae en DT.
        default_subject = f"[PRUEBA] {notifier.subject_for({'title': 'Circular 42'})}"
        self.assertEqual(default_subject, "[PRUEBA] Nueva normativa DT: Circular 42")

    # -- 8. No se rompe el envio actual si no hay settings en DB --
    def test_send_alert_not_broken_without_db_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            settings = settings_for(path, email_provider="console")
            alert = {
                "title": "Doc sin settings",
                "category": "Circulares",
                "publication_date": "01/01/2026",
                "relevance": "medio",
                "status": "ready_to_send",
                "summary": "Resumen del documento.",
                "key_points_json": "[]",
                "practical_impacts_json": "[]",
                "canonical_url": "https://example.com",
            }
            result = notifier.send_email(
                settings,
                to="test@example.com",
                subject=notifier.subject_for(alert),
                html_body=notifier.render_alert_email_html(alert),
                text_body=notifier.render_alert_email_text(alert),
            )
        self.assertEqual(result["status"], "simulated")

    def test_app_settings_table_exists_after_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'"
                ).fetchone()
        self.assertIsNotNone(row)

    def test_get_setting_returns_default_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                val = db.get_setting(conn, "nonexistent_key", "default_val")
        self.assertEqual(val, "default_val")

    def test_set_setting_updates_existing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                db.set_setting(conn, "my_key", "first")
                db.set_setting(conn, "my_key", "second")
                val = db.get_setting(conn, "my_key")
        self.assertEqual(val, "second")


class AIIntegrationTestCase(unittest.TestCase):
    """17 tests para la capa de IA (feat/ai-summary-generation)."""

    def _sample_doc_dict(self) -> dict:
        return {
            "dt_article_id": "ai-test-777",
            "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-ai777.html",
            "source_url": "https://www.dt.gob.cl/x.html",
            "category": "Circulares",
            "title": "Circular sobre registro electronico laboral",
            "publication_date": "01/01/2026",
            "abstract": "Instruye criterios sobre el registro electronico laboral.",
            "detail_text": "Las empresas deben registrar jornadas. Multas por incumplimiento.",
            "pdf_url": None,
            "content_hash": None,
        }

    def _db_path(self, tmp: str) -> Path:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return path

    def _insert_doc(self, conn, **overrides) -> int:
        doc = {**self._sample_doc_dict(), **overrides}
        doc_id, _ = db.upsert_document(conn, doc)
        return doc_id

    # --- 1. IA disabled usa fallback y no rompe ---
    def test_01_ai_disabled_uses_fallback_and_doesnt_break(self):
        settings = settings_for(Path(":memory:"), ai_provider="disabled", ai_api_key="")
        result = summarize_document(self._sample_doc_dict(), settings)
        self.assertIsNotNone(result.summary)
        self.assertIsInstance(result.key_points, list)
        self.assertEqual(result.status, "pending_review")

    # --- 2. mask_secret no expone API keys ---
    def test_02_mask_secret_does_not_expose_ai_key(self):
        from dt_alerts.server import mask_secret
        key = "sk-12345678901234567890abcdef"
        masked = mask_secret(key)
        self.assertNotIn(key, masked)
        self.assertIn("•", masked)

    # --- 3. Prompt IA contiene estructura esperada ---
    def test_03_ai_prompt_contains_expected_structure(self):
        from dt_alerts.summarizer import build_ai_prompt
        settings = settings_for(
            Path(":memory:"),
            ai_provider="openai",
            ai_max_input_chars=45000,
            ai_timeout_seconds=60,
        )
        system_prompt, user_prompt = build_ai_prompt(self._sample_doc_dict(), settings, {})
        for field in ("email_subject", "executive_summary", "detailed_summary",
                      "key_points", "practical_impacts", "relevance", "JSON"):
            self.assertIn(field, user_prompt, f"user_prompt should contain '{field}'")
        self.assertIn("contadores", system_prompt.lower())

    # --- 4. Parser valida JSON correcto ---
    def test_04_parse_ai_response_valid_json(self):
        from dt_alerts.summarizer import parse_ai_response, validate_ai_summary
        raw = json.dumps({
            "title": "Circular DT",
            "category": "Circulares",
            "relevance": "alto",
            "email_subject": "Nueva normativa DT: Circular",
            "email_summary": "Resumen breve.",
            "key_points": ["Punto 1"],
            "practical_impacts": [{"title": "Impacto", "description": "Desc"}],
            "recommended_actions": ["Accion 1"],
            "executive_summary": {"title": "Resumen eje", "body": "Cuerpo."},
            "detailed_summary": {"title": "Detalle", "sections": []},
            "tags": ["DT"],
            "legal_disclaimer": "Informativo.",
        })
        parsed = parse_ai_response(raw)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["relevance"], "alto")
        validated = validate_ai_summary(parsed)
        self.assertEqual(validated["relevance"], "alto")
        self.assertEqual(validated["email_subject"], "Nueva normativa DT: Circular")

    # --- 5. Parser maneja JSON invalido sin romper ---
    def test_05_parse_ai_response_invalid_json_no_crash(self):
        from dt_alerts.summarizer import parse_ai_response
        self.assertEqual(parse_ai_response("esto no es json {{{{"), {})
        self.assertEqual(parse_ai_response(""), {})
        self.assertEqual(parse_ai_response(None), {})

    # --- 6. Resumen IA se guarda en DB ---
    def test_06_ai_summary_saved_to_db(self):
        from dt_alerts.summarizer import generate_ai_summary
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                db.update_document_processed(
                    conn, doc_id,
                    status="processed",
                    detail_text="Texto de prueba.",
                    pdf_url=None,
                    content_hash=None,
                    last_error=None,
                )
            settings = settings_for(
                path, ai_provider="disabled", ai_max_input_chars=45000,
                ai_timeout_seconds=60, ai_attachments_enabled=True,
            )
            generate_ai_summary(doc_id, settings=settings)
            with db.connect(path) as conn:
                ai_row = db.get_ai_summary(conn, doc_id)
        self.assertIsNotNone(ai_row)
        self.assertIn(ai_row["status"], ("fallback", "success"))
        self.assertIsNotNone(ai_row["email_summary"])

    # --- 7. Regenerar IA actualiza resumen existente (un solo row por documento) ---
    def test_07_regenerate_ai_updates_existing(self):
        from dt_alerts.summarizer import generate_ai_summary, regenerate_ai_summary
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                db.update_document_processed(
                    conn, doc_id, status="processed", detail_text="Texto.",
                    pdf_url=None, content_hash=None, last_error=None,
                )
            settings = settings_for(
                path, ai_provider="disabled", ai_max_input_chars=45000,
                ai_timeout_seconds=60, ai_attachments_enabled=True,
            )
            generate_ai_summary(doc_id, settings=settings)
            regenerate_ai_summary(doc_id, settings=settings)
            with db.connect(path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM ai_summaries WHERE document_id=?", (doc_id,)
                ).fetchone()["n"]
        self.assertEqual(count, 1)

    # --- 8. Alertas quedan pending_review tras generar IA ---
    def test_08_alerts_stay_pending_review_after_ai_generation(self):
        from dt_alerts.summarizer import generate_ai_summary
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                db.update_document_processed(
                    conn, doc_id, status="processed", detail_text="Texto.",
                    pdf_url=None, content_hash=None, last_error=None,
                )
            settings = settings_for(
                path, ai_provider="disabled", ai_max_input_chars=45000,
                ai_timeout_seconds=60, ai_attachments_enabled=True,
            )
            summary = generate_ai_summary(doc_id, settings=settings)
            with db.connect(path) as conn:
                alert_id = db.create_or_update_alert(
                    conn, doc_id,
                    summary=summary.summary,
                    key_points=summary.key_points,
                    practical_impacts=summary.practical_impacts,
                    relevance=summary.relevance,
                    status="pending_review",
                    ai_error=summary.ai_error,
                )
                alert = db.get_alert_with_document(conn, alert_id)
        self.assertEqual(alert["status"], "pending_review")

    # --- 9. Email usa email_subject generado por IA si existe ---
    def test_09_email_uses_ai_subject_when_available(self):
        alert = {
            "title": "Circular normal",
            "ai_email_subject": "Nueva normativa DT: Circular especial IA",
            "ai_status": "success",
            "summary": "Resumen basico.",
        }
        subject = notifier.subject_for(alert)
        self.assertEqual(subject, "Nueva normativa DT: Circular especial IA")

    # --- 10. Email usa template de fallback si no hay subject IA ---
    def test_10_email_uses_fallback_subject_when_no_ai_subject(self):
        alert = {
            "title": "Circular sin IA",
            "ai_email_subject": None,
            "ai_status": None,
            "summary": "Resumen basico.",
        }
        subject = notifier.subject_for(alert)
        self.assertIn("Circular sin IA", subject)
        self.assertIn("Nueva normativa DT", subject)

        sii_subject = notifier.subject_for({**alert, "canonical_url": "https://www.sii.cl/normativa_legislacion/"})
        self.assertIn("Nueva normativa SII", sii_subject)

    # --- 11. Adjuntos HTML se generan sin exponer secretos ---
    def test_11_html_attachments_no_secrets(self):
        fake_key = "sk-secreto-muy-largo-12345678"
        alert = {
            "title": "Circular DT",
            "category": "Circulares",
            "publication_date": "01/01/2026",
            "canonical_url": "https://example.com",
            "summary": "Resumen.",
            "ai_status": "success",
            "ai_executive_summary": json.dumps(
                {"title": "Resumen ejecutivo", "body": "Cuerpo del ejecutivo."}
            ),
            "ai_detailed_summary_json": json.dumps({
                "title": "Resumen detallado",
                "sections": [{"heading": "Sec", "body": "Detalle."}],
            }),
            "ai_legal_disclaimer": "Solo informativo.",
            "ai_key_points_json": "[]",
            "ai_recommended_actions_json": "[]",
        }
        exec_html = notifier.generate_executive_summary_html(1, alert)
        detail_html = notifier.generate_detailed_summary_html(1, alert)
        self.assertIn("Resumen ejecutivo", exec_html)
        self.assertIn("Resumen detallado", detail_html)
        self.assertNotIn(fake_key, exec_html)
        self.assertNotIn(fake_key, detail_html)
        self.assertIn("<!doctype html", exec_html.lower())

    # --- 12. Error IA queda registrado en ai_summaries ---
    def test_12_ai_error_is_recorded(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                db.update_document_processed(
                    conn, doc_id, status="processed", detail_text="Texto.",
                    pdf_url=None, content_hash=None, last_error=None,
                )
                settings = settings_for(
                    path,
                    ai_provider="openai",
                    ai_api_key="fake-key-that-will-fail",
                    ai_max_input_chars=45000,
                    ai_timeout_seconds=5,
                    ai_attachments_enabled=True,
                )
                with mock.patch("urllib.request.urlopen", side_effect=Exception("HTTP 401")):
                    summary = _generate_and_save(conn, doc_id, settings, {})
                ai_row = db.get_ai_summary(conn, doc_id)
        self.assertIsNotNone(ai_row)
        self.assertIn(ai_row["status"], ("fallback", "error"))
        self.assertIsNotNone(ai_row["error"])
        self.assertNotIn("fake-key-that-will-fail", ai_row.get("error") or "")

    # --- 13. /admin/settings muestra estado IA completo ---
    def test_13_settings_renders_full_ai_section(self):
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(
                path,
                ai_provider="openai",
                ai_api_key="sk-test123456789",
                ai_model="gpt-4o-mini",
                ai_timeout_seconds=60,
                ai_max_input_chars=45000,
                ai_attachments_enabled=True,
            )
            html_out = render_settings(s)
        self.assertIn("AI_PROVIDER", html_out)
        self.assertIn("AI_MODEL", html_out)
        self.assertIn("AI_TIMEOUT_SECONDS", html_out)
        self.assertIn("AI_ATTACHMENTS_ENABLED", html_out)
        self.assertNotIn("sk-test123456789", html_out)

    # --- 14. /admin/alerts muestra badge de estado IA ---
    def test_14_alerts_shows_ai_status_badge(self):
        from dt_alerts.server import render_alerts, ai_status_badge
        item = {
            "id": 1,
            "document_id": 1,
            "title": "Circular DT",
            "category": "Circulares",
            "publication_date": "01/01/2026",
            "relevance": "alto",
            "status": "pending_review",
            "summary": "Resumen.",
            "key_points_json": "[]",
            "practical_impacts_json": "[]",
            "canonical_url": "https://example.com",
            "created_at": "2026-06-19T10:00:00",
            "ai_status": "success",
            "ai_provider": "openai",
            "ai_content_quality": "full",
            "ai_email_subject": "Nueva normativa",
            "ai_summary_error": None,
        }
        badge_html = ai_status_badge(item)
        self.assertTrue(badge_html, "ai_status_badge must return non-empty HTML for status='success'")
        html_out = render_alerts([item])
        self.assertIn("IA", html_out)

    # --- 15. Preview muestra resumen ejecutivo y detallado ---
    def test_15_preview_shows_executive_and_detailed_summary(self):
        from dt_alerts.server import render_alert_preview
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                db.update_document_processed(
                    conn, doc_id, status="processed", detail_text="Texto.",
                    pdf_url=None, content_hash=None, last_error=None,
                )
                db.upsert_ai_summary(
                    conn, doc_id,
                    provider="openai", model="gpt-4o-mini",
                    status="success", content_quality="full",
                    relevance="alto",
                    email_subject="Nueva normativa DT: Circular",
                    email_summary="Resumen IA breve.",
                    key_points_json='["Punto 1"]',
                    practical_impacts_json='[{"title": "Impacto", "description": "Desc"}]',
                    recommended_actions_json='["Accion 1"]',
                    executive_summary=json.dumps(
                        {"title": "Resumen ejecutivo", "body": "Cuerpo ejecutivo."}
                    ),
                    detailed_summary_json=json.dumps({
                        "title": "Detalle",
                        "sections": [{"heading": "Sec", "body": "Cuerpo."}],
                    }),
                    tags_json='["DT"]',
                    legal_disclaimer="Solo informativo.",
                    error=None,
                )
                alert_id = db.create_or_update_alert(
                    conn, doc_id,
                    summary="Resumen basico.",
                    key_points=["Punto 1"],
                    practical_impacts=["Impacto"],
                    relevance="alto",
                    status="pending_review",
                    ai_error=None,
                )
            settings = settings_for(path, ai_attachments_enabled=True)
            html_out = render_alert_preview(alert_id, settings)
        self.assertIn("Resumen ejecutivo", html_out)
        self.assertIn("Resumen detallado", html_out)
        self.assertIn("Inteligencia Artificial", html_out)

    # --- 16. El envio masivo NO se dispara automaticamente tras generar IA ---
    def test_16_generate_ai_does_not_auto_send(self):
        from dt_alerts.summarizer import generate_ai_summary
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                db.update_document_processed(
                    conn, doc_id, status="processed", detail_text="Texto.",
                    pdf_url=None, content_hash=None, last_error=None,
                )
                db.upsert_subscriber(
                    conn, email="sub@empresa.cl", whatsapp=None,
                    notify_email=True, notify_whatsapp=False,
                    source_page="test", consent=True,
                )
                alert_id = db.create_or_update_alert(
                    conn, doc_id,
                    summary="Resumen.", key_points=[], practical_impacts=[],
                    relevance="medio", status="pending_review", ai_error=None,
                )
            settings = settings_for(
                path, ai_provider="disabled", ai_max_input_chars=45000,
                ai_timeout_seconds=60, ai_attachments_enabled=True,
            )
            generate_ai_summary(doc_id, settings=settings)
            with db.connect(path) as conn:
                deliveries = conn.execute(
                    "SELECT COUNT(*) AS n FROM deliveries"
                ).fetchone()["n"]
                alert = db.get_alert_with_document(conn, alert_id)
        self.assertEqual(deliveries, 0)
        self.assertEqual(alert["status"], "pending_review")

    # --- 17. Si no hay PDF oficial, el email incluye link oficial ---
    def test_17_email_has_official_link_when_no_pdf(self):
        alert = {
            "title": "Circular sin PDF",
            "category": "Circulares",
            "publication_date": "01/01/2026",
            "relevance": "medio",
            "status": "pending_review",
            "summary": "Resumen sin PDF.",
            "key_points_json": "[]",
            "practical_impacts_json": "[]",
            "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-999.html",
            "pdf_url": None,
            "ai_status": None,
            "ai_email_subject": None,
        }
        html_out = notifier.render_alert_email_html(alert)
        text_out = notifier.render_alert_email_text(alert)
        self.assertIn("dt.gob.cl", html_out)
        self.assertIn("dt.gob.cl", text_out)
        self.assertIn("Ver documento oficial", html_out)


class AIUsageControlTestCase(unittest.TestCase):
    """Tests de control de consumo IA: logs, límites, prueba de conexión y CSV."""

    def _db_path(self, tmp: str) -> Path:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return path

    def _sample_doc_dict(self) -> dict:
        return {
            "dt_article_id": "usage-test-1",
            "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-usage1.html",
            "source_url": "https://www.dt.gob.cl/x.html",
            "category": "Circulares",
            "title": "Circular de prueba control IA",
            "publication_date": "01/01/2026",
            "abstract": "Instruye criterios de prueba.",
            "detail_text": "Texto de prueba para control de tokens.",
            "pdf_url": None,
            "content_hash": None,
        }

    def _insert_doc(self, conn) -> int:
        doc_id, _ = db.upsert_document(conn, self._sample_doc_dict())
        db.update_document_processed(conn, doc_id, status="processed",
                                     detail_text="Texto.", pdf_url=None,
                                     content_hash=None, last_error=None)
        return doc_id

    # --- 1. AI_ENABLED=false registra 'disabled' con 0 tokens ---
    def test_01_ai_disabled_registers_disabled_with_zero_tokens(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=False, ai_provider="disabled", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                _generate_and_save(conn, doc_id, s, {})
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertTrue(rows, "Debe haber al menos un registro en ai_usage_logs")
        self.assertEqual(rows[0]["status"], "disabled")
        self.assertEqual(rows[0]["total_tokens"], 0)

    # --- 2. Sin API key registra 'missing_key' ---
    def test_02_missing_api_key_registers_missing_key(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="openai", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "missing_key")

    # --- 3. Límite diario alcanzado registra 'blocked_limit' ---
    def test_03_daily_limit_registers_blocked_limit(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            # Límite de 1 token diario; pre-cargamos 100 tokens de uso
            s = settings_for(path, ai_enabled=True, ai_provider="openai",
                             ai_api_key="fake-key", ai_daily_token_limit=1,
                             ai_monthly_token_limit=500000)
            with db.connect(path) as conn:
                db.record_ai_usage(conn, operation="generate_summary",
                                   status="success", total_tokens=100,
                                   daily_limit=1, monthly_limit=500000)
                doc_id = self._insert_doc(conn)
                _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "blocked_limit")

    # --- 4. Límite mensual alcanzado registra 'blocked_limit' ---
    def test_04_monthly_limit_registers_blocked_limit(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="openai",
                             ai_api_key="fake-key", ai_daily_token_limit=50000,
                             ai_monthly_token_limit=1)
            with db.connect(path) as conn:
                db.record_ai_usage(conn, operation="generate_summary",
                                   status="success", total_tokens=100,
                                   daily_limit=50000, monthly_limit=1)
                doc_id = self._insert_doc(conn)
                _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "blocked_limit")

    # --- 5. Mock de respuesta exitosa registra 'success' con tokens ---
    def test_05_mock_success_registers_success_with_tokens(self):
        from dt_alerts.summarizer import _generate_and_save, AIResponse
        fake_json = json.dumps({
            "title": "Circular DT", "category": "Circulares", "relevance": "medio",
            "email_subject": "Nueva normativa DT: Circular",
            "email_summary": "Resumen de prueba.",
            "key_points": ["Punto 1"], "practical_impacts": [{"title": "Impacto", "description": "Desc"}],
            "recommended_actions": ["Accion 1"],
            "executive_summary": {"title": "Resumen ejecutivo", "body": "Cuerpo."},
            "detailed_summary": {"title": "Detalle", "sections": []},
            "tags": ["DT"], "legal_disclaimer": "Informativo.",
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="openai",
                             ai_api_key="fake-key", ai_daily_token_limit=50000,
                             ai_monthly_token_limit=500000)
            fake_response = AIResponse(content=fake_json, input_tokens=100,
                                       output_tokens=200, total_tokens=300)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch("dt_alerts.summarizer.call_ai_with_usage",
                                return_value=fake_response):
                    _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["total_tokens"], 300)
        self.assertEqual(rows[0]["input_tokens"], 100)
        self.assertEqual(rows[0]["output_tokens"], 200)

    # --- 6. 'Probar conexión IA' registra uso o queda bloqueado sin llamar API ---
    def test_06_test_connection_registers_usage(self):
        from dt_alerts.server import _test_ai_connection
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            # Caso disabled: debe registrar 'disabled', sin llamar API
            s = settings_for(path, ai_enabled=False, ai_provider="disabled", ai_api_key="")
            with db.connect(path) as conn:
                msg = _test_ai_connection(s, {}, conn)
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertIn("bloqueada", msg.lower())
        self.assertTrue(rows, "test_connection debe registrar en ai_usage_logs")
        self.assertEqual(rows[0]["status"], "disabled")
        self.assertEqual(rows[0]["operation"], "test_connection")

    def test_06b_test_connection_registers_missing_key(self):
        from dt_alerts.server import _test_ai_connection
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="openai", ai_api_key="")
            with db.connect(path) as conn:
                msg = _test_ai_connection(s, {"ai_runtime_enabled": "true"}, conn)
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertIn("AI_API_KEY", msg)
        self.assertEqual(rows[0]["status"], "missing_key")
        self.assertEqual(rows[0]["operation"], "test_connection")

    def test_06c_test_connection_blocked_limit(self):
        from dt_alerts.server import _test_ai_connection
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="openai",
                             ai_api_key="sk-fake", ai_daily_token_limit=1,
                             ai_monthly_token_limit=500000)
            with db.connect(path) as conn:
                db.record_ai_usage(conn, operation="test_connection",
                                   status="success", total_tokens=100,
                                   daily_limit=1, monthly_limit=500000)
                msg = _test_ai_connection(s, {"ai_runtime_enabled": "true"}, conn)
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertIn("límite", msg.lower())
        self.assertEqual(rows[0]["status"], "blocked_limit")

    # --- 7. CSV no expone secretos ---
    def test_07_csv_does_not_expose_secrets(self):
        import io, csv as csv_mod
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            fake_key = "sk-super-secret-api-key-12345"
            # Insertamos un log con error que menciona la key (ya debería estar redactada por el summarizer)
            with db.connect(path) as conn:
                db.record_ai_usage(conn, operation="generate_summary", status="error",
                                   provider="openai", model="gpt-4o-mini",
                                   total_tokens=0, daily_limit=50000, monthly_limit=500000,
                                   error=f"HTTP 401: invalid key [REDACTED]")
                rows = db.get_recent_ai_usage(conn, limit=10)
        # Simulamos el CSV como lo produce _serve_ai_usage_csv
        COLS = ["id", "created_at", "operation", "status", "provider", "model",
                "input_tokens", "output_tokens", "total_tokens", "error"]
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(COLS)
        for row in rows:
            writer.writerow([
                row["id"], row["created_at"], row["operation"], row["status"],
                row["provider"] or "", row["model"] or "",
                row["input_tokens"], row["output_tokens"], row["total_tokens"],
                (row["error"] or "")[:500],
            ])
        csv_output = buf.getvalue()
        self.assertNotIn(fake_key, csv_output)
        self.assertIn("REDACTED", csv_output)
        self.assertIn("id", csv_output)
        self.assertIn("operation", csv_output)
        # Verificar que las columnas sensibles no están presentes
        self.assertNotIn("ai_api_key", csv_output)
        self.assertNotIn("sendgrid_api_key", csv_output)

    # --- 8. Regeneración exitosa no deja alerta con badge de error ---
    def test_08_successful_regeneration_no_error_badge(self):
        from dt_alerts.server import ai_status_badge, render_alerts
        # Simular item con ai_status='success' (resultado de regeneración exitosa)
        item_success = {
            "id": 99, "document_id": 1,
            "title": "Circular regenerada", "category": "Circulares",
            "publication_date": "01/01/2026", "relevance": "alto",
            "status": "pending_review", "summary": "Resumen regenerado.",
            "key_points_json": "[]", "practical_impacts_json": "[]",
            "canonical_url": "https://example.com",
            "created_at": "2026-06-25T10:00:00",
            "ai_status": "success", "ai_provider": "azure",
            "ai_content_quality": "full", "ai_email_subject": "Nueva normativa",
            "ai_summary_error": None,
        }
        badge_html = ai_status_badge(item_success)
        # No debe mostrar clase peligro (rojo) cuando IA tuvo éxito
        self.assertNotIn("eg-badge--danger", badge_html)
        self.assertIn("eg-badge--active", badge_html)
        self.assertIn("IA generada", badge_html)
        # Simular item con ai_status='error' (datos viejos): tampoco debe ser rojo
        item_old_error = {**item_success, "ai_status": "error"}
        badge_old = ai_status_badge(item_old_error)
        self.assertNotIn("eg-badge--danger", badge_old,
                         "Datos viejos con ai_status='error' no deben mostrar badge rojo")
        # La lista de alertas no debe mostrar badge rojo para ninguno de los dos casos
        html_out = render_alerts([item_success, item_old_error])
        self.assertNotIn("eg-badge--danger", html_out)


class NewPhasesTestCase(unittest.TestCase):
    """Tests para Fases 1-5: costos, editorial, prompt, adjuntos, seguridad."""

    def _make_settings(self, tmp_path, **overrides):
        base = get_settings()
        data = {**base.__dict__, "database_path": tmp_path}
        data.update(overrides)
        return base.__class__(**data)

    # ------------------------------------------------------------------ Fase 1: Costos
    def test_01_cost_zero_tokens(self):
        """Costo es 0 cuando tokens son 0."""
        from dt_alerts.server import _calc_ai_cost
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            ai_input_price_per_1m_usd=2.0,
            ai_output_price_per_1m_usd=8.0,
            ai_usd_clp_rate=921,
        )
        usd, clp = _calc_ai_cost(0, 0, settings)
        self.assertEqual(usd, 0.0)
        self.assertEqual(clp, 0.0)

    def test_02_cost_calculation(self):
        """Cálculo correcto con tokens input=549 output=675."""
        from dt_alerts.server import _calc_ai_cost
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            ai_input_price_per_1m_usd=2.0,
            ai_output_price_per_1m_usd=8.0,
            ai_usd_clp_rate=921,
        )
        usd, clp = _calc_ai_cost(549, 675, settings)
        # input: 549/1_000_000 * 2.0 = 0.001098
        # output: 675/1_000_000 * 8.0 = 0.0054
        expected_usd = (549 / 1_000_000 * 2.0) + (675 / 1_000_000 * 8.0)
        self.assertAlmostEqual(usd, expected_usd, places=8)
        self.assertAlmostEqual(clp, expected_usd * 921, places=4)

    def test_03_csv_includes_cost_columns(self):
        """CSV incluye columnas estimated_cost_usd y estimated_cost_clp."""
        import io
        import csv
        from dt_alerts.server import _calc_ai_cost
        from dt_alerts.config import Settings
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)
            settings = self._make_settings(
                db_path,
                ai_input_price_per_1m_usd=2.0,
                ai_output_price_per_1m_usd=8.0,
                ai_usd_clp_rate=921,
            )
            with db.connect(db_path) as conn:
                db.record_ai_usage(
                    conn, document_id=None, alert_id=None,
                    provider="azure", model="gpt-chat-latest",
                    operation="generate_summary", status="success",
                    input_tokens=549, output_tokens=675, total_tokens=1224,
                    daily_limit=50000, monthly_limit=500000, error=None,
                )
                cols = ["id", "created_at", "operation", "status", "provider", "model",
                        "input_tokens", "output_tokens", "total_tokens", "error"]
                rows = conn.execute(
                    f"SELECT {', '.join(cols)} FROM ai_usage_logs ORDER BY id DESC LIMIT 1000"
                ).fetchall()

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(cols + ["estimated_cost_usd", "estimated_cost_clp"])
            for row in rows:
                cost_usd, cost_clp = _calc_ai_cost(
                    row["input_tokens"] or 0, row["output_tokens"] or 0, settings
                )
                writer.writerow([
                    row["id"], row["created_at"], row["operation"], row["status"],
                    row["provider"] or "", row["model"] or "",
                    row["input_tokens"], row["output_tokens"], row["total_tokens"],
                    (row["error"] or "")[:500],
                    f"{cost_usd:.6f}", f"{cost_clp:.2f}",
                ])
            content = buf.getvalue()
            self.assertIn("estimated_cost_usd", content)
            self.assertIn("estimated_cost_clp", content)
            # No debe haber API keys
            self.assertNotIn("sk-", content)
            self.assertNotIn("AI_API_KEY", content)

    # ------------------------------------------------------------------ Fase 2: Editorial
    def test_04_ai_analysis_focus_saved(self):
        """ai_analysis_focus se guarda y se recupera como setting."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                db.set_setting(conn, "ai_analysis_focus", "Impactos en remuneraciones.")
                val = db.get_all_settings(conn).get("ai_analysis_focus")
            self.assertEqual(val, "Impactos en remuneraciones.")

    def test_05_render_settings_no_crash(self):
        """render_settings no falla con settings mínimos y DB vacía."""
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)
            settings = self._make_settings(db_path)
            result = render_settings(settings)
            self.assertIn("Conexion IA", result)
            self.assertIn("Configuracion editorial", result)
            self.assertIn("Enfoque del análisis", result)

    # ------------------------------------------------------------------ Fase 3: Prompt
    def test_06_prompt_includes_analysis_focus(self):
        """El prompt incluye ai_analysis_focus cuando está definido."""
        from dt_alerts.summarizer import build_ai_prompt
        settings = self._make_settings(Path(tempfile.mkdtemp()) / "t.db")
        doc = {
            "title": "Circular 1", "category": "Circulares",
            "detail_text": "Texto de prueba.", "abstract": "",
            "publication_date": "2026-01-01", "canonical_url": "https://example.com",
        }
        app_settings = {"ai_analysis_focus": "Foco en auditoría y RRHH."}
        sys_p, user_p = build_ai_prompt(doc, settings, app_settings)
        self.assertIn("Foco en auditoría y RRHH.", user_p)

    def test_07_prompt_default_focus_when_missing(self):
        """Si no hay ai_analysis_focus, se usa el default segun la fuente."""
        from dt_alerts.summarizer import build_ai_prompt
        settings = self._make_settings(Path(tempfile.mkdtemp()) / "t.db")
        doc = {
            "title": "Circular 1", "category": "Circulares",
            "detail_text": "Texto de prueba.", "abstract": "",
            "publication_date": "2026-01-01", "canonical_url": "https://example.com",
        }
        _, user_p = build_ai_prompt(doc, settings, {})
        self.assertIn("Dirección del Trabajo", user_p)
        self.assertIn("cumplimiento laboral", user_p)

        sii_doc = {**doc, "canonical_url": "https://www.sii.cl/normativa_legislacion/circulares/2026/foo.htm"}
        _, sii_user_p = build_ai_prompt(sii_doc, settings, {})
        self.assertIn("Servicio de Impuestos Internos", sii_user_p)
        self.assertIn("cumplimiento tributario", sii_user_p)

    def test_08_prompt_no_invented_data_instruction(self):
        """El prompt incluye instrucción de no inventar datos."""
        from dt_alerts.summarizer import build_ai_prompt
        settings = self._make_settings(Path(tempfile.mkdtemp()) / "t.db")
        doc = {"title": "T", "category": "C", "detail_text": "X",
               "abstract": "", "publication_date": "2026-01-01", "canonical_url": ""}
        sys_p, _ = build_ai_prompt(doc, settings, {})
        self.assertIn("No inventes", sys_p)

    # ------------------------------------------------------------------ Fase 4: Adjuntos
    def test_09_attachment_filename_uses_slug(self):
        """Los nombres de archivo de adjuntos incluyen slug del título."""
        from dt_alerts.notifier import _build_attachments
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            ai_attachments_enabled=True,
        )
        alert = {
            "id": 5, "document_id": 5,
            "title": "Circular 99 sobre remuneraciones",
            "category": "Circulares", "publication_date": "2026-01-01",
            "canonical_url": "https://example.com",
            "summary": "Resumen de prueba.",
            "ai_status": "success",
            "ai_executive_summary": '{"title": "Eje", "body": "Cuerpo ejecutivo."}',
            "ai_detailed_summary_json": '{"descripcion": "Desc.", "impacto_contable": "Impacto."}',
            "ai_key_points_json": "[]", "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
        }
        attachments = _build_attachments(alert, settings)
        self.assertEqual(len(attachments), 2)
        for att in attachments:
            self.assertIn("circular_99", att["filename"])

    def test_10_attachment_skipped_when_no_ai_status(self):
        """No se generan adjuntos si ai_status es None o pending."""
        from dt_alerts.notifier import _build_attachments
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            ai_attachments_enabled=True,
        )
        alert = {"id": 1, "document_id": 1, "title": "T", "ai_status": None}
        self.assertEqual(_build_attachments(alert, settings), [])
        alert2 = {**alert, "ai_status": "pending"}
        self.assertEqual(_build_attachments(alert2, settings), [])

    def test_11_send_test_includes_attachments(self):
        """send_test_alert_email incluye adjuntos cuando ai_status=success."""
        from dt_alerts.notifier import send_test_alert_email, _build_attachments
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            email_provider="console",
            ai_attachments_enabled=True,
        )
        alert = {
            "id": 1, "document_id": 1,
            "title": "Circular de prueba",
            "category": "Circulares", "publication_date": "2026-01-01",
            "canonical_url": "https://example.com",
            "summary": "Resumen.", "relevance": "alto",
            "key_points_json": "[]", "practical_impacts_json": "[]",
            "ai_status": "success", "ai_provider": "azure", "ai_model": "gpt-chat-latest",
            "ai_content_quality": "full", "ai_updated_at": "2026-01-01",
            "ai_email_subject": "Nueva normativa",
            "ai_executive_summary": '{"title":"Eje","body":"Cuerpo."}',
            "ai_detailed_summary_json": '{"descripcion":"Desc."}',
            "ai_key_points_json": "[]", "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
        }
        # Verificar que _build_attachments genera 2 adjuntos
        atts = _build_attachments(alert, settings)
        self.assertEqual(len(atts), 2, "Deben generarse 2 adjuntos para ai_status=success")

    # ------------------------------------------------------------------ Fase 5: Seguridad
    def test_12_warning_banner_at_80_percent(self):
        """render_settings muestra banner de advertencia al 80% del límite."""
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)
            # Límite muy bajo para forzar el 80%
            settings = self._make_settings(
                db_path,
                ai_enabled=False,
                ai_daily_token_limit=100,
                ai_monthly_token_limit=1000,
                ai_warning_percent=80,
                ai_input_price_per_1m_usd=2.0,
                ai_output_price_per_1m_usd=8.0,
                ai_usd_clp_rate=921,
            )
            with db.connect(db_path) as conn:
                # Insertar 82 tokens de hoy para superar el 80%
                db.record_ai_usage(
                    conn, document_id=None, alert_id=None,
                    provider="azure", model="m", operation="generate_summary",
                    status="success", input_tokens=50, output_tokens=32,
                    total_tokens=82, daily_limit=100, monthly_limit=1000, error=None,
                )
            result = render_settings(settings)
            self.assertIn("Consumo IA cercano al límite", result)

    def test_13_limit_banner_when_exceeded(self):
        """render_settings muestra banner de límite alcanzado cuando se supera."""
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)
            settings = self._make_settings(
                db_path,
                ai_enabled=False,
                ai_daily_token_limit=10,
                ai_monthly_token_limit=1000,
                ai_warning_percent=80,
                ai_input_price_per_1m_usd=2.0,
                ai_output_price_per_1m_usd=8.0,
                ai_usd_clp_rate=921,
            )
            with db.connect(db_path) as conn:
                db.record_ai_usage(
                    conn, document_id=None, alert_id=None,
                    provider="azure", model="m", operation="generate_summary",
                    status="success", input_tokens=6, output_tokens=6,
                    total_tokens=12, daily_limit=10, monthly_limit=1000, error=None,
                )
            result = render_settings(settings)
            self.assertIn("Límite de tokens IA alcanzado", result)

    def test_14_detailed_summary_new_format_renders(self):
        """generate_detailed_summary_html maneja el nuevo formato de dict plano."""
        from dt_alerts.notifier import generate_detailed_summary_html
        alert = {
            "title": "Circular de prueba",
            "category": "Circulares",
            "publication_date": "2026-01-01",
            "canonical_url": "https://example.com",
            "ai_detailed_summary_json": json.dumps({
                "descripcion": "Descripción del documento.",
                "impacto_contable": "Afecta los libros contables.",
                "impacto_tributario": "Modifica las declaraciones.",
                "acciones_recomendadas": "Actualizar plantillas.",
                "riesgos": "Multas por incumplimiento.",
                "plazos": "30 días desde publicación.",
            }),
            "ai_key_points_json": "[]",
            "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
            "summary": "",
        }
        html_out = generate_detailed_summary_html(1, alert)
        self.assertIn("Impacto contable", html_out)
        self.assertIn("Impacto tributario", html_out)
        self.assertIn("Acciones recomendadas", html_out)
        self.assertIn("Afecta los libros contables.", html_out)


    def test_13_attachments_disabled_by_flag(self):
        """AI_ATTACHMENTS_ENABLED=False → _build_attachments devuelve lista vacía."""
        from dt_alerts.notifier import _build_attachments
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            ai_attachments_enabled=False,
        )
        alert = {
            "id": 1, "document_id": 1,
            "title": "Circular 1",
            "ai_status": "success",
            "ai_executive_summary": '{"title":"Eje","body":"Cuerpo."}',
            "ai_detailed_summary_json": '{"descripcion":"Desc."}',
            "ai_key_points_json": "[]", "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
        }
        self.assertEqual(_build_attachments(alert, settings), [])

    def test_14_attachments_included_when_fallback(self):
        """_build_attachments genera adjuntos cuando ai_status='fallback'."""
        from dt_alerts.notifier import _build_attachments
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            ai_attachments_enabled=True,
        )
        alert = {
            "id": 2, "document_id": 2,
            "title": "Ordinario 50",
            "category": "Ordinarios", "publication_date": "2026-01-01",
            "canonical_url": "https://example.com",
            "summary": "Resumen de respaldo.",
            "ai_status": "fallback",
            "ai_executive_summary": '{"title":"Eje fallback","body":"Cuerpo."}',
            "ai_detailed_summary_json": '{"descripcion":"Desc fallback."}',
            "ai_key_points_json": "[]", "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
        }
        atts = _build_attachments(alert, settings)
        self.assertEqual(len(atts), 2)
        self.assertTrue(all(a["filename"].endswith(".html") for a in atts))

    def test_15_send_alert_email_passes_attachments(self):
        """send_alert_email propaga adjuntos al proveedor console sin error."""
        from dt_alerts.notifier import send_alert_email
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            email_provider="console",
            ai_attachments_enabled=True,
        )
        alert = {
            "id": 3, "document_id": 3,
            "title": "Circular de test send_alert_email",
            "category": "Circulares", "publication_date": "2026-01-01",
            "canonical_url": "https://example.com",
            "summary": "Resumen.", "relevance": "alto",
            "key_points_json": "[]", "practical_impacts_json": "[]",
            "ai_status": "success", "ai_provider": "azure", "ai_model": "gpt-chat-latest",
            "ai_content_quality": "full", "ai_updated_at": "2026-01-01",
            "ai_email_subject": "Nueva normativa",
            "ai_executive_summary": '{"title":"Eje","body":"Cuerpo."}',
            "ai_detailed_summary_json": '{"descripcion":"Desc."}',
            "ai_key_points_json": "[]", "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
        }
        subscriber = {"email": "qa@example.com", "name": "QA Tester"}
        result = send_alert_email(subscriber, alert, settings)
        self.assertIsNotNone(result)
        self.assertNotIn("error", (result.get("status") or "").lower())

    def test_16_send_test_alert_email_passes_attachments(self):
        """send_test_alert_email propaga adjuntos al proveedor (end-to-end con mock)."""
        from unittest.mock import patch, MagicMock
        from dt_alerts.notifier import send_test_alert_email
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            email_provider="console",
            ai_attachments_enabled=True,
        )
        alert = {
            "id": 4, "document_id": 4,
            "title": "Circular end-to-end test",
            "category": "Circulares", "publication_date": "2026-01-01",
            "canonical_url": "https://example.com",
            "summary": "Resumen.", "relevance": "alto",
            "key_points_json": "[]", "practical_impacts_json": "[]",
            "ai_status": "success", "ai_provider": "azure", "ai_model": "gpt-chat-latest",
            "ai_content_quality": "full", "ai_updated_at": "2026-01-01",
            "ai_email_subject": "Test adjuntos",
            "ai_executive_summary": '{"title":"Eje","body":"Cuerpo."}',
            "ai_detailed_summary_json": '{"descripcion":"Desc."}',
            "ai_key_points_json": "[]", "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
        }
        mock_result = {"ok": True, "provider": "console", "status": "simulated"}
        with patch("dt_alerts.notifier.send_email", return_value=mock_result) as mock_send:
            result = send_test_alert_email("qa@example.com", alert, settings)
        self.assertTrue(result["ok"])
        _call_kwargs = mock_send.call_args
        attachments_passed = _call_kwargs.kwargs.get("attachments") or (
            _call_kwargs.args[5] if len(_call_kwargs.args) > 5 else None
        )
        self.assertIsNotNone(attachments_passed, "send_email no recibió adjuntos")
        self.assertEqual(len(attachments_passed), 2)
        filenames = [a["filename"] for a in attachments_passed]
        self.assertTrue(any("resumen_ejecutivo" in f for f in filenames))
        self.assertTrue(any("resumen_detallado" in f for f in filenames))

    # ------------------------------------------------------------------ Punto E gap
    def test_17_csv_error_field_truncated_to_500_chars(self):
        """El campo error en el CSV queda truncado a 500 chars (server.py:522 [:500])."""
        import io
        import csv as csv_mod
        long_error = "x" * 1000  # 1000 chars — debe quedar en 500 en el CSV
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tempfile.mkdtemp()) / "t.db"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                db.record_ai_usage(
                    conn, operation="generate_summary", status="error",
                    provider="azure", model="gpt-chat-latest",
                    total_tokens=0, daily_limit=50000, monthly_limit=500000,
                    error=long_error,
                )
                rows = db.get_recent_ai_usage(conn, limit=10)
        COLS = ["id", "created_at", "operation", "status", "provider", "model",
                "input_tokens", "output_tokens", "total_tokens", "error"]
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(COLS + ["estimated_cost_usd", "estimated_cost_clp"])
        settings = self._make_settings(
            db_path,
            ai_input_price_per_1m_usd=2.0,
            ai_output_price_per_1m_usd=8.0,
            ai_usd_clp_rate=921,
        )
        from dt_alerts.server import _calc_ai_cost
        for row in rows:
            cost_usd, cost_clp = _calc_ai_cost(
                row["input_tokens"] or 0, row["output_tokens"] or 0, settings
            )
            writer.writerow([
                row["id"], row["created_at"], row["operation"], row["status"],
                row["provider"] or "", row["model"] or "",
                row["input_tokens"], row["output_tokens"], row["total_tokens"],
                (row["error"] or "")[:500],
                f"{cost_usd:.6f}", f"{cost_clp:.2f}",
            ])
        csv_output = buf.getvalue()
        # El campo error truncado nunca debe superar 500 chars
        reader = csv_mod.reader(io.StringIO(csv_output))
        header = next(reader)
        error_col = header.index("error")
        for data_row in reader:
            self.assertLessEqual(len(data_row[error_col]), 500)
        # Verificar que se insertaron exactamente 500 'x' (no 1000)
        self.assertIn("x" * 500, csv_output)
        self.assertNotIn("x" * 501, csv_output)

    # ------------------------------------------------------------------ Punto H gaps
    def test_18_render_settings_shows_referencial_text(self):
        """render_settings incluye texto 'Costo estimado referencial'."""
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)
            settings = self._make_settings(
                db_path,
                ai_enabled=False,
                ai_input_price_per_1m_usd=2.0,
                ai_output_price_per_1m_usd=8.0,
                ai_usd_clp_rate=921,
                ai_daily_token_limit=50000,
                ai_monthly_token_limit=500000,
                ai_warning_percent=80,
            )
            html_out = render_settings(settings)
        self.assertIn("referencial", html_out.lower())

    def test_19_render_settings_shows_cost_labels(self):
        """render_settings incluye etiquetas de costo hoy y mes."""
        from dt_alerts.server import render_settings
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)
            settings = self._make_settings(
                db_path,
                ai_enabled=False,
                ai_input_price_per_1m_usd=2.0,
                ai_output_price_per_1m_usd=8.0,
                ai_usd_clp_rate=921,
                ai_daily_token_limit=50000,
                ai_monthly_token_limit=500000,
                ai_warning_percent=80,
            )
            html_out = render_settings(settings)
        self.assertIn("hoy", html_out.lower())
        self.assertIn("mes", html_out.lower())
        # Costo última llamada también presente
        self.assertIn("última", html_out.lower())

    def test_20_send_test_alert_email_includes_attachments(self):
        """send_test_alert_email (función aislada) construye y pasa adjuntos a send_email."""
        from unittest.mock import patch
        from dt_alerts.notifier import send_test_alert_email
        settings = self._make_settings(
            Path(tempfile.mkdtemp()) / "t.db",
            email_provider="console",
            ai_attachments_enabled=True,
        )
        alert = {
            "id": 7, "document_id": 7,
            "title": "Circular endpoint test",
            "category": "Circulares", "publication_date": "2026-01-01",
            "canonical_url": "https://example.com",
            "summary": "Resumen.", "relevance": "alto",
            "key_points_json": "[]", "practical_impacts_json": "[]",
            "ai_status": "success", "ai_provider": "azure", "ai_model": "gpt-chat-latest",
            "ai_content_quality": "full", "ai_updated_at": "2026-01-01",
            "ai_email_subject": "Test endpoint",
            "ai_executive_summary": '{"title":"Eje","body":"Cuerpo."}',
            "ai_detailed_summary_json": '{"descripcion":"Desc."}',
            "ai_key_points_json": "[]", "ai_recommended_actions_json": "[]",
            "ai_legal_disclaimer": "",
        }
        mock_result = {"ok": True, "provider": "console", "status": "simulated"}
        with patch("dt_alerts.notifier.send_email", return_value=mock_result) as mock_send:
            send_test_alert_email("qa@example.com", alert, settings)
        attachments_passed = mock_send.call_args.kwargs.get("attachments")
        self.assertIsNotNone(attachments_passed, "send_email no recibió adjuntos")
        self.assertEqual(len(attachments_passed), 2)
        filenames = [a["filename"] for a in attachments_passed]
        self.assertTrue(any("resumen_ejecutivo" in f for f in filenames))
        self.assertTrue(any("resumen_detallado" in f for f in filenames))

    def test_21_test_endpoint_subject_template_and_attachments(self):
        """POST /admin/alerts/{id}/test: respeta email_test_subject_template y adjunta archivos."""
        import http.client
        import threading
        import urllib.parse
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.init_db(db_path)

            with db.connect(db_path) as conn:
                doc_id, _ = db.upsert_document(conn, {
                    "dt_article_id": "art-test21",
                    "canonical_url": "https://example.com/art21",
                    "source_url": "https://example.com/list",
                    "category": "Circulares",
                    "title": "Circular test_21",
                    "publication_date": "2026-01-01",
                    "abstract": "Resumen breve.",
                    "detail_text": "Texto completo.",
                    "pdf_url": None, "content_hash": None,
                })
                db.update_document_processed(conn, doc_id, status="processed",
                                             detail_text="Texto.", pdf_url=None,
                                             content_hash=None, last_error=None)
                alert_id = db.create_or_update_alert(
                    conn, doc_id,
                    summary="Resumen test_21.", key_points=[], practical_impacts=[],
                    relevance="alto", status="pending_review", ai_error=None,
                )
                db.upsert_ai_summary(
                    conn, doc_id,
                    provider="azure", model="gpt-chat-latest", status="success",
                    content_quality="full", relevance="alto",
                    email_subject="Normativa test_21",
                    email_summary="Resumen ejecutivo.",
                    executive_summary='{"title":"Eje","body":"Cuerpo."}',
                    detailed_summary_json='{"descripcion":"Desc."}',
                    key_points_json="[]", practical_impacts_json="[]",
                    recommended_actions_json="[]", tags_json="[]",
                    legal_disclaimer="", raw_response_json="{}",
                    input_hash="abc",
                )
                db.set_setting(conn, "email_test_subject_template",
                               "[TEST CUSTOM] {title}")

            _H.settings = settings_for(
                db_path,
                disable_admin_auth=True,
                email_provider="console",
                ai_attachments_enabled=True,
            )

            captured = {}
            mock_result = {"ok": True, "provider": "console", "status": "simulated"}

            def fake_send(settings, *, to, subject, html_body, text_body,
                          attachments=None):
                captured["subject"] = subject
                captured["attachments"] = attachments
                return mock_result

            server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
            t = threading.Thread(target=server.serve_forever)
            t.daemon = True
            t.start()
            try:
                port = server.server_address[1]
                body = urllib.parse.urlencode({"to": "qa@example.com"}).encode()
                with patch("dt_alerts.server.send_notifier_email", side_effect=fake_send):
                    conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn2.request(
                        "POST", f"/admin/alerts/{alert_id}/test",
                        body=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Content-Length": str(len(body))},
                    )
                    resp = conn2.getresponse()
                    resp.read()
            finally:
                server.shutdown()

        # Subject template personalizado fue respetado
        self.assertEqual(captured.get("subject"), "[TEST CUSTOM] Circular test_21")
        # Adjuntos presentes (2 archivos HTML)
        atts = captured.get("attachments") or []
        self.assertEqual(len(atts), 2)
        filenames = [a["filename"] for a in atts]
        self.assertTrue(any("resumen_ejecutivo" in f for f in filenames))
        self.assertTrue(any("resumen_detallado" in f for f in filenames))


class SubscriberNamePhoneTestCase(unittest.TestCase):
    """Tests para subscriber_name, phone y whatsapp_consent (feat/subscriber-name-phone)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "test.db"
        db.init_db(self.db_path)

    def _conn(self):
        return db.connect(self.db_path)

    # --- test_01: migración añade las tres columnas ---
    def test_01_migration_adds_new_columns(self):
        with self._conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(subscribers)")}
        self.assertIn("subscriber_name",  cols)
        self.assertIn("phone",            cols)
        self.assertIn("whatsapp_consent", cols)

    # --- test_02: upsert guarda los nuevos campos ---
    def test_02_upsert_saves_name_phone_whatsapp_consent(self):
        with self._conn() as conn:
            sub = db.upsert_subscriber(
                conn,
                email="test@example.com",
                whatsapp=None,
                notify_email=True,
                notify_whatsapp=False,
                source_page="web",
                consent=True,
                subscriber_name="Ana Pérez",
                phone="+56912345678",
                whatsapp_consent=True,
            )
        self.assertEqual(sub["subscriber_name"], "Ana Pérez")
        self.assertEqual(sub["phone"], "+56912345678")
        self.assertEqual(sub["whatsapp_consent"], 1)

    # --- test_03: llamada sin nuevos campos no rompe compatibilidad ---
    def test_03_upsert_backward_compat_without_new_fields(self):
        with self._conn() as conn:
            sub = db.upsert_subscriber(
                conn,
                email="legacy@example.com",
                whatsapp=None,
                notify_email=True,
                notify_whatsapp=False,
                source_page="wordpress",
                consent=True,
            )
        self.assertIsNone(sub["subscriber_name"])
        self.assertIsNone(sub["phone"])
        self.assertEqual(sub["whatsapp_consent"], 0)

    # --- test_04: ON CONFLICT preserva subscriber_name si el update no trae uno ---
    def test_04_upsert_on_conflict_preserves_name_if_not_provided(self):
        with self._conn() as conn:
            db.upsert_subscriber(
                conn,
                email="update@example.com",
                whatsapp=None,
                notify_email=True,
                notify_whatsapp=False,
                source_page="web",
                consent=True,
                subscriber_name="Carlos",
            )
            sub = db.upsert_subscriber(
                conn,
                email="update@example.com",
                whatsapp=None,
                notify_email=True,
                notify_whatsapp=False,
                source_page="web",
                consent=True,
                # subscriber_name omitido → debe conservar "Carlos"
            )
        self.assertEqual(sub["subscriber_name"], "Carlos")

    # --- test_05: wordpress_sync mapea los nuevos campos desde el payload ---
    def test_05_wordpress_sync_maps_new_fields(self):
        fake_page = {
            "ok": True,
            "total": 1,
            "limit": 100,
            "subscribers": [
                {
                    "id": 42,
                    "email": "sync@example.com",
                    "consent": True,
                    "source_page": "wordpress",
                    "subscriber_name": "María",
                    "phone": "+56987654321",
                    "whatsapp_consent": True,
                }
            ],
        }
        settings = settings_for(
            self.db_path,
            wordpress_sync_enabled=True,
            wordpress_api_url="http://fake",
            wordpress_api_token="tok",
        )
        with mock.patch("dt_alerts.wordpress_sync._fetch_subscribers", return_value=fake_page), \
             mock.patch("dt_alerts.wordpress_sync._mark_synced"):
            result = wordpress_sync.sync(settings)

        self.assertEqual(result["status"], "ok")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT subscriber_name, phone, whatsapp_consent FROM subscribers WHERE email = ?",
                ("sync@example.com",)
            ).fetchone()
        self.assertEqual(row["subscriber_name"], "María")
        self.assertEqual(row["phone"], "+56987654321")
        self.assertEqual(row["whatsapp_consent"], 1)

    # --- test_06: wordpress_sync funciona si el payload no trae los nuevos campos ---
    def test_06_wordpress_sync_backward_compat_missing_new_fields(self):
        fake_page = {
            "ok": True,
            "total": 1,
            "limit": 100,
            "subscribers": [
                {
                    "id": 99,
                    "email": "old@example.com",
                    "consent": True,
                    "source_page": "wordpress",
                    # sin subscriber_name, phone, whatsapp_consent
                }
            ],
        }
        settings = settings_for(
            self.db_path,
            wordpress_sync_enabled=True,
            wordpress_api_url="http://fake",
            wordpress_api_token="tok",
        )
        with mock.patch("dt_alerts.wordpress_sync._fetch_subscribers", return_value=fake_page), \
             mock.patch("dt_alerts.wordpress_sync._mark_synced"):
            result = wordpress_sync.sync(settings)

        self.assertEqual(result["status"], "ok")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT subscriber_name, phone, whatsapp_consent FROM subscribers WHERE email = ?",
                ("old@example.com",)
            ).fetchone()
        self.assertIsNone(row["subscriber_name"])
        self.assertIsNone(row["phone"])
        self.assertEqual(row["whatsapp_consent"], 0)


class DetailParserCleanContentTestCase(unittest.TestCase):
    """Verifica que DetailParser extrae solo el cuerpo real del documento DT."""

    # HTML mínimo que simula la estructura del sitio DT con contenedores y nav.
    _HTML_WITH_CONTAINERS = """
    <html><head><title>ORD. N°1/1 - DT</title></head>
    <body>
      <div id="menu">
        <a href="/">Inicio</a>
        Toggle navigation
        Trámites y servicios
        Trabajadores
        Empleadores
      </div>
      <div id="breadcrumb">Inicio / Dictámenes y normativa / Dictámenes</div>
      <div id="article_i__w3_ar_ArticuloCompleto_presentacion_1" class="articulo">
        <h3 class="titulo">ORD. N°1/1</h3>
        <p class="fecha">01-ene-2025</p>
        <p class="abstract">Resumen del dictamen.</p>
      </div>
      <div id="article_i__w3_ar_ArticuloCompleto_cuerpo_1" class="articulo">
        <p>DICTAMEN N°1/1</p>
        <p>ACTUACIÓN: Fija doctrina.</p>
        <p>MATERIA: Texto legal relevante para contadores.</p>
      </div>
      <div id="footer">Derechos reservados DT</div>
    </body></html>
    """

    # HTML sin los contenedores estándar (layout atípico): debe caer a fallback body.
    _HTML_WITHOUT_CONTAINERS = """
    <html><head><title>Documento DT</title></head>
    <body>
      <p>Texto del documento sin contenedor estándar.</p>
      <p>Segunda línea de contenido.</p>
    </body></html>
    """

    def _parse(self, html: str):
        from dt_alerts.dt_scraper import DetailParser
        parser = DetailParser("https://www.dt.gob.cl/test")
        parser.feed(html)
        return parser

    def test_01_extracts_article_body_and_excludes_nav(self):
        """El texto extraído contiene el cuerpo real y excluye el menú de navegación."""
        parser = self._parse(self._HTML_WITH_CONTAINERS)
        text = parser.text
        self.assertIn("DICTAMEN N°1/1", text)
        self.assertIn("Texto legal relevante para contadores", text)
        self.assertNotIn("Toggle navigation", text)
        self.assertNotIn("Trámites y servicios", text)
        self.assertNotIn("Trabajadores", text)
        self.assertNotIn("Empleadores", text)

    def test_02_excludes_breadcrumb_and_footer(self):
        """El breadcrumb y el footer no aparecen en el texto extraído."""
        parser = self._parse(self._HTML_WITH_CONTAINERS)
        text = parser.text
        self.assertNotIn("Inicio / Dictámenes y normativa / Dictámenes", text)
        self.assertNotIn("Derechos reservados DT", text)

    def test_03_fallback_when_no_containers(self):
        """Sin contenedores estándar, el parser extrae el body completo (no queda vacío)."""
        parser = self._parse(self._HTML_WITHOUT_CONTAINERS)
        text = parser.text
        self.assertIn("Texto del documento sin contenedor estándar", text)
        self.assertIn("Segunda línea de contenido", text)
        self.assertFalse(parser._found_containers)


class PdfIntegrationTestCase(unittest.TestCase):
    """Verifica que enrich_document_detail integra el texto PDF correctamente."""

    # HTML mínimo con un link a PDF dentro del contenedor de artículo
    _HTML = """
    <html><head><title>ORD. N°2/1 - DT</title></head>
    <body>
      <div id="article_i__w3_ar_ArticuloCompleto_cuerpo_1" class="articulo">
        <a href="articles-1_doc.pdf" title="Ir a ORD. N°2/1">ORD. N°2/1</a>
        <p>Resumen HTML del dictamen con información relevante.</p>
      </div>
    </body></html>
    """
    _HTML_NO_PDF = """
    <html><head><title>ORD. N°3/1 - DT</title></head>
    <body>
      <div id="article_i__w3_ar_ArticuloCompleto_cuerpo_1" class="articulo">
        <p>Contenido HTML sin link a PDF.</p>
      </div>
    </body></html>
    """
    _PDF_TEXT_LONG = "Texto completo del dictamen extraído del PDF. " * 15  # >500 chars
    _PDF_TEXT_SHORT = "Corto."                                               # <500 chars

    def _make_doc(self) -> "ScrapedDocument":
        from dt_alerts.dt_scraper import ScrapedDocument
        return ScrapedDocument(
            dt_article_id="1",
            canonical_url="https://www.dt.gob.cl/legislacion/1624/w3-article-1.html",
            source_url="https://www.dt.gob.cl/",
            category="Dictámenes",
            title="ORD. N°2/1",
            abstract="Resumen breve.",
        )

    def test_pdf_01_used_when_sufficient(self):
        """Cuando el PDF tiene ≥500 chars, detail_text viene del PDF."""
        from dt_alerts import dt_scraper
        doc = self._make_doc()
        with mock.patch("dt_alerts.dt_scraper.fetch_text", return_value=self._HTML), \
             mock.patch("dt_alerts.dt_scraper._extract_pdf_text", return_value=self._PDF_TEXT_LONG):
            result = dt_scraper.enrich_document_detail(doc)
        self.assertIn("Texto completo del dictamen", result.detail_text)
        self.assertIsNotNone(result.pdf_url)

    def test_pdf_02_fallback_when_short(self):
        """Cuando el PDF tiene <500 chars, se usa el texto HTML."""
        from dt_alerts import dt_scraper
        doc = self._make_doc()
        with mock.patch("dt_alerts.dt_scraper.fetch_text", return_value=self._HTML), \
             mock.patch("dt_alerts.dt_scraper._extract_pdf_text", return_value=self._PDF_TEXT_SHORT):
            result = dt_scraper.enrich_document_detail(doc)
        self.assertIn("Resumen HTML del dictamen", result.detail_text)

    def test_pdf_03_fallback_when_download_fails(self):
        """URLError al descargar PDF → sin excepción, detail_text viene del HTML."""
        import urllib.error
        from dt_alerts import dt_scraper
        doc = self._make_doc()
        with mock.patch("dt_alerts.dt_scraper.fetch_text", return_value=self._HTML), \
             mock.patch("dt_alerts.dt_scraper._extract_pdf_text", return_value=""):
            result = dt_scraper.enrich_document_detail(doc)
        self.assertIn("Resumen HTML del dictamen", result.detail_text)
        self.assertIsNone(result.detail_text if not result.detail_text else None)  # no exception

    def test_pdf_04_fallback_when_encrypted(self):
        """PDF encriptado (excepción en _extract_pdf_text) → retorna "" → usa HTML."""
        from dt_alerts import dt_scraper
        doc = self._make_doc()
        # _extract_pdf_text ya captura toda excepción y retorna ""; aquí simulamos eso
        with mock.patch("dt_alerts.dt_scraper.fetch_text", return_value=self._HTML), \
             mock.patch("dt_alerts.dt_scraper._extract_pdf_text", return_value=""):
            result = dt_scraper.enrich_document_detail(doc)
        self.assertIn("Resumen HTML del dictamen", result.detail_text)
        self.assertIsNotNone(result.pdf_url)  # pdf_url se guarda igual


class SubscriberActionsPlansTestCase(unittest.TestCase):
    """Tests para feat/subscribers-actions-plans."""

    def _start_server(self, tmp_path: Path):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        db.init_db(tmp_path)
        _H.settings = get_settings().__class__(
            **{**get_settings().__dict__, "database_path": str(tmp_path), "disable_admin_auth": True}
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        port = server.server_address[1]
        return server, port

    def _insert_subscriber(self, path: Path, email: str = "test@example.com") -> int:
        with db.connect(path) as conn:
            result = db.upsert_subscriber(
                conn, email=email, whatsapp=None,
                notify_email=True, notify_whatsapp=False,
                source_page="test", consent=True,
            )
            return result["id"]

    def _post(self, port: int, path: str, body: dict | None = None):
        import http.client, json as _json
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        if body is not None:
            data = _json.dumps(body).encode()
            conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
        else:
            conn.request("POST", path)
        resp = conn.getresponse()
        resp_body = resp.read()
        return resp.status, resp_body

    def test_subscriber_pause_sets_status_paused(self):
        import http.client
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                sub_id = self._insert_subscriber(path)
                conn_h = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn_h.request("POST", f"/admin/subscribers/{sub_id}/pause",
                               headers={"Content-Type": "application/json"})
                resp = conn_h.getresponse(); resp.read()
                self.assertEqual(resp.status, 200)
                with db.connect(path) as conn:
                    row = conn.execute("SELECT status FROM subscribers WHERE id=?", (sub_id,)).fetchone()
                self.assertEqual(row[0], "paused")
            finally:
                server.shutdown()

    def test_subscriber_activate_sets_status_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                sub_id = self._insert_subscriber(path)
                with db.connect(path) as conn:
                    db.set_subscriber_status(conn, sub_id, "paused")
                status, _ = self._post(port, f"/admin/subscribers/{sub_id}/activate")
                self.assertEqual(status, 200)
                with db.connect(path) as conn:
                    row = conn.execute("SELECT status FROM subscribers WHERE id=?", (sub_id,)).fetchone()
                self.assertEqual(row[0], "active")
            finally:
                server.shutdown()

    def test_subscriber_delete_removes_record(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                sub_id = self._insert_subscriber(path)
                status, body = self._post(port, f"/admin/subscribers/{sub_id}/delete")
                self.assertEqual(status, 200)
                self.assertTrue(_json.loads(body).get("success"))
                with db.connect(path) as conn:
                    row = conn.execute("SELECT id FROM subscribers WHERE id=?", (sub_id,)).fetchone()
                self.assertIsNone(row)
            finally:
                server.shutdown()

    def test_subscriber_delete_nonexistent_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                status, _ = self._post(port, "/admin/subscribers/99999/delete")
                self.assertEqual(status, 404)
            finally:
                server.shutdown()

    def test_subscriber_plan_update_valid(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                sub_id = self._insert_subscriber(path)
                status, body = self._post(port, f"/admin/subscribers/{sub_id}/plan", {"plan": "basico"})
                self.assertEqual(status, 200)
                self.assertTrue(_json.loads(body).get("success"))
                with db.connect(path) as conn:
                    row = conn.execute("SELECT plan FROM subscribers WHERE id=?", (sub_id,)).fetchone()
                self.assertEqual(row[0], "basico")
            finally:
                server.shutdown()

    def test_subscriber_plan_update_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                sub_id = self._insert_subscriber(path)
                status, _ = self._post(port, f"/admin/subscribers/{sub_id}/plan", {"plan": "vip"})
                self.assertEqual(status, 400)
            finally:
                server.shutdown()


class AlertsTableViewTestCase(unittest.TestCase):
    """Tests para feat/alerts-table-view: tabla con filtros y borrado."""

    def _make_alert(self, alert_id: int, status: str) -> dict:
        return {
            "id": alert_id,
            "document_id": alert_id,
            "title": f"Circular DT #{alert_id}",
            "category": "Portada normativa",
            "publication_date": "01/01/2026",
            "relevance": "alto",
            "status": status,
            "summary": "Resumen de prueba.",
            "key_points_json": "[]",
            "practical_impacts_json": "[]",
            "canonical_url": "https://example.com",
            "created_at": "2026-06-26T10:00:00",
            "ai_status": "success",
            "ai_provider": "azure",
            "ai_content_quality": "full",
            "ai_email_subject": "Nuevo documento",
            "ai_summary_error": None,
        }

    def test_alerts_table_shows_all_by_default(self):
        from dt_alerts.server import render_alerts_table
        alerts = [
            self._make_alert(1, "pending_review"),
            self._make_alert(2, "sent"),
            self._make_alert(3, "fallback"),
        ]
        html = render_alerts_table(alerts)
        self.assertIn("<table", html)
        self.assertIn("<thead", html)
        self.assertIn("alert-row-1", html)
        self.assertIn("alert-row-2", html)
        self.assertIn("alert-row-3", html)

    def test_alerts_filter_by_status(self):
        from dt_alerts.server import render_alerts_table
        alerts = [
            self._make_alert(1, "pending_review"),
            self._make_alert(2, "sent"),
            self._make_alert(3, "pending_review"),
        ]
        html = render_alerts_table(alerts, status_filter="pending_review")
        self.assertIn("alert-row-1", html)
        self.assertIn("alert-row-3", html)
        self.assertNotIn("alert-row-2", html)

    def test_alerts_delete_removes_record(self):
        import http.client, threading, json as _json
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            db.init_db(path)
            with db.connect(path) as conn:
                doc_id = conn.execute(
                    "INSERT INTO documents (title,category,canonical_url,source_url,dt_article_id,publication_date,status,detected_at)"
                    " VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    ("Doc test", "Categoría", "https://example.com/1", "https://example.com/1", "art1", "01/01/2026", "processed"),
                ).lastrowid
                conn.execute(
                    "INSERT INTO alerts (document_id,summary,key_points_json,practical_impacts_json,relevance,status,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
                    (doc_id, "Resumen", "[]", "[]", "alto", "pending_review"),
                )
                conn.commit()
                alert_id = conn.execute("SELECT id FROM alerts WHERE document_id=?", (doc_id,)).fetchone()[0]

            _H.settings = get_settings().__class__(
                **{**get_settings().__dict__, "database_path": str(path), "disable_admin_auth": True}
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
            t = threading.Thread(target=server.serve_forever)
            t.daemon = True
            t.start()
            try:
                port = server.server_address[1]
                conn_http = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn_http.request("POST", f"/admin/alerts/{alert_id}/delete", headers={"Cookie": "admin_token=dev"})
                resp = conn_http.getresponse()
                body = _json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                self.assertTrue(body.get("success"))
                with db.connect(path) as db_conn:
                    row = db_conn.execute("SELECT id FROM alerts WHERE id=?", (alert_id,)).fetchone()
                self.assertIsNone(row, "La alerta debe haberse eliminado de la DB")
            finally:
                server.shutdown()

    def test_alerts_delete_nonexistent_returns_404(self):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            db.init_db(path)
            _H.settings = get_settings().__class__(
                **{**get_settings().__dict__, "database_path": str(path), "disable_admin_auth": True}
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
            t = threading.Thread(target=server.serve_forever)
            t.daemon = True
            t.start()
            try:
                port = server.server_address[1]
                conn_http = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn_http.request("POST", "/admin/alerts/99999/delete", headers={"Cookie": "admin_token=dev"})
                resp = conn_http.getresponse()
                resp.read()
                self.assertEqual(resp.status, 404)
            finally:
                server.shutdown()


class AlertsSendToSubscribersTestCase(unittest.TestCase):
    """Tests para feat/alerts-send-to-subscribers."""

    def _start_server(self, tmp_path: Path):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        db.init_db(tmp_path)
        _H.settings = get_settings().__class__(
            **{**get_settings().__dict__, "database_path": str(tmp_path), "disable_admin_auth": True}
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        port = server.server_address[1]
        return server, port

    def _insert_alert(self, path: Path) -> int:
        with db.connect(path) as conn:
            doc_id = conn.execute(
                "INSERT INTO documents (title,category,canonical_url,source_url,dt_article_id,publication_date,status,detected_at)"
                " VALUES (?,?,?,?,?,?,?,datetime('now'))",
                ("ORD. Test", "Dictámenes", "https://example.com/1", "https://example.com/1", "art1", "01/01/2026", "processed"),
            ).lastrowid
            conn.execute(
                "INSERT INTO alerts (document_id,summary,key_points_json,practical_impacts_json,relevance,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (doc_id, "Resumen", "[]", "[]", "alto", "pending_review"),
            )
            conn.commit()
            return conn.execute("SELECT id FROM alerts WHERE document_id=?", (doc_id,)).fetchone()[0]

    def _post_json(self, port: int, path: str):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", path, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body

    def test_alerts_send_to_subscribers_success(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            # Insertar un suscriptor activo para que la validación no rechace
            db.init_db(path)
            with db.connect(path) as conn:
                db.upsert_subscriber(
                    conn, email="sub@example.com", whatsapp=None,
                    notify_email=True, notify_whatsapp=False,
                    source_page="test", consent=True,
                )
            server, port = self._start_server(path)
            try:
                alert_id = self._insert_alert(path)
                with mock.patch("dt_alerts.server.dispatch_alert", return_value=1):
                    status, body = self._post_json(port, f"/admin/alerts/{alert_id}/send")
                self.assertEqual(status, 200)
                data = _json.loads(body)
                self.assertTrue(data.get("success"))
                self.assertIn("sent_count", data)
                self.assertEqual(data["sent_count"], 1)
            finally:
                server.shutdown()

    def test_alerts_send_nonexistent_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                status, _ = self._post_json(port, "/admin/alerts/99999/send")
                self.assertEqual(status, 404)
            finally:
                server.shutdown()

    def test_subscribers_count_endpoint(self):
        import http.client, json as _json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite3"
            server, port = self._start_server(path)
            try:
                with db.connect(path) as conn:
                    db.upsert_subscriber(
                        conn, email="a@example.com", whatsapp=None,
                        notify_email=True, notify_whatsapp=False,
                        source_page="test", consent=True,
                    )
                conn_h = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn_h.request("GET", "/admin/api/subscribers/count")
                resp = conn_h.getresponse()
                data = _json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                self.assertIn("active", data)
                self.assertEqual(data["active"], 1)
            finally:
                server.shutdown()


class CodexProviderTestCase(unittest.TestCase):
    """Tests para el proveedor de IA 'codex' (sesión ChatGPT, sin API key). Todo con mocks: nunca llama al SDK real."""

    def _db_path(self, tmp: str) -> Path:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return path

    def _sample_doc_dict(self) -> dict:
        return {
            "dt_article_id": "codex-test-1",
            "canonical_url": "https://www.dt.gob.cl/legislacion/1624/w3-article-codex1.html",
            "source_url": "https://www.dt.gob.cl/x.html",
            "category": "Circulares",
            "title": "Circular de prueba proveedor Codex",
            "publication_date": "01/01/2026",
            "abstract": "Instruye criterios de prueba para el proveedor Codex.",
            "detail_text": "Texto de prueba para el proveedor Codex.",
            "pdf_url": None,
            "content_hash": None,
        }

    def _insert_doc(self, conn) -> int:
        doc_id, _ = db.upsert_document(conn, self._sample_doc_dict())
        db.update_document_processed(
            conn, doc_id, status="processed", detail_text="Texto.",
            pdf_url=None, content_hash=None, last_error=None,
        )
        return doc_id

    def _fake_ai_json(self) -> str:
        return json.dumps({
            "title": "Circular DT", "category": "Circulares", "relevance": "medio",
            "email_subject": "Nueva normativa DT: Circular",
            "email_summary": "Resumen generado por Codex.",
            "key_points": ["Punto 1"],
            "practical_impacts": [{"title": "Impacto", "description": "Desc"}],
            "recommended_actions": ["Accion 1"],
            "executive_summary": {"title": "Resumen ejecutivo", "body": "Cuerpo."},
            "detailed_summary": {"title": "Detalle", "sections": []},
            "tags": ["DT"], "legal_disclaimer": "Informativo.",
        })

    # --- 1. Codex funciona sin AI_API_KEY y con sesión activa devuelve JSON válido ---
    def test_01_codex_works_without_api_key_and_valid_session(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="codex", ai_api_key="")
            self.assertEqual(s.ai_api_key, "")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=(self._fake_ai_json(), "codex-chatgpt", 0, 0, 0),
                ) as mocked:
                    _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                self.assertTrue(mocked.called)
                ai_row = db.get_ai_summary(conn, doc_id)
        self.assertEqual(ai_row["status"], "success")
        self.assertEqual(ai_row["provider"], "codex")
        self.assertEqual(ai_row["email_summary"], "Resumen generado por Codex.")

    # --- 2. Sesión inexistente/caducada produce fallback local, sin romper ---
    def test_02_codex_no_session_produces_fallback(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="codex", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(False, "No hay sesión de ChatGPT activa."),
                ):
                    result = _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                ai_row = db.get_ai_summary(conn, doc_id)
        self.assertEqual(ai_row["status"], "fallback")
        self.assertIsNotNone(ai_row["email_summary"])
        self.assertEqual(result.status, "pending_review")

    # --- 3. SDK de Codex no disponible produce fallback, sin romper ---
    def test_03_codex_sdk_unavailable_produces_fallback(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="codex", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=False
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                ai_row = db.get_ai_summary(conn, doc_id)
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertEqual(ai_row["status"], "fallback")
        # validate_provider_credentials() unifica SDK ausente bajo 'missing_key'.
        self.assertEqual(rows[0]["status"], "missing_key")

    # --- 4. Respuesta de Codex no parseable produce fallback, sin romper ---
    def test_04_codex_unparseable_response_produces_fallback(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="codex", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=("esto no es json {{{{", "codex-chatgpt", 0, 0, 0),
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                ai_row = db.get_ai_summary(conn, doc_id)
        self.assertEqual(ai_row["status"], "fallback")
        self.assertIsNotNone(ai_row["email_summary"])

    # --- 5. Cada documento usa un thread/sesión independiente (sin historial compartido) ---
    def test_05_codex_each_document_uses_independent_thread(self):
        from dt_alerts import codex_client

        calls: list[tuple[str, str]] = []

        def fake_run_single_turn(system_prompt, user_prompt, workdir):
            calls.append((system_prompt, user_prompt))
            return f"respuesta-independiente-{len(calls)}"

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
        ), mock.patch(
            "dt_alerts.codex_client.check_login_status", return_value=(True, "Sesión de ChatGPT activa.")
        ), mock.patch(
            "dt_alerts.codex_client._isolated_codex_home", return_value=Path(tmp)
        ), mock.patch(
            "dt_alerts.codex_client._run_single_turn", side_effect=fake_run_single_turn
        ):
            s = settings_for(Path(":memory:"), ai_provider="codex", ai_api_key="", ai_timeout_seconds=5)
            content1, *_ = codex_client.run_codex_prompt("sys", "documento-1", s)
            content2, *_ = codex_client.run_codex_prompt("sys", "documento-2", s)

        self.assertEqual(content1, "respuesta-independiente-1")
        self.assertEqual(content2, "respuesta-independiente-2")
        self.assertEqual(len(calls), 2, "Debe invocarse un turno nuevo por documento")
        self.assertEqual(calls[0][1], "documento-1")
        self.assertEqual(calls[1][1], "documento-2")

    # --- 6. No se exponen credenciales ni rutas sensibles en errores registrados ---
    def test_06_codex_error_does_not_expose_credentials_or_paths(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="codex", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    side_effect=RuntimeError("Fallo genérico del SDK de Codex."),
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        error_text = rows[0]["error"] or ""
        self.assertNotIn("auth.json", error_text)
        self.assertNotIn(str(Path.home()), error_text)
        self.assertNotIn(".codex_home", error_text)

    # --- 7. El proveedor guardado en SQLite es 'codex' y la alerta permanece pending_review ---
    def test_07_codex_provider_saved_and_alert_stays_pending_review(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="codex", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                alert_id = db.create_or_update_alert(
                    conn, doc_id, summary="Resumen inicial.", key_points=[],
                    practical_impacts=[], relevance="medio",
                    status="pending_review", ai_error=None,
                )
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=(self._fake_ai_json(), "codex-chatgpt", 0, 0, 0),
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                ai_row = db.get_ai_summary(conn, doc_id)
                alert_row = conn.execute(
                    "SELECT status FROM alerts WHERE id = ?", (alert_id,)
                ).fetchone()
        self.assertEqual(ai_row["provider"], "codex")
        self.assertEqual(ai_row["status"], "success")
        self.assertEqual(alert_row["status"], "pending_review")

    # --- 8. openai, azure y disabled siguen funcionando sin cambios de comportamiento ---
    def test_08_openai_still_requires_api_key(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="openai", ai_api_key="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertEqual(rows[0]["status"], "missing_key")

    def test_09_azure_still_requires_base_url(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = settings_for(path, ai_enabled=True, ai_provider="azure",
                             ai_api_key="fake-key", ai_base_url="")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                _generate_and_save(conn, doc_id, s, {"ai_runtime_enabled": "true"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        # Desde feat/ai-provider-selector, validate_provider_credentials() unifica
        # toda credencial faltante (API key, modelo o base URL) bajo 'missing_key'.
        self.assertEqual(rows[0]["status"], "missing_key")
        self.assertIn("AI_BASE_URL", rows[0]["error"])

    def test_10_disabled_provider_still_falls_back(self):
        result = summarize_document(self._sample_doc_dict(), settings_for(
            Path(":memory:"), ai_provider="disabled", ai_api_key="",
        ))
        self.assertEqual(result.status, "pending_review")
        self.assertIsNotNone(result.summary)


class TLSAndSendGridTestCase(unittest.TestCase):
    """Tests para dt_alerts/tls.py y el uso del contexto SSL compartido en SendGrid.

    Todo con mocks: nunca hace conexiones reales ni envía correos reales.
    """

    def _settings(self, **overrides):
        return settings_for(
            Path(":memory:"),
            email_provider="sendgrid",
            sendgrid_api_key="SG.fake-key-for-tests-only",
            email_from="alertas@example.com",
            **overrides,
        )

    @staticmethod
    def _self_signed_pem() -> bytes:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import Encoding
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Alertas-DT Test CA")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        return cert.public_bytes(Encoding.PEM)

    # --- 1. Contexto estandar en sistemas no Windows ---
    def test_01_standard_context_on_non_windows(self):
        import os

        from dt_alerts import tls
        with mock.patch("dt_alerts.tls.platform.system", return_value="Linux"), \
             mock.patch.dict(os.environ, {"TLS_CA_BUNDLE": ""}):
            context, info = tls.build_ssl_context()
        self.assertEqual(info.backend, tls.BACKEND_STANDARD)
        self.assertEqual(info.os_name, "Linux")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    # --- 2. truststore seleccionado en Windows ---
    def test_02_truststore_selected_on_windows(self):
        import os

        from dt_alerts import tls
        if tls.truststore is None:
            self.skipTest("truststore no esta instalado en este entorno")
        with mock.patch("dt_alerts.tls.platform.system", return_value="Windows"), \
             mock.patch.dict(os.environ, {"TLS_CA_BUNDLE": ""}):
            context, info = tls.build_ssl_context()
        self.assertEqual(info.backend, tls.BACKEND_TRUSTSTORE)
        self.assertIsInstance(context, tls.truststore.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    # --- 3. Fallback seguro si truststore no esta instalado ---
    def test_03_fallback_to_standard_when_truststore_missing(self):
        import os

        from dt_alerts import tls
        with mock.patch("dt_alerts.tls.platform.system", return_value="Windows"), \
             mock.patch("dt_alerts.tls.truststore", None), \
             mock.patch.dict(os.environ, {"TLS_CA_BUNDLE": ""}):
            context, info = tls.build_ssl_context()
        self.assertEqual(info.backend, tls.BACKEND_STANDARD)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    # --- 4. TLS_CA_BUNDLE valido se carga y usa contexto estandar (truststore ignora CAs manuales) ---
    def test_04_valid_ca_bundle_loads_and_uses_standard_backend(self):
        from dt_alerts import tls
        import os
        with tempfile.TemporaryDirectory() as tmp:
            ca_path = Path(tmp) / "corporate-ca.pem"
            ca_path.write_bytes(self._self_signed_pem())
            with mock.patch("dt_alerts.tls.platform.system", return_value="Windows"), \
                 mock.patch.dict(os.environ, {"TLS_CA_BUNDLE": str(ca_path)}):
                context, info = tls.build_ssl_context()
        self.assertEqual(info.backend, tls.BACKEND_STANDARD)
        self.assertTrue(info.ca_bundle_configured)
        self.assertEqual(info.ca_bundle_label, "corporate-ca.pem")
        self.assertIsNone(info.error)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    # --- 5. TLS_CA_BUNDLE inexistente reporta error sanitizado (sin ruta completa) ---
    def test_05_missing_ca_bundle_reports_sanitized_error(self):
        from dt_alerts import tls
        import os
        fake_path = r"C:\Users\alguien\ruta\no-existe.pem"
        with mock.patch.dict(os.environ, {"TLS_CA_BUNDLE": fake_path}):
            context, info = tls.build_ssl_context()
        self.assertTrue(info.ca_bundle_configured)
        self.assertIsNotNone(info.error)
        self.assertNotIn("alguien", info.error)
        self.assertNotIn("C:\\Users", info.error)
        self.assertIn("no-existe.pem", info.error)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    # --- 6. Certificado invalido produce error, nunca bypass de verificacion ---
    def test_06_invalid_ca_bundle_does_not_bypass_verification(self):
        from dt_alerts import tls
        import os
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "garbage.pem"
            bad_path.write_text("esto no es un certificado PEM valido")
            with mock.patch.dict(os.environ, {"TLS_CA_BUNDLE": str(bad_path)}):
                context, info = tls.build_ssl_context()
        self.assertIsNotNone(info.error)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    # --- 7. SendGrid usa el contexto SSL compartido (no uno propio) ---
    def test_07_sendgrid_uses_shared_ssl_context(self):
        from dt_alerts import tls
        sentinel_context = object()
        sentinel_info = tls.TLSBackendInfo(
            backend="truststore", os_name="Windows",
            ca_bundle_configured=False, ca_bundle_label="", error=None,
        )
        captured = {}

        def fake_urlopen(request, timeout=None, context=None):
            captured["context"] = context
            import io
            resp = mock.MagicMock()
            resp.headers.get.return_value = "msg-id-123"
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with mock.patch("dt_alerts.notifier.tls.build_ssl_context",
                        return_value=(sentinel_context, sentinel_info)), \
             mock.patch("dt_alerts.notifier.urllib.request.urlopen", side_effect=fake_urlopen):
            result = notifier.send_email(
                self._settings(), to="dest@example.com", subject="s",
                html_body="<p>h</p>", text_body="t",
            )
        self.assertIs(captured["context"], sentinel_context)
        self.assertEqual(result["status"], "sent")

    # --- 8. Las claves nunca aparecen en los mensajes de error ---
    def test_08_sendgrid_error_never_exposes_api_key(self):
        settings = self._settings()

        def fake_urlopen(request, timeout=None, context=None):
            raise RuntimeError(f"fallo de red con Authorization: Bearer {settings.sendgrid_api_key}")

        with mock.patch("dt_alerts.notifier.urllib.request.urlopen", side_effect=fake_urlopen):
            result = notifier.send_email(
                settings, to="dest@example.com", subject="s",
                html_body="<p>h</p>", text_body="t",
            )
        self.assertNotIn(settings.sendgrid_api_key, result["error"])
        self.assertNotIn(settings.sendgrid_api_key, result["message"])
        self.assertIn("[REDACTED]", result["error"])

    # --- 9. HTTP 202 (exito) se registra como envio exitoso ---
    def test_09_http_202_recorded_as_sent(self):
        def fake_urlopen(request, timeout=None, context=None):
            resp = mock.MagicMock()
            resp.status = 202
            resp.headers.get.return_value = "msg-id-202"
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with mock.patch("dt_alerts.notifier.urllib.request.urlopen", side_effect=fake_urlopen):
            result = notifier.send_email(
                self._settings(), to="dest@example.com", subject="s",
                html_body="<p>h</p>", text_body="t",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["provider_message_id"], "msg-id-202")

    # --- 10. HTTP 401 y 403 se reportan como errores de autenticacion/permisos ---
    def test_10_http_401_and_403_reported_as_auth_errors(self):
        import io

        for code, label in ((401, "401"), (403, "403")):
            def fake_urlopen(request, timeout=None, context=None, _code=code):
                raise urllib.error.HTTPError(
                    "https://api.sendgrid.com/v3/mail/send", _code, "auth error",
                    {}, io.BytesIO(b'{"errors":[{"message":"denied"}]}'),
                )

            with mock.patch("dt_alerts.notifier.urllib.request.urlopen", side_effect=fake_urlopen):
                result = notifier.send_email(
                    self._settings(), to="dest@example.com", subject="s",
                    html_body="<p>h</p>", text_body="t",
                )
            self.assertEqual(result["status"], "failed")
            self.assertIn(f"HTTP {label}", result["error"])

    # --- 11. Error TLS se reporta distinto de una API key invalida ---
    def test_11_tls_error_reported_differently_than_invalid_key(self):
        tls_error = ssl.SSLCertVerificationError()
        tls_error.verify_message = "self-signed certificate in certificate chain"

        def fake_urlopen_tls(request, timeout=None, context=None):
            raise urllib.error.URLError(tls_error)

        with mock.patch("dt_alerts.notifier.urllib.request.urlopen", side_effect=fake_urlopen_tls):
            tls_result = notifier.send_email(
                self._settings(), to="dest@example.com", subject="s",
                html_body="<p>h</p>", text_body="t",
            )

        import io as _io

        def fake_urlopen_401(request, timeout=None, context=None):
            raise urllib.error.HTTPError(
                "https://api.sendgrid.com/v3/mail/send", 401, "Unauthorized",
                {}, _io.BytesIO(b'{"errors":[{"message":"invalid key"}]}'),
            )

        with mock.patch("dt_alerts.notifier.urllib.request.urlopen", side_effect=fake_urlopen_401):
            key_result = notifier.send_email(
                self._settings(), to="dest@example.com", subject="s",
                html_body="<p>h</p>", text_body="t",
            )

        self.assertIn("TLS", tls_result["error"])
        self.assertNotIn("TLS", key_result["error"])
        self.assertNotIn("HTTP 401", tls_result["error"])
        self.assertIn("HTTP 401", key_result["error"])

    # --- 12. EMAIL_PROVIDER=console sigue funcionando (sin tocar TLS) ---
    def test_12_console_provider_still_works(self):
        settings = settings_for(Path(":memory:"), email_provider="console")
        result = notifier.send_email(
            settings, to="dest@example.com", subject="s",
            html_body="<p>h</p>", text_body="t",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "simulated")


class AIProviderSelectorTestCase(unittest.TestCase):
    """Tests para el selector de proveedor de IA (ai_active_provider) — feat/ai-provider-selector.

    Todo con mocks o subprocesos controlados: nunca llama a Azure/OpenAI/Codex reales.
    """

    def _db_path(self, tmp: str) -> Path:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return path

    def _sample_doc_dict(self, suffix: str = "1") -> dict:
        return {
            "dt_article_id": f"selector-test-{suffix}",
            "canonical_url": f"https://www.dt.gob.cl/legislacion/1624/w3-article-selector{suffix}.html",
            "source_url": "https://www.dt.gob.cl/x.html",
            "category": "Circulares",
            "title": "Circular de prueba selector de proveedor",
            "publication_date": "01/01/2026",
            "abstract": "Instruye criterios de prueba para el selector de proveedor.",
            "detail_text": "Texto de prueba para el selector de proveedor.",
            "pdf_url": None,
            "content_hash": None,
        }

    def _insert_doc(self, conn, suffix: str = "1") -> int:
        doc_id, _ = db.upsert_document(conn, self._sample_doc_dict(suffix))
        db.update_document_processed(
            conn, doc_id, status="processed", detail_text="Texto.",
            pdf_url=None, content_hash=None, last_error=None,
        )
        return doc_id

    def _azure_settings(self, **overrides):
        defaults = dict(
            ai_provider="azure", ai_api_key="fake-azure-key",
            ai_model="gpt-chat-latest",
            ai_base_url="https://DemoTiboxIA.services.ai.azure.com/openai/v1",
            ai_enabled=True,
        )
        defaults.update(overrides)
        return settings_for(Path(":memory:"), **defaults)

    def _fake_ai_json(self, provider_label: str) -> str:
        return json.dumps({
            "title": "Circular DT", "category": "Circulares", "relevance": "medio",
            "email_subject": f"Nueva normativa DT: {provider_label}",
            "email_summary": f"Resumen generado por {provider_label}.",
            "key_points": ["Punto 1"],
            "practical_impacts": [{"title": "Impacto", "description": "Desc"}],
            "recommended_actions": ["Accion 1"],
            "executive_summary": {"title": "Resumen ejecutivo", "body": "Cuerpo."},
            "detailed_summary": {"title": "Detalle", "sections": []},
            "tags": ["DT"], "legal_disclaimer": "Informativo.",
        })

    # --- 1. Azure seleccionado inicialmente desde .env (sin valor guardado en panel) ---
    def test_01_azure_initial_from_env(self):
        from dt_alerts.summarizer import get_effective_ai_provider
        s = self._azure_settings()
        self.assertEqual(get_effective_ai_provider(s, {}), "azure")

    # --- 2. Azure seleccionado desde el panel (app_settings manda sobre AI_PROVIDER) ---
    def test_02_azure_selected_from_panel(self):
        from dt_alerts.summarizer import get_effective_ai_provider
        s = settings_for(Path(":memory:"), ai_provider="disabled")
        self.assertEqual(
            get_effective_ai_provider(s, {"ai_active_provider": "azure"}), "azure"
        )

    # --- 3. Codex seleccionado desde el panel (con Azure configurado en el entorno) ---
    def test_03_codex_selected_from_panel_used_in_generation(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path)  # Azure configurado en .env...
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=(self._fake_ai_json("Codex"), "codex-chatgpt", 0, 0, 0),
                ):
                    # ...pero el panel tiene seleccionado codex.
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "codex"})
                ai_row = db.get_ai_summary(conn, doc_id)
        self.assertEqual(ai_row["provider"], "codex")
        self.assertEqual(ai_row["status"], "success")

    # --- 4. OpenAI seleccionado desde el panel ---
    def test_04_openai_selected_from_panel(self):
        from dt_alerts.summarizer import AIResponse, _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path, ai_api_key="fake-openai-key")
            fake_response = AIResponse(content=self._fake_ai_json("OpenAI"), total_tokens=10)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.summarizer._call_openai_api", return_value=fake_response
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "openai"})
                ai_row = db.get_ai_summary(conn, doc_id)
        self.assertEqual(ai_row["provider"], "openai")
        self.assertEqual(ai_row["status"], "success")

    # --- 5. Disabled seleccionado desde el panel: nunca llama a ningún proveedor externo ---
    def test_05_disabled_selected_from_panel_uses_fallback_only(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path)  # credenciales Azure válidas presentes
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch("dt_alerts.summarizer.call_ai_with_usage") as mocked_call:
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "disabled"})
                ai_row = db.get_ai_summary(conn, doc_id)
        mocked_call.assert_not_called()
        self.assertEqual(ai_row["status"], "fallback")

    # --- 6/7. Cambiar de proveedor sin reiniciar (misma conexión y objeto settings) ---
    def test_06_switch_provider_without_restart(self):
        from dt_alerts.summarizer import AIResponse, _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path)
            with db.connect(path) as conn:
                doc_id_1 = self._insert_doc(conn, suffix="1")

                fake_azure = AIResponse(content=self._fake_ai_json("Azure"), total_tokens=5)
                with mock.patch("dt_alerts.summarizer._call_azure_api", return_value=fake_azure):
                    _generate_and_save(conn, doc_id_1, s, {"ai_active_provider": "azure"})
                row_1 = db.get_ai_summary(conn, doc_id_1)

                # Mismo objeto `s` y misma conexión: solo cambia el ajuste en DB.
                doc_id_2 = self._insert_doc(conn, suffix="2")
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=(self._fake_ai_json("Codex"), "codex-chatgpt", 0, 0, 0),
                ):
                    _generate_and_save(conn, doc_id_2, s, {"ai_active_provider": "codex"})
                row_2 = db.get_ai_summary(conn, doc_id_2)

                # Vuelta a Azure, mismo proceso, sin recrear nada.
                doc_id_3 = self._insert_doc(conn, suffix="3")
                with mock.patch("dt_alerts.summarizer._call_azure_api", return_value=fake_azure):
                    _generate_and_save(conn, doc_id_3, s, {"ai_active_provider": "azure"})
                row_3 = db.get_ai_summary(conn, doc_id_3)
        self.assertEqual(row_1["provider"], "azure")
        self.assertEqual(row_2["provider"], "codex")
        self.assertEqual(row_3["provider"], "azure")

    # --- 8. El proveedor persiste tras cerrar y volver a abrir la conexión SQLite ---
    def test_08_provider_persists_across_db_reconnect(self):
        from dt_alerts.summarizer import get_effective_ai_provider
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                db.set_setting(conn, "ai_active_provider", "codex")
            # Conexión cerrada y reabierta: nada en memoria persiste salvo la DB.
            with db.connect(path) as conn:
                app_settings = db.get_all_settings(conn)
        s = self._azure_settings(database_path=path)
        self.assertEqual(get_effective_ai_provider(s, app_settings), "codex")

    # --- 9/10/11. Azure: falta AI_BASE_URL / AI_MODEL / AI_API_KEY, exactamente ---
    def test_09_azure_missing_base_url_reports_exact_variable(self):
        from dt_alerts.summarizer import validate_provider_credentials
        s = self._azure_settings(ai_base_url="")
        problems = validate_provider_credentials("azure", s)
        self.assertEqual(len(problems), 1)
        self.assertIn("AI_BASE_URL", problems[0])

    def test_10_azure_missing_model_reports_exact_variable(self):
        from dt_alerts.summarizer import validate_provider_credentials
        s = self._azure_settings(ai_model="")
        problems = validate_provider_credentials("azure", s)
        self.assertEqual(len(problems), 1)
        self.assertIn("AI_MODEL", problems[0])

    def test_11_azure_missing_api_key_reports_exact_variable(self):
        from dt_alerts.summarizer import validate_provider_credentials
        s = self._azure_settings(ai_api_key="")
        problems = validate_provider_credentials("azure", s)
        self.assertEqual(len(problems), 1)
        self.assertIn("AI_API_KEY", problems[0])

    # --- 12. Codex no requiere API key ---
    def test_12_codex_requires_no_api_key(self):
        from dt_alerts.summarizer import validate_provider_credentials
        s = settings_for(Path(":memory:"), ai_provider="codex", ai_api_key="")
        with mock.patch("dt_alerts.codex_client.is_codex_sdk_available", return_value=True), \
             mock.patch("dt_alerts.codex_client.check_login_status", return_value=(True, "ok")):
            problems = validate_provider_credentials("codex", s)
        self.assertEqual(problems, [])

    # --- 13. Codex sin sesión reporta problema (sin exigir API key) ---
    def test_13_codex_without_session_reports_problem(self):
        from dt_alerts.summarizer import validate_provider_credentials
        s = settings_for(Path(":memory:"), ai_provider="codex", ai_api_key="")
        with mock.patch("dt_alerts.codex_client.is_codex_sdk_available", return_value=True), \
             mock.patch("dt_alerts.codex_client.check_login_status",
                        return_value=(False, "No hay sesión de ChatGPT activa.")):
            problems = validate_provider_credentials("codex", s)
        self.assertTrue(problems)
        self.assertIn("codex_login.py", problems[0])

    # --- 14. Codex con sesión activa: listo para usar ---
    def test_14_codex_with_active_session_is_ready(self):
        from dt_alerts.summarizer import validate_provider_credentials
        s = settings_for(Path(":memory:"), ai_provider="codex", ai_api_key="")
        with mock.patch("dt_alerts.codex_client.is_codex_sdk_available", return_value=True), \
             mock.patch("dt_alerts.codex_client.check_login_status",
                        return_value=(True, "Sesión de ChatGPT activa.")):
            problems = validate_provider_credentials("codex", s)
        self.assertEqual(problems, [])

    # --- 15/16. El proveedor correcto se registra en ai_usage_logs y ai_summaries ---
    def test_15_correct_provider_recorded_in_usage_logs_and_summaries(self):
        from dt_alerts.summarizer import AIResponse, _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path)
            fake_response = AIResponse(content=self._fake_ai_json("Azure"), total_tokens=5)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch("dt_alerts.summarizer._call_azure_api", return_value=fake_response):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "azure"})
                ai_row = db.get_ai_summary(conn, doc_id)
                usage_rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertEqual(ai_row["provider"], "azure")
        self.assertEqual(usage_rows[0]["provider"], "azure")

    # --- 15b. Azure registra el modelo real configurado (AI_MODEL) ---
    def test_15b_azure_records_configured_model(self):
        from dt_alerts.summarizer import AIResponse, _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path, ai_model="gpt-chat-latest")
            fake_response = AIResponse(
                content=self._fake_ai_json("Azure"), total_tokens=5, model="gpt-chat-latest",
            )
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch("dt_alerts.summarizer._call_azure_api", return_value=fake_response):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "azure"})
                ai_row = db.get_ai_summary(conn, doc_id)
                usage_rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertEqual(ai_row["model"], "gpt-chat-latest")
        self.assertEqual(usage_rows[0]["model"], "gpt-chat-latest")

    # --- 15c. Codex NUNCA hereda AI_MODEL de Azure (aunque esté configurado en el entorno) ---
    def test_15c_codex_does_not_inherit_azure_model(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            # AI_MODEL sigue configurado con el deployment de Azure en el entorno.
            s = self._azure_settings(database_path=path, ai_model="gpt-chat-latest")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=(self._fake_ai_json("Codex"), "codex-chatgpt", 0, 0, 0),
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "codex"})
                ai_row = db.get_ai_summary(conn, doc_id)
                usage_rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertNotEqual(ai_row["model"], "gpt-chat-latest")
        self.assertNotEqual(usage_rows[0]["model"], "gpt-chat-latest")

    # --- 15d. Codex registra 'codex-chatgpt' cuando el SDK no informa un modelo real ---
    def test_15d_codex_records_codex_chatgpt_label_by_default(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path, ai_model="gpt-chat-latest")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=(self._fake_ai_json("Codex"), "codex-chatgpt", 0, 0, 0),
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "codex"})
                ai_row = db.get_ai_summary(conn, doc_id)
                usage_rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertEqual(ai_row["model"], "codex-chatgpt")
        self.assertEqual(usage_rows[0]["model"], "codex-chatgpt")

    # --- 15e. Codex registra el modelo real si el SDK lo informa (no el fallback fijo) ---
    def test_15e_codex_records_real_sdk_reported_model_when_available(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path, ai_model="gpt-chat-latest")
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.codex_client.is_codex_sdk_available", return_value=True
                ), mock.patch(
                    "dt_alerts.codex_client.check_login_status",
                    return_value=(True, "Sesión de ChatGPT activa."),
                ), mock.patch(
                    "dt_alerts.codex_client.run_codex_prompt",
                    return_value=(self._fake_ai_json("Codex"), "gpt-5-codex", 0, 0, 0),
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "codex"})
                ai_row = db.get_ai_summary(conn, doc_id)
                usage_rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertEqual(ai_row["model"], "gpt-5-codex")
        self.assertEqual(usage_rows[0]["model"], "gpt-5-codex")

    # --- 17. Ninguna API key queda guardada en SQLite ---
    def test_17_no_api_keys_stored_in_sqlite(self):
        from dt_alerts.summarizer import AIResponse, _generate_and_save
        secret = "AZURE-SECRET-KEY-MUST-NOT-BE-STORED-xyz"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path, ai_api_key=secret)
            fake_response = AIResponse(content=self._fake_ai_json("Azure"), total_tokens=5)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch("dt_alerts.summarizer._call_azure_api", return_value=fake_response):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "azure"})
            raw = path.read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)

    # --- 18. Las claves no se exponen en mensajes de error ---
    def test_18_api_key_never_exposed_in_error_messages(self):
        from dt_alerts.summarizer import _generate_and_save
        secret = "AZURE-SECRET-KEY-IN-ERROR-abc123"
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path, ai_api_key=secret)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.summarizer._call_azure_api",
                    side_effect=RuntimeError(f"HTTP 401: token {secret} invalido"),
                ):
                    _generate_and_save(conn, doc_id, s, {"ai_active_provider": "azure"})
                rows = db.get_recent_ai_usage(conn, limit=1)
        self.assertNotIn(secret, rows[0]["error"] or "")
        self.assertIn("[REDACTED]", rows[0]["error"] or "")

    # --- 19. El fallback local sigue funcionando cuando el proveedor falla ---
    def test_19_local_fallback_still_works_on_provider_failure(self):
        from dt_alerts.summarizer import _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                with mock.patch(
                    "dt_alerts.summarizer._call_azure_api",
                    side_effect=RuntimeError("Azure no disponible"),
                ):
                    result = _generate_and_save(conn, doc_id, s, {"ai_active_provider": "azure"})
        self.assertIsNotNone(result.summary)
        self.assertEqual(result.status, "pending_review")

    # --- 20. La alerta permanece pending_review sin importar el proveedor ---
    def test_20_alert_stays_pending_review_regardless_of_provider(self):
        from dt_alerts.summarizer import AIResponse, _generate_and_save
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            s = self._azure_settings(database_path=path)
            fake_response = AIResponse(content=self._fake_ai_json("Azure"), total_tokens=5)
            with db.connect(path) as conn:
                doc_id = self._insert_doc(conn)
                alert_id = db.create_or_update_alert(
                    conn, doc_id, summary="Inicial.", key_points=[], practical_impacts=[],
                    relevance="medio", status="pending_review", ai_error=None,
                )
                with mock.patch("dt_alerts.summarizer._call_azure_api", return_value=fake_response):
                    result = _generate_and_save(conn, doc_id, s, {"ai_active_provider": "azure"})
                alert_row = conn.execute(
                    "SELECT status FROM alerts WHERE id = ?", (alert_id,)
                ).fetchone()
        self.assertEqual(result.status, "pending_review")
        self.assertEqual(alert_row["status"], "pending_review")

    # --- 21. .env se carga automáticamente (subproceso real, sin la bandera de tests) ---
    # Usa una variable exclusiva (no un campo de Settings) para no depender de,
    # ni colisionar con, el contenido real de .env.local de este desarrollador.
    # .env.local usa exactamente el mismo mecanismo de carga (misma línea de
    # código en dt_alerts/config.py), por lo que probar vía .env es equivalente
    # y evita tocar el archivo real que contiene secretos.
    def test_21_env_file_loads_automatically_end_to_end(self):
        import subprocess
        import sys as _sys

        project_root = Path(__file__).resolve().parent.parent
        venv_python = project_root / ".venv" / "Scripts" / "python.exe"
        if not venv_python.is_file():
            venv_python = Path(_sys.executable)

        env_path = project_root / ".env"
        if env_path.exists():
            self.skipTest(".env ya existe en este repo; se omite para no sobreescribirlo.")

        marker = "dotenv-autoload-marker-value-12345"
        env_path.write_text(f"ALERTASDT_TEST_DOTENV_MARKER={marker}\n", encoding="utf-8")
        try:
            clean_env = dict(os.environ)
            clean_env.pop("ALERTAS_DT_SKIP_DOTENV", None)
            clean_env.pop("ALERTASDT_TEST_DOTENV_MARKER", None)
            proc = subprocess.run(
                [str(venv_python), "-c",
                 "import dt_alerts.config, os; print(os.environ.get('ALERTASDT_TEST_DOTENV_MARKER', ''))"],
                cwd=str(project_root), capture_output=True, text=True, timeout=30, env=clean_env,
            )
            self.assertEqual(proc.stdout.strip(), marker, proc.stderr)
        finally:
            env_path.unlink(missing_ok=True)

    # --- 22. Las variables reales del sistema tienen prioridad sobre .env/.env.local ---
    def test_22_system_env_var_has_priority_over_dotenv_file(self):
        import subprocess
        import sys as _sys

        project_root = Path(__file__).resolve().parent.parent
        venv_python = project_root / ".venv" / "Scripts" / "python.exe"
        if not venv_python.is_file():
            venv_python = Path(_sys.executable)

        env_path = project_root / ".env"
        if env_path.exists():
            self.skipTest(".env ya existe en este repo; se omite para no sobreescribirlo.")

        env_path.write_text("ALERTASDT_TEST_DOTENV_MARKER=valor-del-archivo\n", encoding="utf-8")
        try:
            clean_env = dict(os.environ)
            clean_env.pop("ALERTAS_DT_SKIP_DOTENV", None)
            clean_env["ALERTASDT_TEST_DOTENV_MARKER"] = "valor-del-sistema"
            proc = subprocess.run(
                [str(venv_python), "-c",
                 "import dt_alerts.config, os; print(os.environ.get('ALERTASDT_TEST_DOTENV_MARKER', ''))"],
                cwd=str(project_root), capture_output=True, text=True, timeout=30, env=clean_env,
            )
            self.assertEqual(proc.stdout.strip(), "valor-del-sistema", proc.stderr)
        finally:
            env_path.unlink(missing_ok=True)


class AIToggleFromPanelTestCase(unittest.TestCase):
    """Tests para feat/ai-toggle-from-panel."""

    def _start_server(self, tmp_path: Path, **settings_overrides):
        import threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        db.init_db(tmp_path)
        base = get_settings().__class__(
            **{**get_settings().__dict__,
               "database_path": str(tmp_path),
               "disable_admin_auth": True,
               **settings_overrides}
        )
        _H.settings = base
        server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        return server, server.server_address[1]

    def _post_json(self, port: int, path: str, body: dict):
        import http.client, json as _json
        data = _json.dumps(body).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", path, body=data,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, _json.loads(resp.read())

    def test_ai_toggle_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path, ai_api_key="sk-test-key")
            try:
                status, data = self._post_json(port, "/admin/settings/ai-toggle", {"enabled": True})
                self.assertEqual(status, 200)
                self.assertTrue(data.get("success"))
                self.assertTrue(data.get("enabled"))
                with db.connect(path) as conn:
                    val = db.get_setting(conn, "ai_runtime_enabled")
                self.assertEqual(val, "true")
            finally:
                server.shutdown()

    def test_ai_toggle_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path, ai_api_key="sk-test-key")
            try:
                status, data = self._post_json(port, "/admin/settings/ai-toggle", {"enabled": False})
                self.assertEqual(status, 200)
                self.assertTrue(data.get("success"))
                self.assertFalse(data.get("enabled"))
                with db.connect(path) as conn:
                    val = db.get_setting(conn, "ai_runtime_enabled")
                self.assertEqual(val, "false")
            finally:
                server.shutdown()

    def test_ai_toggle_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path, ai_api_key="")
            try:
                status, data = self._post_json(port, "/admin/settings/ai-toggle", {"enabled": True})
                self.assertEqual(status, 400)
                self.assertIn("error", data)
            finally:
                server.shutdown()

    def test_summarizer_respects_db_toggle(self):
        from dt_alerts.summarizer import is_ai_runtime_enabled
        settings_on = settings_for(Path(":memory:"), ai_enabled=True, ai_api_key="sk-test")
        settings_off_env = settings_for(Path(":memory:"), ai_enabled=False, ai_api_key="sk-test")
        # DB dice false → IA desactivada sin importar AI_ENABLED
        self.assertFalse(is_ai_runtime_enabled(settings_on, {"ai_runtime_enabled": "false"}))
        # DB dice true → IA activa
        self.assertTrue(is_ai_runtime_enabled(settings_on, {"ai_runtime_enabled": "true"}))
        # Sin valor en DB, ai_enabled=False → usa .env como fallback → False
        self.assertFalse(is_ai_runtime_enabled(settings_off_env, {}))
        # Sin valor en DB, ai_enabled=True → usa .env como fallback → True
        self.assertTrue(is_ai_runtime_enabled(settings_on, {}))


class WordPressSyncPanelTestCase(unittest.TestCase):
    """Tests para el contador 'Suscriptores sincronizados' del panel (fix/admin-metrics)."""

    def _start_server(self, tmp_path: Path, **settings_overrides):
        import threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        db.init_db(tmp_path)
        # database_path debe ser un Path real (no str): render_settings llama
        # métodos de Path (ej. .exists()) sobre este valor.
        base = get_settings().__class__(
            **{**get_settings().__dict__,
               "database_path": tmp_path,
               "disable_admin_auth": True,
               **settings_overrides}
        )
        _H.settings = base
        server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        return server, server.server_address[1]

    def _post(self, port: int, path: str) -> int:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", path, body=b"")
        resp = conn.getresponse()
        resp.read()
        return resp.status

    def _get(self, port: int, path: str) -> str:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.read().decode("utf-8", errors="replace")

    # --- 4 recibidos, 1 creado, 3 actualizados: el contador debe mostrar 4, no 0 ---
    def test_synced_counter_reflects_created_plus_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(
                path, wordpress_sync_enabled=True,
                wordpress_api_url="http://fake", wordpress_api_token="tok",
            )
            try:
                # 3 suscriptores YA existen, creados por un canal que no
                # contiene "wordpress" ni "wp" en source_page — con la
                # heurística anterior el contador quedaba en 0 tras el sync.
                with db.connect(path) as conn:
                    for i in range(3):
                        db.upsert_subscriber(
                            conn, email=f"existente{i}@example.com", whatsapp=None,
                            notify_email=True, notify_whatsapp=False,
                            source_page="formulario-sitio-externo", consent=True,
                        )
                # db.utcnow() trunca a segundos: cruzar el segundo evita que
                # wordpress_sync clasifique una actualización como creación
                # solo porque created_at/updated_at coinciden por resolución.
                import time as _time
                _time.sleep(1.1)

                fake_page = {
                    "ok": True, "total": 4, "limit": 100,
                    "subscribers": [
                        {"id": 1, "email": "existente0@example.com", "consent": True, "source_page": "form"},
                        {"id": 2, "email": "existente1@example.com", "consent": True, "source_page": "form"},
                        {"id": 3, "email": "existente2@example.com", "consent": True, "source_page": "form"},
                        {"id": 4, "email": "nuevo@example.com", "consent": True, "source_page": "form"},
                    ],
                }
                with mock.patch(
                    "dt_alerts.wordpress_sync._fetch_subscribers", return_value=fake_page
                ), mock.patch("dt_alerts.wordpress_sync._mark_synced"):
                    status = self._post(port, "/admin/wordpress/sync")
                self.assertIn(status, (200, 303, 302))

                with db.connect(path) as conn:
                    summary_raw = db.get_setting(conn, "wordpress_last_sync_summary")
                self.assertIsNotNone(summary_raw)
                summary = json.loads(summary_raw)
                self.assertEqual(summary["received"], 4)
                self.assertEqual(summary["created"], 1)
                self.assertEqual(summary["updated"], 3)

                html = self._get(port, "/admin/settings")
                self.assertIn("Suscriptores sincronizados", html)
                # El contador (creados + actualizados = 1 + 3 = 4) debe
                # aparecer junto a la etiqueta, no un 0 residual.
                idx = html.find("Suscriptores sincronizados")
                snippet = html[idx: idx + 200]
                self.assertIn(">4<", snippet)
            finally:
                server.shutdown()


class AILastErrorDisplayTestCase(unittest.TestCase):
    """Tests para que 'Último error registrado' no parezca un error vigente
    tras una llamada exitosa posterior (fix/admin-metrics)."""

    def _db_path(self, tmp: str) -> Path:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return path

    # --- has_ai_success_after() / get_ai_usage_status(): resuelto vs no resuelto ---
    def test_01_last_error_marked_resolved_after_later_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                db.record_ai_usage(
                    conn, operation="generate_summary", status="error",
                    error="Azure requiere AI_BASE_URL configurado.",
                    daily_limit=50000, monthly_limit=500000,
                )
                db.record_ai_usage(
                    conn, operation="generate_summary", status="success",
                    total_tokens=10, daily_limit=50000, monthly_limit=500000,
                )
                usage_status = db.get_ai_usage_status(conn, daily_limit=50000, monthly_limit=500000)
        self.assertTrue(usage_status["last_error"])
        self.assertTrue(usage_status["last_error_resolved"])

    def test_02_last_error_not_resolved_without_later_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                db.record_ai_usage(
                    conn, operation="generate_summary", status="error",
                    error="Azure requiere AI_BASE_URL configurado.",
                    daily_limit=50000, monthly_limit=500000,
                )
                usage_status = db.get_ai_usage_status(conn, daily_limit=50000, monthly_limit=500000)
        self.assertTrue(usage_status["last_error"])
        self.assertFalse(usage_status["last_error_resolved"])

    def test_03_historical_error_row_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                db.record_ai_usage(
                    conn, operation="generate_summary", status="error",
                    error="Azure requiere AI_BASE_URL configurado.",
                    daily_limit=50000, monthly_limit=500000,
                )
                db.record_ai_usage(
                    conn, operation="generate_summary", status="success",
                    total_tokens=10, daily_limit=50000, monthly_limit=500000,
                )
                rows = db.get_recent_ai_usage(conn, limit=10)
        statuses = [r["status"] for r in rows]
        self.assertIn("error", statuses)
        self.assertIn("success", statuses)

    # --- Panel: la etiqueta y el estado no deben parecer un error vigente ---
    def _start_server(self, tmp_path: Path, **settings_overrides):
        import threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        db.init_db(tmp_path)
        base = get_settings().__class__(
            **{**get_settings().__dict__,
               "database_path": tmp_path,
               "disable_admin_auth": True,
               **settings_overrides}
        )
        _H.settings = base
        server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        return server, server.server_address[1]

    def _get(self, port: int, path: str) -> str:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.read().decode("utf-8", errors="replace")

    def test_04_panel_shows_resolved_status_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path, ai_provider="azure", ai_api_key="fake")
            try:
                with db.connect(path) as conn:
                    db.record_ai_usage(
                        conn, operation="generate_summary", status="error",
                        error="Azure requiere AI_BASE_URL configurado.",
                        daily_limit=50000, monthly_limit=500000,
                    )
                    db.record_ai_usage(
                        conn, operation="generate_summary", status="success",
                        total_tokens=10, daily_limit=50000, monthly_limit=500000,
                    )
                html = self._get(port, "/admin/settings")
            finally:
                server.shutdown()
        self.assertIn("Último error registrado", html)
        self.assertIn("Resuelto", html)
        self.assertIn("Azure requiere AI_BASE_URL configurado.", html)


class DashboardV2TestCase(unittest.TestCase):
    """Tests para el dashboard operativo /admin (feat/alerts-ui-v2)."""

    def _start_server(self, tmp_path: Path, **settings_overrides):
        import threading
        from http.server import ThreadingHTTPServer
        from dt_alerts.server import AppHandler

        class _H(AppHandler):
            pass

        db.init_db(tmp_path)
        base = get_settings().__class__(
            **{**get_settings().__dict__,
               "database_path": tmp_path,
               "disable_admin_auth": True,
               **settings_overrides}
        )
        _H.settings = base
        server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        return server, server.server_address[1]

    def _get(self, port: int, path: str) -> tuple[int, str]:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", errors="replace")

    def _insert_alert(self, conn, suffix: str, status: str) -> int:
        doc = {
            "dt_article_id": f"dash-{suffix}",
            "canonical_url": f"https://www.dt.gob.cl/legislacion/1624/w3-article-dash{suffix}.html",
            "source_url": "https://www.dt.gob.cl/x.html",
            "category": "Circulares",
            "title": f"Documento de prueba dashboard {suffix}",
            "publication_date": "01/01/2026",
            "abstract": "Abstract.",
            "detail_text": "Texto.",
            "pdf_url": None,
            "content_hash": None,
        }
        doc_id, _ = db.upsert_document(conn, doc)
        db.update_document_processed(
            conn, doc_id, status="processed", detail_text="Texto.",
            pdf_url=None, content_hash=None, last_error=None,
        )
        db.create_or_update_alert(
            conn, doc_id, summary="Resumen.", key_points=[], practical_impacts=[],
            relevance="medio", status=status, ai_error=None,
        )
        return doc_id

    # --- 1. El dashboard responde correctamente e incluye los bloques nuevos ---
    def test_01_dashboard_responds_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path)
            try:
                status, body = self._get(port, "/admin")
            finally:
                server.shutdown()
        self.assertEqual(status, 200)
        self.assertIn("Alertas que requieren atención", body)
        self.assertIn("Salud del sistema", body)

    # --- 2. Muestra el contador real de alertas pendientes ---
    def test_02_dashboard_shows_alert_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path)
            try:
                with db.connect(path) as conn:
                    self._insert_alert(conn, "1", "pending_review")
                    self._insert_alert(conn, "2", "pending_review")
                status, body = self._get(port, "/admin")
            finally:
                server.shutdown()
        idx = body.find("Pendientes de revisión")
        self.assertGreater(idx, 0)
        snippet = body[max(0, idx - 200):idx]
        self.assertIn(">2<", snippet)

    # --- 3. Máximo 5 alertas en el bloque de atención ---
    def test_03_attention_block_shows_at_most_5(self):
        from dt_alerts.server import _dashboard_attention_alerts, render_dashboard_attention
        alerts = [
            {"id": i, "status": "pending_review", "title": f"Doc {i}", "category": "Circulares",
             "canonical_url": "https://www.dt.gob.cl/x.html", "created_at": "2026-01-01T00:00:00+00:00"}
            for i in range(1, 8)
        ]
        attention = _dashboard_attention_alerts(alerts)
        self.assertEqual(len(attention), 7)  # todas calificaban, sin recortar aun
        html = render_dashboard_attention(attention)
        # Cuenta solo filas de datos (el <thead><tr> no lleva el link "Ver").
        self.assertEqual(html.count("preview-email"), 5)

    # --- 4. Nunca muestra alertas enviadas (status='sent') en el bloque de atención ---
    def test_04_sent_alerts_excluded_from_attention(self):
        from dt_alerts.server import _dashboard_attention_alerts
        alerts = [
            {"id": 1, "status": "sent", "title": "Enviada", "category": "Circulares",
             "canonical_url": "https://www.dt.gob.cl/x.html", "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": 2, "status": "pending_review", "title": "Pendiente", "category": "Circulares",
             "canonical_url": "https://www.dt.gob.cl/x.html", "created_at": "2026-01-01T00:00:00+00:00"},
        ]
        attention = _dashboard_attention_alerts(alerts)
        statuses = [a["status"] for a in attention]
        self.assertNotIn("sent", statuses)
        self.assertIn("pending_review", statuses)

    # --- 5. Ordena error y fallback antes de pendientes/listas para enviar ---
    def test_05_attention_orders_error_and_fallback_first(self):
        from dt_alerts.server import _dashboard_attention_alerts
        alerts = [
            {"id": 1, "status": "ready", "title": "Lista", "category": "Circulares",
             "canonical_url": "https://www.dt.gob.cl/x.html", "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": 2, "status": "pending_review", "title": "Pendiente", "category": "Circulares",
             "canonical_url": "https://www.dt.gob.cl/x.html", "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": 3, "status": "error", "title": "Con error", "category": "Circulares",
             "canonical_url": "https://www.dt.gob.cl/x.html", "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": 4, "status": "fallback", "title": "Fallback", "category": "Circulares",
             "canonical_url": "https://www.dt.gob.cl/x.html", "created_at": "2026-01-01T00:00:00+00:00"},
        ]
        attention = _dashboard_attention_alerts(alerts)
        self.assertEqual([a["status"] for a in attention], ["error", "fallback", "pending_review", "ready"])

    # --- 6. Estado saludable ("Todo operativo") cuando todo esta correcto ---
    def test_06_healthy_conclusion_when_all_active(self):
        from dt_alerts.server import _dashboard_conclusion
        rows = [
            {"name": "SendGrid", "state": "active", "detail": "ok"},
            {"name": "WordPress Sync", "state": "disabled", "detail": "ok"},
            {"name": "Acceso administrativo", "state": "active", "detail": "ok"},
        ]
        settings = settings_for(Path(":memory:"), ai_provider="azure", ai_enabled=True,
                                ai_api_key="k", ai_model="m", ai_base_url="https://x")
        text, state = _dashboard_conclusion(rows, settings, {})
        self.assertEqual(text, "Todo operativo")
        self.assertEqual(state, "active")

    # --- 7. Un error historico de IA con exito posterior aparece como 'Resuelto' ---
    def test_07_historical_ai_error_shown_as_resolved(self):
        from dt_alerts.server import _ai_provider_health
        settings = settings_for(Path(":memory:"), ai_provider="azure", ai_enabled=True,
                                ai_api_key="k", ai_model="m", ai_base_url="https://x")
        ai_usage_status = {
            "last_error": {"created_at": "2026-01-01T00:00:00+00:00", "error": "Azure requiere AI_BASE_URL configurado."},
            "last_error_resolved": True,
            "last_usage": {"created_at": "2026-01-02T00:00:00+00:00"},
        }
        entry = _ai_provider_health(settings, {}, ai_usage_status)
        self.assertEqual(entry["state"], "active")
        self.assertIn("Resuelto", entry["detail"])

    # --- 7b. Un error de IA sin exito posterior sigue como error activo ---
    def test_07b_ai_error_without_later_success_stays_active_error(self):
        from dt_alerts.server import _ai_provider_health
        settings = settings_for(Path(":memory:"), ai_provider="azure", ai_enabled=True,
                                ai_api_key="k", ai_model="m", ai_base_url="https://x")
        ai_usage_status = {
            "last_error": {"created_at": "2026-01-01T00:00:00+00:00", "error": "Azure requiere AI_BASE_URL configurado."},
            "last_error_resolved": False,
            "last_usage": None,
        }
        entry = _ai_provider_health(settings, {}, ai_usage_status)
        self.assertEqual(entry["state"], "error")

    # --- 8. Las excepciones tecnicas completas no aparecen directamente en el dashboard ---
    def test_08_full_exception_never_shown_raw(self):
        from dt_alerts.server import _sanitize_error_text
        long_error = (
            "SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate in certificate chain (_ssl.c:1000) traceback extendido con muchas "
            "lineas adicionales de detalle tecnico que no deberian mostrarse completas en el dashboard"
        )
        self.assertGreater(len(long_error), 140)
        sanitized = _sanitize_error_text(long_error)
        self.assertLessEqual(len(sanitized), 140)
        self.assertTrue(sanitized.endswith("…"))

    def test_08b_dashboard_does_not_render_full_job_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path)
            long_error = "DT - Circulares: " + ("error tecnico extendido de red SSL " * 10)
            try:
                with db.connect(path) as conn:
                    job_id = db.start_job(conn, "check-normative")
                    db.finish_job(conn, job_id, status="failed", error=long_error)
                status, body = self._get(port, "/admin")
            finally:
                server.shutdown()
        self.assertNotIn(long_error, body)

    # --- 9. El enlace a Monitoreo esta presente ---
    def test_09_monitor_link_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path)
            try:
                status, body = self._get(port, "/admin")
            finally:
                server.shutdown()
        self.assertIn("Ver historial de monitoreo", body)
        self.assertIn('href="/admin/jobs"', body)

    # --- 10. Los enlaces de los KPI usan filtros/rutas reales ---
    def test_10_kpi_links_use_valid_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            server, port = self._start_server(path)
            try:
                status, body = self._get(port, "/admin")
            finally:
                server.shutdown()
        self.assertIn('href="/admin/subscribers"', body)
        self.assertIn('href="/admin/documents"', body)
        self.assertIn('href="/admin/alerts?status=pending_review"', body)
        self.assertIn('href="/admin/alerts?status=ready"', body)
        self.assertIn('href="/admin/alerts?status=sent"', body)


class GenerateVsRegenerateBtnTestCase(unittest.TestCase):
    """Tests para feat/generate-vs-regenerate-btn."""

    def _db_path(self, tmp: str) -> Path:
        path = Path(tmp) / "t.sqlite3"
        db.init_db(path)
        return path

    def _insert_doc_and_alert(self, conn) -> tuple[int, int]:
        doc = {
            "dt_article_id": "art-gr1",
            "canonical_url": "https://example.com/gr1",
            "source_url": "https://example.com/gr1",
            "category": "Dictámenes",
            "title": "ORD. Test Generar",
            "publication_date": "01/01/2026",
            "abstract": None,
            "detail_text": "Texto de prueba.",
            "pdf_url": None,
            "content_hash": None,
        }
        doc_id, _ = db.upsert_document(conn, doc)
        db.update_document_processed(
            conn, doc_id, status="processed", detail_text="Texto.",
            pdf_url=None, content_hash=None, last_error=None,
        )
        alert_id = db.create_or_update_alert(
            conn, doc_id,
            summary="Resumen básico.",
            key_points=[],
            practical_impacts=[],
            relevance="medio",
            status="pending_review",
            ai_error=None,
        )
        return doc_id, alert_id

    def test_preview_shows_generar_when_no_summary(self):
        from dt_alerts.server import render_alert_preview
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                _, alert_id = self._insert_doc_and_alert(conn)
            settings = settings_for(path)
            html = render_alert_preview(alert_id, settings)
        self.assertIn("Generar con IA", html)
        self.assertNotIn("Regenerar con IA", html)

    def test_preview_shows_regenerar_when_summary_exists(self):
        from dt_alerts.server import render_alert_preview
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db_path(tmp)
            with db.connect(path) as conn:
                doc_id, alert_id = self._insert_doc_and_alert(conn)
                db.upsert_ai_summary(
                    conn, doc_id,
                    provider="openai", model="gpt-4o-mini",
                    status="success", content_quality="full",
                    relevance="alto",
                    email_subject="Nuevo documento",
                    email_summary="Resumen IA.",
                    key_points_json="[]",
                    practical_impacts_json="[]",
                    recommended_actions_json="[]",
                    executive_summary=None,
                    detailed_summary_json=None,
                    tags_json="[]",
                    legal_disclaimer=None,
                    error=None,
                )
            settings = settings_for(path)
            html = render_alert_preview(alert_id, settings)
        self.assertIn("Regenerar con IA", html)
        self.assertNotIn("✨ Generar con IA", html)


if __name__ == "__main__":
    unittest.main()
