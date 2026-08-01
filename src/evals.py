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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from src import AssistantError, RateLimitError
from src.chain import answer_question
from src.config import (
    CHUNK_SIZE,
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

#: One whole chunk, which is why it is CHUNK_SIZE rather than a number of its own: a trim
#: below what the chunker emits decapitates chunks by construction. At 500 it truncated the
#: *head* of chunks running 589-698 chars, and the policy corpus states its rules last —
#: a section describes the topic, then closes with the number. The 30-day claim deadline,
#: the USD 200 NAT alert threshold and the 60-day dispute window all sat past char 470 and
#: were cut, so the judge scored answers against context with the answer removed and
#: returned 0.00 recall for chunks retrieval had ranked 0th or 1st.
MAX_CONTEXT_CHARS = CHUNK_SIZE

#: How many times to retry a judge call that was rate limited.
JUDGE_ATTEMPTS = 4

#: How many times to retry an *answer* call that was rate limited. The judge path has
#: waited out 429s since D-27; the answer path had no retry at all, so a two-second
#: tokens-per-minute pause cost the whole item.
ANSWER_ATTEMPTS = 3

#: A retry hint at or above this many seconds means the daily token budget is gone, not
#: the per-minute one. Groq answers a tokens-per-minute 429 with a wait of seconds; the
#: 100k-tokens/day bucket refills at ~1.2 tokens/s, so it answers with minutes — the
#: observed hints were 16m and 18m. Neither waiting nor retrying helps at that scale, and
#: every remaining question would spend a request to earn the identical 429.
DAILY_LIMIT_RETRY_SECONDS = 120.0

#: Recorded against questions a stopped run never reached. They stay in the results, and
#: therefore in ``errored``, on purpose: an aborted run has not measured them, and a
#: report that quietly shrinks its own denominator is the exact failure this suite exists
#: to prevent (see :func:`grounding_summary`).
NOT_ATTEMPTED = "Not attempted — the run stopped after the daily token budget ran out."


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


ITEM_KINDS: tuple[str, ...] = ("document", "policy", "cross_document", "refusal")


def load_golden_set(
    path: Path = GOLDEN_SET_PATH,
    limit: int | None = None,
    kinds: Sequence[str] | None = None,
) -> list[GoldenItem]:
    """Load the golden set, optionally filtered by question kind.

    ``kinds`` exists because of the free tier: a full run costs roughly twice the
    100k tokens/day budget (decision D-27), so an all-or-nothing run reliably dies
    part-way and reports on whatever happened to finish first. Splitting by kind makes
    each slice affordable and, more importantly, makes coverage deliberate — the last
    truncated run silently evaluated only ``document`` questions and left the refusal
    cases, which are the ones that actually test grounding, unmeasured.
    """
    if not path.exists():
        raise AssistantError(f"Golden set not found at {path}.")

    if kinds:
        unknown = sorted(set(kinds) - set(ITEM_KINDS))
        if unknown:
            raise AssistantError(
                f"Unknown question kind(s): {', '.join(unknown)}. "
                f"Choose from: {', '.join(ITEM_KINDS)}."
            )

    wanted = set(kinds) if kinds else None
    items: list[GoldenItem] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if wanted is not None and raw["kind"] not in wanted:
            continue
        items.append(
            GoldenItem(
                id=raw["id"],
                kind=raw["kind"],
                question=raw["question"],
                reference=raw["reference"],
                document_id=raw.get("document_id"),
            )
        )

    if wanted is not None and not items:
        raise AssistantError(f"No golden items match kind(s): {', '.join(sorted(wanted))}.")
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


#: Policy chunks reserved in the judge's context, per question kind. context_precision
#: counts an irrelevant context against the score, so a fixed half-and-half split caps a
#: policy question near 0.5 — two invoice chunks that cannot be relevant to it by
#: construction — and conversely starves a document question of two slots it needs.
#:
#: ``cross_document`` genuinely wants both and is not a compromise: q23 asks how the NAT
#: Gateway charge moved between two invoices *and whether it breaches the policy
#: threshold*, so its evidence is two figures from the documents and one line from the
#: policy corpus. Scoring it against either collection alone marks a correct answer wrong.
POLICY_SLOTS: dict[str, int] = {
    "policy": MAX_CONTEXTS,
    "cross_document": MAX_CONTEXTS // 2,
    "document": 0,
    "refusal": 0,  # never scored — refusal items are excluded before scoring
}


def _judge_contexts(answer: Answer, kind: ItemKind) -> list[str]:
    """The trimmed retrieval set shown to the judge, weighted by what the question needs.

    What retrieval returned, NOT what the model cited (decision D-36). context_precision
    and context_recall are retrieval metrics, and faithfulness judges the answer against
    the context the model was given. Passing citations instead scored 0.077 precision and
    0.15 faithfulness on a pipeline whose extraction is exactly right — an artefact of
    showing the judge two snippets out of ten.

    Still trimmed, because the free tier limits tokens per minute (D-27) — but trimmed
    per collection rather than off the head of their concatenation. ``Answer.retrieved``
    is document-hits-first, so ``retrieved[:4]`` showed the judge four invoice line items
    and no policy text whatsoever: policy questions were scored on whether a hotel
    spending cap follows from an EC2 bill, at 0.08 faithfulness, for answers that had
    cited ``travel_policy.md`` correctly.

    Slots go to the collection holding the question's evidence (:data:`POLICY_SLOTS`) and
    whatever is left over is filled from the other, so a short retrieval never wastes the
    judge's budget on nothing.
    """
    policy = answer.retrieved_policy
    source = answer.retrieved or answer.citations
    documents = [citation for citation in source if citation not in policy]

    chosen = policy[: POLICY_SLOTS.get(kind, 0)]
    for pool in (documents, policy):
        chosen += [c for c in pool if c not in chosen][: MAX_CONTEXTS - len(chosen)]
    return [citation.snippet[:MAX_CONTEXT_CHARS] for citation in chosen] or [
        "(no context was retrieved)"
    ]


async def _score_item(
    result: ItemResult, metrics: dict[str, Any], *, pace: float = DEFAULT_PACE
) -> None:
    """Score one answered item. A metric failure degrades to ``None``, never a crash."""
    answer = result.answer
    if answer is None:
        return

    contexts = _judge_contexts(answer, result.item.kind)
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


async def _answer_item(
    item: GoldenItem, record: FinancialRecord | None
) -> tuple[Answer | None, AssistantError | None, float | None]:
    """Answer one golden question, sleeping through short rate limits.

    Returns ``(answer, failure, daily_limit_wait)``. A tokens-per-minute 429 clears in
    seconds and is worth waiting out. A daily-budget 429 is handed back instead, with its
    retry hint, so the caller can stop the run: no number of attempts conjures tokens that
    do not exist, and the hint is the only thing that tells the two cases apart.
    """
    for attempt in range(1, ANSWER_ATTEMPTS + 1):
        try:
            answer = answer_question(
                item.question, record=record, document_id=item.document_id
            )
            return answer, None, None
        except RateLimitError as exc:
            wait = exc.retry_after_seconds
            if wait is not None and wait >= DAILY_LIMIT_RETRY_SECONDS:
                return None, exc, wait
            if attempt == ANSWER_ATTEMPTS:
                return None, exc, None
            logger.info(
                "%s rate limited; waiting %.1fs (attempt %d/%d)",
                item.id, wait or 10.0, attempt, ANSWER_ATTEMPTS,
            )
            await asyncio.sleep(wait or 10.0)
        except AssistantError as exc:
            return None, exc, None
    raise AssertionError("unreachable: the loop returns on every path")


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

    The run stops on the first daily-budget rate limit. Pacing cannot help there: the
    budget is spent for the day, so continuing spends one request per remaining question
    to collect the same 429 each time. Unreached questions are still recorded, as errors,
    so the report shows 24 items whatever happened rather than a shrunken denominator.
    """
    results: list[ItemResult] = []

    for index, item in enumerate(items, 1):
        record = records.get(item.document_id) if item.document_id else None
        result = ItemResult(item=item)
        answer, failure, daily_limit_wait = await _answer_item(item, record)

        result.answer = answer
        if failure is not None:
            result.error = str(failure)
            status = f"ERROR {type(failure).__name__}"
        else:
            assert answer is not None  # _answer_item returns one or the other
            status = "refused" if answer.refused else f"{len(answer.citations)} cites"
        print(f"  [{index:>2}/{len(items)}] {item.id} {item.kind:<14} {status}", flush=True)
        results.append(result)

        if daily_limit_wait is not None:
            unreached = items[index:]
            results.extend(ItemResult(item=i, error=NOT_ATTEMPTED) for i in unreached)
            print(
                f"\n  STOPPED: Groq's daily token budget is exhausted — it asked for "
                f"{daily_limit_wait / 60:.0f}m before the next call on this model.\n"
                f"  {len(unreached)} question(s) left unattempted; they are reported as "
                f"errors, not skipped.\n"
                f"  Re-run once the budget refills, or point REASONING_MODEL "
                f"(src/config.py) at a model with its own budget — Groq meters each one "
                f"separately. Note that a re-run restarts: the answer path streams, and "
                f"streamed responses do not go through the LLM cache.",
                flush=True,
            )
            break

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
    derived = [r for r in answered if r.answer and r.answer.derived_figures]

    def rate(subset: list[ItemResult], total: list[ItemResult]) -> float | None:
        """The share of ``total`` in ``subset``, or ``None`` when nothing was measured.

        ``None`` rather than ``0.0``, because the two mean opposite things and only one
        of them is good news. A run where every question errored divided by an empty set
        and reported 0% unsupported figures, 0% contradictions and a 100% citation rate —
        a flawless scorecard for a run that answered nothing. That is how the q16
        unsupported-figure bug stayed invisible across two runs: both were read as passes.

        The denominators differ per metric, so this is not only about wholly empty runs.
        A ``--kinds refusal`` run answers plenty while leaving ``non_refusal`` empty, and
        used to report a perfect citation rate over zero applicable answers.
        """
        return len(subset) / len(total) if total else None

    citation_rate = None if not non_refusal else 1.0 - (len(uncited) / len(non_refusal))

    return {
        "answered": len(answered),
        "errored": len(results) - len(answered),
        #: Denominators, so a reader can tell "0 of 0" from "0 of 24" at a glance.
        "non_refusal_answered": len(non_refusal),
        "refusal_answered": len(refusal_items),
        "citation_rate": citation_rate,
        "false_refusal_rate": rate(false_refusals, non_refusal),
        "missed_refusal_rate": rate(missed_refusals, refusal_items),
        "unsupported_figure_rate": rate(unsupported, answered),
        "contradiction_rate": rate(contradicting, answered),
        "invented_citation_rate": rate(invented_cites, answered),
        #: Not a pass/fail rate. Derived figures are excluded from unsupported_figure_rate
        #: because the model showed verified working, so without this line they would
        #: vanish from the report entirely — and a category nobody looks at is where a
        #: regression hides. Reported so the derivations can be read and judged.
        "derived_figure_rate": rate(derived, answered),
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
        "derived": [
            f"{r.item.id}:{[f'{c.derivation} = {c.claimed}' for c in r.answer.derived_figures]}"
            for r in derived
            if r.answer
        ],
    }


# ── Reporting ────────────────────────────────────────────────────────────────


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "  - "


def _pct(value: float | None) -> str:
    """A grounding rate, or ``n/a`` when its denominator was empty.

    Never renders an unmeasured rate as a number: "0.0%" and "not measured" look alike
    in a summary and only one of them is a pass.
    """
    return f"{value:.1%}" if value is not None else "n/a"


def _grounding_line(label: str, value: float | None, denominator: int, want: str) -> str:
    """One grounding row, carrying the size of the set the rate was taken over.

    The denominator is printed because the rate alone cannot distinguish "clean across
    24 answers" from "nothing to measure" — the ambiguity that let two all-errored runs
    read as passes and hid the q16 unsupported-figure bug.
    """
    return f"  {label:<28}{_pct(value):<9}n={denominator:<5}want {want}"


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
    answered_n = grounding["answered"]
    non_refusal_n = grounding["non_refusal_answered"]
    refusal_n = grounding["refusal_answered"]
    print(f"  answered / errored          {answered_n} / {grounding['errored']}")
    for label, key, denominator, want in (
        ("citation rate", "citation_rate", non_refusal_n, "100%"),
        ("false refusal rate", "false_refusal_rate", non_refusal_n, "0%"),
        ("missed refusal rate", "missed_refusal_rate", refusal_n, "0%"),
        ("unsupported figure rate", "unsupported_figure_rate", answered_n, "0%"),
        ("contradiction rate", "contradiction_rate", answered_n, "0%"),
        ("invented citation rate", "invented_citation_rate", answered_n, "0%"),
        # "review" rather than a target: a verified derivation is not a defect, but it is
        # the model doing arithmetic, so the working is printed below to be read.
        ("derived figure rate", "derived_figure_rate", answered_n, "review"),
    ):
        print(_grounding_line(label, grounding[key], denominator, want))

    for label, key in (
        ("false refusals", "false_refusals"),
        ("missed refusals", "missed_refusals"),
        ("uncited answers", "uncited"),
        ("unsupported figures", "unsupported"),
        ("invented citations", "invented_citations"),
        ("derived figures (verified working)", "derived"),
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

    # An evaluator that reports PASS having measured nothing is worse than no evaluator:
    # it manufactures confidence. Both of these fired on a run where all 28 questions
    # errored on a corrupt vector store — every grounding rate was vacuously 0, every
    # Ragas mean was None, and the run printed "PASS - all targets met".
    if grounding["errored"]:
        failures.append(f"errored_items({grounding['errored']})")
    if answers:
        unmeasured = [name for name in means if means[name] is None]
        if unmeasured:
            failures.extend(f"unmeasured:{name}" for name in unmeasured)

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
            # None means the denominator was empty, so there is nothing to fail on —
            # a --kinds refusal run has no non-refusal answers by design. The rate
            # prints as n/a rather than being counted as a clean 0%.
            value = grounding[key]
            if value is not None and value > 0:
                failures.append(label)
        if grounding["citation_rate"] is not None and grounding["citation_rate"] < 1.0:
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
        "--kinds",
        type=str,
        default=None,
        help=(
            "comma-separated question kinds to run: "
            + ", ".join(ITEM_KINDS)
            + ". Splits a run that would otherwise exceed the daily token budget."
        ),
    )
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
        kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
        items = load_golden_set(limit=args.limit, kinds=kinds)
        scope = f" [{', '.join(kinds)}]" if kinds else " [all kinds]"
        print(
            f"\nAnswering {len(items)} golden questions{scope} (pace={args.pace}s)...",
            flush=True,
        )
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
