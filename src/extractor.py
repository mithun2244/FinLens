"""Turn a :class:`ParsedDocument` into a validated :class:`FinancialRecord` (Phase 2).

The extraction strategy is deliberately lopsided, because the two halves of the problem
have different failure modes:

**Numbers are extracted deterministically.** Line items come from Docling's table
structure; subtotal, tax, and total come from labelled-amount regexes over the document
text. No language model proposes a figure out of thin air.

**Metadata is extracted by the LLM.** Vendor name, dates, document type, and invoice
number are prose-like and vary wildly in presentation — exactly what a model is good at
and a regex is not.

**The seam between them is guarded.** The model *may* supply an amount the regexes
missed, but any monetary value it returns must appear verbatim in the document text or it
is discarded with a warning (:func:`_amount_appears_in_document`). This is the mechanical
form of Rule 5: the model can read, it cannot invent.

Nothing here repairs an inconsistency. A document whose figures do not reconcile produces
a record with warnings attached, never adjusted numbers (decision D-12).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from src import ExtractionError
from src.config import get_settings
from src.llm import get_chat_model, invoke_with_retry, llm_available
from src.schemas import DocumentType, FinancialRecord, LineItem, ParsedDocument, TableBlock

logger = logging.getLogger(__name__)

__all__ = ["extract_record"]

# ── Column-name vocabulary for line-item tables ──────────────────────────────

_COLUMN_ROLES: dict[str, tuple[str, ...]] = {
    "description": (
        "description", "item", "details", "particulars", "service", "product",
        "merchant", "transaction", "activity", "narrative",
    ),
    "quantity": ("qty", "quantity", "units", "hours", "count", "usage"),
    "unit_price": ("unit price", "unit cost", "unit", "rate", "price", "each", "per unit"),
    "amount": ("amount", "line total", "total", "charge", "value", "cost", "debit", "sum"),
}

# ── Money ────────────────────────────────────────────────────────────────────

#: A labelled total. Requires two decimal places, which is what separates a real amount
#: from a rate ("8.5%"), a quantity, or a date fragment.
_MONEY = r"[-(]?\s*(?:USD|EUR|GBP|CAD|AUD|\$|€|£)?\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?"

#: Comma used as a decimal separator ("1.234,56" or "462,00"). Out of scope (prd.md §7),
#: and critically it must be REJECTED rather than mis-parsed: stripping the comma would
#: silently turn 1.234,56 into 1.23456. Matches a comma followed by one or two digits
#: that are not themselves followed by a digit, which never occurs in a thousands group.
_DECIMAL_COMMA_RE = re.compile(r",\d{1,2}(?!\d)")

_SUBTOTAL_RE = re.compile(rf"\bsub[\s\-]*total\b[^\d]{{0,40}}({_MONEY})", re.I)
_TOTAL_RE = re.compile(
    rf"\b(?:total\s+due|amount\s+due|grand\s+total|balance\s+due|total\s+amount|total)\b"
    rf"[^\d]{{0,40}}({_MONEY})",
    re.I,
)
_TAX_RE = re.compile(
    rf"\b(sales\s+tax|value[\s\-]*added\s+tax|vat|gst|hst|tax|levy|duty)\b"
    rf"\s*(?:\(?\s*([\d.]+)\s*%\s*\)?)?[^\d]{{0,40}}({_MONEY})",
    re.I,
)
_CURRENCY_CODE_RE = re.compile(r"\b(USD|EUR|GBP|CAD|AUD|JPY|CHF|INR|SGD)\b")
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}

_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d",
)


def _to_decimal(raw: str | None) -> Decimal | None:
    """Parse a currency string into ``Decimal``. Returns ``None`` rather than guessing.

    Handles thousands separators, currency symbols and codes, and the accounting
    parenthesis convention for negatives. Comma-as-decimal-separator (``1.234,56``) is
    **not** supported — prd.md §7 scopes v1 to Latin-script, period-decimal documents,
    and silently mis-parsing such a value would be far worse than declining it.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    if _DECIMAL_COMMA_RE.search(text):
        return None

    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[(),\s]", "", text)
    cleaned = re.sub(r"(?i)USD|EUR|GBP|CAD|AUD|JPY|CHF|INR|SGD", "", cleaned)
    cleaned = cleaned.replace("$", "").replace("€", "").replace("£", "").replace("¥", "").replace("₹", "")
    if cleaned.endswith("-"):
        cleaned, negative = cleaned[:-1], True
    if not cleaned or not re.fullmatch(r"-?\d*\.?\d+", cleaned):
        return None

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative and value > 0 else value


