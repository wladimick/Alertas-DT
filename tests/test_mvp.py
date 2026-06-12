from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dt_alerts import db
from dt_alerts.config import get_settings
from dt_alerts.dt_scraper import parse_listing
from dt_alerts.summarizer import summarize_document


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
