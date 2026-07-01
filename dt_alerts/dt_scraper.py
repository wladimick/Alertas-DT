from __future__ import annotations

import hashlib
import io
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Iterable


ARTICLE_RE = re.compile(r"w3-article-(\d+)\.html", re.IGNORECASE)
PDF_RE = re.compile(r"\.pdf($|[?#])", re.IGNORECASE)
USER_AGENT = "DT-Alertas-Contadores-MVP/0.1 (+https://example.com)"

# IDs de los contenedores que contienen el cuerpo real del documento DT.
# Excluyen menús, nav, breadcrumb y footer que el sitio inyecta en el <body>.
_ARTICLE_CONTAINER_RE = re.compile(
    r"article_i__w3_ar_ArticuloCompleto_(?:presentacion|cuerpo|Catalogacion)_\d+",
    re.IGNORECASE,
)

# Líneas de navegación y ruido conocido que pueden colarse vía PDF o páginas sin
# los contenedores estándar (segundo filtro de seguridad).
_NOISE_LINES: frozenset[str] = frozenset({
    "Contenido principal", "Toggle navigation", "Compartir", "Imprimir",
    "Trámites y servicios", "Trabajadores", "Empleadores", "Sindicatos",
    "Centro de consultas", "Dictámenes y normativa", "Inspecciones y oficinas",
    "Estudios y estadísticas", "Mi DT", "Noticias", "Quiénes Somos", "Contáctenos",
    "Buscar dictámenes", "Recorrer por", "Legislación", "Periodo",
    "Referencias Legales", "Inicio / Dictámenes y normativa / Dictámenes",
    "Inicio / Dictámenes y normativa / Normativa",
    "Inicio / Dictámenes y normativa / Órdenes de Servicio",
})


@dataclass
class ScrapedDocument:
    dt_article_id: str
    canonical_url: str
    source_url: str
    category: str
    title: str
    publication_date: str | None = None
    abstract: str | None = None
    detail_text: str | None = None
    pdf_url: str | None = None
    content_hash: str | None = None

    def to_db_dict(self) -> dict[str, str | None]:
        return asdict(self)


