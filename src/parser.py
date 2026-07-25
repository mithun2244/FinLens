"""Document ingestion and layout parsing (phases.md Phase 2).

Turns a PDF or image into a :class:`~src.schemas.ParsedDocument`: page-level text, tables
with their row structure intact, rendered page images for the previewer, and normalized
bounding boxes for citation highlighting.

Two behaviours are worth knowing before reading the code:

**Docling is the primary path; OCR is a fallback** (decisions D-1, D-15). A digital PDF is
parsed from its text layer, which is deterministic and fast. Only pages whose
``text_yield_ratio`` says they have no usable text layer are re-parsed through Docling's
**local** RapidOCR engine. Nothing here calls a cloud model — Groq serves no image-input
model, and page images never leave the machine.

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
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import ParsingError
from src.config import (
    DOCLING_IMAGE_SCALE,
    DOCLING_NUM_THREADS,
    EXPECTED_CHARS_PER_PAGE,
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER,
    OCR_LANGUAGES,
    OCR_TEXT_SCORE,
    PARSE_CACHE_SIZE,
    SUPPORTED_EXTENSIONS,
    TABLE_MODE_ACCURATE,
    UPLOAD_DIR,
    get_settings,
)
from src.schemas import BoundingBox, ParsedDocument, ParsedPage, TableBlock

if TYPE_CHECKING:
    from docling.datamodel.document import ConversionResult

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

#: Parsed documents keyed by (content hash, ocr flag). Re-uploading a file already parsed
#: in this process returns immediately instead of re-running the layout models.
_PARSE_CACHE: OrderedDict[tuple[str, bool], ParsedDocument] = OrderedDict()


def _content_hash(path: Path) -> str:
    """MD5 of the file's bytes. Identity is the content, not the name or the path."""
    digest = hashlib.md5()  # noqa: S324 - cache key, not a security primitive
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def has_text_layer(path: Path) -> tuple[bool, int, int]:
    """Cheaply decide whether a PDF already carries extractable text.

    Runs PyMuPDF over the file — 1-10 ms against Docling's seconds — purely to choose the
    right pipeline up front. Without it, a scanned PDF is parsed **twice**: once to
    discover the text yield is too low, then again with OCR enabled.

    This is triage only. The extracted text is deliberately discarded: PyMuPDF returns a
    flat character stream with no table structure, and line items come from Docling's
    TableFormer rows (decision D-7). Substituting raw text here would make every invoice
    fall back to narrative pattern-matching at 0.55 confidence and cost the line-item
    recall the product is built on.

    Returns:
        ``(has_text, page_count, total_chars)``. ``has_text`` is ``False`` for images and
        for anything PyMuPDF cannot open, so the caller falls back to OCR.
    """
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return False, 1, 0

    try:
        import fitz
    except ImportError:  # pragma: no cover - PyMuPDF is a declared dependency
        logger.debug("PyMuPDF unavailable; skipping fast-path triage")
        return True, 0, 0

    try:
        with fitz.open(path) as document:
            pages = document.page_count or 1
            chars = sum(len(page.get_text("text") or "") for page in document)
    except Exception as exc:  # noqa: BLE001 - triage must never block parsing
        logger.debug("Triage could not read %s (%s); deferring to Docling", path.name, exc)
        return True, 0, 0

    per_page = chars / max(1, pages)
    return per_page >= MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER, pages, chars


def parse_cache_info() -> dict[str, int]:
    """Size and capacity of the in-process parse cache."""
    return {"entries": len(_PARSE_CACHE), "capacity": PARSE_CACHE_SIZE}


def clear_parse_cache() -> None:
    """Drop every cached parse. Used by tests and by an explicit user 'reprocess'."""
    _PARSE_CACHE.clear()


# ── Converter construction (expensive — built once, cached) ───────────────────


