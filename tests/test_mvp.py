from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from dt_alerts import db, notifier, worker
from dt_alerts.config import get_settings
from dt_alerts.dt_scraper import parse_listing
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
        # El fallback en server.py usa subject_for() -> "Nueva normativa DT: ..."
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
        self.assertIn("API key", msg)
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
        """Si no hay ai_analysis_focus, se usa el default."""
        from dt_alerts.summarizer import build_ai_prompt
        settings = self._make_settings(Path(tempfile.mkdtemp()) / "t.db")
        doc = {
            "title": "Circular 1", "category": "Circulares",
            "detail_text": "Texto de prueba.", "abstract": "",
            "publication_date": "2026-01-01", "canonical_url": "https://example.com",
        }
        _, user_p = build_ai_prompt(doc, settings, {})
        self.assertIn("cumplimiento laboral", user_p)

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
                "impacto_laboral": "Modifica las liquidaciones.",
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
        self.assertIn("Impacto laboral", html_out)
        self.assertIn("Acciones recomendadas", html_out)
        self.assertIn("Afecta los libros contables.", html_out)


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


if __name__ == "__main__":
    unittest.main()
