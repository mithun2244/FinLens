"""Document ingestion and layout parsing (phases.md Phase 2).

Turns a PDF into a :class:`~src.schemas.ParsedDocument`: page-level text, tables with
their row structure intact, rendered page images for the previewer, and normalized
bounding boxes for citation highlighting.

Three behaviours are worth knowing before reading the code:

**pdfplumber reads the text layer; there is no OCR** (revises decisions D-1, D-15). This
used to run Docling, whose TableFormer model recovered tables from unruled layouts and
whose RapidOCR fallback read scanned pages. Both are torch or onnxruntime models, and
neither fits in a 512 MB deployment. What replaced them reads ruled and whitespace-aligned
tables from the PDF's own text layer, which is what the fixture invoices actually use.

**A PDF with no text layer is now an error, not a fallback.** Nothing here can read a
picture of a document. Rejecting it is the only honest option: a scanned invoice parsed
without OCR yields no line items and no total, which is indistinguishable from a
successful parse of an empty document.

**Nothing is ever invented.** If a page cannot be read, that fact propagates as a
``ParsingError`` or a zero text yield. The parser never substitutes a plausible value
(rules.md Rule 2.3, decision D-12).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src import ParsingError
from src.config import (
    EXPECTED_CHARS_PER_PAGE,
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER,
    MIN_COLUMN_GUTTER,
    PAGE_RENDER_DPI,
    PARSE_CACHE_SIZE,
    SUPPORTED_EXTENSIONS,
    TABLE_STRATEGIES,
    UPLOAD_DIR,
)
from src.schemas import BoundingBox, ParsedDocument, ParsedPage, TableBlock

logger = logging.getLogger(__name__)

__all__ = [
    "parse_document",
    "page_image_dir",
    "warm_up",
    "has_text_layer",
    "parse_cache_info",
    "clear_parse_cache",
]


# ── Fast-path triage and result cache ────────────────────────────────────────

#: Parsed documents keyed by content hash. Re-uploading a file already parsed in this
#: process returns immediately instead of re-reading it.
_PARSE_CACHE: OrderedDict[str, ParsedDocument] = OrderedDict()


def _content_hash(path: Path) -> str:
    """MD5 of the file's bytes. Identity is the content, not the name or the path."""
    digest = hashlib.md5()  # noqa: S324 - cache key, not a security primitive
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def has_text_layer(path: Path) -> tuple[bool, int, int]:
    """Decide whether a PDF carries extractable text.

    Runs PyMuPDF, which answers in 1-10 ms. This used to choose between the text and OCR
    pipelines; now it decides whether the document is readable at all, so its answer is
    the difference between a parse and a rejection.

    The extracted text is still discarded rather than used: PyMuPDF returns a flat
    character stream with no table structure, and line items come from table rows
    (decision D-7). Substituting raw text here would make every invoice fall back to
    narrative pattern-matching at 0.55 confidence.

    Returns:
        ``(has_text, page_count, total_chars)``. ``has_text`` is ``False`` for images and
        for anything PyMuPDF cannot open.
    """
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return False, 1, 0

    try:
        import fitz
    except ImportError:  # pragma: no cover - PyMuPDF is a declared dependency
        logger.debug("PyMuPDF unavailable; skipping triage")
        return True, 0, 0

    try:
        with fitz.open(path) as document:
            pages = document.page_count or 1
            chars = sum(len(page.get_text("text") or "") for page in document)
    except Exception as exc:  # noqa: BLE001 - triage must never block parsing
        logger.debug("Triage could not read %s (%s); deferring to the parser", path.name, exc)
        return True, 0, 0

    per_page = chars / max(1, pages)
    return per_page >= MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER, pages, chars


def parse_cache_info() -> dict[str, int]:
    """Size and capacity of the in-process parse cache."""
    return {"entries": len(_PARSE_CACHE), "capacity": PARSE_CACHE_SIZE}


