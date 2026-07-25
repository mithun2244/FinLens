"""Tests for src.extractor (phases.md Phase 2 DoD).

Default runs are **free and offline**: every test here uses ``use_llm=False`` or exercises
pure helpers, so the deterministic half of extraction — which is the half that produces
numbers — is fully covered without a single API call.

Tests that need the live model are marked ``integration`` and excluded from the default
run (rules.md Rule 2.4).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src import ExtractionError
from src.config import FIXTURES_DIR
from src.extractor import (
    _amount_appears_in_document,
    _extract_totals,
    _looks_like_summary_row,
    _map_columns,
    _to_decimal,
    extract_record,
)
from src.parser import parse_document
from src.schemas import FinancialRecord, ParsedDocument

CLEAN = FIXTURES_DIR / "clean_invoice.pdf"
UNBALANCED = FIXTURES_DIR / "unbalanced_invoice.pdf"
MULTIPAGE = FIXTURES_DIR / "multipage_statement.pdf"
SCANNED = FIXTURES_DIR / "scanned_receipt.png"


# ── Pure helpers: fast, no Docling, no network ───────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30.66", Decimal("30.66")),
        ("$1,284.50", Decimal("1284.50")),
        ("1,284.50 USD", Decimal("1284.50")),
        ("€412.90", Decimal("412.90")),
        ("(30.66)", Decimal("-30.66")),
        ("30.66-", Decimal("-30.66")),
        ("  412.9000  ", Decimal("412.9000")),
        ("730", Decimal("730")),
    ],
)
def test_to_decimal_parses_currency_forms(raw: str, expected: Decimal) -> None:
    assert _to_decimal(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "n/a", "-", "abc", "1.234,56"])
def test_to_decimal_declines_rather_than_guesses(raw: str | None) -> None:
    """Comma-decimal and junk both return None — a wrong number is worse than no number."""
    assert _to_decimal(raw) is None


def test_to_decimal_never_returns_float() -> None:
    """Decimal only (D-6). A float here would corrupt every downstream total."""
    assert isinstance(_to_decimal("501.27"), Decimal)


def test_map_columns_recognises_standard_invoice_headers() -> None:
    mapping = _map_columns(["Description", "Qty", "Unit Price", "Amount"])
    assert mapping["description"] == "Description"
    assert mapping["quantity"] == "Qty"
    assert mapping["unit_price"] == "Unit Price"
    assert mapping["amount"] == "Amount"


def test_map_columns_does_not_bind_one_header_to_two_roles() -> None:
    """'Unit Price' must not also satisfy 'amount' via its 'price' substring."""
    mapping = _map_columns(["Item", "Unit Price", "Total"])
    assert mapping["unit_price"] == "Unit Price"
    assert mapping["amount"] == "Total"
    assert mapping["unit_price"] != mapping["amount"]


def test_map_columns_handles_alternative_vocabulary() -> None:
    mapping = _map_columns(["Merchant", "Units", "Rate", "Charge"])
    assert mapping["description"] == "Merchant"
    assert mapping["amount"] == "Charge"


def test_amount_verification_accepts_figures_present_in_the_document() -> None:
    text = "Subtotal 462.00 Sales Tax 39.27 TOTAL DUE 1,284.50"
    assert _amount_appears_in_document(Decimal("462.00"), text)
    assert _amount_appears_in_document(Decimal("1284.50"), text)  # separators normalized


def test_amount_verification_rejects_figures_the_model_invented() -> None:
    """The guard behind Rule 5: a computed or imagined figure matches nothing."""
    text = "Subtotal 462.00 Sales Tax 39.27"
    assert not _amount_appears_in_document(Decimal("501.27"), text)


def test_summary_rows_are_not_line_items() -> None:
    for label in ("Subtotal", "TOTAL DUE", "Sales Tax", "Balance Due", "VISA", "Change"):
        assert _looks_like_summary_row(label)
    for label in ("Coffee beans 1kg", "EC2 t3.medium instance-hours", "Oat milk x3"):
        assert not _looks_like_summary_row(label)


def test_extract_totals_reads_labelled_amounts() -> None:
    text = "Subtotal\n\n462.00\n\nSales Tax (8.5%)\n\n39.27\n\nTOTAL DUE 501.27"
    totals = _extract_totals(text)
    assert totals["subtotal"] == Decimal("462.00")
    assert totals["total_amount"] == Decimal("501.27")
    assert totals["tax_lines"][0]["amount"] == Decimal("39.27")
    assert totals["tax_lines"][0]["rate"] == Decimal("0.085")


def test_extract_totals_does_not_mistake_a_rate_for_an_amount() -> None:
    """'8.5%' must never be read as $8.50 — hence the two-decimal requirement."""
    totals = _extract_totals("Sales Tax 8.5% 3.02")
    assert totals["tax_lines"][0]["amount"] == Decimal("3.02")


def test_extract_totals_prefers_the_final_total() -> None:
    """Statements carry running totals; the last one is what is actually owed."""
    totals = _extract_totals("Total 100.00\nmore charges\nTotal Due 250.00")
    assert totals["total_amount"] == Decimal("250.00")


def test_subtotal_label_does_not_satisfy_the_total_pattern() -> None:
    assert _extract_totals("Subtotal 462.00")["total_amount"] is None


# ── End-to-end against fixtures (Docling, but no LLM) ────────────────────────


@pytest.fixture(scope="module")
def clean_record() -> FinancialRecord:
    parsed = parse_document(CLEAN, document_id="x-clean", persist_source=False)
    return extract_record(parsed, use_llm=False)


@pytest.fixture(scope="module")
def unbalanced_record() -> FinancialRecord:
    parsed = parse_document(UNBALANCED, document_id="x-unbal", persist_source=False)
    return extract_record(parsed, use_llm=False)


@pytest.fixture(scope="module")
def statement_record() -> FinancialRecord:
    parsed = parse_document(MULTIPAGE, document_id="x-multi", persist_source=False)
    return extract_record(parsed, use_llm=False)


@pytest.fixture(scope="module")
def receipt_record() -> FinancialRecord:
    parsed = parse_document(SCANNED, document_id="x-receipt", persist_source=False)
    return extract_record(parsed, use_llm=False)


pytestmark_slow = pytest.mark.slow


@pytest.mark.slow
def test_clean_invoice_extracts_every_line_item(clean_record: FinancialRecord) -> None:
    assert len(clean_record.line_items) == 3
    assert {item.amount for item in clean_record.line_items} == {
        Decimal("30.66"), Decimal("412.90"), Decimal("18.44")
    }


@pytest.mark.slow
def test_clean_invoice_totals_are_exact(clean_record: FinancialRecord) -> None:
    """FR-6 target is 95% total-amount accuracy; a digital PDF must be exact."""
    assert clean_record.subtotal == Decimal("462.00")
    assert clean_record.total_amount == Decimal("501.27")


@pytest.mark.slow
def test_clean_invoice_tax_line_carries_rate_and_amount(clean_record: FinancialRecord) -> None:
    assert len(clean_record.tax_lines) == 1
    tax = clean_record.tax_lines[0]
    assert tax.amount == Decimal("39.27")
    assert tax.rate == Decimal("0.085")


@pytest.mark.slow
def test_clean_invoice_reconciles(clean_record: FinancialRecord) -> None:
    assert clean_record.computed_total == clean_record.total_amount
    assert clean_record.validation_state == "validated"
    assert clean_record.is_validated


@pytest.mark.slow
def test_clean_invoice_quantities_and_unit_prices_survive(clean_record: FinancialRecord) -> None:
    nat = next(i for i in clean_record.line_items if "NAT Gateway" in i.description)
    assert nat.quantity == Decimal("1")
    assert nat.unit_price == Decimal("412.9000")


@pytest.mark.slow
def test_all_monetary_values_are_decimal(clean_record: FinancialRecord) -> None:
    for item in clean_record.line_items:
        assert isinstance(item.amount, Decimal)
    assert isinstance(clean_record.total_amount, Decimal)
    assert isinstance(clean_record.computed_total, Decimal)


# ── FR-2.4: mismatches are reported, never repaired (D-12) ───────────────────


@pytest.mark.slow
def test_unbalanced_invoice_is_flagged_as_mismatch(unbalanced_record: FinancialRecord) -> None:
    assert unbalanced_record.validation_state == "mismatch"
    assert not unbalanced_record.is_validated


@pytest.mark.slow
def test_unbalanced_invoice_keeps_both_figures_untouched(
    unbalanced_record: FinancialRecord,
) -> None:
    """The stated total stays wrong and the computed total stays right.

    This is the heart of decision D-12: the extractor must not quietly reconcile them.
    """
    assert unbalanced_record.total_amount == Decimal("528.40")  # as printed
    assert unbalanced_record.computed_total == Decimal("501.27")  # as computed


@pytest.mark.slow
def test_unbalanced_invoice_warning_states_the_difference(
    unbalanced_record: FinancialRecord,
) -> None:
    warnings = " ".join(unbalanced_record.extraction_warnings)
    assert "528.40" in warnings and "501.27" in warnings
    assert "27.13" in warnings


# ── Multi-page ───────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_statement_collects_line_items_from_both_pages(
    statement_record: FinancialRecord,
) -> None:
    assert len(statement_record.line_items) == 5
    assert {item.source_page for item in statement_record.line_items} == {1, 2}


@pytest.mark.slow
def test_statement_reconciles(statement_record: FinancialRecord) -> None:
    assert statement_record.total_amount == Decimal("1951.87")
    assert statement_record.validation_state == "validated"


# ── OCR path: text-derived line items ────────────────────────────────────────


@pytest.mark.slow
def test_receipt_recovers_items_without_a_table(receipt_record: FinancialRecord) -> None:
    """No table exists in the OCR output, so items come from the narrative fallback."""
    assert len(receipt_record.line_items) == 3
    descriptions = " ".join(i.description for i in receipt_record.line_items)
    assert "Coffee beans" in descriptions and "Sandwich" in descriptions


@pytest.mark.slow
def test_receipt_text_items_are_marked_low_confidence(receipt_record: FinancialRecord) -> None:
    """Pattern-matched items must not look as trustworthy as structural ones."""
    for item in receipt_record.line_items:
        assert item.confidence_band == "low"


@pytest.mark.slow
def test_receipt_excludes_summary_rows_from_line_items(
    receipt_record: FinancialRecord,
) -> None:
    amounts = {item.amount for item in receipt_record.line_items}
    assert Decimal("35.50") not in amounts  # subtotal
    assert Decimal("38.52") not in amounts  # total
    assert Decimal("3.02") not in amounts  # tax


@pytest.mark.slow
def test_receipt_reconciles_after_the_text_fallback(receipt_record: FinancialRecord) -> None:
    assert receipt_record.total_amount == Decimal("38.52")
    assert receipt_record.computed_total == Decimal("38.52")
    assert receipt_record.validation_state == "validated"


@pytest.mark.slow
def test_advisory_warning_does_not_flip_the_validation_banner(
    receipt_record: FinancialRecord,
) -> None:
    """A 'please verify' note must not render as a red arithmetic mismatch."""
    assert receipt_record.has_advisories
    assert receipt_record.is_validated


@pytest.mark.slow
def test_receipt_marks_the_ocr_path(receipt_record: FinancialRecord) -> None:
    assert receipt_record.used_vision_fallback is True


# ── Degradation without an LLM ───────────────────────────────────────────────


@pytest.mark.slow
def test_extraction_works_offline_and_says_what_is_missing(
    clean_record: FinancialRecord,
) -> None:
    """Numbers still extract without the model; metadata is absent, and it says so."""
    assert clean_record.total_amount == Decimal("501.27")
    assert len(clean_record.line_items) == 3
    assert any("offline mode" in w.lower() for w in clean_record.extraction_warnings)


@pytest.mark.slow
def test_offline_notice_does_not_affect_arithmetic_validation(
    clean_record: FinancialRecord,
) -> None:
    """Skipping metadata is advisory; the totals still reconcile and the banner is green."""
    assert clean_record.validation_state == "validated"


def test_thousands_without_separators_are_parsed() -> None:
    """'1951.87' must parse. Requiring a comma group would drop every 4-figure total."""
    totals = _extract_totals("TOTAL DUE 1951.87")
    assert totals["total_amount"] == Decimal("1951.87")


def test_thousands_with_separators_are_parsed() -> None:
    assert _extract_totals("TOTAL DUE 12,345.67")["total_amount"] == Decimal("12345.67")


@pytest.mark.slow
def test_empty_document_raises_extraction_error() -> None:
    empty = ParsedDocument(
        document_id="empty",
        filename="blank.pdf",
        source_path="blank.pdf",
        page_count=1,
        pages=[],
        markdown="",
    )
    with pytest.raises(ExtractionError, match="No text could be extracted"):
        extract_record(empty, use_llm=False)


# ── Live model (excluded from the default run) ───────────────────────────────


@pytest.mark.integration
def test_llm_extracts_vendor_and_dates() -> None:
    parsed = parse_document(CLEAN, document_id="i-clean", persist_source=False)
    record = extract_record(parsed, use_llm=True)
    assert "Amazon Web Services" in record.vendor_name
    assert record.invoice_number == "INV-7741820"
    assert str(record.billing_date) == "2026-07-01"


@pytest.mark.integration
def test_llm_does_not_override_deterministic_totals() -> None:
    """Numbers come from the document structure, not from the model."""
    parsed = parse_document(UNBALANCED, document_id="i-unbal", persist_source=False)
    record = extract_record(parsed, use_llm=True)
    assert record.total_amount == Decimal("528.40")
    assert record.computed_total == Decimal("501.27")
    assert record.validation_state == "mismatch"
