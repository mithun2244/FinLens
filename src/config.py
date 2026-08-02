"""Centralized configuration: model IDs, paths, thresholds, environment loading.

This module is the root of the dependency chain (architecture.md §4). It imports from
nothing else in ``src/``.

**Decision D-4 (memory.md): no model ID is ever inlined anywhere else in this codebase.**
Groq retires preview models on short notice — see D-3, where the brief's specified
Llama 3.2 Vision endpoints turned out to be decommissioned. Centralizing the IDs here
makes that class of breakage a one-line fix. Verify liveness with
``python scripts/check_models.py``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Paths ─────────────────────────────────────────────────────────────────────

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
UPLOAD_DIR: Final[Path] = DATA_DIR / "uploads"
CHROMA_DIR: Final[Path] = DATA_DIR / "chroma"
LLM_CACHE_PATH: Final[Path] = DATA_DIR / "llm_cache.db"
EVALS_DIR: Final[Path] = PROJECT_ROOT / "evals"
FIXTURES_DIR: Final[Path] = EVALS_DIR / "fixtures"
GOLDEN_SET_PATH: Final[Path] = EVALS_DIR / "golden_set.jsonl"
EVAL_REPORTS_DIR: Final[Path] = EVALS_DIR / "reports"

# ── Groq model IDs (decision D-4: the ONLY place these strings appear) ────────

#: Primary reasoning model for grounded RAG answers (FR-4). Verified served 2026-07-28.
#:
#: Do not swap this to dodge a spent daily budget. ``openai/gpt-oss-120b`` was tried on
#: 2026-07-27 for exactly that reason — Groq meters each model separately, so it does buy
#: a fresh bucket — and the sweep it unblocked was not comparable: citation rate 58.3%
#: over 24 questions, and ``billing_date`` dropped from ``scanned_receipt.png``, which is
#: a scored extraction field at the time, not just an answer-quality wobble. The extractor
#: shares this constant, so a swap moves the deterministic numbers too. Wait for the
#: refill instead, or accept that the run measures the substitute rather than the system.
REASONING_MODEL: Final[str] = "llama-3.3-70b-versatile"

#: Cheap/fast model for query rewriting, classification, and the Ragas judge (D-10).
#: Verified served 2026-07-25.
UTILITY_MODEL: Final[str] = "llama-3.1-8b-instant"

#: There is deliberately NO vision model here (decision D-15).
#:
#: The brief specified Llama 3.2 Vision; those endpoints were retired (D-3). We moved to
#: Llama 4 Scout; ``check_models.py`` then proved Groq serves no image-input model at all
#: — not Scout, not Maverick, nothing. Scanned pages were therefore handled by a LOCAL
#: OCR engine instead. That fallback has since been removed too: it was an onnxruntime
#: model, and the 512 MB budget has no room for it. Scanned documents are now rejected at
#: upload rather than silently parsed into an empty record — see ``src/parser.py``.
#:
#: Fallback candidates, tried in order if a primary ID stops being served.
REASONING_MODEL_FALLBACKS: Final[tuple[str, ...]] = (
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "llama-3.1-8b-instant",
)

ModelRole = Literal["reasoning", "utility"]

MODELS_BY_ROLE: Final[dict[ModelRole, str]] = {
    "reasoning": REASONING_MODEL,
    "utility": UTILITY_MODEL,
}

# ── Table extraction (pdfplumber) ────────────────────────────────────────────

#: ``(vertical, horizontal)`` strategy pairs handed to pdfplumber, tried in order until
#: one yields rows.
#:
#: The axes need different strategies, which is the whole reason this is a list of pairs
#: rather than a list of strategies. Financial documents are typically ruled *across* —
#: a line under the header, a line between rows — while the columns are held apart by
#: whitespace alone. Measured on clean_invoice.pdf:
#:
#:     lines / lines   0 tables      (no vertical rules exist to close the cells)
#:     text  / text    1 table, 31 rows of page furniture, headers like "Amazon"
#:     text  / lines   1 table, 5 rows, headers Description/Qty/Unit Price/Amount
#:
#: So the middle entry is the one that matters, and ``text/text`` is kept only as a last
#: resort for a table drawn with no rules at all — where it reliably swallows the whole
#: page, hence last.
#:
#: Docling's TableFormer was replaced here (see the 512 MB refactor): it inferred cell
#: structure from a layout model and needed none of this, but it is a torch model and
#: torch alone exceeds the memory budget.
TABLE_STRATEGIES: Final[tuple[tuple[str, str], ...]] = (
    ("lines", "lines"),
    ("text", "lines"),
    ("text", "text"),
)

#: Minimum horizontal whitespace, in points, that counts as a column gutter rather than
#: the space between two words. Used to derive explicit column boundaries from word
#: positions — see ``_gutter_boundaries``, and the mid-word splits it exists to prevent.
MIN_COLUMN_GUTTER: Final[float] = 4.0

#: Minimum extracted characters per page for a PDF to count as having a text layer.
#:
#: This used to select between the text pipeline and OCR. With OCR gone it decides
#: something starker: whether the document can be read at all. Below this the upload is
#: rejected with an explanation, because the alternative — returning a document with no
#: line items and no total — looks like a successful parse of an empty invoice.
#:
#: Deliberately low. Measured on the fixture set:
#:
#:     multipage_statement.pdf  289 chars/page
#:     clean_invoice.pdf        511 chars/page
#:
#: The question is "is there a text layer at all", not "is this page dense". A threshold
#: near 100 words/page (~500 chars) would wrongly reject the sparse two-page statement;
#: 50 separates the real cases with a wide margin.
MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER: Final[int] = 50

#: How many parsed documents to keep in the in-process cache, keyed by content hash.
#: Re-uploading a file already parsed in this session returns instantly.
PARSE_CACHE_SIZE: Final[int] = 16

# ── Embedding model (served remotely — see the note on D-2 below) ────────────

#: The same model as before, now called over HTTP instead of loaded in-process.
#:
#: **This reverses decision D-2.** Embeddings used to be computed locally, and the
#: guarantee was that financial text never left the machine at embed time. Fitting inside
#: 512 MB means sentence-transformers and torch have to go, and every remaining option
#: sends the text somewhere. Keeping the *same* model matters for two reasons: retrieval
#: quality is unchanged, and the vectors stay 384-dimensional, so existing Chroma
#: collections remain readable rather than needing a rebuild.
EMBEDDING_MODEL: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS: Final[int] = 384

#: MiniLM truncates at 256 tokens — this is why chunks are small (architecture.md §3.4).
#: Upgrade path if retrieval quality is insufficient (OQ-4): ``BAAI/bge-small-en-v1.5``.
EMBEDDING_MAX_TOKENS: Final[int] = 256

# ── Chroma collections ────────────────────────────────────────────────────────

COLLECTION_DOCUMENTS: Final[str] = "financial_documents"
COLLECTION_POLICIES: Final[str] = "policy_corpus"

# ── Ingestion constraints ─────────────────────────────────────────────────────

MAX_UPLOAD_BYTES: Final[int] = 25 * 1024 * 1024  # FR-1.1
#: Still enumerated, though no longer supported: an upload with one of these suffixes gets
#: "images are not supported since OCR was removed" rather than the generic "supported
#: formats: .pdf", which does not tell a user who just uploaded a photo of a receipt what
#: went wrong or what to do instead.
IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg", ".webp"})
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({".pdf"})

#: Denominator for ``ParsedPage.text_yield_ratio``.
#:
#: The ratio answers one question: *does this page have a usable text layer?* — not "how
#: dense is it". A digital invoice page yields 600-1500 extracted characters; a scanned
#: page yields ~0. With this denominator and a 0.15 threshold, the OCR fallback fires
#: below ~150 characters, which cleanly separates the two cases without misfiring on
#: legitimately sparse documents like a one-line receipt.
EXPECTED_CHARS_PER_PAGE: Final[int] = 1000

#: Page render DPI for the high-resolution previewer (design.md §5.1).
PAGE_RENDER_DPI: Final[int] = 150

#: FR-2.4 arithmetic validation tolerance. Decimal, never float (decision D-6).
AMOUNT_TOLERANCE: Final[Decimal] = Decimal("0.02")

#: Confidence bands for the UI confidence dot (design.md §5.2).
CONFIDENCE_HIGH: Final[float] = 0.85
CONFIDENCE_LOW: Final[float] = 0.60

# ── Chunking (decision D-7: a table row is NEVER split) ───────────────────────

CHUNK_SIZE: Final[int] = 800
CHUNK_OVERLAP: Final[int] = 120

# ── Runtime settings (env-overridable) ────────────────────────────────────────


class Settings(BaseSettings):
    """Environment-backed runtime settings.

    Loaded from ``.env`` at the project root. See ``.env.example`` for documentation
    of every key and where to obtain it free.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Required — Groq free tier, no credit card.
    groq_api_key: str = Field(default="", description="Groq Cloud API key")

    # Optional — LangSmith. The app must run normally without it (decision D-11).
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "multimodal-fin-assistant"

    # Deployment — browser origins allowed to call the API (CORS).
    #
    # Anywhere but local development the frontend is served from a different host, and a
    # different host is a different origin, so this must be set wherever the API is
    # deployed or the browser blocks every call. Comma-separated rather than a JSON list
    # because it gets typed into a hosting dashboard by hand.
    allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Behavior.
    # Required — Hugging Face Inference API, free tier. Embeddings are computed there
    # rather than in-process; without it retrieval cannot run at all.
    hf_token: str = Field(default="", description="Hugging Face token for the embedding API")

    #: Where the embedding request goes. Overridable so a self-hosted Text Embeddings
    #: Inference server, or any OpenAI-compatible embedding endpoint, can be dropped in
    #: without a code change — which is also how this gets back to embeddings that never
    #: leave the machine, if that matters more than the memory budget later.
    embedding_endpoint_url: str = ""

    text_yield_threshold: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Below this text-yield ratio a page is treated as having no text layer",
    )
    retrieval_k_document: int = Field(default=6, ge=1, le=50)
    retrieval_k_policy: int = Field(default=4, ge=0, le=50)
    llm_cache_enabled: bool = True
    llm_timeout_seconds: float = Field(default=90.0, gt=0)
    llm_max_retries: int = Field(default=4, ge=0, le=10)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("groq_api_key")
    @classmethod
    def _strip_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("allowed_origins")
    @classmethod
    def _reject_wildcard_origin(cls, value: str) -> str:
        """A wildcard here is a vulnerability rather than a convenience.

        The API is mounted with ``allow_credentials=True``. Starlette answers a wildcard
        under that setting by echoing the caller's own origin back in
        ``Access-Control-Allow-Origin``, which is not the harmless "public API" behaviour
        the ``*`` suggests: it means any site a user has open can make credentialed
        requests to this one and read the replies. Enumerating origins is the only safe
        form, so a wildcard fails at startup instead of surviving to an audit.
        """
        if "*" in value:
            raise ValueError(
                "allowed_origins cannot contain '*'. This API sends credentials, and a "
                "wildcard combined with credentials lets any site call it on a signed-in "
                "user's behalf. List the frontend origins explicitly, comma-separated."
            )
        return value.strip()

    @property
    def embeddings_configured(self) -> bool:
        """True when the embedding API can be called.

        A custom endpoint may be unauthenticated (a local TEI server, say), so either a
        token or an explicit URL is enough.
        """
        return bool(self.hf_token.strip() or self.embedding_endpoint_url.strip())

    @property
    def groq_configured(self) -> bool:
        """True when a Groq key is present. Cloud reasoning is unavailable without it."""
        return bool(self.groq_api_key) and self.groq_api_key != "gsk_your_key_here"

    @property
    def allowed_origin_list(self) -> list[str]:
        """``allowed_origins`` as CORSMiddleware wants it, with blanks dropped.

        Blanks are dropped because a trailing comma in a dashboard field is easy to leave
        behind, and an empty string is an origin that matches nothing while looking set.
        """
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def tracing_enabled(self) -> bool:
        """True only when tracing is both requested and actually configured (D-11)."""
        return self.langchain_tracing_v2 and bool(self.langchain_api_key)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton, loading ``.env`` on first call."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def ensure_directories() -> None:
    """Create the local data directories. Safe to call repeatedly."""
    for directory in (UPLOAD_DIR, CHROMA_DIR, EVAL_REPORTS_DIR, FIXTURES_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    """Configure stdlib logging.

    Rule 2.3: financial data is never logged at any level. Log document IDs, page
    numbers, timings, and token counts — never amounts, vendor names, or parsed text.
    """
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party libraries are noisy at INFO and leak document content into logs.
    for noisy in ("httpx", "httpcore", "chromadb", "sentence_transformers", "docling"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
