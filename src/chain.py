"""Grounded, streaming, cited question answering (phases.md Phase 4).

This is where grounding stops being a prompt instruction and becomes enforcement. The
model is asked to cite and to refuse, but it is also **checked**:

- Citation markers it emits are matched against chunks actually retrieved. A marker
  pointing at nothing is dropped and recorded in :attr:`Answer.dropped_citations`.
- Every monetary figure it states is traced back to either a field of the extracted
  record or a retrieved snippet. A figure supported by neither is reported as unsupported
  (Rule 5), and one that contradicts the record is reported as contradicting (FR-4.4).

The extracted :class:`FinancialRecord` goes into the prompt alongside retrieved chunks so
the model reads totals rather than re-deriving them from prose — the single largest source
of wrong numbers in invoice RAG (architecture.md §6).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel

from src import LLMError, RateLimitError
from src.config import AMOUNT_TOLERANCE, MODELS_BY_ROLE, REASONING_MODEL
from src.llm import (
    get_chat_model,
    invoke_with_retry,
    llm_available,
    translate_provider_error,
)
from src.observability import TokenCollector, stage, tracing_config
from src.schemas import Answer, Citation, FinancialRecord, NumericCheck, RunStats
from src.vectorstore import retrieve_with_policies

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

__all__ = ["ChatTurn", "StreamEvent", "stream_answer", "answer_question", "suggested_prompts"]


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class StreamEvent(BaseModel):
    """One item in the answer stream.

    Modelled as tagged events rather than a bare token iterator so the UI can show a named
    stage while working — "Retrieving from 2 documents…" reads as competence where a bare
    spinner reads as stalling (design.md §1, §5.3).
    """

    type: Literal["stage", "token", "answer", "error"]
    stage: str | None = None
    token: str | None = None
    answer: Answer | None = None
    message: str | None = None
    retry_after_seconds: float | None = None


# ── Prompts ──────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a financial document analyst. You explain charges to people who \
are looking at a bill they do not understand.

GROUNDING RULES — these are absolute:
1. Every factual claim must be supported by the provided context. Cite it inline, \
immediately after the claim, as [filename:page].
2. Never state a figure that does not appear in the EXTRACTED RECORD or the RETRIEVED \
CONTEXT. Do not add, subtract, or otherwise compute new amounts. If a figure is not given \
to you, say so plainly rather than working it out.
3. If the context does not answer the question — whether what is missing is a figure, a \
name, an address, a date, or anything else — open with this sentence verbatim: "I cannot \
determine this from the provided documents." Do not paraphrase it. Then say what would be \
needed. A refusal is a correct answer; a guess is not.
4. Keep what the DOCUMENT says separate from what the POLICY says. Attribute each \
explicitly, e.g. "your invoice shows X [invoice.pdf:1], and your billing policy states \
Y [policy.md:1]".
5. The EXTRACTED RECORD is authoritative for amounts. If a retrieved snippet appears to \
disagree with it, trust the record and note the discrepancy.

STYLE:
- Lead with the direct answer, then the supporting detail. The user asked a question; \
answer it in the first sentence.
- Quote amounts exactly as given, including currency.
- Be concise. Three short paragraphs at most unless the question demands more.
- No preamble, no "Based on the provided context", no restating the question."""

_REWRITE_PROMPT = """Rewrite the user's latest question into a standalone search query.

Resolve pronouns and references ("this charge", "that line", "the same vendor") using the \
conversation. Keep the specific nouns and amounts — they matter for retrieval. Output only \
the rewritten query, nothing else. If the question is already standalone, output it unchanged."""

_REFUSAL_MARKERS = (
    "cannot determine this from the provided documents",
    "cannot determine this from the provided",
    "not available in the provided",
    "does not appear in the provided",
)

#: Refusal wordings that name no source, matched only in the opening sentence.
#:
#: The system prompt mandates one canonical refusal sentence, but models paraphrase it —
#: a missing address came back as "The account holder's home address is not available."
#: These catch that drift. They are deliberately not checked against the whole
#: :data:`_REFUSAL_WINDOW`: unlike the markers above they do not require "the provided
#: ...", so mid-answer they match ordinary partial answers ("the tax breakdown is not
#: available, but the total is ..."). Mislabelling one of those costs more than a missed
#: refusal — :attr:`Answer.is_trustworthy` treats ``refused`` as standing in for
#: citations, so a false positive would pass an uncited answer off as grounded.
_REFUSAL_OPENERS = (
    "is not available",
    "are not available",
    "do not contain",
    "does not contain",
    "is not stated",
    "is not disclosed",
)

