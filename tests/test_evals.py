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

from src import RateLimitError
from src.config import EVALS_DIR, GOLDEN_SET_PATH
from src.evals import (
    GROQ_OPENAI_BASE_URL,
    TARGETS,
    GoldenItem,
    ItemResult,
    _verdict,
    evaluate_answers,
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
    assert summary["false_refusal_rate"] is None


# ── Unmeasured rates must not read as clean ones ─────────────────────────────


def test_empty_run_reports_rates_as_unmeasured_not_zero() -> None:
    """Regression: an empty run reported a flawless grounding scorecard.

    Every rate divided by an empty set and came back 0% (or 100% for citations), which
    is indistinguishable from a genuinely clean run in the printed summary.
    """
    summary = grounding_summary([])
    for key in (
        "citation_rate",
        "false_refusal_rate",
        "missed_refusal_rate",
        "unsupported_figure_rate",
        "contradiction_rate",
        "invented_citation_rate",
    ):
        assert summary[key] is None, f"{key} claimed a value over zero answers"


def test_all_errored_run_reports_rates_as_unmeasured() -> None:
    """The shape of the run that hid the q16 bug: 24 questions, none answered."""
    answers = [_errored() for _ in range(24)]
    summary = grounding_summary(answers)

    assert summary["answered"] == 0
    assert summary["errored"] == 24
    assert summary["citation_rate"] is None
    assert summary["unsupported_figure_rate"] is None


def test_an_empty_run_never_prints_a_passing_scorecard() -> None:
    """The property that matters: nothing measured must never read as everything fine."""
    from src.evals import print_report

    for answers in ([], [_errored() for _ in range(24)]):
        report = print_report([], answers, grounding_summary(answers), 1.0)
        grounding = report["grounding"]
        assert grounding["citation_rate"] is None
        assert grounding["unsupported_figure_rate"] is None
        # No grounding failure may be claimed either — there is nothing to claim it from.
        assert "unsupported_figures" not in report["below_target"]
        assert "uncited_answers" not in report["below_target"]


def test_vacuous_denominator_is_not_reported_as_a_perfect_rate() -> None:
    """A --kinds refusal run answers plenty but has no non-refusal items.

    It used to report a 100% citation rate and a 0% false refusal rate over zero
    applicable answers, which reads as two passes that were never measured.
    """
    refusals_only = [_result("refusal", refused=True, citations=0) for _ in range(4)]
    summary = grounding_summary(refusals_only)

    assert summary["answered"] == 4
    assert summary["refusal_answered"] == 4
    assert summary["non_refusal_answered"] == 0
    assert summary["missed_refusal_rate"] == 0.0, "this one *was* measured"
    assert summary["citation_rate"] is None
    assert summary["false_refusal_rate"] is None


def test_unmeasured_rates_are_printed_as_na() -> None:
    """The summary must say n/a, not a number, for a rate with no denominator."""
    from src.evals import _grounding_line, _pct

    assert _pct(None) == "n/a"
    assert _pct(0.0) == "0.0%"

    unmeasured = _grounding_line("unsupported figure rate", None, 0, "0%")
    assert "n/a" in unmeasured and "n=0" in unmeasured
    assert "0.0%" not in unmeasured, "an unmeasured rate must not render as a clean 0%"

    measured = _grounding_line("unsupported figure rate", 0.0, 24, "0%")
    assert "0.0%" in measured and "n=24" in measured


def test_the_grounding_line_always_shows_its_denominator() -> None:
    """The rate alone cannot separate 'clean over 24' from 'nothing measured'."""
    assert "n=24" in _grounding_line_for(0.0, 24)
    assert "n=0" in _grounding_line_for(None, 0)


def _grounding_line_for(value: float | None, denominator: int) -> str:
    from src.evals import _grounding_line

    return _grounding_line("citation rate", value, denominator, "100%")


def test_a_measured_clean_run_still_reports_real_rates() -> None:
    """The guard must not turn genuine passes into n/a."""
    results = [_result("document"), _result("policy"), _result("refusal", refused=True, citations=0)]
    summary = grounding_summary(results)

    assert summary["citation_rate"] == 1.0
    assert summary["false_refusal_rate"] == 0.0
    assert summary["missed_refusal_rate"] == 0.0
    assert summary["unsupported_figure_rate"] == 0.0


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


# ── The verdict must never claim more than was measured ──────────────────────


def _errored(kind: str = "document") -> ItemResult:
    item = GoldenItem(id="e", kind=kind, question="q", reference="r")  # type: ignore[arg-type]
    result = ItemResult(item=item)
    result.error = "The local search index is corrupt and cannot be read."
    return result


def test_a_run_where_everything_errored_cannot_pass() -> None:
    """Regression: 28/28 errored on a corrupt store and the suite printed
    "PASS - all targets met". Every grounding rate was vacuously 0 and every Ragas
    mean was None, so nothing tripped the verdict."""
    from src.evals import print_report

    answers = [_errored() for _ in range(28)]
    report = print_report([], answers, grounding_summary(answers), 1.0)

    assert report["below_target"], "a run that measured nothing must not pass"
    assert any("errored_items" in name for name in report["below_target"])


def test_unmeasured_ragas_metrics_fail_the_run() -> None:
    """A metric that failed to run must not be silently treated as absent."""
    from src.evals import print_report

    answers = [_result("document")]  # answered, but never scored
    report = print_report([], answers, grounding_summary(answers), 1.0)

    assert any("unmeasured:" in name for name in report["below_target"])


def test_extraction_only_run_does_not_demand_ragas_metrics() -> None:
    """`--extraction` legitimately skips scoring, so absent metrics are not a failure."""
    from src.evals import print_report

    report = print_report([], [], grounding_summary([]), 1.0)
    assert not any("unmeasured:" in name for name in report["below_target"])


def test_the_new_guards_do_not_misfire_on_a_scored_run() -> None:
    """The guard must not be so strict that a genuinely measured run cannot pass.

    Extraction targets are excluded here — this call passes no extraction results, so
    those legitimately read as 0%. What matters is that neither new guard fires.
    """
    from src.evals import print_report

    good = _result("document")
    good.faithfulness = 0.95
    good.answer_relevancy = 0.90
    good.context_precision = 0.95
    good.context_recall = 0.95
    report = print_report([], [good], grounding_summary([good]), 1.0)

    assert not any("errored_items" in name for name in report["below_target"])
    assert not any("unmeasured:" in name for name in report["below_target"])


# ── --kinds filter ───────────────────────────────────────────────────────────


def test_kinds_filter_selects_only_that_kind() -> None:
    items = load_golden_set(kinds=["refusal"])
    assert items
    assert {i.kind for i in items} == {"refusal"}


def test_kinds_filter_accepts_several_kinds() -> None:
    items = load_golden_set(kinds=["policy", "cross_document", "refusal"])
    assert {i.kind for i in items} == {"policy", "cross_document", "refusal"}
    assert "document" not in {i.kind for i in items}


def test_kinds_filter_partitions_the_golden_set_exactly() -> None:
    """Every item belongs to exactly one slice — no double-counting, no gaps."""
    from src.evals import ITEM_KINDS

    total = len(load_golden_set())
    partitioned = sum(len(load_golden_set(kinds=[k])) for k in ITEM_KINDS)
    assert partitioned == total


def test_unknown_kind_is_rejected_with_the_valid_options() -> None:
    from src import AssistantError

    with pytest.raises(AssistantError, match="Unknown question kind"):
        load_golden_set(kinds=["nonsense"])


def test_kinds_and_limit_compose() -> None:
    assert len(load_golden_set(kinds=["policy"], limit=2)) == 2


def test_no_kinds_returns_everything() -> None:
    assert len(load_golden_set(kinds=None)) == len(load_golden_set())


def test_kinds_flag_is_actually_wired_into_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: --kinds was parsed but never passed to load_golden_set.

    The flag was accepted in silence and the run evaluated all 28 questions anyway,
    which is worse than rejecting it — an argument that appears to work but does
    nothing produces confident, wrongly-scoped results.
    """
    import src.evals as evals

    seen: dict[str, object] = {}

    def _capture(path=None, limit=None, kinds=None):  # type: ignore[no-untyped-def]
        seen["kinds"] = kinds
        seen["limit"] = limit
        return []

    monkeypatch.setattr(evals, "load_golden_set", _capture)
    monkeypatch.setattr(evals, "build_index", lambda **_: {})
    monkeypatch.setattr(evals, "evaluate_extraction", lambda *_a, **_k: [])
    monkeypatch.setattr(evals, "print_report", lambda *a, **k: {"below_target": []})
    monkeypatch.setattr(evals, "configure_logging", lambda: None)

    evals.main(["--kinds", "policy,refusal", "--no-score"])

    assert seen["kinds"] == ["policy", "refusal"], "the flag must reach load_golden_set"


def test_kinds_flag_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--kinds "policy, refusal"` must work as well as the unspaced form."""
    import src.evals as evals

    seen: dict[str, object] = {}

    def _capture(path=None, limit=None, kinds=None):  # type: ignore[no-untyped-def]
        seen["kinds"] = kinds
        return []

    monkeypatch.setattr(evals, "load_golden_set", _capture)
    monkeypatch.setattr(evals, "build_index", lambda **_: {})
    monkeypatch.setattr(evals, "evaluate_extraction", lambda *_a, **_k: [])
    monkeypatch.setattr(evals, "print_report", lambda *a, **k: {"below_target": []})
    monkeypatch.setattr(evals, "configure_logging", lambda: None)

    evals.main(["--kinds", "policy, refusal", "--no-score"])
    assert seen["kinds"] == ["policy", "refusal"]


# ── Daily-limit abort (2026-07-27) ───────────────────────────────────────────
#
# A sweep hit Groq's daily token budget on q02 and then spent 22 more requests
# collecting the identical 429. These pin the stop, and pin that stopping does not
# quietly shrink the report's denominator.


def _golden(n: int) -> list[GoldenItem]:
    return [
        GoldenItem(id=f"q{i:02d}", kind="document", question="q?", reference="r")
        for i in range(1, n + 1)
    ]


def test_daily_limit_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 asking for minutes stops the run instead of burning the remaining items."""
    import asyncio

    import src.evals as evals

    calls: list[str] = []

    def _fake(question, *, record=None, document_id=None):  # type: ignore[no-untyped-def]
        calls.append(question)
        if len(calls) == 1:
            return _answer()
        raise RateLimitError("daily budget gone", retry_after_seconds=960.0)

    monkeypatch.setattr(evals, "answer_question", _fake)
    results = asyncio.run(evaluate_answers(_golden(24), {}, score=False, pace=0))

    assert len(calls) == 2, "must stop at the first daily limit, not retry or continue"
    assert len(results) == 24, "unreached questions stay in the report"
    assert results[0].answer is not None
    assert results[1].error == "daily budget gone"
    assert all(r.error == evals.NOT_ATTEMPTED for r in results[2:])


def test_stopped_run_still_counts_unreached_items_as_errors() -> None:
    """The abort must not let a run that measured 1 of 24 read as a clean scorecard."""
    import src.evals as evals

    results = [ItemResult(item=_golden(1)[0], answer=_answer())]
    results += [ItemResult(item=i, error=evals.NOT_ATTEMPTED) for i in _golden(23)]

    summary = grounding_summary(results)

    assert summary["answered"] == 1
    assert summary["errored"] == 23, "unreached items are unmeasured, not passed"


def test_short_rate_limit_is_waited_out_not_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tokens-per-minute 429 clears in seconds, so it must cost a pause, not the item."""
    import asyncio

    import src.evals as evals

    attempts: list[int] = []

    def _fake(question, *, record=None, document_id=None):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimitError("per-minute", retry_after_seconds=0.01)
        return _answer()

    monkeypatch.setattr(evals, "answer_question", _fake)
    results = asyncio.run(evaluate_answers(_golden(1), {}, score=False, pace=0))

    assert len(attempts) == 2, "the short wait must be retried"
    assert results[0].answer is not None and results[0].error is None
