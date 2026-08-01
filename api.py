"""FastAPI backend for the Next.js frontend.

    uvicorn api:app --reload --port 8000

A second consumer of ``src/``, sitting beside ``app.py``. Neither owns business logic —
that all lives in ``src/`` and is imported, which is exactly what decision D-8 was for:
``src/`` never imports a UI framework, so a Streamlit dashboard and a React SPA can run
against the same core without it knowing either exists.

**On ``/api/chat``:** it delegates to ``src.chain.stream_answer`` rather than calling Groq
directly. That function is not a thin wrapper — it carries query rewriting, dual retrieval
across the document and policy corpora, citation resolution against chunks actually
retrieved, and the numeric cross-check that traces every figure back to the extracted
record. Streaming from Groq by hand here would silently drop the entire grounding layer,
which is the product.
"""

from __future__ import annotations

# Environment before anything reads it — see the identical note in app.py (decision D-33).
from dotenv import load_dotenv

load_dotenv(override=True)

import json  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402
from collections.abc import AsyncIterator, Iterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Literal  # noqa: E402

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src import AssistantError, ParsingError  # noqa: E402
from src.chain import ChatTurn, stream_answer, suggested_prompts  # noqa: E402
from src.config import (  # noqa: E402
    COLLECTION_POLICIES,
    FIXTURES_DIR,
    MAX_UPLOAD_BYTES,
    MODELS_BY_ROLE,
    SUPPORTED_EXTENSIONS,
    configure_logging,
    ensure_directories,
    get_settings,
)
from src.extractor import extract_record  # noqa: E402
from src.llm import llm_available  # noqa: E402
from src.parser import parse_document, warm_up  # noqa: E402
from src.schemas import FinancialRecord, ParsedDocument, RunStats  # noqa: E402
from src.vectorstore import (  # noqa: E402
    collection_stats,
    delete_document,
    ingest_document,
    ingest_policy_files,
)
from src.vectorstore import warm_up as warm_embeddings  # noqa: E402

logger = logging.getLogger(__name__)

