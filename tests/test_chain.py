"""Tests for src.chain (phases.md Phase 4 DoD).

The verification layer — citation resolution and numeric cross-checking — is pure and
tested exhaustively offline. That layer is what makes grounding an enforced property
rather than a prompt instruction, so it is the part that most needs tests that do not
depend on what a model happened to say.

Live-model behaviour (refusal, citation, streaming) is covered by ``integration`` tests,
excluded from the default run.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.chain import (
    ChatTurn,
    _format_record,
    _labelled_field,
    _looks_like_refusal,
    cross_check_numbers,
    parse_citations,
    stream_answer,
    suggested_prompts,
)
from src.config import FIXTURES_DIR
from src.schemas import Citation, FinancialRecord, LineItem, TaxLine


def _citation(filename: str, page: int, snippet: str = "", score: float = 0.9) -> Citation:
    return Citation(
        document_id=filename.replace(".", "-"),
        filename=filename,
        page=page,
        snippet=snippet,
        score=score,
    )


@pytest.fixture
def record() -> FinancialRecord:
    return FinancialRecord(
        document_id="aws-current",
        filename="clean_invoice.pdf",
        vendor_name="Amazon Web Services, Inc.",
        document_type="invoice",
        currency="USD",
        line_items=[
            LineItem(description="EC2 t3.medium instance-hours", amount=Decimal("30.66"), source_page=1),
            LineItem(description="NAT Gateway data processing (GB)", amount=Decimal("412.90"), source_page=1),
            LineItem(description="S3 Standard storage", amount=Decimal("18.44"), source_page=1),
        ],
        subtotal=Decimal("462.00"),
        tax_lines=[TaxLine(label="Sales Tax", rate=Decimal("0.085"), amount=Decimal("39.27"))],
        total_amount=Decimal("501.27"),
    )


# ── Citation resolution ──────────────────────────────────────────────────────


def test_citations_resolve_against_retrieved_chunks() -> None:
    available = [_citation("clean_invoice.pdf", 1), _citation("cloud_billing_policy.md", 1)]
    text = "The charge is NAT Gateway data processing [clean_invoice.pdf:1]."
    resolved, dropped = parse_citations(text, available)
    assert len(resolved) == 1
    assert resolved[0].filename == "clean_invoice.pdf"
    assert dropped == []


def test_invented_citations_are_dropped_and_reported() -> None:
    """A marker pointing at a source we never retrieved means the model made it up."""
    available = [_citation("clean_invoice.pdf", 1)]
    text = "Per your contract [master_agreement.pdf:7], this is standard."
    resolved, dropped = parse_citations(text, available)
    assert resolved == []
    assert dropped == ["[master_agreement.pdf:7]"]


def test_citation_to_a_page_that_was_not_retrieved_is_dropped() -> None:
    available = [_citation("clean_invoice.pdf", 1)]
    _, dropped = parse_citations("See [clean_invoice.pdf:9].", available)
    assert dropped == ["[clean_invoice.pdf:9]"]


def test_repeated_citations_are_deduplicated() -> None:
    available = [_citation("clean_invoice.pdf", 1)]
    text = "A [clean_invoice.pdf:1] and B [clean_invoice.pdf:1] and C [clean_invoice.pdf:1]."
    resolved, _ = parse_citations(text, available)
    assert len(resolved) == 1


def test_citation_parsing_accepts_page_prefix_forms() -> None:
    available = [_citation("clean_invoice.pdf", 3)]
    for form in ("[clean_invoice.pdf:3]", "[clean_invoice.pdf: p.3]", "[clean_invoice.pdf:p3]"):
        resolved, dropped = parse_citations(f"See {form}.", available)
        assert len(resolved) == 1, f"failed on {form}"
        assert dropped == []


def test_citation_matching_is_case_insensitive() -> None:
    available = [_citation("Clean_Invoice.pdf", 1)]
    resolved, _ = parse_citations("See [clean_invoice.pdf:1].", available)
    assert len(resolved) == 1


def test_resolved_citations_carry_bbox_for_the_previewer() -> None:
    """Citations come from retrieval, so they keep the geometry the store returned (D-16)."""
    from src.schemas import BoundingBox

    hit = _citation("clean_invoice.pdf", 1)
    hit = hit.model_copy(update={"bbox": BoundingBox(left=0.1, top=0.2, right=0.9, bottom=0.4)})
    resolved, _ = parse_citations("See [clean_invoice.pdf:1].", [hit])
    assert resolved[0].bbox is not None


def test_text_with_no_citations_resolves_to_nothing() -> None:
    resolved, dropped = parse_citations("The total is high.", [_citation("a.pdf", 1)])
    assert resolved == [] and dropped == []


# ── Numeric cross-checking ───────────────────────────────────────────────────


def test_figure_matching_a_record_field_is_supported(record: FinancialRecord) -> None:
    checks = cross_check_numbers("The total is 501.27 USD.", record, [])
    assert checks[0].is_supported
    assert not checks[0].contradicts_record


def test_figure_contradicting_the_record_is_flagged(record: FinancialRecord) -> None:
    """FR-4.4: the model says the total is 528.40, the record says 501.27."""
    checks = cross_check_numbers("Your total is 528.40 USD.", record, [])
    contradicting = [c for c in checks if c.contradicts_record]
    assert len(contradicting) == 1
    assert contradicting[0].claimed == Decimal("528.40")
    assert contradicting[0].expected == Decimal("501.27")


def test_line_item_amounts_are_supported(record: FinancialRecord) -> None:
    checks = cross_check_numbers("NAT Gateway data processing was 412.90.", record, [])
    assert all(c.is_supported for c in checks)


def test_figure_from_a_policy_document_is_supported_by_context(
    record: FinancialRecord,
) -> None:
    """A policy threshold is legitimate even though it is in no record field."""
    context = [_citation("cloud_billing_policy.md", 1, "alert threshold is USD 200.00 per month")]
    checks = cross_check_numbers("Your policy caps this at 200.00 per month.", record, context)
    check = next(c for c in checks if c.claimed == Decimal("200.00"))
    assert check.found_in_context
    assert check.is_supported


def test_invented_figure_is_unsupported(record: FinancialRecord) -> None:
    """The core Rule 5 check: a number in neither the record nor the context."""
    checks = cross_check_numbers("You were also charged 77.31 for support.", record, [])
    unsupported = [c for c in checks if not c.is_supported]
    assert Decimal("77.31") in [c.claimed for c in unsupported]


def test_computed_totals_are_recognised_as_supported(record: FinancialRecord) -> None:
    checks = cross_check_numbers("Line items and tax come to 501.27.", record, [])
    assert all(c.is_supported for c in checks)


def test_rates_and_quantities_are_not_treated_as_amounts(record: FinancialRecord) -> None:
    """'8.5%' and '730 hours' must not be checked as money."""
    checks = cross_check_numbers("Tax is 8.5% on 730 instance-hours.", record, [])
    assert Decimal("730") not in [c.claimed for c in checks]


def test_figures_are_deduplicated(record: FinancialRecord) -> None:
    checks = cross_check_numbers("501.27 and again 501.27 and 501.27.", record, [])
    assert len(checks) == 1


def test_labelled_field_detection_finds_the_nearest_label() -> None:
    text = "The subtotal is 462.00 and the total is 501.27"
    assert _labelled_field(text, text.index("462.00")) == "subtotal"
    assert _labelled_field(text, text.index("501.27")) == "total_amount"


def test_subtotal_label_is_not_read_as_total() -> None:
    """'subtotal' contains 'total'; a substring search reports every subtotal as a total."""
    text = "This amount includes a subtotal of 462.00"
    assert _labelled_field(text, text.index("462.00")) == "subtotal"


def test_stated_subtotal_is_not_flagged_as_contradicting_the_total(
    record: FinancialRecord,
) -> None:
    """Regression: the answer 'total is 501.27 ... includes a subtotal of 462.00' was
    reporting 462.00 as a contradicting total."""
    text = "The total is 501.27 USD. This amount includes a subtotal of 462.00 USD."
    checks = cross_check_numbers(text, record, [])
    assert [c for c in checks if c.contradicts_record] == []


def test_figure_from_another_document_is_not_a_contradiction(
    record: FinancialRecord,
) -> None:
    """Multi-document questions: a figure about the prior invoice is grounded in its own
    retrieved chunk, and must not be judged against the active document's record."""
    context = [_citation("prior_invoice.pdf", 1, "Total amount: 98.03 USD\nSubtotal: 90.35 USD")]
    text = "The total on the previous invoice was 98.03 USD [prior_invoice.pdf:1]."
    checks = cross_check_numbers(text, record, context)
    assert [c for c in checks if c.contradicts_record] == []
    assert all(c.is_supported for c in checks)