def _amount_appears_in_document(value: Decimal, text: str) -> bool:
    """True when this figure is actually written in the document.

    The guard on LLM-proposed amounts. Comparison is on the digits, so ``1,284.50`` in
    the document matches ``1284.50`` from the model, but a figure the model computed or
    imagined matches nothing.
    """
    plain = f"{abs(value):,.2f}"
    without_separators = plain.replace(",", "")
    haystack = text.replace(" ", "")
    return plain.replace(" ", "") in haystack or without_separators in haystack


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ── Line items (deterministic, from table structure) ─────────────────────────


def _normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", header.lower()).strip()


def _map_columns(headers: list[str]) -> dict[str, str | None]:
    """Map table columns onto line-item roles.

    Roles are assigned in a fixed order and each header is consumed once, so a table with
    both ``Unit Price`` and ``Amount`` cannot bind both to the same column.
    """
    available = {header: _normalize_header(header) for header in headers}
    mapping: dict[str, str | None] = {role: None for role in _COLUMN_ROLES}

    for role in ("description", "quantity", "unit_price", "amount"):
        keys = _COLUMN_ROLES[role]
        chosen: str | None = None
        for key in keys:  # exact match wins over substring
            for header, normalized in available.items():
                if normalized == key:
                    chosen = header
                    break
            if chosen:
                break
        if chosen is None:
            for key in keys:
                for header, normalized in available.items():
                    if key in normalized:
                        chosen = header
                        break
                if chosen:
                    break
        if chosen is not None:
            mapping[role] = chosen
            available.pop(chosen, None)
    return mapping


def _is_line_item_table(mapping: dict[str, str | None]) -> bool:
    """A line-item table needs something to name a charge and something to price it."""
    return mapping["description"] is not None and mapping["amount"] is not None


#: A "<description> <amount>" pair inside a run of text. Used only when no table was
#: detected — receipts and OCR output frequently have no table structure at all.
#: The description must start with a letter and cannot contain '%' or '#', which keeps
#: rates ("8.5%") and reference numbers ("#4471-02") out of the description.
_TEXT_LINE_ITEM_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 .,'&/()\-]*?)\s+((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})\b"
)

#: Labels that mark a summary row rather than a purchased item.
_NON_ITEM_LABELS: tuple[str, ...] = (
    "subtotal", "sub total", "total", "tax", "vat", "gst", "hst", "balance", "amount due",
    "payment", "paid", "change", "cash", "card", "visa", "mastercard", "amex", "approved",
    "tender", "due", "gratuity", "service charge", "rounding",
)


def _looks_like_summary_row(description: str) -> bool:
    lowered = description.lower().strip()
    return any(label in lowered for label in _NON_ITEM_LABELS)


def _extract_line_items_from_text(parsed: ParsedDocument) -> list[LineItem]:
    """Recover line items from narrative text when no table was detected.

    Receipts — especially OCR'd ones — often have no ruled table for TableFormer to find,
    so the structural path yields nothing. This scans for ``description amount`` pairs and
    drops anything that reads as a summary row.

    Confidence is deliberately low (0.55): this is pattern-matching over prose, not
    structure, and the UI shows it as a hollow low-confidence dot so the user knows to
    check it (design.md §5.2).
    """
    items: list[LineItem] = []
    for page in parsed.pages:
        # Narrative only: this path runs when no table was found, but reading the combined
        # markdown would risk re-parsing serialized table rows as if they were prose.
        for raw_line in page.narrative_markdown.splitlines():
            line = raw_line.strip().lstrip("#").strip()
            if not line or line.startswith("|"):
                continue
            for match in _TEXT_LINE_ITEM_RE.finditer(line):
                description = match.group(1).strip(" .,-")
                amount = _to_decimal(match.group(2))
                if amount is None or len(description) < 2:
                    continue
                if _looks_like_summary_row(description):
                    continue
                items.append(
                    LineItem(
                        description=description,
                        amount=amount,
                        source_page=page.page_number,
                        confidence=0.55,
                    )
                )
    return items


