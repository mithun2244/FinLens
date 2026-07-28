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
import os
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
#: a scored extraction field, not just an answer-quality wobble. The extractor
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
#: — not Scout, not Maverick, nothing. Scanned pages are therefore handled by Docling's
#: LOCAL OCR engine instead (see ``OCR_*`` below), which is faster, free, deterministic,
#: rate-limit-free, and keeps document images on the machine. ``src/parser.py`` owns that
#: fallback; no cloud model ever sees a page image.
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

# ── Local OCR (decision D-15: replaces the cloud vision fallback entirely) ────

#: Docling OCR engine. RapidOCR ships with docling-slim and runs on onnxruntime CPU —
#: no system binary, no extra download beyond its own small ONNX models.
OCR_ENGINE: Final[str] = "rapidocr"

#: OCR languages, in RapidOCR's codes. Non-Latin scripts are out of scope (prd.md §7).
OCR_LANGUAGES: Final[tuple[str, ...]] = ("english",)

#: Minimum RapidOCR text confidence. Below this a detection is discarded rather than
#: guessed at — a misread digit in a financial document is worse than a missing one.
OCR_TEXT_SCORE: Final[float] = 0.5

#: TableFormer mode. ACCURATE is ~2x slower than FAST but materially better on the
#: ruled multi-column tables that invoices actually use — and table fidelity is the
#: whole point of choosing Docling (architecture.md §3.2).
TABLE_MODE_ACCURATE: Final[bool] = True

#: Docling page-image scale factor (1.0 == 72 DPI). 2.0 gives ~144 DPI, close to
#: PAGE_RENDER_DPI, which is enough for the previewer without ballooning memory.
#:
#: Measured: turning page images off entirely saves nothing (3.63 s off vs 3.22 s on —
#: inside run-to-run noise), so they stay on. They are what the document previewer and
#: citation highlighting are built from.
DOCLING_IMAGE_SCALE: Final[float] = 2.0

#: Threads for Docling's layout and TableFormer models.
#:
#: This is the single largest parsing lever, and it is easy to miss: Docling's
#: ``AcceleratorOptions`` defaults to **4 threads** and sets torch's thread count itself,
#: so calling ``torch.set_num_threads()`` from application code is overridden and does
#: nothing. Measured on a 12-core machine, one page, TableFormer ACCURATE:
#:
#:     num_threads=4   3.58 s   (Docling's default)
#:     num_threads=8   2.20 s
#:     num_threads=12  2.06 s   (-42%)
#:
#: Capped at 8 by default: the gain from 8 to 12 is small, and leaving headroom keeps the
#: UI responsive while a document parses.
DOCLING_NUM_THREADS: Final[int] = max(1, min(8, (os.cpu_count() or 4)))

#: Minimum extracted characters per page for a PDF to count as having a text layer.
#:
#: Used by the PyMuPDF triage pass to decide OCR *before* invoking Docling, which avoids
#: parsing a scanned document twice. Deliberately low. Measured on the fixture set:
#:
#:     scanned_receipt.png        0 chars/page
#:     multipage_statement.pdf  289 chars/page
#:     clean_invoice.pdf        511 chars/page
#:
#: The question is "is there a text layer at all", not "is this page dense". A threshold
#: near 100 words/page (~500 chars) would wrongly send the sparse two-page statement
#: through OCR; 50 separates the real cases with a wide margin.
MIN_CHARS_PER_PAGE_FOR_TEXT_LAYER: Final[int] = 50

#: How many parsed documents to keep in the in-process cache, keyed by content hash.
#: Re-uploading a file already parsed in this session returns instantly.
PARSE_CACHE_SIZE: Final[int] = 16

# ── Local embedding model (decision D-2: embeddings never leave the machine) ──

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
IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg", ".webp"})
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({".pdf"}) | IMAGE_EXTENSIONS

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

    # Behavior.
    text_yield_threshold: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Below this Docling text-yield ratio a page escalates to local OCR (FR-2.3, D-15)",
    )
    ocr_enabled: bool = Field(
        default=True,
        description="Allow the local OCR fallback for scanned pages. Disable to parse text layers only.",
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

    @property
    def groq_configured(self) -> bool:
        """True when a Groq key is present. Cloud reasoning is unavailable without it."""
        return bool(self.groq_api_key) and self.groq_api_key != "gsk_your_key_here"

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
