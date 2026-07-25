"""Tests for src.evals (phases.md Phase 5B).

The eval suite is itself code, and a broken evaluator is worse than none — it reports
confidence that was never measured. These tests cover the scoring logic and the Rule 1
guard on the judge, all offline.

The earlier version of this suite had two real bugs that these tests now pin down: it
checked LLM-only fields during a deterministic-only run, and it printed "PASS" while
individual fixtures were failing.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.config import EVALS_DIR, GOLDEN_SET_PATH
from src.evals import (
    GROQ_OPENAI_BASE_URL,
    TARGETS,
    GoldenItem,
    ItemResult,
    _verdict,
    grounding_summary,
    load_golden_set,
)
from src.schemas import Answer, Citation, NumericCheck

EXPECTATIONS_PATH = EVALS_DIR / "extraction_expectations.jsonl"


def _answer(
    *,
    refused: bool = False,
    citations: int = 1,
    unsupported: bool = False,
    contradicting: bool = False,
    dropped: bool = False,
) -> Answer:
    checks: list[NumericCheck] = []
    if unsupported:
        checks.append(NumericCheck(claimed=Decimal("77.31")))
    if contradicting:
        checks.append(
            NumericCheck(
                claimed=Decimal("528.40"), matched_field="total_amount", expected=Decimal("501.27")
            )
        )
    return Answer(
        question="q",
        text="answer text",
        citations=[
            Citation(document_id="d", filename="f.pdf", page=1, snippet="s", score=0.9)
            for _ in range(citations)
        ],
        numeric_checks=checks,
        dropped_citations=["[made_up.pdf:9]"] if dropped else [],
        refused=refused,
        model="llama-3.3-70b-versatile",
        latency_seconds=1.0,
    )


def _result(kind: str, **kwargs: object) -> ItemResult:
    item = GoldenItem(id="x", kind=kind, question="q", reference="r")  # type: ignore[arg-type]
    return ItemResult(item=item, answer=_answer(**kwargs))  # type: ignore[arg-type]


# ── Golden set integrity ─────────────────────────────────────────────────────


def test_golden_set_meets_the_minimum_size() -> None:
    """FR-6.1 requires at least 20 triples."""
    assert len(load_golden_set()) >= 20


def test_golden_set_covers_every_question_type() -> None:
    kinds = {item.kind for item in load_golden_set()}
    assert kinds == {"document", "policy", "cross_document", "refusal"}


def test_golden_set_includes_refusal_cases() -> None:
    """Without these, a system that answers everything confidently scores perfectly."""
    refusals = [item for item in load_golden_set() if item.kind == "refusal"]
    assert len(refusals) >= 3


def test_every_golden_item_has_a_reference() -> None:
    for item in load_golden_set():
        assert item.reference.strip(), f"{item.id} has no reference answer"
        assert item.question.strip()


def test_golden_item_ids_are_unique() -> None:
    ids = [item.id for item in load_golden_set()]
    assert len(ids) == len(set(ids))


def test_document_scoped_items_name_a_document() -> None:
    for item in load_golden_set():
        if item.kind in ("document", "refusal"):
            assert item.document_id, f"{item.id} is document-scoped but names no document"


def test_limit_truncates_the_golden_set() -> None:
    assert len(load_golden_set(limit=5)) == 5


def test_golden_set_is_valid_jsonl() -> None:
    for number, line in enumerate(GOLDEN_SET_PATH.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            json.loads(line)  # raises with the offending line number in context
            assert "reference" in json.loads(line), f"line {number} has no reference"


def test_extraction_expectations_are_valid_and_decimal_safe() -> None:
    for line in EXPECTATIONS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        Decimal(row["total_amount"])  # must parse exactly
        assert len(row["line_item_amounts"]) == row["line_item_count"]
        assert row["validation_state"] in ("validated", "mismatch", "incomplete")


def test_expectations_include_the_unbalanced_fixture() -> None:
    """The mismatch case must be an expectation, not an accident."""
    rows = [
        json.loads(line)
        for line in EXPECTATIONS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mismatched = [r for r in rows if r["validation_state"] == "mismatch"]
    assert len(mismatched) == 1
    assert mismatched[0]["fixture"] == "unbalanced_invoice.pdf"


# ── Grounding summary ────────────────────────────────────────────────────────


def test_clean_run_reports_no_problems() -> None:
    results = [_result("document"), _result("policy"), _result("refusal", refused=True, citations=0)]
    summary = grounding_summary(results)
    assert summary["citation_rate"] == 1.0
    assert summary["false_refusal_rate"] == 0.0
    assert summary["missed_refusal_rate"] == 0.0
    assert summary["unsupported_figure_rate"] == 0.0


def test_false_refusal_is_detected() -> None:
    """The check that stops 'refuse everything' from scoring perfectly."""
    results = [_result("document", refused=True), _result("document")]
    assert grounding_summary(results)["false_refusal_rate"] == 0.5


def test_missed_refusal_is_detected() -> None:
    """Answering a question that should have been refused is the opposite failure."""
    results = [_result("refusal", refused=False)]
    assert grounding_summary(results)["missed_refusal_rate"] == 1.0


def test_refusals_are_excluded_from_the_citation_rate() -> None:
    """A refusal legitimately has no citations and must not count against grounding."""
    results = [_result("refusal", refused=True, citations=0), _result("document", citations=2)]
    assert grounding_summary(results)["citation_rate"] == 1.0


def test_uncited_answer_lowers_the_citation_rate() -> None:
    results = [_result("document", citations=0), _result("document", citations=1)]
    summary = grounding_summary(results)
    assert summary["citation_rate"] == 0.5
    assert summary["uncited"] == ["x"]


def test_unsupported_figure_is_reported() -> None:
    summary = grounding_summary([_result("document", unsupported=True)])
    assert summary["unsupported_figure_rate"] == 1.0
    assert "77.31" in summary["unsupported"][0]


def test_contradicting_figure_is_reported() -> None:
    assert grounding_summary([_result("document", contradicting=True)])["contradiction_rate"] == 1.0


def test_invented_citation_is_reported() -> None:
    summary = grounding_summary([_result("document", dropped=True)])
    assert summary["invented_citation_rate"] == 1.0


def test_errored_items_are_counted_separately() -> None:
    errored = ItemResult(item=GoldenItem(id="e", kind="document", question="q", reference="r"))
    errored.error = "rate limited"
    summary = grounding_summary([errored, _result("document")])
    assert summary["errored"] == 1
    assert summary["answered"] == 1


def test_empty_run_does_not_divide_by_zero() -> None:
    summary = grounding_summary([])
    assert summary["answered"] == 0
    assert summary["false_refusal_rate"] == 0.0


# ── Verdicts ─────────────────────────────────────────────────────────────────


def test_verdict_passes_at_the_target() -> None:
    assert _verdict("faithfulness", TARGETS["faithfulness"]) == "PASS"


def test_verdict_fails_below_the_target() -> None:
    assert _verdict("faithfulness", TARGETS["faithfulness"] - 0.01) == "FAIL"


def test_verdict_reports_unmeasured_metrics_as_na() -> None:
    """A metric that failed to run must not silently read as a pass."""
    assert _verdict("faithfulness", None) == "n/a"


def test_field_accuracy_has_a_target() -> None:
    """Regression: extraction fixtures could FAIL while the run summary printed PASS."""
    assert "field_accuracy" in TARGETS


def test_prd_targets_are_encoded_faithfully() -> None:
    assert TARGETS["faithfulness"] == 0.85
    assert TARGETS["answer_relevancy"] == 0.80
    assert TARGETS["context_recall"] == 0.85
    assert TARGETS["total_amount_accuracy"] == 0.95
    assert TARGETS["line_item_recall"] == 0.90


# ── Rule 1 ───────────────────────────────────────────────────────────────────


def test_judge_endpoint_is_groq_not_openai() -> None:
    """The judge must never be able to reach a paid provider (Rule 1, D-26)."""
    assert GROQ_OPENAI_BASE_URL.startswith("https://api.groq.com")
    assert "openai.com" not in GROQ_OPENAI_BASE_URL


def test_judge_construction_refuses_without_a_groq_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import AssistantError
    from src.evals import _build_judge

    import src.config as config

    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setattr(config.Settings, "groq_configured", property(lambda self: False))
    with pytest.raises(AssistantError, match="GROQ_API_KEY"):
        _build_judge()
