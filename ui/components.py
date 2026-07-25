"""Render helpers for the dashboard surfaces (design.md §5).

Each function returns an HTML string or writes directly to Streamlit. Business logic
lives in ``src/``; this module only decides how things look.

The rule that shapes most of this file: **every state is conveyed by an icon and a label
as well as a colour**, because the product is a table full of numbers and colour-only
encoding fails a meaningful share of users (design.md §7).
"""

from __future__ import annotations

import base64
import html
import re
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from src.schemas import Answer, Citation, FinancialRecord, LineItem, ParsedDocument, RunStats

__all__ = [
    "app_header",
    "observability_bar",
    "empty_workspace",
    "vendor_card",
    "validation_banner",
    "line_item_table",
    "totals_panel",
    "page_view",
    "render_answer_html",
    "warning_strips",
    "user_message",
]

_CITATION_MARKER = re.compile(r"\[([^\[\]]+?):\s*(?:p\.?\s*)?(\d+)\s*\]")


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _block(markup: str) -> str:
    """Strip leading whitespace from every line of an HTML block.

    Streamlit renders through a markdown parser, and markdown turns any line indented
    four or more spaces into a literal code block. Indented HTML in a triple-quoted
    string therefore reaches the page as visible ``<div class=...>`` text instead of
    markup. Stripping per line is safe here — none of this markup is whitespace-sensitive.
    """
    return "\n".join(line.strip() for line in markup.strip().splitlines())


def _money(value: Decimal | None, currency: str = "") -> str:
    if value is None:
        return "—"
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def _unit_price(value: Decimal | None) -> str:
    """Format a unit price without discarding precision.

    Unit prices legitimately carry more than two decimals — an EC2 hour at 0.0420 rounds
    to 0.04 under the two-decimal money format, which reads as a different (and wrong)
    rate. Shows up to four decimals, trimmed, with a two-decimal floor.
    """
    if value is None:
        return "—"
    whole, _, frac = f"{value:,.4f}".partition(".")
    frac = frac.rstrip("0").ljust(2, "0")
    return f"{whole}.{frac}"


def _percent(rate: Decimal) -> str:
    """``Decimal("0.085")`` → ``8.5``, not ``8.500``."""
    return f"{(rate * 100).normalize():f}"


# ── Shell ────────────────────────────────────────────────────────────────────


def app_header(indexed_documents: int, indexed_policies: int) -> str:
    return _block(f"""
    <div class="fl-header">
      <div class="fl-brand">
        <span class="fl-brand-mark">FinLens</span>
        <span class="fl-brand-sub">multimodal financial assistant</span>
      </div>
      <div class="fl-brand-sub">
        {indexed_documents} document(s) · {indexed_policies} policy chunk(s) indexed
      </div>
    </div>
    """)


def observability_bar(stats: RunStats, *, model: str, rate_limited: float | None = None) -> str:
    """Token usage, per-stage latency, and active model (design.md §5.4).

    The permanent ``~$0.00`` is deliberate: it is the project's zero-cost thesis rendered
    as UI. Estimated token counts are prefixed with ``~`` so an estimate never reads as a
    measurement.
    """
    prefix = "~" if stats.tokens_estimated else ""
    timings = (
        f"parse {stats.parse_seconds:.1f}s · "
        f"retrieve {stats.retrieve_seconds:.2f}s · "
        f"generate {stats.generate_seconds:.2f}s"
    )
    if rate_limited:
        status = f'<span class="warn">● rate limited · retrying in {rate_limited:.0f}s</span>'
    else:
        status = '<span class="ok">● Groq free tier</span>'

    return _block(f"""
    <div class="fl-obs">
      <span>⚡ <strong>{_esc(model)}</strong></span>
      <span class="sep">│</span>
      <span>⏱ {timings}</span>
      <span class="sep">│</span>
      <span>▤ {prefix}{stats.total_tokens:,} tokens · <span class="cost">$0.00</span></span>
      <span class="sep">│</span>
      {status}
      <span class="sep">│</span>
      <span>{stats.chunks_retrieved} chunks retrieved</span>
    </div>
    """)