def clear_parse_cache() -> None:
    """Drop every cached parse. Used by tests and by an explicit user 'reprocess'."""
    _PARSE_CACHE.clear()


def warm_up(with_ocr: bool = False) -> None:
    """Import the parsing libraries so the first upload does not pay for it.

    Kept, and kept cheap. Under Docling this loaded layout and TableFormer models and cost
    ~9 s; pdfplumber and PyMuPDF have no models, so this is just an import. The signature
    keeps ``with_ocr`` so existing callers do not break — it is ignored, and there is no
    OCR stage left to enable.
    """
    import pdfplumber  # noqa: F401
    import fitz  # noqa: F401


# ── Input validation ─────────────────────────────────────────────────────────


def _validate_source(path: Path) -> None:
    """Reject unsupported input with a specific, actionable message (FR-1.4)."""
    if not path.exists():
        raise ParsingError(f"File not found: {path.name}")
    if not path.is_file():
        raise ParsingError(f"{path.name} is a directory, not a document.")

    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        # Named separately from the generic case: "images are not supported" is actionable
        # in a way that "supported formats: .pdf" is not, when the user just uploaded a
        # photo of a receipt and reasonably expected it to work.
        raise ParsingError(
            f"{path.name} is an image, and image and scanned-document support was removed "
            f"when OCR was dropped to fit the memory budget. Upload a PDF with a text "
            f"layer — a print-to-PDF or the original download rather than a photo."
        )
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ParsingError(
            f"Cannot read '{suffix or path.name}'. Supported formats: {supported}."
        )

    size = path.stat().st_size
    if size == 0:
        raise ParsingError(f"{path.name} is empty (0 bytes).")
    if size > MAX_UPLOAD_BYTES:
        raise ParsingError(
            f"{path.name} is {size / 1_048_576:.1f} MB, above the "
            f"{MAX_UPLOAD_BYTES / 1_048_576:.0f} MB limit. Split it into smaller files."
        )


def _explain_open_failure(path: Path, detail: str) -> str:
    """Turn a library error into something a user can act on (rules.md Rule 2.3)."""
    lowered = detail.lower()
    if "password" in lowered or "encrypt" in lowered:
        return f"{path.name} is password-protected. Remove the password and upload it again."
    if "not a pdf" in lowered or "no /root" in lowered or "startxref" in lowered:
        return (
            f"{path.name} appears to be corrupt or is not a valid PDF. "
            f"Try re-exporting or re-downloading it."
        )
    return f"Could not parse {path.name}. The file may be damaged or in an unsupported variant."


# ── Geometry ─────────────────────────────────────────────────────────────────


def _normalize_bbox(
    bbox: tuple[float, float, float, float] | None, width: float, height: float
) -> BoundingBox | None:
    """Convert a pdfplumber bbox to 0-1, top-left origin coordinates.

    pdfplumber reports ``(x0, top, x1, bottom)`` in points, already measured from the top
    of the page — so unlike Docling's bottom-left boxes there is no flip here, only a
    scale. The clamping stays: a table border drawn a hair outside the mediabox would
    otherwise produce a value above 1.0 and fail ``BoundingBox`` validation.
    """
    if bbox is None or width <= 0 or height <= 0:
        return None

    try:
        left, top, right, bottom = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None

    def clamp(value: float, limit: float) -> float:
        return max(0.0, min(1.0, value / limit))

    normalized = BoundingBox(
        left=clamp(left, width),
        top=clamp(top, height),
        right=clamp(right, width),
        bottom=clamp(bottom, height),
    )
    # A zero-area box highlights nothing and is worse than no box at all, because the UI
    # renders it as an invisible overlay rather than falling back to page-level highlight.
    if normalized.width <= 0 or normalized.height <= 0:
        return None
    return normalized


# ── Extraction ───────────────────────────────────────────────────────────────


