"""Evaluation suite: Ragas metrics, extraction accuracy, and grounding checks (Phase 5B).

    python -m src.evals              # everything
    python -m src.evals --extraction # deterministic only, zero API calls
    python -m src.evals --limit 8    # quick pass over the first 8 golden items

Three layers, deliberately separate:

**Extraction accuracy** is deterministic — per-field comparison against
``evals/extraction_expectations.jsonl``. No judge, no tokens, no flakiness. Numbers are
the product, so they are measured exactly rather than scored by a model.

**Ragas metrics** (faithfulness, answer relevancy, context precision, context recall)
score the generated answers. Judge and embeddings are both pointed at our free stack —
see :func:`_build_judge` for how, and why the default had to be overridden.

**Grounding checks** are our own, and cover what Ragas does not. In particular
**false refusals**: a system that refuses everything scores perfectly on faithfulness
while being useless, so refusal correctness is measured in both directions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from src import AssistantError
from src.chain import answer_question
from src.config import (
    COLLECTION_DOCUMENTS,
    COLLECTION_POLICIES,
    EMBEDDING_MODEL,
    EVAL_REPORTS_DIR,
    EVALS_DIR,
    FIXTURES_DIR,
    GOLDEN_SET_PATH,
    UTILITY_MODEL,
    configure_logging,
    get_settings,
)
from src.extractor import extract_record
from src.parser import parse_document
from src.schemas import Answer, FinancialRecord
from src.vectorstore import ingest_document, ingest_policy_files, reset_collection

logger = logging.getLogger(__name__)

#: Groq's OpenAI-compatible endpoint. See _build_judge for why this is used.
GROQ_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"

EXPECTATIONS_PATH = EVALS_DIR / "extraction_expectations.jsonl"

#: Seconds to wait between model calls. Groq's free tier limits tokens per minute, and a
#: full run is ~130 calls of ~1,600 tokens each. Fired without pacing, the run stalls in
#: SDK retry backoff and never finishes (decision D-27).
DEFAULT_PACE = 2.0

#: Judge prompt trimming. The free tier allows 6,000 tokens per minute on the utility
#: model, and an untrimmed judge call requested ~1,745 — roughly three calls a minute.
#: Cutting the context keeps the judge informed while letting a full run finish.
MAX_CONTEXTS = 4
MAX_CONTEXT_CHARS = 500

#: How many times to retry a judge call that was rate limited.
JUDGE_ATTEMPTS = 4


def _retry_after_seconds(message: str) -> float | None:
    """Extract Groq's own "Please try again in 7.15s" hint from a 429 message."""
    if "rate_limit" not in message and "429" not in message:
        return None
    match = re.search(r"try again in\s*([\d.]+)\s*(ms|s)\b", message, re.I)
    if not match:
        return 10.0
    value = float(match.group(1))
    return value / 1000 if match.group(2).lower() == "ms" else value

ItemKind = Literal["document", "policy", "cross_document", "refusal"]

#: PRD §8 targets.
TARGETS: dict[str, float] = {
    "faithfulness": 0.85,
    "answer_relevancy": 0.80,
    "context_recall": 0.85,
    "context_precision": 0.80,
    "total_amount_accuracy": 0.95,
    "line_item_recall": 0.90,
    "field_accuracy": 0.95,
}

#: Fields only the LLM can supply. Skipped when extraction ran offline, so that a
#: deterministic-only run reports on what it actually attempted rather than marking
#: every fixture failed for fields it was never asked to produce.
_LLM_ONLY_FIELDS = ("invoice_number", "billing_date")


# ── Data ─────────────────────────────────────────────────────────────────────


@dataclass
class GoldenItem:
    id: str
    kind: ItemKind
    question: str
    reference: str
    document_id: str | None = None


@dataclass
class ItemResult:
    item: GoldenItem
    answer: Answer | None = None
    error: str | None = None
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    @property
    def refused_correctly(self) -> bool | None:
        """None when the answer failed; otherwise whether refusal matched expectation."""
        if self.answer is None:
            return None
        should_refuse = self.item.kind == "refusal"
        return self.answer.refused == should_refuse


@dataclass
class ExtractionResult:
    fixture: str
    fields_checked: int = 0
    fields_correct: int = 0
    total_correct: bool = False
    line_items_expected: int = 0
    line_items_matched: int = 0
    failures: list[str] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)