def empty_workspace() -> str:
    """The first thing a new user sees, so it has to be good (design.md §5.1).

    The "parsed locally on your machine" line is the single most reassuring fact about
    this product and belongs in the first thing anyone reads.
    """
    return _block("""
    <div class="fl-empty">
      <div class="fl-empty-icon">⬒</div>
      <div class="fl-empty-title">Drop a financial document here</div>
      <div class="fl-empty-sub">
        PDF, PNG, JPG · up to 25 MB<br>
        Parsed <strong>locally on your machine</strong> — the document never leaves it
      </div>
    </div>
    """)


# ── Extraction dashboard ─────────────────────────────────────────────────────


def vendor_card(record: FinancialRecord) -> str:
    bits: list[str] = []
    if record.invoice_number:
        bits.append(f"#{_esc(record.invoice_number)}")
    if record.billing_period_start and record.billing_period_end:
        bits.append(f"{record.billing_period_start} → {record.billing_period_end}")
    elif record.billing_date:
        bits.append(str(record.billing_date))
    bits.append(f"{record.page_count} page(s)")
    if record.used_vision_fallback:
        bits.append("read via OCR")

    return _block(f"""
    <div class="fl-panel">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;">
        <div>
          <div class="fl-vendor">{_esc(record.vendor_name)}</div>
          <div class="fl-meta">{_esc(" · ".join(bits))}</div>
        </div>
        <span class="fl-badge">{_esc(record.document_type.replace("_", " "))}</span>
      </div>
    </div>
    """)


def validation_banner(record: FinancialRecord) -> str:
    """One of three states, driven by arithmetic alone (decision D-19).

    Advisory warnings render separately, because a "please verify" note must never look
    like a failed reconciliation.
    """
    state = record.validation_state
    currency = record.currency

    if state == "validated":
        body = (
            f"Line items + tax = {_money(record.total_amount, currency)}, "
            f"matching the stated total."
        )
        icon = "✓"
    elif state == "mismatch":
        difference = abs(record.computed_total - (record.total_amount or Decimal("0")))
        body = (
            f"Line items + tax = {_money(record.computed_total, currency)}, but the document "
            f"states {_money(record.total_amount, currency)} "
            f"(difference {_money(difference, currency)}). Review the rows below."
        )
        icon = "⚠"
    else:
        body = "The total could not be read from this document. Nothing has been assumed in its place."
        icon = "⚠"

    banner = _block(f"""
    <div class="fl-banner {state}">
      <span class="icon">{icon}</span>
      <span><strong>{state.title()}.</strong> {_esc(body)}</span>
    </div>
    """)

    advisories = ""
    if record.extraction_warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in record.extraction_warnings)
        advisories = _block(f"""
        <div class="fl-banner incomplete">
          <span class="icon">ℹ</span>
          <span><strong>Notes.</strong>
            <ul style="margin:4px 0 0 16px;padding:0;">{items}</ul>
          </span>
        </div>
        """)
    return banner + advisories


def line_item_table(items: Iterable[LineItem], currency: str) -> str:
    rows: list[str] = []
    for item in items:
        band = item.confidence_band
        rows.append(
            f"""<tr>
              <td class="desc">{_esc(item.description)}</td>
              <td class="num">{_esc(item.quantity) if item.quantity is not None else "—"}</td>
              <td class="num">{_unit_price(item.unit_price)}</td>
              <td class="num">{_money(item.amount)}</td>
              <td style="text-align:center;" title="confidence {item.confidence:.0%}, page {item.source_page}">
                <span class="fl-dot {band}"></span>
              </td>
            </tr>"""
        )

    if not rows:
        return (
            '<div class="fl-meta">No line items detected. This may be a summary document — '
            "try asking a question about it directly.</div>"
        )

    return _block(f"""
    <div class="fl-scroll">
      <table class="fl-table">
        <thead><tr>
          <th>Description</th><th style="text-align:right;">Qty</th>
          <th style="text-align:right;">Unit price</th>
          <th style="text-align:right;">Amount ({_esc(currency)})</th>
          <th style="text-align:center;">Conf.</th>
        </tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    """)


