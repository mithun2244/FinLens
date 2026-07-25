"""Pydantic data contracts shared across every module (architecture.md §5).

Two rules govern this file:

1. **All monetary values are ``Decimal``, never ``float``** (decision D-6). Float currency
   arithmetic silently produces wrong totals, and this product's entire value is that its
   numbers are exactly right.
2. **A missing value is ``None`` plus a warning — never a fabricated default** (decision
   D-12, rules.md Rule 2.3). A silently-wrong number is worse than a visible failure.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from src.config import AMOUNT_TOLERANCE, CONFIDENCE_HIGH, CONFIDENCE_LOW

DocumentType = Literal["invoice", "statement", "receipt", "expense_report"]
ChunkType = Literal["text", "table_row", "table_summary", "record_summary"]
ConfidenceBand = Literal["high", "medium", "low"]
ValidationState = Literal["validated", "mismatch", "incomplete"]


class _Base(BaseModel):
    """Shared model config: immutable-by-default semantics and strict field checking."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


# ── Extraction ────────────────────────────────────────────────────────────────


class LineItem(_Base):
    """A single row of an invoice, statement, or expense-report table.

    One ``LineItem`` becomes exactly one vector chunk at ingest time — a row is never
    split across chunks (decision D-7), because split rows are the dominant cause of
    hallucinated invoice figures.
    """

    description: str = Field(min_length=1)
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal
    category: str | None = None
    source_page: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confidence_band(self) -> ConfidenceBand:
        """UI confidence dot state (design.md §5.2). Never color-only — pairs with shape."""
        if self.confidence >= CONFIDENCE_HIGH:
            return "high"
        if self.confidence >= CONFIDENCE_LOW:
            return "medium"
        return "low"

    @property
    def implied_amount(self) -> Decimal | None:
        """``quantity * unit_price`` when both are present, for cross-checking ``amount``."""
        if self.quantity is None or self.unit_price is None:
            return None
        return self.quantity * self.unit_price


class TaxLine(_Base):
    """A tax component: VAT, GST, sales tax, or a levy line."""

    label: str = Field(min_length=1)
    rate: Decimal | None = Field(default=None, description="Fractional, e.g. 0.085 for 8.5%")
    amount: Decimal

    @field_validator("rate")
    @classmethod
    def _rate_is_fractional(cls, value: Decimal | None) -> Decimal | None:
        """Reject a rate expressed as a percentage (8.5) instead of a fraction (0.085)."""
        if value is not None and value > 1:
            raise ValueError(
                f"Tax rate must be fractional (0.085 for 8.5%), received {value}"
            )
        return value