#: Ends a sentence. Used to isolate the opening sentence for :data:`_REFUSAL_OPENERS`.
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")

#: A citation marker: ``[invoice.pdf:1]`` or ``[invoice.pdf: p.1]``.
_CITATION_RE = re.compile(r"\[([^\[\]]+?):\s*(?:p\.?\s*)?(\d+)\s*\]")

#: A monetary figure. Two decimals required, which keeps quantities, dates, and rates out.
_FIGURE_RE = re.compile(r"(?:USD|EUR|GBP|\$|€|£)?\s?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\b")

#: Words that name a record field when they appear just before a figure.
#:
#: Matched as whole words with the longest alternative first, because "subtotal" contains
#: "total" — a naive substring search scores the inner match higher and reports every
#: subtotal as a contradicting total.
_FIELD_LABEL_RE = re.compile(
    r"\b(sub[\s-]*total|grand\s+total|amount\s+due|balance\s+due|total)\b", re.I
)
_LABEL_TO_FIELD: dict[str, str] = {
    "subtotal": "subtotal",
    "sub total": "subtotal",
    "sub-total": "subtotal",
}


# ── Context assembly ─────────────────────────────────────────────────────────


def _format_record(record: FinancialRecord | None) -> str:
    """The extracted record as compact JSON — authoritative for amounts."""
    if record is None:
        return "(no structured record available for this document)"

    payload: dict[str, Any] = {
        "vendor": record.vendor_name,
        "document_type": record.document_type,
        "filename": record.filename,
        "currency": record.currency,
        "line_items": [
            {
                "description": item.description,
                "quantity": str(item.quantity) if item.quantity is not None else None,
                "unit_price": str(item.unit_price) if item.unit_price is not None else None,
                "amount": str(item.amount),
                "page": item.source_page,
            }
            for item in record.line_items
        ],
        "subtotal": str(record.subtotal) if record.subtotal is not None else None,
        "tax_lines": [
            {"label": tax.label, "rate": str(tax.rate) if tax.rate else None, "amount": str(tax.amount)}
            for tax in record.tax_lines
        ],
        "total_amount": str(record.total_amount) if record.total_amount is not None else None,
        "validation_state": record.validation_state,
    }
    if record.invoice_number:
        payload["invoice_number"] = record.invoice_number
    if record.billing_date:
        payload["billing_date"] = str(record.billing_date)
    if record.billing_period_start and record.billing_period_end:
        payload["billing_period"] = f"{record.billing_period_start} to {record.billing_period_end}"
    if record.extraction_warnings:
        payload["extraction_warnings"] = record.extraction_warnings

    return json.dumps(payload, indent=2)


def _format_hits(citations: list[Citation]) -> str:
    if not citations:
        return "(nothing retrieved)"
    return "\n\n".join(f"[{hit.filename}:{hit.page}]\n{hit.snippet}" for hit in citations)


def _format_history(history: list[ChatTurn], limit: int = 6) -> str:
    if not history:
        return ""
    recent = history[-limit:]
    lines = [f"{turn.role.upper()}: {turn.content}" for turn in recent]
    return "CONVERSATION SO FAR:\n" + "\n".join(lines) + "\n\n"


def _build_messages(
    question: str,
    record: FinancialRecord | None,
    document_hits: list[Citation],
    policy_hits: list[Citation],
    history: list[ChatTurn],
) -> list[tuple[str, str]]:
    human = (
        f"{_format_history(history)}"
        f"EXTRACTED RECORD (authoritative for amounts):\n{_format_record(record)}\n\n"
        f"RETRIEVED FROM THE DOCUMENT:\n{_format_hits(document_hits)}\n\n"
        f"RETRIEVED FROM POLICY DOCUMENTS:\n{_format_hits(policy_hits)}\n\n"
        f"QUESTION: {question}"
    )
    return [("system", _SYSTEM_PROMPT), ("human", human)]


# ── Query rewriting ──────────────────────────────────────────────────────────


def _rewrite_query(question: str, history: list[ChatTurn]) -> str:
    """Resolve references against the conversation so retrieval sees a standalone query.

    Skipped entirely on the first turn — there is nothing to resolve, and a needless model
    call would add latency to the most latency-sensitive moment in the product.
    """
    if not history:
        return question

    try:
        model = get_chat_model("utility")
        result = invoke_with_retry(
            model.invoke,
            [
                ("system", _REWRITE_PROMPT),
                ("human", f"{_format_history(history)}LATEST QUESTION: {question}"),
            ],
            attempts=2,
        )
        rewritten = str(getattr(result, "content", "")).strip()
    except LLMError as exc:
        logger.info("Query rewriting unavailable (%s); using the original question", exc)
        return question

    # A rewrite that collapses or balloons the question is worse than no rewrite.
    if not rewritten or len(rewritten) > max(400, len(question) * 4):
        return question
    logger.info("Rewrote query for retrieval")
    return rewritten