def test_contradiction_still_fires_when_the_figure_is_nowhere(
    record: FinancialRecord,
) -> None:
    """The guard must not become so permissive that real contradictions slip through."""
    checks = cross_check_numbers("Your total is 528.40 USD.", record, [])
    assert len([c for c in checks if c.contradicts_record]) == 1


def test_cross_check_without_a_record_still_uses_context() -> None:
    context = [_citation("policy.md", 1, "capped at 200.00 per night")]
    checks = cross_check_numbers("The cap is 200.00.", None, context)
    assert checks[0].found_in_context


# ── Refusal detection ────────────────────────────────────────────────────────


def test_refusal_phrase_is_detected() -> None:
    assert _looks_like_refusal("I cannot determine this from the provided documents.")


def test_normal_answer_is_not_a_refusal() -> None:
    assert not _looks_like_refusal("The charge is NAT Gateway data processing of 412.90.")


def test_a_trailing_caveat_is_not_a_refusal() -> None:
    """Regression: a cited, substantive answer ending in a caveat was flagged as refused."""
    text = (
        "The charge is NAT Gateway data processing of 412.90 USD [clean_invoice.pdf:1]. "
        "Your billing policy explains it is billed per GB [cloud_billing_policy.md:1]. "
        "The invoice details do not appear in the provided documents beyond this line."
    )
    assert not _looks_like_refusal(text)