class FinancialRecord(_Base):
    """The canonical structured record extracted from one document (FR-2.1).

    This is passed into the reasoning prompt alongside retrieved chunks so the model
    reads totals rather than re-deriving them from prose — a major hallucination
    reduction (architecture.md §6, step 4).
    """

    document_id: str
    filename: str
    vendor_name: str = Field(min_length=1)
    vendor_address: str | None = None
    document_type: DocumentType
    invoice_number: str | None = None
    billing_date: date | None = None
    due_date: date | None = None
    billing_period_start: date | None = None
    billing_period_end: date | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax_lines: list[TaxLine] = Field(default_factory=list)
    total_amount: Decimal | None = None
    page_count: int = Field(default=1, ge=1)
    used_vision_fallback: bool = False
    extraction_warnings: list[str] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("currency")
    @classmethod
    def _uppercase_currency(cls, value: str) -> str:
        return value.upper()

    @property
    def line_item_total(self) -> Decimal:
        return sum((item.amount for item in self.line_items), Decimal("0"))

    @property
    def tax_total(self) -> Decimal:
        return sum((tax.amount for tax in self.tax_lines), Decimal("0"))

    @property
    def computed_total(self) -> Decimal:
        return self.line_item_total + self.tax_total

    def validate_arithmetic(self) -> list[str]:
        """Check ``Σ line items + Σ tax == total`` within tolerance (FR-2.4).

        Returns the warnings produced. Appends them to ``extraction_warnings`` as a side
        effect so they survive into the UI validation banner (design.md §5.2).

        **This never mutates a number to force a balance** (decision D-12). A mismatch is
        reported, not repaired.
        """
        warnings: list[str] = []

        if self.total_amount is None:
            warnings.append(
                "Total amount could not be read from this document. "
                "Review the source and enter it manually if needed."
            )
        else:
            difference = abs(self.computed_total - self.total_amount)
            if difference > AMOUNT_TOLERANCE:
                warnings.append(
                    f"Line items plus tax total {self.computed_total}, but the document "
                    f"states {self.total_amount} (difference {difference}). "
                    f"Some rows may have been missed or misread."
                )

        if self.subtotal is None:
            warnings.append("Subtotal could not be read from this document.")
        elif abs(self.subtotal - self.line_item_total) > AMOUNT_TOLERANCE:
            warnings.append(
                f"Line items sum to {self.line_item_total}, but the stated subtotal is "
                f"{self.subtotal}."
            )

        if not self.line_items:
            warnings.append(
                "No line items were detected. This may be a summary statement — "
                "try asking a question about it directly."
            )

        for warning in warnings:
            if warning not in self.extraction_warnings:
                self.extraction_warnings.append(warning)
        return warnings

    @property
    def validation_state(self) -> ValidationState:
        """Which of the three banner states the Extraction Dashboard should show.

        Deliberately independent of ``extraction_warnings``: an advisory note ("these
        items were read from text, please verify") must not turn a document whose
        arithmetic is perfectly correct into a red mismatch. The banner reports the
        maths; the warning list reports everything else (design.md §5.2).
        """
        if self.total_amount is None:
            return "incomplete"
        if abs(self.computed_total - self.total_amount) > AMOUNT_TOLERANCE:
            return "mismatch"
        return "validated"

    @property
    def is_validated(self) -> bool:
        """True when the stated total reconciles with the extracted lines and tax."""
        return self.validation_state == "validated"

    @property
    def has_advisories(self) -> bool:
        """True when something is worth flagging even though the arithmetic is fine."""
        return bool(self.extraction_warnings)


# ── Parsing ───────────────────────────────────────────────────────────────────


class BoundingBox(_Base):
    """A region on a page, normalized to 0-1 with a **top-left origin**.

    Docling reports coordinates in absolute points with a *bottom-left* origin;
    ``src.parser`` converts them here so the values drop straight into CSS
    ``top``/``left`` percentages for the citation highlight overlay (design.md §5.1).
    """

    left: float = Field(ge=0.0, le=1.0)
    top: float = Field(ge=0.0, le=1.0)
    right: float = Field(ge=0.0, le=1.0)
    bottom: float = Field(ge=0.0, le=1.0)

    @property
    def width(self) -> float:
        return max(0.0, self.right - self.left)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    def as_css_percent(self) -> dict[str, str]:
        """Ready-to-use CSS for an absolutely-positioned highlight rectangle."""
        return {
            "left": f"{self.left * 100:.3f}%",
            "top": f"{self.top * 100:.3f}%",
            "width": f"{self.width * 100:.3f}%",
            "height": f"{self.height * 100:.3f}%",
        }


class TableBlock(_Base):
    """One table detected by Docling's TableFormer, with row structure preserved.

    Each entry in ``rows`` becomes exactly one vector chunk in Phase 3 — a row is never
    split (decision D-7). ``bbox`` drives citation highlighting when a table row is the
    source of an answer.
    """

    page_number: int = Field(ge=1)
    headers: list[str] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)
    bbox: BoundingBox | None = None
    caption: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_serialized_rows(self) -> list[str]:
        """Render each row as the flat string Phase 3 will embed.

        ``"Description: EC2 t3.medium | Qty: 730 | Unit Price: 0.0420 | Amount: 30.66"``
        Keeping the column name beside every value is what lets retrieval match a
        question like "what was the NAT gateway amount" to the right row.
        """
        return [
            " | ".join(f"{key}: {value}" for key, value in row.items() if value)
            for row in self.rows
        ]