#: Browser origins allowed to call this API, from ``ALLOWED_ORIGINS`` in the environment.
#: Defaults to the local Next.js dev server; a deployed frontend is a different origin and
#: is blocked until it is listed. See :class:`src.config.Settings` for why a wildcard is
#: rejected rather than supported.
ALLOWED_ORIGINS = get_settings().allowed_origin_list

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load the heavy models before the first request rather than during it.

    Measured cold: Docling converter 8.6 s, MiniLM 3.6 s. Paying that here means the
    first upload costs ~2 s rather than ~14 s (decision D-35).
    """
    configure_logging()
    ensure_directories()
    logger.info("Warming Docling and embedding models...")
    warm_up(with_ocr=False)
    warm_embeddings()
    logger.info("FinLens API ready")
    yield


app = FastAPI(
    title="FinLens API",
    description="Grounded question answering over financial documents.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

#: Parsed documents and their records, by document_id.
#:
#: In-process and therefore single-worker. Running uvicorn with --workers > 1 would give
#: each worker its own copy and uploads would appear to vanish between requests. The
#: embedded Chroma store is single-process anyway (decision D-34), so one worker is the
#: right deployment shape for this app regardless.
_DOCUMENTS: dict[str, tuple[ParsedDocument, FinancialRecord]] = {}


# ── Response models ──────────────────────────────────────────────────────────


class LineItemOut(BaseModel):
    description: str
    quantity: str | None = None
    unit_price: str | None = None
    amount: str
    source_page: int
    confidence: float
    confidence_band: Literal["high", "medium", "low"]


class TaxLineOut(BaseModel):
    label: str
    rate: str | None = None
    amount: str


class PageOut(BaseModel):
    page_number: int
    width_points: float
    height_points: float
    has_image: bool


class DocumentOut(BaseModel):
    """The extracted record, serialized for the browser.

    Every monetary value crosses the wire as a **string**, never a JSON number.
    ``Decimal("412.90")`` becomes the float ``412.9`` in JavaScript, and 0.1 + 0.2 is
    famously not 0.3 there. Decision D-6 keeps money exact in Python; sending strings is
    the same decision extended to the client, which formats them rather than doing
    arithmetic on them.
    """

    document_id: str
    filename: str
    vendor_name: str
    vendor_address: str | None = None
    document_type: str
    invoice_number: str | None = None
    billing_date: str | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    currency: str
    line_items: list[LineItemOut]
    subtotal: str | None = None
    tax_lines: list[TaxLineOut]
    total_amount: str | None = None
    computed_total: str
    validation_state: Literal["validated", "mismatch", "incomplete"]
    is_validated: bool
    extraction_warnings: list[str]
    page_count: int
    pages: list[PageOut]
    used_ocr: bool
    parse_seconds: float
    suggested_prompts: list[str]


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    document_id: str | None = None
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


class HealthOut(BaseModel):
    status: str
    llm_configured: bool
    reasoning_model: str
    documents_loaded: int
    collections: dict[str, int]


# ── Serialization ────────────────────────────────────────────────────────────


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value}"


def _to_document_out(
    parsed: ParsedDocument, record: FinancialRecord, *, has_policies: bool
) -> DocumentOut:
    return DocumentOut(
        document_id=record.document_id,
        filename=record.filename,
        vendor_name=record.vendor_name,
        vendor_address=record.vendor_address,
        document_type=record.document_type,
        invoice_number=record.invoice_number,
        billing_date=str(record.billing_date) if record.billing_date else None,
        billing_period_start=(
            str(record.billing_period_start) if record.billing_period_start else None
        ),
        billing_period_end=(
            str(record.billing_period_end) if record.billing_period_end else None
        ),
        currency=record.currency,
        line_items=[
            LineItemOut(
                description=item.description,
                quantity=_money(item.quantity),
                unit_price=_money(item.unit_price),
                amount=f"{item.amount}",
                source_page=item.source_page,
                confidence=item.confidence,
                confidence_band=item.confidence_band,
            )
            for item in record.line_items
        ],
        subtotal=_money(record.subtotal),
        tax_lines=[
            TaxLineOut(label=tax.label, rate=_money(tax.rate), amount=f"{tax.amount}")
            for tax in record.tax_lines
        ],
        total_amount=_money(record.total_amount),
        computed_total=f"{record.computed_total}",
        validation_state=record.validation_state,
        is_validated=record.is_validated,
        extraction_warnings=record.extraction_warnings,
        page_count=parsed.page_count,
        pages=[
            PageOut(
                page_number=page.page_number,
                width_points=page.width_points,
                height_points=page.height_points,
                has_image=bool(page.image_path),
            )
            for page in parsed.pages
        ],
        used_ocr=parsed.used_ocr,
        parse_seconds=round(parsed.parse_seconds, 2),
        suggested_prompts=suggested_prompts(record, has_policies=has_policies),
    )


def _policies_indexed() -> bool:
    try:
        return collection_stats().get(COLLECTION_POLICIES, 0) > 0
    except AssistantError:
        return False


# ── Endpoints ────────────────────────────────────────────────────────────────


@app.get("/api/health", response_model=HealthOut)
def health() -> HealthOut:
    try:
        collections = collection_stats()
    except AssistantError:
        collections = {}
    return HealthOut(
        status="ok",
        llm_configured=llm_available(),
        reasoning_model=MODELS_BY_ROLE["reasoning"],
        documents_loaded=len(_DOCUMENTS),
        collections=collections,
    )


@app.post("/api/upload", response_model=DocumentOut)
async def upload(file: UploadFile = File(...)) -> DocumentOut:
    """Parse, extract, and index one document.

    Errors carry the message from ``src/`` verbatim. Those are written to be shown to a
    user (rules.md Rule 2.3) — "This PDF is password-protected" rather than a stack trace.
    """
    filename = Path(file.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Cannot read '{suffix or filename}'. "
                f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
            ),
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail=f"{filename} is empty (0 bytes).")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{filename} is {len(payload) / 1_048_576:.1f} MB, above the "
                f"{MAX_UPLOAD_BYTES / 1_048_576:.0f} MB limit."
            ),
        )

    document_id = str(uuid.uuid4())[:8]
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / filename
        staged.write_bytes(payload)
        try:
            parsed = parse_document(staged, document_id=document_id)
            record = extract_record(parsed)
            ingest_document(parsed, record)
        except ParsingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AssistantError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    _DOCUMENTS[document_id] = (parsed, record)
    logger.info(
        "uploaded document_id=%s: %d line items, %s",
        document_id, len(record.line_items), record.validation_state,
    )
    return _to_document_out(parsed, record, has_policies=_policies_indexed())


@app.post("/api/chat")
def chat(request: ChatRequest) -> StreamingResponse:
    """Stream a grounded answer as newline-delimited JSON.

    NDJSON rather than Server-Sent Events: the client needs structured events (stage
    labels, tokens, and a final answer carrying citations and numeric checks), and each
    line here maps one-to-one onto a ``StreamEvent``. SSE would add framing for a
    reconnect semantic this endpoint does not use.
    """
    record = None
    if request.document_id:
        entry = _DOCUMENTS.get(request.document_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail="That document is not loaded. Upload it again.",
            )
        record = entry[1]

    def events() -> Iterator[str]:
        stats = RunStats(model=MODELS_BY_ROLE["reasoning"])
        try:
            for event in stream_answer(
                request.question,
                record=record,
                document_id=request.document_id,
                history=list(request.history),
                stats=stats,
            ):
                payload: dict[str, Any] = {"type": event.type}
                if event.type == "stage":
                    payload["stage"] = event.stage
                elif event.type == "token":
                    payload["token"] = event.token
                elif event.type == "error":
                    payload["message"] = event.message
                    payload["retry_after_seconds"] = event.retry_after_seconds
                elif event.type == "answer" and event.answer is not None:
                    answer = event.answer
                    payload["answer"] = {
                        "text": answer.text,
                        "refused": answer.refused,
                        "is_grounded": answer.is_grounded,
                        "is_trustworthy": answer.is_trustworthy,
                        "model": answer.model,
                        "latency_seconds": round(answer.latency_seconds, 2),
                        "prompt_tokens": answer.prompt_tokens,
                        "completion_tokens": answer.completion_tokens,
                        "citations": [
                            {
                                "document_id": c.document_id,
                                "filename": c.filename,
                                "page": c.page,
                                "label": c.label,
                                "snippet": c.snippet,
                                "score": round(c.score, 3),
                                "chunk_type": c.chunk_type,
                                # Normalized 0-1, top-left origin, so the client can
                                # place an overlay without knowing page dimensions
                                # (decision D-16). Null when Docling gave no provenance.
                                "bbox": (
                                    {
                                        "left": c.bbox.left,
                                        "top": c.bbox.top,
                                        "right": c.bbox.right,
                                        "bottom": c.bbox.bottom,
                                    }
                                    if c.bbox
                                    else None
                                ),
                            }
                            for c in answer.citations
                        ],
                        "dropped_citations": answer.dropped_citations,
                        # Surfaced so the UI can warn. These are the checks that make
                        # grounding enforced rather than merely requested.
                        "unsupported_figures": [
                            f"{c.claimed}" for c in answer.unsupported_figures
                        ],
                        "contradicting_figures": [
                            {
                                "claimed": f"{c.claimed}",
                                "field": c.matched_field,
                                "expected": f"{c.expected}" if c.expected else None,
                            }
                            for c in answer.contradicting_figures
                        ],
                    }
                    payload["stats"] = {
                        "retrieve_seconds": round(stats.retrieve_seconds, 2),
                        "generate_seconds": round(stats.generate_seconds, 2),
                        "chunks_retrieved": stats.chunks_retrieved,
                        "total_tokens": stats.total_tokens,
                        "tokens_estimated": stats.tokens_estimated,
                    }
                yield json.dumps(payload) + "\n"
        except AssistantError as exc:
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/policies")
def load_policies() -> dict[str, int | str]:
    """Index the bundled synthetic policy corpus, enabling cross-document answers."""
    paths = sorted((FIXTURES_DIR / "policies").glob("*.md"))
    if not paths:
        raise HTTPException(
            status_code=404,
            detail="No policy documents found. Run `python scripts/make_fixtures.py`.",
        )
    return {"indexed": ingest_policy_files(paths), "files": len(paths)}


@app.get("/api/samples")
def samples() -> list[dict[str, str]]:
    """Sample documents, so a first-time visitor can try the product immediately."""
    catalogue = [
        ("AWS invoice", "clean_invoice.pdf"),
        ("Card statement", "multipage_statement.pdf"),
        ("Scanned receipt", "scanned_receipt.png"),
        ("Unbalanced invoice", "unbalanced_invoice.pdf"),
    ]
    return [
        {"label": label, "filename": name}
        for label, name in catalogue
        if (FIXTURES_DIR / name).exists()
    ]


@app.post("/api/samples/{filename}", response_model=DocumentOut)
def load_sample(filename: str) -> DocumentOut:
    # Reject any path separator or traversal before touching the filesystem.
    safe = Path(filename).name
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid sample name.")

    source = FIXTURES_DIR / safe
    if not source.is_file():
        raise HTTPException(status_code=404, detail=f"No sample named {safe}.")

    document_id = str(uuid.uuid4())[:8]
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / safe
        shutil.copy2(source, staged)
        try:
            parsed = parse_document(staged, document_id=document_id)
            record = extract_record(parsed)
            ingest_document(parsed, record)
        except AssistantError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    _DOCUMENTS[document_id] = (parsed, record)
    return _to_document_out(parsed, record, has_policies=_policies_indexed())


@app.get("/api/documents/{document_id}/pages/{page_number}")
def page_image(document_id: str, page_number: int) -> FileResponse:
    """Serve a rendered page image for the document previewer.

    The path comes from the in-memory ``ParsedDocument`` rather than being built from the
    URL, so a traversal attempt cannot escape the uploads directory: an unknown
    ``document_id`` simply misses the dictionary, and an out-of-range page misses the
    page list. No user-supplied string ever reaches the filesystem.
    """
    entry = _DOCUMENTS.get(document_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="That document is not loaded.")

    parsed = entry[0]
    page = next((p for p in parsed.pages if p.page_number == page_number), None)
    if page is None or not page.image_path:
        raise HTTPException(
            status_code=404, detail=f"No rendered image for page {page_number}."
        )

    path = Path(page.image_path)
    if not path.is_file():
        raise HTTPException(
            status_code=410,
            detail="The page image is no longer on disk. Re-upload the document.",
        )

    return FileResponse(
        path,
        media_type="image/png",
        # Immutable: a page image never changes for a given document_id.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.delete("/api/documents/{document_id}")
def remove_document(document_id: str) -> dict[str, str | int]:
    if document_id not in _DOCUMENTS:
        raise HTTPException(status_code=404, detail="No such document.")
    removed = delete_document(document_id)
    _DOCUMENTS.pop(document_id, None)
    return {"document_id": document_id, "chunks_removed": removed}