# ── Verification ─────────────────────────────────────────────────────────────


def parse_citations(text: str, available: list[Citation]) -> tuple[list[Citation], list[str]]:
    """Resolve inline ``[filename:page]`` markers against chunks actually retrieved.

    Returns the resolved citations (deduplicated, in order of first appearance) and any
    markers that matched nothing — a marker pointing at a source that was never retrieved
    means the model invented it, which the UI surfaces rather than hides.
    """
    lookup = {(hit.filename.lower(), hit.page): hit for hit in available}
    resolved: list[Citation] = []
    dropped: list[str] = []
    seen: set[tuple[str, int]] = set()

    for raw_name, raw_page in _CITATION_RE.findall(text):
        name = raw_name.strip().lower().split("/")[-1].split("\\")[-1]
        try:
            page = int(raw_page)
        except ValueError:
            continue

        key = (name, page)
        if key in seen:
            continue
        seen.add(key)

        hit = lookup.get(key)
        if hit is not None:
            resolved.append(hit)
        else:
            dropped.append(f"[{raw_name}:{raw_page}]")

    if dropped:
        logger.warning("Dropped %d citation marker(s) that matched no retrieved chunk", len(dropped))
    return resolved, dropped


def _record_amounts(record: FinancialRecord | None) -> dict[str, Decimal]:
    if record is None:
        return {}
    amounts: dict[str, Decimal] = {}
    if record.total_amount is not None:
        amounts["total_amount"] = record.total_amount
    if record.subtotal is not None:
        amounts["subtotal"] = record.subtotal
    amounts["computed_total"] = record.computed_total
    amounts["line_item_total"] = record.line_item_total
    for tax in record.tax_lines:
        amounts[f"tax:{tax.label}"] = tax.amount
    for index, item in enumerate(record.line_items):
        amounts[f"line_item:{index}:{item.description[:30]}"] = item.amount
    amounts.update(_validator_amounts(record))
    return amounts


def _validator_amounts(record: FinancialRecord) -> dict[str, Decimal]:
    """Figures our own validators derive and disclose to the model.

    :func:`_format_record` hands the model ``extraction_warnings``, and an arithmetic
    mismatch warning states the difference outright ("difference 27.13"). That figure is
    in no line item and no retrieved snippet, so reporting it back scored as an
    unsupported figure — the verifier penalised the model for repeating something the
    backend told it. Answering "do these figures add up?" *requires* naming the gap.

    The conditions below deliberately mirror
    :meth:`FinancialRecord.validate_arithmetic` rather than deriving whatever they can.
    A figure counts as grounded only when the warning that discloses it was actually
    emitted; a discrepancy inside tolerance is never shown to the model, so it must not
    be verifiable either. Otherwise this becomes a laundering channel, where any number
    the model can reach by arithmetic passes as "read from a source".
    """
    amounts: dict[str, Decimal] = {}

    if record.total_amount is not None:
        difference = abs(record.computed_total - record.total_amount)
        if difference > AMOUNT_TOLERANCE:
            amounts["validator:total_difference"] = difference

    if record.subtotal is not None:
        difference = abs(record.subtotal - record.line_item_total)
        if difference > AMOUNT_TOLERANCE:
            amounts["validator:subtotal_difference"] = difference

    return amounts


def _labelled_field(text: str, position: int) -> str | None:
    """Which record field, if any, the words just before a figure name."""
    window = text[max(0, position - 45) : position]
    matches = list(_FIELD_LABEL_RE.finditer(window))
    if not matches:
        return None
    label = re.sub(r"\s+", " ", matches[-1].group(1).lower())
    return _LABEL_TO_FIELD.get(label, "total_amount")