def _extract_line_items(parsed: ParsedDocument) -> tuple[list[LineItem], list[str]]:
    """Pull line items straight out of Docling's table rows.

    Confidence is lower for OCR-derived pages: the row structure is the same, but the
    characters inside it were inferred from pixels rather than read from a text layer.
    """
    items: list[LineItem] = []
    warnings: list[str] = []
    base_confidence = 0.75 if parsed.used_ocr else 0.95

    tables: list[TableBlock] = [
        table for table in parsed.tables if _is_line_item_table(_map_columns(table.headers))
    ]
    if not tables and parsed.tables:
        warnings.append(
            f"Found {len(parsed.tables)} table(s), but none had recognisable "
            f"description and amount columns. Line items may be incomplete."
        )

    if not tables:
        # No usable table anywhere — fall back to reading items out of the text.
        text_items = _extract_line_items_from_text(parsed)
        if text_items:
            warnings.append(
                f"No line-item table was detected, so {len(text_items)} item(s) were read "
                f"from the document text instead. Please verify them against the original."
            )
        return text_items, warnings

    for table in tables:
        mapping = _map_columns(table.headers)
        for row in table.rows:
            description = (row.get(mapping["description"] or "", "") or "").strip()
            amount = _to_decimal(row.get(mapping["amount"] or ""))
            if not description or amount is None:
                continue  # header repeat, spacer, or a subtotal row inside the table

            items.append(
                LineItem(
                    description=description,
                    quantity=_to_decimal(row.get(mapping["quantity"] or "")),
                    unit_price=_to_decimal(row.get(mapping["unit_price"] or "")),
                    amount=amount,
                    source_page=table.page_number,
                    confidence=base_confidence,
                )
            )

    for item in items:
        implied = item.implied_amount
        if implied is not None and abs(implied - item.amount) > Decimal("0.02"):
            warnings.append(
                f"Line '{item.description[:40]}': quantity x unit price = {implied}, "
                f"but the stated amount is {item.amount}."
            )
    return items, warnings


# ── Totals (deterministic, from labelled amounts) ────────────────────────────


def _extract_totals(text: str) -> dict[str, Any]:
    """Find subtotal, tax lines, and total by their labels."""
    found: dict[str, Any] = {"subtotal": None, "total_amount": None, "tax_lines": []}

    subtotal_match = _SUBTOTAL_RE.search(text)
    if subtotal_match:
        found["subtotal"] = _to_decimal(subtotal_match.group(1))

    # Prefer the last total on the document — statements repeat running totals, and the
    # final one is the amount actually owed.
    total_matches = list(_TOTAL_RE.finditer(text))
    if total_matches:
        found["total_amount"] = _to_decimal(total_matches[-1].group(1))

    seen: set[tuple[str, Decimal]] = set()
    for match in _TAX_RE.finditer(text):
        label = match.group(1).strip().title()
        rate = _to_decimal(match.group(2))
        amount = _to_decimal(match.group(3))
        if amount is None:
            continue
        if (label, amount) in seen:
            continue
        seen.add((label, amount))
        found["tax_lines"].append(
            {"label": label, "rate": rate / 100 if rate is not None else None, "amount": amount}
        )
    return found


def _detect_currency(text: str) -> str:
    match = _CURRENCY_CODE_RE.search(text)
    if match:
        return match.group(1).upper()
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return "USD"


def _guess_document_type(text: str, filename: str) -> DocumentType:
    haystack = f"{filename} {text[:2000]}".lower()
    if "expense report" in haystack or "expense claim" in haystack:
        return "expense_report"
    if "statement" in haystack:
        return "statement"
    if "receipt" in haystack:
        return "receipt"
    return "invoice"


# ── Metadata (LLM, with every amount verified against the document) ──────────


class _HeaderFields(BaseModel):
    """What the model is asked for. Amounts are strings so they can be verified first."""

    vendor_name: str | None = Field(default=None, description="Company that issued the document")
    vendor_address: str | None = None
    document_type: Literal["invoice", "statement", "receipt", "expense_report"] | None = None
    invoice_number: str | None = None
    billing_date: str | None = Field(default=None, description="ISO-8601 (YYYY-MM-DD)")
    due_date: str | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    currency: str | None = Field(default=None, description="ISO-4217 code, e.g. USD")
    subtotal: str | None = Field(default=None, description="Only if printed on the document")
    total_amount: str | None = Field(default=None, description="Only if printed on the document")


_SYSTEM_PROMPT = """You extract header metadata from financial documents.

Rules you must follow exactly:
- Report only what is written in the document. Never calculate, infer, or estimate.
- If a field is not present, return null for it. Never guess a plausible value.
- Dates must be ISO-8601 (YYYY-MM-DD). If a date is ambiguous, return null.
- Amounts must be copied character-for-character as they appear. Never total anything up.
- vendor_name is the party that ISSUED the document, not the recipient.

A null is always better than a guess. Downstream validation depends on it."""


def _extract_header_fields(text: str) -> tuple[_HeaderFields | None, list[str]]:
    """Ask the utility model for header metadata. Degrades to ``None`` on any failure."""
    warnings: list[str] = []
    if not llm_available():
        return None, ["No Groq API key configured — vendor and dates were not extracted."]

    excerpt = text[:6000]
    try:
        model = get_chat_model("reasoning").with_structured_output(_HeaderFields)
        # Retried: Groq intermittently returns a 400 `tool_use_failed` when the model
        # emits malformed tool arguments. It succeeds on retry (see src/llm.py).
        result = invoke_with_retry(
            model.invoke,
            [
                ("system", _SYSTEM_PROMPT),
                ("human", f"Extract the header fields from this document:\n\n{excerpt}"),
            ],
        )
    except Exception as exc:  # noqa: BLE001 - extraction must survive a model failure
        logger.warning("Header extraction failed: %s", type(exc).__name__)
        return None, [f"Automatic metadata extraction was unavailable: {exc}"]

    if not isinstance(result, _HeaderFields):
        return None, ["The model returned an unexpected shape for header fields."]
    return result, warnings


