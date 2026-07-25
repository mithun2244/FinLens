"""Tests for ui.components (phases.md Phase 5A).

Every test here pins a bug found by actually opening the app in a browser. None of them
would have been caught by reading the code: the markup was valid, the numbers were real,
and nothing raised — the output was just wrong on screen.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.schemas import (
    Answer,
    BoundingBox,
    Citation,
    FinancialRecord,
    LineItem,
    NumericCheck,
    RunStats,
    TaxLine,
)
from ui.components import (
    _block,
    _percent,
    _unit_price,
    empty_workspace,
    line_item_table,
    observability_bar,
    render_answer_html,
    totals_panel,
    validation_banner,
    vendor_card,
    warning_strips,
)


@pytest.fixture
def record() -> FinancialRecord:
    return FinancialRecord(
        document_id="d1",
        filename="clean_invoice.pdf",
        vendor_name="Amazon Web Services, Inc.",
        document_type="invoice",
        currency="USD",
        line_items=[
            LineItem(
                description="EC2 t3.medium instance-hours",
                quantity=Decimal("730"),
                unit_price=Decimal("0.0420"),
                amount=Decimal("30.66"),
                source_page=1,
            ),
            LineItem(
                description="NAT Gateway data processing (GB)",
                quantity=Decimal("1"),
                unit_price=Decimal("412.9000"),
                amount=Decimal("412.90"),
                source_page=1,
                confidence=0.55,
            ),
            LineItem(
                description="S3 Standard storage",
                quantity=Decimal("1"),
                unit_price=Decimal("18.4400"),
                amount=Decimal("18.44"),
                source_page=1,
            ),
        ],
        subtotal=Decimal("462.00"),
        tax_lines=[TaxLine(label="Sales Tax", rate=Decimal("0.085"), amount=Decimal("39.27"))],
        total_amount=Decimal("501.27"),
    )


# ── Markdown/HTML interaction ────────────────────────────────────────────────


def test_block_removes_indentation() -> None:
    """Regression: indented HTML reached the page as literal `<div class=...>` text.

    Streamlit renders through markdown, and markdown turns any line indented four or
    more spaces into a code block.
    """
    result = _block("""
        <div class="x">
          <span>hi</span>
        </div>
    """)
    assert not any(line.startswith(" ") for line in result.splitlines())
    assert result.startswith("<div")


def test_no_rendered_block_is_markdown_indented(record: FinancialRecord) -> None:
    """Any four-space-indented line in emitted HTML becomes a visible code block."""
    blocks = [
        empty_workspace(),
        vendor_card(record),
        validation_banner(record),
        line_item_table(record.line_items, record.currency),
        totals_panel(record),
        observability_bar(RunStats(model="m"), model="m"),
    ]
    for markup in blocks:
        for line in markup.splitlines():
            assert not line.startswith("    "), f"indented line would render as code: {line!r}"


def test_advisory_notes_render_as_markup_not_text(record: FinancialRecord) -> None:
    """The specific block that leaked: the advisory list under the validation banner."""
    record.extraction_warnings.append("No Groq API key configured.")
    markup = validation_banner(record)
    assert "<li>No Groq API key configured.</li>" in markup
    for line in markup.splitlines():
        assert not line.startswith("    ")


# ── Number formatting ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.0420"), "0.042"),
        (Decimal("412.9000"), "412.90"),
        (Decimal("18.44"), "18.44"),
        (Decimal("1234.5678"), "1,234.5678"),
        (Decimal("2"), "2.00"),
    ],
)
def test_unit_price_keeps_precision(value: Decimal, expected: str) -> None:
    """Regression: 0.0420/hour rendered as '0.04', which is a different rate."""
    assert _unit_price(value) == expected


def test_unit_price_handles_missing_value() -> None:
    assert _unit_price(None) == "—"


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (Decimal("0.085"), "8.5"),
        (Decimal("0.20"), "20"),
        (Decimal("0.0825"), "8.25"),
    ],
)
def test_percent_is_not_zero_padded(rate: Decimal, expected: str) -> None:
    """Regression: the tax line read 'Sales Tax (8.500%)'."""
    assert _percent(rate) == expected


def test_tax_label_shows_a_clean_rate(record: FinancialRecord) -> None:
    assert "Sales Tax (8.5%)" in totals_panel(record)


def test_unit_price_appears_in_the_table(record: FinancialRecord) -> None:
    assert "0.042" in line_item_table(record.line_items, record.currency)


# ── Table and totals content ─────────────────────────────────────────────────


def test_amounts_are_rendered_with_tabular_alignment(record: FinancialRecord) -> None:
    markup = line_item_table(record.line_items, record.currency)
    assert markup.count('class="num"') >= len(record.line_items) * 3


def test_confidence_is_shape_and_colour_not_colour_alone(record: FinancialRecord) -> None:
    """design.md §7: state must never be conveyed by colour alone."""
    markup = line_item_table(record.line_items, record.currency)
    assert "fl-dot high" in markup
    assert "fl-dot low" in markup  # the 0.55-confidence row


def test_empty_line_items_get_an_explanation() -> None:
    markup = line_item_table([], "USD")
    assert "No line items detected" in markup


def test_totals_include_iso_currency_on_the_grand_total(record: FinancialRecord) -> None:
    assert "501.27 USD" in totals_panel(record)


def test_description_is_escaped() -> None:
    item = LineItem(description="<script>alert(1)</script>", amount=Decimal("1.00"), source_page=1)
    markup = line_item_table([item], "USD")
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


# ── Validation banner states ─────────────────────────────────────────────────


def test_validated_banner(record: FinancialRecord) -> None:
    markup = validation_banner(record)
    assert "fl-banner validated" in markup
    assert "✓" in markup


def test_mismatch_banner_states_the_difference(record: FinancialRecord) -> None:
    broken = record.model_copy(update={"total_amount": Decimal("528.40")})
    markup = validation_banner(broken)
    assert "fl-banner mismatch" in markup
    assert "528.40" in markup and "27.13" in markup


def test_incomplete_banner_does_not_invent_a_total(record: FinancialRecord) -> None:
    incomplete = record.model_copy(update={"total_amount": None})
    markup = validation_banner(incomplete)
    assert "fl-banner incomplete" in markup
    assert "Nothing has been assumed" in markup


# ── Observability bar ────────────────────────────────────────────────────────


def test_observability_marks_estimated_tokens() -> None:
    stats = RunStats(model="m", prompt_tokens=100, completion_tokens=50, tokens_estimated=True)
    assert "~150" in observability_bar(stats, model="m")


def test_observability_does_not_mark_measured_tokens() -> None:
    stats = RunStats(model="m", prompt_tokens=100, completion_tokens=50)
    markup = observability_bar(stats, model="m")
    assert "150 tokens" in markup and "~150" not in markup


def test_observability_always_shows_zero_cost() -> None:
    assert "$0.00" in observability_bar(RunStats(model="m"), model="m")


def test_observability_shows_a_rate_limit_countdown() -> None:
    markup = observability_bar(RunStats(model="m"), model="m", rate_limited=42.0)
    assert "rate limited" in markup and "42s" in markup


# ── Chat rendering ───────────────────────────────────────────────────────────


def _citation(page: int = 1) -> Citation:
    return Citation(
        document_id="d1",
        filename="clean_invoice.pdf",
        page=page,
        snippet="Total amount: 501.27",
        score=0.9,
        bbox=BoundingBox(left=0.1, top=0.2, right=0.9, bottom=0.3),
    )


def test_known_citation_becomes_a_chip() -> None:
    markup = render_answer_html("The total is 501.27 [clean_invoice.pdf:1].", [_citation()])
    assert 'class="fl-cite"' in markup


def test_unknown_citation_is_left_visible_not_deleted() -> None:
    """An invented reference must stay on screen rather than being silently swallowed."""
    markup = render_answer_html("Per [made_up.pdf:9] this is standard.", [_citation()])
    assert "made_up.pdf:9" in markup
    assert markup.count('class="fl-cite"') == 0


def test_answer_text_is_escaped() -> None:
    markup = render_answer_html("<img src=x onerror=alert(1)>", [])
    assert "<img" not in markup


def _answer(**kwargs: object) -> Answer:
    return Answer(question="q", text="t", model="m", latency_seconds=1.0, **kwargs)  # type: ignore[arg-type]


def test_uncited_answer_gets_a_warning_strip() -> None:
    assert "unverified" in warning_strips(_answer())


def test_refusal_gets_no_unverified_warning() -> None:
    assert warning_strips(_answer(refused=True)) == ""


def test_unsupported_figure_is_surfaced() -> None:
    answer = _answer(
        citations=[_citation()], numeric_checks=[NumericCheck(claimed=Decimal("77.31"))]
    )
    assert "77.31" in warning_strips(answer)


def test_contradicting_figure_is_surfaced() -> None:
    answer = _answer(
        citations=[_citation()],
        numeric_checks=[
            NumericCheck(
                claimed=Decimal("528.40"),
                matched_field="total_amount",
                expected=Decimal("501.27"),
            )
        ],
    )
    strips = warning_strips(answer)
    assert "528.40" in strips and "501.27" in strips


def test_invented_citation_is_surfaced() -> None:
    answer = _answer(citations=[_citation()], dropped_citations=["[made_up.pdf:9]"])
    assert "made_up.pdf:9" in warning_strips(answer)


def test_clean_answer_has_no_warning_strips() -> None:
    assert warning_strips(_answer(citations=[_citation()])) == ""