def test_refusal_at_the_start_is_still_detected() -> None:
    text = (
        "I cannot determine this from the provided documents. "
        "To answer, I would need the company's payroll records."
    )
    assert _looks_like_refusal(text)


# ── Prompt assembly ──────────────────────────────────────────────────────────


def test_record_is_serialized_with_string_amounts(record: FinancialRecord) -> None:
    """Decimals must reach the prompt as exact strings, never as floats (D-6)."""
    payload = _format_record(record)
    assert '"total_amount": "501.27"' in payload
    assert "501.27000" not in payload


def test_record_serialization_includes_validation_state(record: FinancialRecord) -> None:
    assert "validation_state" in _format_record(record)


def test_missing_record_is_stated_not_faked() -> None:
    assert "no structured record" in _format_record(None)


# ── Quick-prompt chips ───────────────────────────────────────────────────────


def test_chips_are_generated_from_the_record(record: FinancialRecord) -> None:
    chips = suggested_prompts(record, has_policies=True)
    assert any("AWS" in chip for chip in chips)
    assert any("tax" in chip.lower() for chip in chips)
    assert any("policy" in chip.lower() for chip in chips)


def test_chips_name_the_largest_line_item(record: FinancialRecord) -> None:
    chips = suggested_prompts(record)
    assert any("NAT Gateway" in chip for chip in chips)


def test_chips_offer_to_explain_a_mismatch(record: FinancialRecord) -> None:
    broken = record.model_copy(update={"total_amount": Decimal("528.40")})
    assert any("add up" in chip for chip in suggested_prompts(broken))


def test_chips_without_a_record_are_generic() -> None:
    assert suggested_prompts(None)


def test_policy_chip_is_absent_without_a_policy_corpus(record: FinancialRecord) -> None:
    assert not any("policy" in chip.lower() for chip in suggested_prompts(record, has_policies=False))


# ── Guard rails ──────────────────────────────────────────────────────────────


def test_empty_question_is_rejected_without_calling_a_model() -> None:
    events = list(stream_answer("   "))
    assert len(events) == 1
    assert events[0].type == "error"


def test_error_events_carry_a_user_facing_message() -> None:
    event = next(iter(stream_answer("")))
    assert event.message
    assert "Traceback" not in event.message