def totals_panel(record: FinancialRecord) -> str:
    rows = [
        f'<div class="fl-total-row"><span>Subtotal</span>'
        f'<span class="v">{_money(record.subtotal)}</span></div>'
    ]
    for tax in record.tax_lines:
        label = tax.label + (f" ({_percent(tax.rate)}%)" if tax.rate is not None else "")
        rows.append(
            f'<div class="fl-total-row"><span>{_esc(label)}</span>'
            f'<span class="v">{_money(tax.amount)}</span></div>'
        )
    rows.append(
        f'<div class="fl-total-row grand"><span>TOTAL</span>'
        f'<span class="v">{_money(record.total_amount, record.currency)}</span></div>'
    )
    return f'<div class="fl-totals">{"".join(rows)}</div>'


# ── Document previewer ───────────────────────────────────────────────────────


def page_view(parsed: ParsedDocument, page_number: int, highlight: Citation | None = None) -> str:
    """A page image with an optional citation highlight laid over it.

    The overlay is absolutely-positioned CSS driven by ``BoundingBox.as_css_percent()``.
    That geometry is why this works without a PDF rendering library — and why the
    Streamlit-versus-React question (design.md §2) resolved toward Streamlit.
    """
    page = next((p for p in parsed.pages if p.page_number == page_number), None)
    if page is None or not page.image_path:
        return '<div class="fl-meta">No preview available for this page.</div>'

    path = Path(page.image_path)
    if not path.exists():
        return '<div class="fl-meta">Page image is missing from disk.</div>'

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")

    overlay = ""
    if highlight is not None and highlight.bbox is not None and highlight.page == page_number:
        css = highlight.bbox.as_css_percent()
        overlay = (
            f'<div class="fl-highlight" style="left:{css["left"]};top:{css["top"]};'
            f'width:{css["width"]};height:{css["height"]};"></div>'
        )

    return _block(f"""
    <div class="fl-page-wrap">
      <img src="data:image/png;base64,{encoded}" alt="Page {page_number}"/>
      {overlay}
    </div>
    """)


# ── Chat ─────────────────────────────────────────────────────────────────────


def user_message(text: str) -> str:
    return f'<div class="fl-msg-user">{_esc(text)}</div>'


def render_answer_html(text: str, citations: list[Citation]) -> str:
    """Turn inline ``[file:page]`` markers into citation chips.

    Markers that resolved to a real retrieved chunk become chips; anything else is left
    as plain text rather than silently deleted, so an invented reference stays visible.
    """
    known = {(c.filename.lower(), c.page) for c in citations}

    def replace(match: re.Match[str]) -> str:
        name, page = match.group(1).strip(), int(match.group(2))
        if (name.lower(), page) in known:
            return f'<span class="fl-cite">⧉ {_esc(name)} · p.{page}</span>'
        return match.group(0)

    escaped = _esc(text)
    # Markers survive escaping unchanged, so substitution runs on the escaped text.
    rendered = _CITATION_MARKER.sub(replace, escaped).replace("\n", "<br>")
    return f'<div class="fl-msg-assistant">{rendered}</div>'


def warning_strips(answer: Answer) -> str:
    """Every trust signal the chain computed, surfaced rather than hidden (Rule 5)."""
    strips: list[str] = []

    if not answer.is_grounded and not answer.refused:
        strips.append(
            "⚠ This answer could not be traced to your documents. Treat it as unverified."
        )
    if answer.dropped_citations:
        refs = ", ".join(answer.dropped_citations)
        strips.append(f"⚠ Referenced sources that were not retrieved: {refs}. Treat with caution.")
    for check in answer.contradicting_figures:
        strips.append(
            f"⚠ The figure {check.claimed:,.2f} does not match the extracted "
            f"{check.matched_field} of {check.expected:,.2f}."
        )
    for check in answer.unsupported_figures:
        strips.append(
            f"⚠ The figure {check.claimed:,.2f} does not appear in the document or the "
            f"retrieved context."
        )

    return "".join(f'<div class="fl-warn-strip">{_esc(s)}</div>' for s in strips)