def cross_check_numbers(
    text: str, record: FinancialRecord | None, context: list[Citation]
) -> list[NumericCheck]:
    """Trace every monetary figure in an answer back to its source (FR-4.4, Rule 5)."""
    amounts = _record_amounts(record)

    # Validator warnings belong in the haystack because _format_record shows them to the
    # model verbatim, which makes them a source it read from just as much as a retrieved
    # snippet is. Kept as a text fallback behind the named entries in _validator_amounts:
    # those give the UI real provenance ("validator:total_difference") where this only
    # says "seen somewhere", but this keeps working if a warning's wording changes or a
    # new validator starts disclosing a figure.
    sources = [hit.snippet for hit in context]
    if record is not None:
        sources.extend(record.extraction_warnings)
    haystack = " ".join(sources).replace(",", "")
    checks: list[NumericCheck] = []
    seen: set[Decimal] = set()

    for match in _FIGURE_RE.finditer(text):
        try:
            claimed = Decimal(match.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001 - a malformed figure is simply not checked
            continue
        if claimed in seen:
            continue
        seen.add(claimed)

        plain = f"{claimed:f}".rstrip("0").rstrip(".")
        found = f"{claimed:.2f}" in haystack or plain in haystack

        # 1. Does it equal a figure in the extracted record? Then it was read, not produced.
        #    Checked before the label heuristic: a stated "subtotal of 462.00" that matches
        #    the record's subtotal is correct regardless of what word precedes it.
        matched = next((name for name, value in amounts.items() if value == claimed), None)
        if matched:
            checks.append(
                NumericCheck(
                    claimed=claimed,
                    matched_field=matched,
                    expected=claimed,
                    found_in_context=found,
                )
            )
            continue

        # 2. Does it appear verbatim in something the model was shown — a policy
        #    threshold, a figure from a *different* document in a multi-document
        #    question, or a discrepancy one of our own validators disclosed? Either way
        #    it was read from a source, so it is grounded and is not a contradiction of
        #    the active record.
        if found:
            checks.append(NumericCheck(claimed=claimed, found_in_context=True))
            continue

        # 3. In neither. If the wording names a record field, this contradicts it;
        #    otherwise the figure is unsupported outright.
        field = _labelled_field(text, match.start())
        checks.append(
            NumericCheck(
                claimed=claimed,
                matched_field=field,
                expected=amounts.get(field) if field else None,
            )
        )

    return checks


#: How far into an answer a refusal phrase must appear to count as a refusal.
#:
#: A genuine refusal opens with the phrase — the system prompt requires it, and instructs
#: the model to lead with its answer. The same wording appearing late is usually a caveat
#: attached to a real answer ("...the documents do not provide further detail on why"),
#: and labelling that a refusal would mislabel a well-cited answer in the UI.
_REFUSAL_WINDOW = 160


def _looks_like_refusal(text: str) -> bool:
    opening = text[:_REFUSAL_WINDOW].lower()
    if any(marker in opening for marker in _REFUSAL_MARKERS):
        return True

    end = _SENTENCE_END_RE.search(opening)
    first_sentence = opening[: end.start()] if end else opening
    return any(marker in first_sentence for marker in _REFUSAL_OPENERS)


# ── Answering ────────────────────────────────────────────────────────────────


def stream_answer(
    question: str,
    *,
    record: FinancialRecord | None = None,
    document_id: str | None = None,
    history: list[ChatTurn] | None = None,
    stats: RunStats | None = None,
) -> Iterator[StreamEvent]:
    """Answer a question, streaming tokens and finishing with a verified :class:`Answer`.

    Yields ``stage`` events while working, ``token`` events as the model generates, and
    exactly one terminal ``answer`` or ``error`` event.
    """
    history = history or []
    stats = stats or RunStats(model=REASONING_MODEL)
    started = time.perf_counter()

    if not question.strip():
        yield StreamEvent(type="error", message="Ask a question to get started.")
        return

    if not llm_available():
        yield StreamEvent(
            type="error",
            message=(
                "No Groq API key configured, so questions cannot be answered. Add "
                "GROQ_API_KEY to .env — a free key is available at console.groq.com/keys. "
                "Document parsing and extraction work without it."
            ),
        )
        return

    try:
        yield StreamEvent(type="stage", stage="Understanding the question…")
        with stage(stats, "retrieve"):
            search_query = _rewrite_query(question, history)
            document_hits, policy_hits = retrieve_with_policies(
                search_query, document_id=document_id
            )

        stats.chunks_retrieved = len(document_hits) + len(policy_hits)
        sources = len({hit.document_id for hit in document_hits + policy_hits})
        yield StreamEvent(
            type="stage",
            stage=f"Retrieved {stats.chunks_retrieved} passages from {sources} document(s)…",
        )

        if not document_hits and not policy_hits:
            yield StreamEvent(
                type="error",
                message=(
                    "Nothing has been indexed yet, so there is no context to answer from. "
                    "Upload a document first."
                ),
            )
            return

        messages = _build_messages(question, record, document_hits, policy_hits, history)
        collector = TokenCollector()
        model = get_chat_model("reasoning")
        config = cast("RunnableConfig", {"callbacks": [collector], **tracing_config()})

        yield StreamEvent(type="stage", stage="Reasoning…")
        pieces: list[str] = []
        with stage(stats, "generate"):
            # Provider errors surface while iterating, not at call time, so
            # invoke_with_translation cannot wrap this. Translating here is what keeps a
            # 429 mid-stream from reaching the UI as a raw groq.RateLimitError.
            try:
                for chunk in model.stream(messages, config=config):
                    piece = str(getattr(chunk, "content", "") or "")
                    if piece:
                        pieces.append(piece)
                        yield StreamEvent(type="token", token=piece)
            except (LLMError, GeneratorExit):
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised as our own typed error
                raise translate_provider_error(exc) from exc

        text = "".join(pieces).strip()
        available = document_hits + policy_hits
        citations, dropped = parse_citations(text, available)

        collector.apply_to(stats, fallback_text=text)
        if not stats.prompt_tokens:
            from src.observability import estimate_tokens

            stats.prompt_tokens = estimate_tokens(messages[0][1] + messages[1][1])
            stats.tokens_estimated = True

        answer = Answer(
            question=question,
            text=text,
            citations=citations,
            retrieved=available,
            numeric_checks=cross_check_numbers(text, record, available),
            dropped_citations=dropped,
            refused=_looks_like_refusal(text),
            model=MODELS_BY_ROLE["reasoning"],
            latency_seconds=time.perf_counter() - started,
            prompt_tokens=stats.prompt_tokens,
            completion_tokens=stats.completion_tokens,
        )

        logger.info(
            "answered in %.2fs: %d citations, %d dropped, %d unsupported figures, refused=%s",
            answer.latency_seconds,
            len(answer.citations),
            len(answer.dropped_citations),
            len(answer.unsupported_figures),
            answer.refused,
        )
        yield StreamEvent(type="answer", answer=answer)

    except RateLimitError as exc:
        wait = f" Retrying in {exc.retry_after_seconds:.0f}s." if exc.retry_after_seconds else ""
        yield StreamEvent(
            type="error",
            message=f"Groq's free-tier rate limit was reached.{wait}",
            retry_after_seconds=exc.retry_after_seconds,
        )
    except LLMError as exc:
        yield StreamEvent(type="error", message=str(exc))


def answer_question(
    question: str,
    *,
    record: FinancialRecord | None = None,
    document_id: str | None = None,
    history: list[ChatTurn] | None = None,
    stats: RunStats | None = None,
) -> Answer:
    """Non-streaming convenience wrapper. Raises :class:`LLMError` on failure."""
    for event in stream_answer(
        question, record=record, document_id=document_id, history=history, stats=stats
    ):
        if event.type == "answer" and event.answer is not None:
            return event.answer
        if event.type == "error":
            message = event.message or "The question could not be answered."
            # Re-raise rate limits as rate limits, carrying the retry hint. Flattening
            # every failure into a bare LLMError here discarded the one detail a caller
            # can act on: a caller unable to tell a 7-second tokens-per-minute pause from
            # a 16-minute daily-budget stop can only keep firing doomed requests. The
            # eval suite did exactly that for 22 questions (see src/evals.py).
            if event.retry_after_seconds is not None:
                raise RateLimitError(message, retry_after_seconds=event.retry_after_seconds)
            raise LLMError(message)
    raise LLMError("The model produced no answer.")


def suggested_prompts(record: FinancialRecord | None, *, has_policies: bool = False) -> list[str]:
    """Quick-prompt chips generated from the document, not hardcoded (design.md §5.3).

    Contextual chips are the difference between a demo and a product: a chip reading
    "Explain this AWS charge" only makes sense once we know the vendor.
    """
    if record is None:
        return ["What is this document?", "What am I being charged for?"]

    chips = ["Why was this charge deducted?"]
    vendor = record.vendor_name.split(",")[0].split(" Inc")[0].strip()
    if vendor and vendor.lower() != "unknown vendor":
        short = "AWS" if "Amazon Web Services" in vendor else vendor
        chips.append(f"Explain this {short} charge")

    if record.line_items:
        largest = max(record.line_items, key=lambda item: abs(item.amount))
        chips.append(f"Why is '{largest.description[:32]}' so expensive?")
    if record.tax_lines:
        chips.append("Break down the tax")
    if has_policies:
        chips.append("Verify against company policy")
    if record.validation_state == "mismatch":
        chips.append("Why don't these figures add up?")
    chips.append("Find anything unusual")
    return chips