def fetch_text(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def fetch_bytes(url: str, timeout: int = 30, max_bytes: int = 8_000_000) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("El PDF supera el tamaño máximo permitido para el MVP.")
    return raw


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def article_id_from_url(url: str) -> str | None:
    match = ARTICLE_RE.search(url or "")
    return match.group(1) if match else None


def canonical_article_url(base_url: str, href: str) -> str:
    absolute = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(absolute)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


class ListingParser(HTMLParser):
    def __init__(self, source_url: str, category: str):
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.category = category
        self.cards: list[dict[str, str | None]] = []
        self._card: dict[str, str | None] | None = None
        self._div_depth = 0
        self._field: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        classes = attrs_dict.get("class", "")
        if tag == "div" and "recuadro" in classes.split():
            self._card = {
                "href": None,
                "title": None,
                "abstract": None,
                "date": None,
            }
            self._div_depth = 1
            return

        if self._card is None:
            return

        if tag == "div":
            self._div_depth += 1
        elif tag == "a" and ARTICLE_RE.search(attrs_dict.get("href", "")):
            self._card["href"] = attrs_dict.get("href")
            self._card["title_attr"] = attrs_dict.get("title")
            self._start_field("title")
        elif tag == "h6" and "fecha" in classes.split():
            self._start_field("date")
        elif tag == "p" and "abstract" in classes.split():
            self._start_field("abstract")

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._field and tag in {"a", "h6", "p"}:
            self._finish_field()

        if self._card is not None and tag == "div":
            self._div_depth -= 1
            if self._div_depth <= 0:
                self._flush_card()

    def _start_field(self, field: str) -> None:
        self._field = field
        self._buffer = []

    def _finish_field(self) -> None:
        if self._card is not None and self._field:
            self._card[self._field] = normalize_text(" ".join(self._buffer))
        self._field = None
        self._buffer = []

    def _flush_card(self) -> None:
        if self._field:
            self._finish_field()
        if self._card and self._card.get("href"):
            self.cards.append(self._card)
        self._card = None
        self._div_depth = 0


class DetailParser(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.title = ""
        self.text_parts: list[str] = []
        self.pdf_links: list[str] = []
        self._in_body = False
        self._in_title = False
        self._skip_depth = 0
        # Rastreo de contenedores de artículo DT (presentacion / cuerpo / Catalogacion).
        # Solo acumulamos texto cuando estamos dentro de alguno de ellos.
        self._article_depth = 0   # profundidad de divs anidados dentro del contenedor
        self._found_containers = False  # True si la página tiene los contenedores esperados

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag == "body":
            self._in_body = True
        elif tag == "title":
            self._in_title = True
        elif tag in {"script", "style", "noscript", "svg", "form"}:
            self._skip_depth += 1
        elif tag == "a":
            href = attrs_dict.get("href", "")
            if PDF_RE.search(href):
                self.pdf_links.append(canonical_article_url(self.page_url, href))

        # Detectar entrada a un contenedor de artículo DT.
        # Al encontrar el primero, descartamos cualquier texto de nav acumulado antes.
        if tag == "div" and _ARTICLE_CONTAINER_RE.search(attrs_dict.get("id", "")):
            if not self._found_containers:
                self.text_parts = []  # descarta nav/menú previo al primer contenedor
            self._article_depth += 1
            self._found_containers = True
        elif self._article_depth > 0 and tag == "div":
            self._article_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "body":
            self._in_body = False
        elif tag == "title":
            self._in_title = False
        elif tag in {"script", "style", "noscript", "svg", "form"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "div" and self._article_depth > 0:
            self._article_depth -= 1

    def handle_data(self, data: str) -> None:
        text = normalize_text(data)
        if not text:
            return
        if self._in_title:
            self.title += (" " + text) if self.title else text
            return
        if not self._in_body or self._skip_depth:
            return
        # Si encontramos contenedores DT, solo acumular dentro de ellos.
        # Si la página no tiene los contenedores esperados (layout atípico),
        # caemos al comportamiento anterior (todo el body) como fallback.
        if self._found_containers and self._article_depth == 0:
            return
        if text not in _NOISE_LINES:
            self.text_parts.append(text)

    @property
    def text(self) -> str:
        return normalize_text(" ".join(self.text_parts))


def parse_listing(html: str, source_url: str, category: str) -> list[ScrapedDocument]:
    parser = ListingParser(source_url=source_url, category=category)
    parser.feed(html)
    docs: list[ScrapedDocument] = []
    seen: set[str] = set()

    for card in parser.cards:
        href = card.get("href") or ""
        article_id = article_id_from_url(href)
        if not article_id or article_id in seen:
            continue
        seen.add(article_id)
        canonical_url = canonical_article_url(source_url, href)
        title = normalize_text(card.get("title")) or f"Documento DT {article_id}"
        abstract = normalize_text(card.get("abstract")) or normalize_text(
            card.get("title_attr")
        )
        docs.append(
            ScrapedDocument(
                dt_article_id=article_id,
                canonical_url=canonical_url,
                source_url=source_url,
                category=category,
                title=title,
                publication_date=normalize_text(card.get("date")) or None,
                abstract=abstract or None,
            )
        )

    return docs


def fetch_listing(source: dict[str, str], limit: int = 25) -> list[ScrapedDocument]:
    html = fetch_text(source["url"])
    docs = parse_listing(html, source["url"], source["category"])
    return docs[:limit]


# Mínimo de caracteres que debe tener el texto PDF para preferirlo sobre el HTML.
# Descarta PDFs escaneados (retornan "") y PDFs de error (<100 chars).
_PDF_MIN_CHARS = 500


def _extract_pdf_text(pdf_url: str, timeout: int = 30) -> str:
    """Descarga y extrae texto del PDF en memoria. Retorna "" ante cualquier error."""
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError:
        import logging
        logging.getLogger(__name__).warning("pdfplumber no instalado; omitiendo PDF %s", pdf_url)
        return ""
    try:
        raw = fetch_bytes(pdf_url, timeout=timeout)
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            parts: list[str] = []
            for page in pdf.pages[:20]:
                text = page.extract_text() or ""
                if text:
                    parts.append(text)
        return normalize_text(" ".join(parts))
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Error extrayendo texto PDF: %s", pdf_url, exc_info=True)
        return ""


def enrich_document_detail(doc: ScrapedDocument, include_pdf_text: bool = True) -> ScrapedDocument:
    html = fetch_text(doc.canonical_url)
    parser = DetailParser(doc.canonical_url)
    parser.feed(html)

    body_text = parser.text
    pdf_url = parser.pdf_links[0] if parser.pdf_links else None
    if include_pdf_text and pdf_url:
        pdf_text = _extract_pdf_text(pdf_url)
        if len(pdf_text) >= _PDF_MIN_CHARS:
            body_text = pdf_text

    doc.detail_text = truncate_text(body_text, 24_000) or doc.abstract
    doc.pdf_url = pdf_url
    doc.content_hash = content_hash(
        [
            doc.dt_article_id,
            doc.title,
            doc.publication_date or "",
            doc.abstract or "",
            doc.detail_text or "",
            doc.pdf_url or "",
        ]
    )
    return doc


def content_hash(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def truncate_text(value: str, max_chars: int) -> str:
    value = normalize_text(value)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def parse_sources(sources: list[dict[str, str]], limit: int = 25) -> list[ScrapedDocument]:
    all_docs: list[ScrapedDocument] = []
    seen: set[str] = set()
    for source in sources:
        try:
            docs = fetch_listing(source, limit=limit)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"No se pudo leer {source['category']}: {exc}") from exc
        for doc in docs:
            if doc.dt_article_id in seen:
                continue
            seen.add(doc.dt_article_id)
            all_docs.append(doc)
    return all_docs
