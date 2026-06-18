from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dt_alerts import db, notifier, worker
from dt_alerts.config import get_settings
from dt_alerts.dt_scraper import parse_listing
from dt_alerts.summarizer import summarize_document


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

    def test_fallback_summary_is_pending_review_without_api_key(self) -> None:
        settings = get_settings()
        settings = settings.__class__(**{**settings.__dict__, "openai_api_key": ""})
        result = summarize_document(
            {
                "title": "Circular sobre remuneraciones",
                "category": "Circulares",
                "publication_date": "01/01/2026",
                "canonical_url": "https://example.com",
                "abstract": "Instruye criterios sobre remuneraciones y registro electrónico.",
                "detail_text": "Instruye criterios sobre remuneraciones y registro electrónico. Las empresas deben revisar controles de cumplimiento.",
            },
            settings,
        )
        self.assertEqual(result.status, "pending_review")
        self.assertEqual(result.relevance, "alto")
        self.assertTrue(result.practical_impacts)


if __name__ == "__main__":
    unittest.main()