class _ExplodingModel:
    """A chat model whose stream fails partway, the way a real 429 does."""

    def __init__(self, message: str) -> None:
        self._message = message

    def stream(self, *_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        yield type("Chunk", (), {"content": "The total is "})()
        raise RuntimeError(self._message)


def _patch_chain(monkeypatch: pytest.MonkeyPatch, model: object) -> None:
    import src.chain as chain

    monkeypatch.setattr(chain, "llm_available", lambda: True)
    monkeypatch.setattr(
        chain,
        "retrieve_with_policies",
        lambda *a, **k: ([_citation("clean_invoice.pdf", 1, "Total amount: 501.27")], []),
    )
    monkeypatch.setattr(chain, "get_chat_model", lambda *a, **k: model)


def test_rate_limit_mid_stream_becomes_an_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a provider 429 raised while iterating escaped untranslated and crashed
    the caller. Streaming cannot use invoke_with_translation, so the stream loop needs its
    own translation."""
    message = (
        "Error code: 429 - Rate limit reached for model `llama-3.3-70b-versatile` on "
        "tokens per day (TPD): Limit 100000. Please try again in 1m48.864s."
    )
    _patch_chain(monkeypatch, _ExplodingModel(message))

    events = list(stream_answer("What is the total?"))
    assert events[-1].type == "error"
    assert "rate limit" in (events[-1].message or "").lower()


def test_error_event_never_leaks_a_stack_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_chain(monkeypatch, _ExplodingModel("Error code: 429 - rate_limit_exceeded"))
    message = list(stream_answer("What is the total?"))[-1].message or ""
    for leak in ("Traceback", "RuntimeError", "File \"", "groq._base_client"):
        assert leak not in message


def test_unexpected_provider_failure_is_still_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any provider exception, not just rate limits, must surface as an error event."""
    _patch_chain(monkeypatch, _ExplodingModel("connection reset by peer"))
    events = list(stream_answer("What is the total?"))
    assert events[-1].type == "error"
    assert events[-1].message


# ── Live model (excluded from the default run) ───────────────────────────────


@pytest.fixture(scope="module")
def indexed() -> FinancialRecord:
    """Index the AWS invoices and the policy corpus into the real collections."""
    from src.config import COLLECTION_DOCUMENTS, COLLECTION_POLICIES
    from src.extractor import extract_record
    from src.parser import parse_document
    from src.vectorstore import ingest_document, ingest_policy_files, reset_collection

    reset_collection(COLLECTION_DOCUMENTS)
    reset_collection(COLLECTION_POLICIES)
    current: FinancialRecord | None = None
    for name, doc_id in (("clean_invoice.pdf", "aws-current"), ("prior_invoice.pdf", "aws-prior")):
        parsed = parse_document(FIXTURES_DIR / name, document_id=doc_id, persist_source=False)
        extracted = extract_record(parsed, use_llm=False)
        ingest_document(parsed, extracted)
        if doc_id == "aws-current":
            current = extracted
    ingest_policy_files(sorted((FIXTURES_DIR / "policies").glob("*.md")))
    assert current is not None
    return current


@pytest.mark.integration
def test_answer_is_cited(indexed: FinancialRecord) -> None:
    from src.chain import answer_question

    answer = answer_question(
        "Why was the NAT Gateway charge deducted?", record=indexed, document_id="aws-current"
    )
    assert answer.is_grounded
    assert answer.citations


@pytest.mark.integration
def test_answer_states_no_unsupported_figures(indexed: FinancialRecord) -> None:
    from src.chain import answer_question

    answer = answer_question(
        "What is the total on this invoice and what is the tax?",
        record=indexed,
        document_id="aws-current",
    )
    assert answer.unsupported_figures == []
    assert answer.contradicting_figures == []


@pytest.mark.integration
def test_model_refuses_when_the_answer_is_absent(indexed: FinancialRecord) -> None:
    """FR-4.2: a refusal is a correct answer; a guess is not."""
    from src.chain import answer_question

    answer = answer_question(
        "What was the CEO's salary last year?", record=indexed, document_id="aws-current"
    )
    assert answer.refused or not answer.unsupported_figures


@pytest.mark.integration
def test_tokens_stream_incrementally(indexed: FinancialRecord) -> None:
    tokens = [
        event.token
        for event in stream_answer(
            "What is the total?", record=indexed, document_id="aws-current"
        )
        if event.type == "token"
    ]
    assert len(tokens) > 1, "response arrived in a single chunk — it did not stream"


@pytest.mark.integration
def test_stages_are_reported_before_tokens(indexed: FinancialRecord) -> None:
    types = [
        event.type
        for event in stream_answer("What is the total?", record=indexed, document_id="aws-current")
    ]
    assert types[0] == "stage"
    assert types[-1] == "answer"


@pytest.mark.integration
def test_followup_resolves_against_history(indexed: FinancialRecord) -> None:
    """Multi-turn: 'the month before' must retrieve the prior invoice."""
    from src.chain import answer_question

    history = [
        ChatTurn(role="user", content="What is the total on the current AWS invoice?"),
        ChatTurn(role="assistant", content="The total is 501.27 USD [clean_invoice.pdf:1]."),
    ]
    answer = answer_question("And what was it the month before?", record=indexed, history=history)
    assert "98.03" in answer.text or answer.refused