class ParsedPage(_Base):
    """One page as produced by Docling, before structured extraction."""

    page_number: int = Field(ge=1)
    #: Everything extracted from the page — narrative text plus serialized table rows.
    #: Drives ``text_yield_ratio``, so a table-heavy page with a sparse header is not
    #: mistaken for a scan.
    markdown: str = ""
    #: Narrative text only, with table rows excluded. This is what Phase 3 chunks: table
    #: rows get their own chunks (D-7), and indexing them twice would let a single row
    #: outvote the rest of the document during retrieval.
    narrative_markdown: str = ""
    image_path: str | None = None
    width_points: float = Field(default=0.0, ge=0.0)
    height_points: float = Field(default=0.0, ge=0.0)
    char_count: int = Field(default=0, ge=0)
    text_yield_ratio: float = Field(ge=0.0, le=1.0)
    table_count: int = Field(default=0, ge=0)
    used_ocr: bool = False

    def needs_ocr(self, threshold: float) -> bool:
        """True when this page has no usable text layer and must go through OCR (FR-2.3).

        Decision D-15: the escalation target is Docling's **local** OCR engine, not a
        cloud vision model — Groq serves no image-input model, and local OCR is free,
        deterministic, rate-limit-free, and keeps page images on the machine.
        """
        return self.text_yield_ratio < threshold


class ParsedDocument(_Base):
    """Output contract of ``src.parser.parse_document`` (architecture.md §3.2)."""

    document_id: str
    filename: str
    source_path: str
    page_count: int = Field(ge=1)
    pages: list[ParsedPage]
    markdown: str
    #: Framework-free. pandas DataFrames are constructed at the UI boundary, never here
    #: (decision D-8), so ``src/`` stays importable without a frontend.
    tables: list[TableBlock] = Field(default_factory=list)
    used_ocr: bool = False
    parse_seconds: float = Field(default=0.0, ge=0.0)

    @property
    def mean_text_yield(self) -> float:
        if not self.pages:
            return 0.0
        return sum(page.text_yield_ratio for page in self.pages) / len(self.pages)

    @property
    def total_rows(self) -> int:
        return sum(table.row_count for table in self.tables)

    def pages_needing_ocr(self, threshold: float) -> list[int]:
        return [page.page_number for page in self.pages if page.needs_ocr(threshold)]

    def tables_on_page(self, page_number: int) -> list[TableBlock]:
        return [table for table in self.tables if table.page_number == page_number]


# ── Retrieval & answering ─────────────────────────────────────────────────────


class Citation(_Base):
    """A retrieved chunk, rendered in the UI as a clickable citation chip.

    Clicking it drives the document previewer: switch page, scroll, highlight, pulse
    (design.md §5.3). ``bbox`` is populated when Docling exposes coordinates precise
    enough for a region overlay; when it is ``None`` the UI falls back to page-level
    highlighting (open question ODQ-1).
    """

    document_id: str
    filename: str
    page: int = Field(ge=1)
    snippet: str
    score: float
    chunk_type: ChunkType = "text"
    #: Region to highlight in the previewer. Populated from Docling provenance — confirmed
    #: available in Phase 2, which resolves open question ODQ-1 in favour of pixel-accurate
    #: highlighting rather than page-level fallback.
    bbox: BoundingBox | None = None

    @property
    def label(self) -> str:
        """Chip text, e.g. ``aws-invoice.pdf · p.1``."""
        return f"{self.filename} · p.{self.page}"