@lru_cache(maxsize=2)
def _get_converter(with_ocr: bool) -> Any:
    """Return a cached :class:`DocumentConverter`, with or without the OCR stage.

    Building a converter loads Docling's layout and TableFormer models, which takes
    ~15-20 s. Caching is not an optimization here — without it, every upload would pay
    that cost and blow NFR-2 (< 8 s per text page) on its own.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import (
        DocumentConverter,
        ImageFormatOption,
        PdfFormatOption,
    )

    options = PdfPipelineOptions()
    # Docling defaults to 4 threads and sets torch's thread count itself, so this must be
    # configured here — torch.set_num_threads() from application code is overridden and
    # has no effect. Worth 42% of parse time on a 12-core machine (config.py).
    options.accelerator_options = AcceleratorOptions(
        num_threads=DOCLING_NUM_THREADS, device=AcceleratorDevice.CPU
    )
    options.do_ocr = with_ocr
    options.do_table_structure = True
    # Assigned as a whole rather than mutated field-by-field: the attribute is declared as
    # BaseTableStructureOptions, which has neither `mode` nor `do_cell_matching`.
    options.table_structure_options = TableStructureOptions(
        mode=TableFormerMode.ACCURATE if TABLE_MODE_ACCURATE else TableFormerMode.FAST,
        do_cell_matching=True,
    )
    options.generate_page_images = True
    options.images_scale = DOCLING_IMAGE_SCALE

    if with_ocr:
        options.ocr_options = RapidOcrOptions(
            lang=list(OCR_LANGUAGES),
            text_score=OCR_TEXT_SCORE,
            force_full_page_ocr=True,
        )

    logger.info("Building Docling converter (ocr=%s)", with_ocr)
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.IMAGE],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
        },
    )


def warm_up(with_ocr: bool = False) -> None:
    """Pre-load Docling's models so the first real upload is not the slow one.

    Call this at application start (Phase 5) rather than making the user pay ~20 s on
    their first document.
    """
    _get_converter(with_ocr)


# ── Input validation ─────────────────────────────────────────────────────────


def _validate_source(path: Path) -> None:
    """Reject unsupported input with a specific, actionable message (FR-1.4)."""
    if not path.exists():
        raise ParsingError(f"File not found: {path.name}")
    if not path.is_file():
        raise ParsingError(f"{path.name} is a directory, not a document.")

    suffix = path.suffix.lower()
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


def _convert(path: Path, with_ocr: bool) -> ConversionResult:
    """Run Docling, translating its failures into actionable ``ParsingError``s."""
    from docling.datamodel.base_models import ConversionStatus
    from docling.exceptions import ConversionError

    try:
        result = _get_converter(with_ocr).convert(path)
    except ConversionError as exc:
        raise ParsingError(_explain_conversion_failure(path, str(exc))) from exc
    except (OSError, ValueError) as exc:
        raise ParsingError(f"Could not read {path.name}: {exc}") from exc

    if result.status == ConversionStatus.FAILURE:
        raise ParsingError(_explain_conversion_failure(path, str(result.errors)))
    if result.status == ConversionStatus.PARTIAL_SUCCESS:
        logger.warning("Partial conversion for %s — some pages may be incomplete", path.name)
    return result


def _explain_conversion_failure(path: Path, detail: str) -> str:
    """Turn a library error into something a user can act on (rules.md Rule 2.3)."""
    lowered = detail.lower()
    if "password" in lowered or "encrypt" in lowered:
        return (
            f"{path.name} is password-protected. Remove the password and upload it again."
        )
    if "data format error" in lowered or "could not load" in lowered:
        return (
            f"{path.name} appears to be corrupt or is not a valid "
            f"{path.suffix.lstrip('.').upper()} file. Try re-exporting or re-downloading it."
        )
    return f"Could not parse {path.name}. The file may be damaged or in an unsupported variant."


# ── Geometry ─────────────────────────────────────────────────────────────────


def _normalize_bbox(bbox: Any, width: float, height: float) -> BoundingBox | None:
    """Convert a Docling bbox to 0-1, **top-left origin** coordinates.

    Docling reports absolute points with ``CoordOrigin.BOTTOMLEFT``, where ``t`` is the
    larger y value. CSS wants top-left with y growing downward, so bottom-left boxes are
    flipped here — once, at the boundary — rather than in the UI (decision D-16).
    """
    if bbox is None or width <= 0 or height <= 0:
        return None

    try:
        left, right = float(bbox.l), float(bbox.r)
        top_raw, bottom_raw = float(bbox.t), float(bbox.b)
    except (AttributeError, TypeError, ValueError):
        return None

    origin = getattr(getattr(bbox, "coord_origin", None), "value", "")
    if str(origin).upper() == "BOTTOMLEFT":
        top, bottom = height - top_raw, height - bottom_raw
    else:
        top, bottom = top_raw, bottom_raw

    if top > bottom:
        top, bottom = bottom, top
    if left > right:
        left, right = right, left

    def clamp(value: float, limit: float) -> float:
        return max(0.0, min(1.0, value / limit))

    return BoundingBox(
        left=clamp(left, width),
        top=clamp(top, height),
        right=clamp(right, width),
        bottom=clamp(bottom, height),
    )


def _item_page_and_bbox(item: Any, pages: dict[int, tuple[float, float]]) -> tuple[int | None, BoundingBox | None]:
    """Read ``page_no`` and a normalized bbox off any provenance-carrying Docling item."""
    provenance = getattr(item, "prov", None)
    if not provenance:
        return None, None

    first = provenance[0]
    page_no = getattr(first, "page_no", None)
    if page_no is None:
        return None, None

    width, height = pages.get(int(page_no), (0.0, 0.0))
    return int(page_no), _normalize_bbox(getattr(first, "bbox", None), width, height)


# ── Extraction helpers ───────────────────────────────────────────────────────


def _page_dimensions(document: Any) -> dict[int, tuple[float, float]]:
    dimensions: dict[int, tuple[float, float]] = {}
    for page_no, page in document.pages.items():
        size = getattr(page, "size", None)
        dimensions[int(page_no)] = (
            float(getattr(size, "width", 0.0) or 0.0),
            float(getattr(size, "height", 0.0) or 0.0),
        )
    return dimensions


def _extract_tables(document: Any, pages: dict[int, tuple[float, float]]) -> list[TableBlock]:
    """Pull each detected table out with its rows intact.

    Row structure is the reason Docling was chosen over pdfplumber or PyPDF2
    (architecture.md §3.2). A table that arrives here as flattened text is a parse
    failure even when nothing raised.
    """
    blocks: list[TableBlock] = []

    for index, table in enumerate(getattr(document, "tables", [])):
        page_no, bbox = _item_page_and_bbox(table, pages)
        try:
            frame = table.export_to_dataframe(document)
        except (TypeError, AttributeError):
            frame = table.export_to_dataframe()
        except Exception as exc:  # noqa: BLE001 - a bad table must not sink the document
            logger.warning("Table %d could not be exported: %s", index, type(exc).__name__)
            continue

        if frame is None or frame.empty:
            continue

        headers = [str(column).strip() for column in frame.columns]
        rows: list[dict[str, str]] = []
        for record in frame.to_dict(orient="records"):
            row = {
                str(key).strip(): ("" if value is None else str(value).strip())
                for key, value in record.items()
            }
            if any(row.values()):
                rows.append(row)

        if not rows:
            continue

        blocks.append(
            TableBlock(
                page_number=page_no or 1,
                headers=headers,
                rows=rows,
                bbox=bbox,
                caption=(table.caption_text(document) or None)
                if hasattr(table, "caption_text")
                else None,
            )
        )

    return blocks


def _page_text(document: Any, pages: dict[int, tuple[float, float]]) -> dict[int, list[str]]:
    """Group extracted text items by the page they came from."""
    by_page: dict[int, list[str]] = {page_no: [] for page_no in pages}

    for item in getattr(document, "texts", []):
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue
        page_no, _ = _item_page_and_bbox(item, pages)
        by_page.setdefault(page_no or 1, []).append(text)

    return by_page


def page_image_dir(document_id: str) -> Path:
    """Where rendered page images for a document live."""
    return UPLOAD_DIR / document_id / "pages"


def _save_page_images(document: Any, document_id: str) -> dict[int, str]:
    """Write each rendered page to disk for the previewer (design.md §5.1)."""
    target_dir = page_image_dir(document_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    saved: dict[int, str] = {}
    for page_no, page in document.pages.items():
        image_ref = getattr(page, "image", None)
        pil_image = getattr(image_ref, "pil_image", None) if image_ref else None
        if pil_image is None:
            continue

        destination = target_dir / f"page_{int(page_no):03d}.png"
        try:
            pil_image.save(destination, format="PNG")
        except OSError as exc:
            logger.warning("Could not write page image %d: %s", page_no, exc)
            continue
        saved[int(page_no)] = str(destination)

    return saved


def _text_yield_ratio(char_count: int) -> float:
    """How much of an expected text layer this page actually produced.

    Deliberately a blunt instrument: it separates "has a text layer" from "is a picture
    of a document". See ``EXPECTED_CHARS_PER_PAGE`` for why the denominator is what it is.
    """
    return min(1.0, char_count / EXPECTED_CHARS_PER_PAGE)


def _build_parsed_document(
    result: ConversionResult,
    *,
    document_id: str,
    source: Path,
    used_ocr: bool,
    parse_seconds: float,
) -> ParsedDocument:
    document = result.document
    dimensions = _page_dimensions(document)
    tables = _extract_tables(document, dimensions)
    text_by_page = _page_text(document, dimensions)
    images = _save_page_images(document, document_id)

    pages: list[ParsedPage] = []
    for page_no in sorted(dimensions) or [1]:
        width, height = dimensions.get(page_no, (0.0, 0.0))
        page_tables = [table for table in tables if table.page_number == page_no]

        narrative = "\n".join(text_by_page.get(page_no, []))
        table_rows = [row for table in page_tables for row in table.to_serialized_rows()]
        markdown = "\n".join(filter(None, [narrative, *table_rows]))

        pages.append(
            ParsedPage(
                page_number=page_no,
                markdown=markdown,
                narrative_markdown=narrative,
                image_path=images.get(page_no),
                width_points=width,
                height_points=height,
                char_count=len(markdown),
                text_yield_ratio=_text_yield_ratio(len(markdown)),
                table_count=len(page_tables),
                used_ocr=used_ocr,
            )
        )

    return ParsedDocument(
        document_id=document_id,
        filename=source.name,
        source_path=str(source),
        page_count=max(1, len(pages)),
        pages=pages,
        markdown=document.export_to_markdown(),
        tables=tables,
        used_ocr=used_ocr,
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
        source: Path to a PDF or image file.
        document_id: Stable identifier. Generated if omitted.
        force_ocr: ``True`` forces the OCR pass, ``False`` forbids it, ``None`` (default)
            decides per page from ``text_yield_ratio``.
        persist_source: Copy the original into ``data/uploads/<document_id>/`` (FR-1.5).

    Returns:
        A fully populated ``ParsedDocument``.

    Raises:
        ParsingError: The file is missing, unsupported, oversized, corrupt, or
            password-protected. The message is safe to show a user directly.
    """
    settings = get_settings()
    path = Path(source).expanduser().resolve()
    _validate_source(path)

    document_id = document_id or str(uuid.uuid4())
    is_image = path.suffix.lower() in IMAGE_EXTENSIONS
    started = time.perf_counter()

    # Fast-path triage. An image has no text layer by definition; for a PDF, PyMuPDF
    # answers the same question in milliseconds. Deciding here means a scanned PDF goes
    # straight to the OCR pipeline instead of being parsed once to discover it needs OCR
    # and then parsed again.
    if force_ocr is None:
        if is_image:
            first_pass_ocr = settings.ocr_enabled
        else:
            text_layer, pages, chars = has_text_layer(path)
            first_pass_ocr = (not text_layer) and settings.ocr_enabled
            logger.info(
                "Triage %s: %d page(s), %d chars, text_layer=%s -> ocr=%s",
                path.name, pages, chars, text_layer, first_pass_ocr,
            )
    else:
        first_pass_ocr = force_ocr

    cache_key = (_content_hash(path), first_pass_ocr)
    cached = _PARSE_CACHE.get(cache_key)
    if cached is not None:
        _PARSE_CACHE.move_to_end(cache_key)
        logger.info("Parse cache hit for %s (document_id=%s)", path.name, document_id)
        if persist_source:
            _persist_source(path, document_id)
        # Same content, possibly a new identifier. Page images on disk stay valid because
        # their paths are absolute and the rendered pages are identical.
        return cached.model_copy(
            update={"document_id": document_id, "parse_seconds": 0.0}
        )

    logger.info(
        "Parsing document_id=%s ocr=%s image=%s", document_id, first_pass_ocr, is_image
    )
    result = _convert(path, with_ocr=first_pass_ocr)
    parsed = _build_parsed_document(
        result,
        document_id=document_id,
        source=path,
        used_ocr=first_pass_ocr,
        parse_seconds=time.perf_counter() - started,
    )

    # Second pass: some pages came back without a usable text layer, so re-run with OCR.
    # Docling has no per-page OCR toggle, so this re-converts the document; that is why
    # the first pass is skipped entirely for images.
    should_retry = (
        force_ocr is None
        and settings.ocr_enabled
        and not first_pass_ocr
        and bool(parsed.pages_needing_ocr(settings.text_yield_threshold))
    )
    if should_retry:
        low_pages = parsed.pages_needing_ocr(settings.text_yield_threshold)
        logger.info(
            "document_id=%s: %d/%d pages below text-yield threshold — re-parsing with OCR",
            document_id,
            len(low_pages),
            parsed.page_count,
        )
        ocr_result = _convert(path, with_ocr=True)
        parsed = _build_parsed_document(
            ocr_result,
            document_id=document_id,
            source=path,
            used_ocr=True,
            parse_seconds=time.perf_counter() - started,
        )

    if persist_source:
        _persist_source(path, document_id)

    _PARSE_CACHE[cache_key] = parsed
    _PARSE_CACHE.move_to_end(cache_key)
    while len(_PARSE_CACHE) > PARSE_CACHE_SIZE:
        _PARSE_CACHE.popitem(last=False)

    logger.info(
        "document_id=%s parsed: %d pages, %d tables, %d rows, ocr=%s, %.1fs",
        document_id,
        parsed.page_count,
        len(parsed.tables),
        parsed.total_rows,
        parsed.used_ocr,
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