def _clean_cell(value: Any) -> str:
    """Table cells come back as ``str | None``, often with wrapped-line newlines."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _looks_numeric(cell: str) -> bool:
    """True when a cell is a bare number or amount, ignoring currency noise."""
    stripped = cell.replace(",", "").replace("$", "").replace("£", "").replace("€", "").strip()
    if not stripped:
        return False
    try:
        Decimal(stripped)
    except InvalidOperation:
        return False
    return True


def _is_probable_header(row: list[str]) -> bool:
    """Whether a row reads as column headers rather than data.

    A table continued onto a second page repeats no header — the statement fixture's page
    2 starts straight at "GITHUB INC | 1 | 21.00 | 21.00". Taking the first row as headers
    regardless silently ate that line item and named the columns after it, so the page
    contributed one row instead of two and the document totalled 3 items instead of 5.

    Amounts are the discriminator: column headers do not contain bare numbers, and every
    continuation row does.
    """
    cells = [cell for cell in row if cell]
    return bool(cells) and not any(_looks_numeric(cell) for cell in cells)


def _table_from_rows(
    raw_rows: list[list[Any]],
    page_number: int,
    bbox: BoundingBox | None,
    inherited_headers: list[str] | None = None,
) -> TableBlock | None:
    """Turn pdfplumber's list-of-lists into a :class:`TableBlock`.

    The first non-empty row is taken as the header. That is a heuristic, and it is the
    same one Docling's output effectively encoded — ``_map_columns`` in the extractor is
    what actually decides whether the headers mean anything, and a table whose headers do
    not map to description/amount is skipped there rather than here.
    """
    rows = [[_clean_cell(cell) for cell in row] for row in raw_rows if row]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return None

    if _is_probable_header(rows[0]):
        header_row, data_rows = rows[0], rows[1:]
    elif inherited_headers:
        # A continuation table: every row is data, and the columns are the ones the table
        # was introduced with on an earlier page.
        header_row, data_rows = list(inherited_headers), rows
    else:
        return None

    if not data_rows:
        return None

    groups = _column_groups(header_row)
    if not groups:
        return None

    headers = [header_row[group[0]] or f"column_{group[0] + 1}" for group in groups]
    body: list[dict[str, str]] = []
    for row in data_rows:
        merged = [
            " ".join(part for part in (row[i] if i < len(row) else "" for i in group) if part)
            for group in groups
        ]
        # A row with nothing in its first column is a totals block, not a line item: the
        # horizontal rule under the last item opens one more band, and the Subtotal/Tax
        # figures fall into it as ``['', '', '', '462.00 39.27']``. Kept out of the table
        # because it would otherwise become a table_row chunk of bare numbers with no
        # description — retrieval noise, and a row that D-7's "one chunk per row" promise
        # was never about. The figures themselves are not lost; they are read from the
        # narrative text by the extractor and carried in the record summary chunk.
        if not merged or not merged[0]:
            continue
        body.append(dict(zip(headers, merged)))

    if not body:
        return None
    return TableBlock(page_number=page_number, headers=headers, rows=body, bbox=bbox)


def _column_groups(header_row: list[str]) -> list[list[int]]:
    """Group column indices so each group is one logical column.

    ``vertical_strategy="text"`` splits on whitespace gaps, and a wide free-text column
    splits with it: the description "EC2 t3.medium instance-hours" comes back as three
    columns, ``['EC2 t3.medium', 'instance-hou', 'rs']``, under the single header
    ``['Description', '', '']``.

    An empty header is the signal — it means that column is a continuation of the last
    named one, so it is folded back in and the cells are rejoined with a space. Without
    this the extractor sees a description of "EC2 t3.medium" and two unnamed columns, and
    ``_map_columns`` cannot recognise the table as line items at all.
    """
    groups: list[list[int]] = []
    for index, header in enumerate(header_row):
        if header or not groups:
            groups.append([index])
        else:
            groups[-1].append(index)
    return groups


def _gutter_boundaries(page: Any, table: Any) -> list[float] | None:
    """Column boundaries taken from the vertical gutters between words.

    ``vertical_strategy="text"`` derives columns from *character* alignment, which lands
    boundaries inside words: "EC2 t3.medium instance-hours" came back as three columns,
    ``['EC2 t3.medium', 'instance-hou', 'rs']``, and the description that reached the
    extractor was a fragment. Raising ``text_x_tolerance`` does not fix it — it merges
    Unit Price into Amount before it merges the description back together.

    This works one level up, on whole words. Every word's horizontal span inside the table
    is merged into a set of occupied intervals; whatever is left is genuine whitespace
    running the full height of the table, which is what a column gutter is. Boundaries go
    down the middle of each gap wide enough to be a gutter rather than a word space.

    Returns ``None`` when no interior gutter is found, so the caller can fall back rather
    than force a single-column table.
    """
    x0, top, x1, bottom = table.bbox
    try:
        words = [
            word
            for word in page.extract_words()
            if word["top"] >= top - 1
            and word["bottom"] <= bottom + 1
            and word["x0"] >= x0 - 1
            and word["x1"] <= x1 + 1
        ]
    except Exception:  # noqa: BLE001 - fall back to the strategy's own columns
        return None
    if not words:
        return None

    spans = sorted((float(word["x0"]), float(word["x1"])) for word in words)
    merged: list[list[float]] = [list(spans[0])]
    for left, right in spans[1:]:
        if left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])

    boundaries = [float(x0)]
    for left_span, right_span in zip(merged, merged[1:]):
        if right_span[0] - left_span[1] >= MIN_COLUMN_GUTTER:
            boundaries.append((left_span[1] + right_span[0]) / 2)
    boundaries.append(float(x1))
    # Two entries means the page edges only — no interior column was found.
    return boundaries if len(boundaries) > 2 else None


def _extract_with_gutters(page: Any, table: Any, settings: dict[str, str]) -> list[list[Any]]:
    """Re-extract a located table using word-gutter column boundaries.

    The strategy pair is what *finds* the table and fixes its row bands; the columns it
    infers along the way are the unreliable part. So the located region is re-read with
    explicit vertical lines, and the original extraction is used only when no gutter can
    be found.
    """
    boundaries = _gutter_boundaries(page, table)
    if boundaries is None:
        return list(table.extract())

    explicit = dict(settings)
    explicit["vertical_strategy"] = "explicit"
    try:
        rebuilt = page.find_tables(
            table_settings={**explicit, "explicit_vertical_lines": boundaries}
        )
    except Exception:  # noqa: BLE001
        return list(table.extract())

    # find_tables can return several regions; keep the one covering this table.
    for candidate in rebuilt:
        if abs(candidate.bbox[1] - table.bbox[1]) < 2 and abs(candidate.bbox[3] - table.bbox[3]) < 2:
            return list(candidate.extract())
    return list(table.extract())


def _extract_page_tables(
    page: Any, page_number: int, inherited_headers: list[str] | None = None
) -> list[TableBlock]:
    """Find tables on one page, trying each strategy until one produces rows.

    Strategies are tried in order rather than merged: running both and concatenating
    yields the same table twice on a ruled invoice, and duplicate line items are worse
    than a missed one — they would inflate the computed subtotal and trip the arithmetic
    validator against a total that was correct all along.
    """
    for vertical, horizontal in TABLE_STRATEGIES:
        settings = {"vertical_strategy": vertical, "horizontal_strategy": horizontal}
        try:
            found = page.find_tables(table_settings=settings)
        except Exception as exc:  # noqa: BLE001 - a bad strategy must not kill the page
            logger.debug(
                "Table strategy %s/%s failed on page %d: %s",
                vertical, horizontal, page_number, exc,
            )
            continue

        blocks: list[TableBlock] = []
        for table in found:
            try:
                raw_rows = _extract_with_gutters(page, table, settings)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not extract a table on page %d: %s", page_number, exc)
                continue
            block = _table_from_rows(
                raw_rows,
                page_number,
                _normalize_bbox(getattr(table, "bbox", None), page.width, page.height),
                inherited_headers=inherited_headers,
            )
            if block is not None:
                blocks.append(block)

        if blocks:
            logger.debug(
                "page %d: %d table(s) via %s/%s strategy",
                page_number, len(blocks), vertical, horizontal,
            )
            return blocks
    return []


def page_image_dir(document_id: str) -> Path:
    """Where rendered page images for a document live."""
    return UPLOAD_DIR / document_id / "pages"


def _save_page_images(path: Path, document_id: str) -> dict[int, str]:
    """Render each page to disk for the previewer (design.md §5.1).

    PyMuPDF rather than pdfplumber: it was already a dependency for triage, and
    ``page.to_image()`` would pull in a separate raster stack. Pages are rendered and
    written one at a time so peak memory is one page's pixmap, not the whole document —
    which matters at 512 MB, where a 20-page render held in memory is not affordable.
    """
    try:
        import fitz
    except ImportError:  # pragma: no cover - declared dependency
        return {}

    target_dir = page_image_dir(document_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    saved: dict[int, str] = {}
    try:
        with fitz.open(path) as document:
            for index, page in enumerate(document, start=1):
                destination = target_dir / f"page_{index:03d}.png"
                try:
                    pixmap = page.get_pixmap(dpi=PAGE_RENDER_DPI)
                    pixmap.save(destination)
                    del pixmap
                except Exception as exc:  # noqa: BLE001 - previewer is not load-bearing
                    logger.warning("Could not render page %d: %s", index, exc)
                    continue
                saved[index] = str(destination)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not render pages for document_id=%s: %s", document_id, exc)

    return saved


def _text_yield_ratio(char_count: int) -> float:
    """How much of an expected text layer this page actually produced.

    Deliberately a blunt instrument: it separates "has a text layer" from "is a picture
    of a document". See ``EXPECTED_CHARS_PER_PAGE`` for why the denominator is what it is.
    """
    return min(1.0, char_count / EXPECTED_CHARS_PER_PAGE)


def _build_parsed_document(
    path: Path,
    *,
    document_id: str,
    parse_seconds: float,
) -> ParsedDocument:
    """Read the PDF once, building pages and tables together.

    One pass, page by page, with each page released before the next is opened. The
    alternative — collecting every page's objects and post-processing — is how a parser
    quietly becomes the largest allocation in a 512 MB process.
    """
    import pdfplumber

    pages: list[ParsedPage] = []
    tables: list[TableBlock] = []
    last_headers: list[str] | None = None
    markdown_parts: list[str] = []

    images = _save_page_images(path, document_id)

    try:
        with pdfplumber.open(path) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                page_tables = _extract_page_tables(page, index, last_headers)
                if page_tables:
                    # Carried to the next page so a table continued across a page break
                    # keeps its columns. See _is_probable_header.
                    last_headers = page_tables[-1].headers
                tables.extend(page_tables)

                narrative = page.extract_text() or ""
                table_rows = [row for table in page_tables for row in table.to_serialized_rows()]
                markdown = "\n".join(filter(None, [narrative, *table_rows]))
                markdown_parts.append(markdown)

                pages.append(
                    ParsedPage(
                        page_number=index,
                        markdown=markdown,
                        narrative_markdown=narrative,
                        image_path=images.get(index),
                        width_points=float(page.width or 0.0),
                        height_points=float(page.height or 0.0),
                        char_count=len(markdown),
                        text_yield_ratio=_text_yield_ratio(len(markdown)),
                        table_count=len(page_tables),
                        used_ocr=False,
                    )
                )
                # pdfplumber caches every char, line and rect it has seen on the page.
                # Without this the cache grows for the life of the document and a
                # 20-page statement holds all of it at once.
                page.flush_cache()
    except ParsingError:
        raise
    except Exception as exc:  # noqa: BLE001 - translated into an actionable message
        raise ParsingError(_explain_open_failure(path, str(exc))) from exc

    return ParsedDocument(
        document_id=document_id,
        filename=path.name,
        source_path=str(path),
        page_count=max(1, len(pages)),
        pages=pages,
        markdown="\n\n".join(markdown_parts),
        tables=tables,
        used_ocr=False,
        parse_seconds=parse_seconds,
    )


# ── Public API ───────────────────────────────────────────────────────────────


def parse_document(
    source: str | Path,
    *,
    document_id: str | None = None,
    force_ocr: bool | None = None,
    persist_source: bool = True,
) -> ParsedDocument:
    """Parse a financial document into a :class:`ParsedDocument`.

    Args:
        source: Path to a PDF.
        document_id: Stable identifier. Generated if omitted.
        force_ocr: Accepted and ignored — there is no OCR stage left. Kept so existing
            callers and stored requests do not break on an unexpected keyword.
        persist_source: Copy the original into ``data/uploads/<document_id>/`` (FR-1.5).

    Returns:
        A fully populated ``ParsedDocument``.

    Raises:
        ParsingError: The file is missing, unsupported, an image, oversized, corrupt,
            password-protected, or has no text layer. The message is safe to show a user
            directly.
    """
    if force_ocr:
        logger.warning("force_ocr=True ignored: OCR was removed with the Docling pipeline")

    path = Path(source).expanduser().resolve()
    _validate_source(path)

    document_id = document_id or str(uuid.uuid4())
    started = time.perf_counter()

    text_layer, page_count, chars = has_text_layer(path)
    logger.info(
        "Triage %s: %d page(s), %d chars, text_layer=%s", path.name, page_count, chars, text_layer
    )

    cache_key = _content_hash(path)
    cached = _PARSE_CACHE.get(cache_key)
    if cached is not None:
        _PARSE_CACHE.move_to_end(cache_key)
        logger.info("Parse cache hit for %s (document_id=%s)", path.name, document_id)
        if persist_source:
            _persist_source(path, document_id)
        # Same content, possibly a new identifier. Page images on disk stay valid because
        # their paths are absolute and the rendered pages are identical.
        return cached.model_copy(update={"document_id": document_id, "parse_seconds": 0.0})

    logger.info("Parsing document_id=%s", document_id)
    parsed = _build_parsed_document(
        path,
        document_id=document_id,
        parse_seconds=time.perf_counter() - started,
    )

    # Checked here rather than from the triage pass, because the two failures need
    # different messages and triage cannot tell them apart: PyMuPDF reports a corrupt
    # zero-page file as "1 page, 0 characters", which is indistinguishable from a scan.
    # Letting pdfplumber attempt the read first means a damaged file raises its own
    # "corrupt or not a valid PDF" error on the way through, and only a document that
    # genuinely parsed but carries no text reaches this.
    if all(page.char_count < MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER for page in parsed.pages):
        raise ParsingError(
            f"{path.name} has no extractable text layer — it looks like a scan or a photo. "
            f"OCR was removed to fit the memory budget, so this cannot be read. Upload a "
            f"PDF exported from the original document rather than a scan of it."
        )

    if persist_source:
        _persist_source(path, document_id)

    _PARSE_CACHE[cache_key] = parsed
    _PARSE_CACHE.move_to_end(cache_key)
    while len(_PARSE_CACHE) > PARSE_CACHE_SIZE:
        _PARSE_CACHE.popitem(last=False)

    logger.info(
        "document_id=%s parsed: %d pages, %d tables, %d rows, %.1fs",
        document_id,
        parsed.page_count,
        len(parsed.tables),
        parsed.total_rows,
        parsed.parse_seconds,
    )
    return parsed


def _persist_source(path: Path, document_id: str) -> None:
    """Keep the original alongside its rendered pages (FR-1.5). Local only."""
    destination_dir = UPLOAD_DIR / document_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / path.name
    if destination.exists() and destination.samefile(path):
        return
    try:
        shutil.copy2(path, destination)
    except OSError as exc:
        # Not fatal: parsing succeeded, we simply could not archive the original.
        logger.warning("Could not persist source for document_id=%s: %s", document_id, exc)