class NumericCheck(_Base):
    """A figure asserted in an answer, checked against its grounding (FR-4.4).

    Two distinct questions are asked of every number the model states:

    - **Is it supported?** Does it match a field of the extracted record, or appear
      verbatim in a retrieved snippet? A figure supported by neither was produced by the
      model rather than read from a source, which is precisely what Rule 5 forbids.
    - **Does it contradict?** Where it names a record field, does the value agree?

    An unsupported figure is the more serious finding, and the earlier version of this
    model could not express it — a figure matching no field reported ``is_consistent``
    as ``True``.
    """

    claimed: Decimal
    #: Record field this figure matches, e.g. ``"total_amount"``. ``None`` if it matches none.
    matched_field: str | None = None
    expected: Decimal | None = None
    #: True when the figure appears verbatim in a retrieved chunk (e.g. a policy threshold).
    found_in_context: bool = False

    @property
    def is_supported(self) -> bool:
        """True when this figure was read from somewhere rather than produced."""
        return self.matched_field is not None or self.found_in_context

    @property
    def contradicts_record(self) -> bool:
        """True when the figure names a record field but disagrees with its value."""
        if self.expected is None:
            return False
        return abs(self.claimed - self.expected) > AMOUNT_TOLERANCE


class Answer(_Base):
    """A completed assistant turn, with everything the UI needs to render trust signals."""

    question: str
    text: str
    citations: list[Citation] = Field(default_factory=list)
    #: Everything retrieval returned, not only what the model chose to cite.
    #:
    #: These are different sets and conflating them corrupts evaluation. Ragas'
    #: context_precision and context_recall are *retrieval* metrics: scoring them against
    #: citations measures which chunks the model referenced, and since an answer typically
    #: cites one or two of ten retrieved chunks, recall against a full reference answer is
    #: near-zero by construction. Faithfulness has the same problem — judged against two
    #: snippets, a correct answer looks unfaithful because the supporting facts were never
    #: shown to the judge.
    retrieved: list[Citation] = Field(default_factory=list)
    numeric_checks: list[NumericCheck] = Field(default_factory=list)
    model: str
    latency_seconds: float = Field(ge=0.0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

    #: Citation markers the model emitted that matched no retrieved chunk. A non-empty
    #: list means the model invented a source, which is worth surfacing loudly.
    dropped_citations: list[str] = Field(default_factory=list)
    refused: bool = False

    @property
    def is_grounded(self) -> bool:
        """False triggers the 'unverified' warning strip in the chat panel (FR-4.1)."""
        return bool(self.citations)

    @property
    def unsupported_figures(self) -> list[NumericCheck]:
        """Figures found in neither the record nor the retrieved context (Rule 5)."""
        return [check for check in self.numeric_checks if not check.is_supported]

    @property
    def contradicting_figures(self) -> list[NumericCheck]:
        """Figures that disagree with the extracted record — surfaced inline (FR-4.4)."""
        return [check for check in self.numeric_checks if check.contradicts_record]

    @property
    def is_trustworthy(self) -> bool:
        """Everything the UI needs to decide whether to show a warning strip."""
        return (
            (self.is_grounded or self.refused)
            and not self.unsupported_figures
            and not self.contradicting_figures
            and not self.dropped_citations
        )


class RunStats(_Base):
    """Per-request telemetry powering the Observability Bar (design.md §5.4).

    Populated locally so latency and token counts are visible **without** a LangSmith
    account (decision D-11).
    """

    model: str
    parse_seconds: float = Field(default=0.0, ge=0.0)
    retrieve_seconds: float = Field(default=0.0, ge=0.0)
    generate_seconds: float = Field(default=0.0, ge=0.0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    #: True when the provider returned no usage metadata and counts were estimated from
    #: text length. The UI shows a "~" prefix so an estimate never reads as a measurement.
    tokens_estimated: bool = False
    chunks_retrieved: int = Field(default=0, ge=0)
    cache_hit: bool = False
    trace_url: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def total_seconds(self) -> float:
        return self.parse_seconds + self.retrieve_seconds + self.generate_seconds

    @property
    def estimated_cost_usd(self) -> Decimal:
        """Always ``0.00``. Rendered permanently in the UI — it is the project's thesis."""
        return Decimal("0.00")