def load_golden_set(path: Path = GOLDEN_SET_PATH, limit: int | None = None) -> list[GoldenItem]:
    if not path.exists():
        raise AssistantError(f"Golden set not found at {path}.")
    items: list[GoldenItem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        items.append(
            GoldenItem(
                id=raw["id"],
                kind=raw["kind"],
                question=raw["question"],
                reference=raw["reference"],
                document_id=raw.get("document_id"),
            )
        )
    return items[:limit] if limit else items


# ── Index setup ──────────────────────────────────────────────────────────────

FIXTURE_IDS: tuple[tuple[str, str], ...] = (
    ("clean_invoice.pdf", "aws-current"),
    ("prior_invoice.pdf", "aws-prior"),
    ("multipage_statement.pdf", "statement"),
    ("scanned_receipt.png", "receipt"),
    ("unbalanced_invoice.pdf", "northwind"),
)


def build_index(*, use_llm: bool = True) -> dict[str, FinancialRecord]:
    """Parse, extract, and index every fixture. Returns records by ``document_id``."""
    reset_collection(COLLECTION_DOCUMENTS)
    reset_collection(COLLECTION_POLICIES)

    records: dict[str, FinancialRecord] = {}
    for filename, document_id in FIXTURE_IDS:
        parsed = parse_document(FIXTURES_DIR / filename, document_id=document_id, persist_source=False)
        record = extract_record(parsed, use_llm=use_llm)
        ingest_document(parsed, record)
        records[document_id] = record
        print(f"  indexed {filename:<26} {len(record.line_items)} items, total={record.total_amount}")

    policies = sorted((FIXTURES_DIR / "policies").glob("*.md"))
    print(f"  indexed {len(policies)} policy documents ({ingest_policy_files(policies)} chunks)")
    return records


# ── Extraction accuracy (deterministic, zero API calls) ──────────────────────


def _compare(name: str, actual: Any, expected: Any, result: ExtractionResult) -> None:
    result.fields_checked += 1
    if actual == expected:
        result.fields_correct += 1
    else:
        result.failures.append(f"{name}: expected {expected!r}, got {actual!r}")


def evaluate_extraction(
    records: dict[str, FinancialRecord], *, llm_fields: bool = True
) -> list[ExtractionResult]:
    """Per-field comparison against known-correct values. No model involved."""
    if not EXPECTATIONS_PATH.exists():
        raise AssistantError(f"Extraction expectations not found at {EXPECTATIONS_PATH}.")

    results: list[ExtractionResult] = []
    for line in EXPECTATIONS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected = json.loads(line)
        record = records.get(expected["document_id"])
        result = ExtractionResult(fixture=expected["fixture"])

        if record is None:
            result.failures.append("document was not indexed")
            results.append(result)
            continue

        _compare("vendor", expected["vendor_contains"].lower() in record.vendor_name.lower(), True, result)
        _compare("document_type", record.document_type, expected["document_type"], result)
        _compare("currency", record.currency, expected["currency"], result)
        _compare("validation_state", record.validation_state, expected["validation_state"], result)
        _compare(
            "subtotal",
            record.subtotal,
            Decimal(expected["subtotal"]) if expected["subtotal"] else None,
            result,
        )
        _compare(
            "tax_total", record.tax_total, Decimal(expected["tax_total"]), result
        )
        _compare("line_item_count", len(record.line_items), expected["line_item_count"], result)

        if llm_fields:
            if expected.get("invoice_number") is not None:
                _compare("invoice_number", record.invoice_number, expected["invoice_number"], result)
            if expected.get("billing_date") is not None:
                _compare("billing_date", str(record.billing_date), expected["billing_date"], result)
        elif any(expected.get(name) is not None for name in _LLM_ONLY_FIELDS):
            result.skipped_fields = list(_LLM_ONLY_FIELDS)

        expected_total = Decimal(expected["total_amount"])
        _compare("total_amount", record.total_amount, expected_total, result)
        result.total_correct = record.total_amount == expected_total

        wanted = [Decimal(amount) for amount in expected["line_item_amounts"]]
        actual_amounts = [item.amount for item in record.line_items]
        result.line_items_expected = len(wanted)
        result.line_items_matched = sum(1 for amount in wanted if amount in actual_amounts)
        if result.line_items_matched < result.line_items_expected:
            missing = [str(a) for a in wanted if a not in actual_amounts]
            result.failures.append(f"line items missing: {missing}")

        results.append(result)
    return results


# ── Ragas judge (Rule 1: free tier only) ─────────────────────────────────────


def _build_judge() -> Any:
    """Construct a Ragas judge backed by Groq.

    **Rule 1 / decision D-26.** Ragas defaults to a paid OpenAI judge, which must be
    overridden. The obvious route — ``llm_factory(provider="groq", client=Groq(...))`` —
    is broken in ragas 0.4.3: the instructor adapter mis-patches the Groq client and
    raises ``'Groq' object has no attribute 'messages'``.

    The working route uses the ``openai`` **client library** as an HTTP transport pointed
    at Groq's OpenAI-compatible endpoint. This is not OpenAI: the base URL is Groq, the
    credential is ``GROQ_API_KEY``, no request reaches OpenAI, and the cost is $0. The
    assertion below is the guard that keeps it that way.
    """
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory

    settings = get_settings()
    if not settings.groq_configured:
        raise AssistantError(
            "GROQ_API_KEY is required to run the Ragas metrics. Use --extraction to run "
            "the deterministic checks without it."
        )

    client = AsyncOpenAI(api_key=settings.groq_api_key, base_url=GROQ_OPENAI_BASE_URL)
    assert str(client.base_url).startswith(GROQ_OPENAI_BASE_URL), (
        "Judge client is not pointed at Groq — refusing to run and risk a paid call."
    )
    return llm_factory(UTILITY_MODEL, provider="openai", client=client)


def _build_judge_embeddings() -> Any:
    """Local MiniLM for the embedding-based metric. Never a paid embedding API."""
    from ragas.embeddings import HuggingFaceEmbeddings as RagasHFEmbeddings

    return RagasHFEmbeddings(model=EMBEDDING_MODEL, device="cpu")


async def _score_item(
    result: ItemResult, metrics: dict[str, Any], *, pace: float = DEFAULT_PACE
) -> None:
    """Score one answered item. A metric failure degrades to ``None``, never a crash."""
    answer = result.answer
    if answer is None:
        return

    # Trimmed to stay inside the free tier's tokens-per-minute budget. The judge needs
    # enough context to verify a claim, not the whole retrieval set (decision D-27).
    contexts = [
        citation.snippet[:MAX_CONTEXT_CHARS] for citation in answer.citations[:MAX_CONTEXTS]
    ] or ["(no context was cited)"]
    question, reference = result.item.question, result.item.reference

    async def safe(name: str, factory: Any) -> float | None:
        """Run one metric, honouring the server's own retry-after hint on a 429.

        Groq replies to a rate limit with "Please try again in 7.15s", and instructor
        gives up after a single attempt. Retrying on that hint is what turns a run that
        reports mostly ``None`` into one that finishes.
        """
        for attempt in range(1, JUDGE_ATTEMPTS + 1):
            try:
                outcome = await factory()
                return float(outcome.value) if outcome.value is not None else None
            except Exception as exc:  # noqa: BLE001 - classified by message below
                wait = _retry_after_seconds(str(exc))
                if wait is None or attempt == JUDGE_ATTEMPTS:
                    logger.warning(
                        "%s failed on %s: %s", name, result.item.id, type(exc).__name__
                    )
                    return None
                logger.info(
                    "%s rate limited on %s; waiting %.1fs (attempt %d/%d)",
                    name, result.item.id, wait, attempt, JUDGE_ATTEMPTS,
                )
                await asyncio.sleep(wait + 1.0)
            finally:
                if pace:
                    await asyncio.sleep(pace)
        return None

    # Factories, not coroutines: a coroutine can only be awaited once, so a retry needs a
    # fresh call rather than the same awaitable.
    result.faithfulness = await safe(
        "faithfulness",
        lambda: metrics["faithfulness"].ascore(
            user_input=question, response=answer.text, retrieved_contexts=contexts
        ),
    )
    result.answer_relevancy = await safe(
        "answer_relevancy",
        lambda: metrics["answer_relevancy"].ascore(user_input=question, response=answer.text),
    )
    result.context_precision = await safe(
        "context_precision",
        lambda: metrics["context_precision"].ascore(
            user_input=question, reference=reference, retrieved_contexts=contexts
        ),
    )
    result.context_recall = await safe(
        "context_recall",
        lambda: metrics["context_recall"].ascore(
            user_input=question, retrieved_contexts=contexts, reference=reference
        ),
    )


async def evaluate_answers(
    items: list[GoldenItem],
    records: dict[str, FinancialRecord],
    *,
    score: bool = True,
    pace: float = DEFAULT_PACE,
) -> list[ItemResult]:
    """Answer every golden question, then score the non-refusal ones with Ragas.

    ``pace`` is a deliberate delay between model calls. A full run is ~28 answers plus
    ~100 judge calls, each carrying a ~1,600-token prompt; fired back to back that
    saturates Groq's free-tier tokens-per-minute allowance and the run collapses into SDK
    retry backoff. Pacing makes the run slower but finite — see decision D-27.
    """
    results: list[ItemResult] = []

    for index, item in enumerate(items, 1):
        record = records.get(item.document_id) if item.document_id else None
        result = ItemResult(item=item)
        try:
            result.answer = answer_question(
                item.question, record=record, document_id=item.document_id
            )
            status = "refused" if result.answer.refused else f"{len(result.answer.citations)} cites"
        except AssistantError as exc:
            result.error = str(exc)
            status = f"ERROR {type(exc).__name__}"
        print(f"  [{index:>2}/{len(items)}] {item.id} {item.kind:<14} {status}", flush=True)
        results.append(result)
        if pace and index < len(items):
            await asyncio.sleep(pace)

    if not score:
        return results

    metrics = _build_metrics()
    scorable = [r for r in results if r.answer is not None and r.item.kind != "refusal"]
    print(f"\nScoring {len(scorable)} answers with Ragas (refusal items excluded)...", flush=True)
    for index, result in enumerate(scorable, 1):
        await _score_item(result, metrics, pace=pace)
        print(
            f"  [{index:>2}/{len(scorable)}] {result.item.id} "
            f"faith={_fmt(result.faithfulness)} rel={_fmt(result.answer_relevancy)} "
            f"prec={_fmt(result.context_precision)} rec={_fmt(result.context_recall)}",
            flush=True,
        )
    return results


def _build_metrics() -> dict[str, Any]:
    import ragas.metrics.collections as collections

    judge = _build_judge()
    embeddings = _build_judge_embeddings()
    return {
        "faithfulness": collections.Faithfulness(llm=judge),
        "answer_relevancy": collections.AnswerRelevancy(llm=judge, embeddings=embeddings),
        "context_precision": collections.ContextPrecision(llm=judge),
        "context_recall": collections.ContextRecall(llm=judge),
    }


# ── Grounding checks (ours, not Ragas) ───────────────────────────────────────


def grounding_summary(results: list[ItemResult]) -> dict[str, Any]:
    """Checks Ragas does not make, including the one that matters most: false refusals.

    A system that refuses every question scores perfectly on faithfulness while being
    useless. Refusal correctness is therefore measured in both directions.
    """
    answered = [r for r in results if r.answer is not None]
    non_refusal = [r for r in answered if r.item.kind != "refusal"]
    refusal_items = [r for r in answered if r.item.kind == "refusal"]

    false_refusals = [r for r in non_refusal if r.answer and r.answer.refused]
    missed_refusals = [r for r in refusal_items if r.answer and not r.answer.refused]
    uncited = [r for r in non_refusal if r.answer and not r.answer.is_grounded]
    unsupported = [r for r in answered if r.answer and r.answer.unsupported_figures]
    contradicting = [r for r in answered if r.answer and r.answer.contradicting_figures]
    invented_cites = [r for r in answered if r.answer and r.answer.dropped_citations]

    def rate(subset: list[ItemResult], total: list[ItemResult]) -> float:
        return len(subset) / len(total) if total else 0.0

    return {
        "answered": len(answered),
        "errored": len(results) - len(answered),
        "citation_rate": 1.0 - rate(uncited, non_refusal),
        "false_refusal_rate": rate(false_refusals, non_refusal),
        "missed_refusal_rate": rate(missed_refusals, refusal_items),
        "unsupported_figure_rate": rate(unsupported, answered),
        "contradiction_rate": rate(contradicting, answered),
        "invented_citation_rate": rate(invented_cites, answered),
        "false_refusals": [r.item.id for r in false_refusals],
        "missed_refusals": [r.item.id for r in missed_refusals],
        "uncited": [r.item.id for r in uncited],
        "unsupported": [
            f"{r.item.id}:{[str(c.claimed) for c in r.answer.unsupported_figures]}"
            for r in unsupported
            if r.answer
        ],
        "invented_citations": [
            f"{r.item.id}:{r.answer.dropped_citations}" for r in invented_cites if r.answer
        ],
    }


# ── Reporting ────────────────────────────────────────────────────────────────


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "  - "


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _verdict(name: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    target = TARGETS.get(name)
    if target is None:
        return ""
    return "PASS" if value >= target else "FAIL"


def print_report(
    extraction: list[ExtractionResult],
    answers: list[ItemResult],
    grounding: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    print("\n" + "=" * 78)
    print("EXTRACTION ACCURACY (deterministic — no model, no tokens)")
    print("=" * 78)
    fields_checked = sum(r.fields_checked for r in extraction)
    fields_correct = sum(r.fields_correct for r in extraction)
    totals_correct = sum(1 for r in extraction if r.total_correct)
    items_expected = sum(r.line_items_expected for r in extraction)
    items_matched = sum(r.line_items_matched for r in extraction)

    for result in extraction:
        mark = "PASS" if not result.failures else "FAIL"
        print(
            f"  {mark}  {result.fixture:<26} fields {result.fields_correct}/{result.fields_checked}"
            f"   line items {result.line_items_matched}/{result.line_items_expected}"
        )
        for failure in result.failures:
            print(f"          - {failure}")
        if result.skipped_fields:
            print(f"          ~ skipped (need the model): {', '.join(result.skipped_fields)}")

    field_accuracy = fields_correct / fields_checked if fields_checked else 0.0
    total_accuracy = totals_correct / len(extraction) if extraction else 0.0
    line_recall = items_matched / items_expected if items_expected else 0.0

    print(
        f"\n  field accuracy       {field_accuracy:.1%}  ({fields_correct}/{fields_checked})"
        f"   target {TARGETS['field_accuracy']:.0%}  {_verdict('field_accuracy', field_accuracy)}"
    )
    print(
        f"  total_amount accuracy {total_accuracy:.1%}  ({totals_correct}/{len(extraction)})"
        f"   target {TARGETS['total_amount_accuracy']:.0%}  "
        f"{_verdict('total_amount_accuracy', total_accuracy)}"
    )
    print(
        f"  line item recall     {line_recall:.1%}  ({items_matched}/{items_expected})"
        f"   target {TARGETS['line_item_recall']:.0%}  {_verdict('line_item_recall', line_recall)}"
    )

    print("\n" + "=" * 78)
    print("RAGAS METRICS (Groq judge + local MiniLM embeddings — $0)")
    print("=" * 78)
    means = {
        "faithfulness": _mean([r.faithfulness for r in answers]),
        "answer_relevancy": _mean([r.answer_relevancy for r in answers]),
        "context_precision": _mean([r.context_precision for r in answers]),
        "context_recall": _mean([r.context_recall for r in answers]),
    }
    for name, value in means.items():
        target = TARGETS.get(name)
        suffix = f"   target {target:.2f}  {_verdict(name, value)}" if target else ""
        print(f"  {name:<20} {_fmt(value)}{suffix}")

    by_kind: dict[str, list[float | None]] = {}
    for answered in answers:
        by_kind.setdefault(answered.item.kind, []).append(answered.faithfulness)
    print("\n  faithfulness by question type:")
    for kind, values in sorted(by_kind.items()):
        mean = _mean(values)
        if mean is not None:
            print(f"    {kind:<16} {mean:.2f}  (n={len([v for v in values if v is not None])})")

    print("\n" + "=" * 78)
    print("GROUNDING CHECKS (ours — what Ragas does not measure)")
    print("=" * 78)
    print(f"  answered / errored          {grounding['answered']} / {grounding['errored']}")
    print(f"  citation rate               {grounding['citation_rate']:.1%}  (want 100%)")
    print(f"  false refusal rate          {grounding['false_refusal_rate']:.1%}  (want 0%)")
    print(f"  missed refusal rate         {grounding['missed_refusal_rate']:.1%}  (want 0%)")
    print(f"  unsupported figure rate     {grounding['unsupported_figure_rate']:.1%}  (want 0%)")
    print(f"  contradiction rate          {grounding['contradiction_rate']:.1%}  (want 0%)")
    print(f"  invented citation rate      {grounding['invented_citation_rate']:.1%}  (want 0%)")

    for label, key in (
        ("false refusals", "false_refusals"),
        ("missed refusals", "missed_refusals"),
        ("uncited answers", "uncited"),
        ("unsupported figures", "unsupported"),
        ("invented citations", "invented_citations"),
    ):
        if grounding[key]:
            print(f"\n  {label}: {grounding[key]}")

    measured: dict[str, float | None] = {
        **means,
        "total_amount_accuracy": total_accuracy,
        "line_item_recall": line_recall,
        "field_accuracy": field_accuracy,
    }
    failures = [name for name in TARGETS if _verdict(name, measured.get(name)) == "FAIL"]

    # Grounding rates are pass/fail at zero, not against a threshold: a single answer
    # containing an invented figure or an invented citation is a correctness bug, and
    # averaging it away across a run would hide exactly what this suite exists to catch.
    if grounding["answered"]:
        for label, key in (
            ("false_refusals", "false_refusal_rate"),
            ("missed_refusals", "missed_refusal_rate"),
            ("unsupported_figures", "unsupported_figure_rate"),
            ("contradictions", "contradiction_rate"),
            ("invented_citations", "invented_citation_rate"),
        ):
            if grounding[key] > 0:
                failures.append(label)
        if grounding["citation_rate"] < 1.0:
            failures.append("uncited_answers")

    print("\n" + "=" * 78)
    print(f"Completed in {elapsed:.1f}s.  Estimated cost: $0.00")
    print("PASS — all targets met." if not failures else f"BELOW TARGET: {sorted(set(failures))}")
    print("=" * 78)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "extraction": {
            "field_accuracy": field_accuracy,
            "total_amount_accuracy": total_accuracy,
            "line_item_recall": line_recall,
            "per_fixture": [
                {
                    "fixture": r.fixture,
                    "fields_correct": r.fields_correct,
                    "fields_checked": r.fields_checked,
                    "failures": r.failures,
                }
                for r in extraction
            ],
        },
        "ragas": {k: v for k, v in means.items()},
        "grounding": grounding,
        "targets": TARGETS,
        "below_target": sorted(set(failures)),
        "per_item": [
            {
                "id": r.item.id,
                "kind": r.item.kind,
                "question": r.item.question,
                "refused": r.answer.refused if r.answer else None,
                "citations": [c.label for c in r.answer.citations] if r.answer else [],
                "faithfulness": r.faithfulness,
                "answer_relevancy": r.answer_relevancy,
                "context_precision": r.context_precision,
                "context_recall": r.context_recall,
                "error": r.error,
            }
            for r in answers
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the financial assistant pipeline.")
    parser.add_argument("--extraction", action="store_true", help="deterministic checks only, no API calls")
    parser.add_argument("--no-score", action="store_true", help="answer questions but skip Ragas scoring")
    parser.add_argument("--limit", type=int, default=None, help="only the first N golden items")
    parser.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE,
        help=f"seconds between model calls (default {DEFAULT_PACE}); 0 disables pacing",
    )
    args = parser.parse_args(argv)

    configure_logging()
    logging.getLogger("src.parser").setLevel(logging.WARNING)
    logging.getLogger("src.vectorstore").setLevel(logging.WARNING)
    logging.getLogger("src.chain").setLevel(logging.WARNING)

    started = time.perf_counter()
    print("Indexing fixtures...")
    records = build_index(use_llm=not args.extraction)

    extraction = evaluate_extraction(records, llm_fields=not args.extraction)
    if args.extraction:
        elapsed = time.perf_counter() - started
        report = print_report(extraction, [], grounding_summary([]), elapsed)
    else:
        items = load_golden_set(limit=args.limit)
        print(f"\nAnswering {len(items)} golden questions (pace={args.pace}s)...", flush=True)
        answers = asyncio.run(
            evaluate_answers(items, records, score=not args.no_score, pace=args.pace)
        )
        elapsed = time.perf_counter() - started
        report = print_report(extraction, answers, grounding_summary(answers), elapsed)

    EVAL_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = EVAL_REPORTS_DIR / f"report-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nReport written to {path}")

    return 1 if report["below_target"] else 0


if __name__ == "__main__":
    sys.exit(main())