def _verified_amount(raw: str | None, text: str, field: str, warnings: list[str]) -> Decimal | None:
    """Accept a model-proposed amount only if it is written in the document (Rule 5)."""
    value = _to_decimal(raw)
    if value is None:
        return None
    if not _amount_appears_in_document(value, text):
        warnings.append(
            f"A proposed {field} of {value} was discarded because it does not appear "
            f"in the document text."
        )
        logger.warning("Rejected unverifiable %s proposed by the model", field)
        return None
    return value


# ── Public API ───────────────────────────────────────────────────────────────


def extract_record(parsed: ParsedDocument, *, use_llm: bool = True) -> FinancialRecord:
    """Build a validated :class:`FinancialRecord` from a parsed document.

    Args:
        parsed: Output of :func:`src.parser.parse_document`.
        use_llm: Set ``False`` to run fully offline. Line items and totals still extract
            deterministically; vendor and dates will be absent with a warning.

    Returns:
        A ``FinancialRecord`` with ``extraction_warnings`` populated by FR-2.4 arithmetic
        validation. **Warnings are never resolved by adjusting a figure** (decision D-12).

    Raises:
        ExtractionError: The document yielded no usable content at all.
    """
    text = parsed.markdown or "\n".join(page.markdown for page in parsed.pages)
    if not text.strip():
        raise ExtractionError(
            f"No text could be extracted from {parsed.filename}. If it is a scan, check "
            f"that the image is legible and right-way-up."
        )

    warnings: list[str] = []
    line_items, item_warnings = _extract_line_items(parsed)
    warnings.extend(item_warnings)

    totals = _extract_totals(text)
    subtotal: Decimal | None = totals["subtotal"]
    total_amount: Decimal | None = totals["total_amount"]
    tax_lines = [
        {"label": tax["label"], "rate": tax["rate"], "amount": tax["amount"]}
        for tax in totals["tax_lines"]
    ]

    header: _HeaderFields | None = None
    if use_llm:
        header, header_warnings = _extract_header_fields(text)
        warnings.extend(header_warnings)
    else:
        warnings.append(
            "Metadata extraction was skipped (offline mode) — vendor, dates, and document "
            "reference were not read. Amounts are unaffected."
        )

    # The model may fill a gap the regexes left, but only with a figure it can point to.
    if header is not None:
        if total_amount is None:
            total_amount = _verified_amount(header.total_amount, text, "total", warnings)
        if subtotal is None:
            subtotal = _verified_amount(header.subtotal, text, "subtotal", warnings)

    currency = (header.currency if header and header.currency else None) or _detect_currency(text)
    document_type: DocumentType = (
        header.document_type
        if header and header.document_type
        else _guess_document_type(text, parsed.filename)
    )
    vendor_name = (header.vendor_name if header else None) or _fallback_vendor(parsed)

    record = FinancialRecord(
        document_id=parsed.document_id,
        filename=parsed.filename,
        vendor_name=vendor_name,
        vendor_address=header.vendor_address if header else None,
        document_type=document_type,
        invoice_number=header.invoice_number if header else None,
        billing_date=_parse_date(header.billing_date) if header else None,
        due_date=_parse_date(header.due_date) if header else None,
        billing_period_start=_parse_date(header.billing_period_start) if header else None,
        billing_period_end=_parse_date(header.billing_period_end) if header else None,
        currency=currency[:3].upper(),
        line_items=line_items,
        subtotal=subtotal,
        tax_lines=tax_lines,  # type: ignore[arg-type]
        total_amount=total_amount,
        page_count=parsed.page_count,
        used_vision_fallback=parsed.used_ocr,
        extraction_warnings=warnings,
    )

    record.validate_arithmetic()  # FR-2.4 — reports, never repairs
    logger.info(
        "document_id=%s extracted: %d line items, %d tax lines, total_present=%s, warnings=%d",
        record.document_id,
        len(record.line_items),
        len(record.tax_lines),
        record.total_amount is not None,
        len(record.extraction_warnings),
    )
    return record


def _fallback_vendor(parsed: ParsedDocument) -> str:
    """Best-effort vendor name when the model is unavailable.

    Uses the first substantial line of the document, which is where an issuer's name
    almost always sits. Deliberately weak and honest about it — this is a placeholder the
    user can correct in the UI, not a claim.
    """
    for line in (parsed.markdown or "").splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if len(cleaned) >= 3 and not cleaned.startswith("|"):
            return cleaned[:120]
    return "Unknown vendor"
