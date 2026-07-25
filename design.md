# Design Specification — Multimodal AI Financial Assistant

**Status:** Draft v1.0
**Last updated:** 2026-07-25
**Scope:** The complete web dashboard. This is a full application design, not a script with
widgets bolted on.

---

## 1. Design Philosophy

### The product's core emotion is **trust**.

Users bring a document they don't understand and a charge they may be angry about. The interface's
job is to make them feel that the numbers on screen are *real* — read from their document, not
invented by a chatbot. Every design decision below serves that.

**Three principles, in priority order:**

1. **Traceability over polish.** Every figure must be one click from its source. A citation chip
   that scrolls the previewer to the exact line on the exact page does more for trust than any
   amount of visual refinement.
2. **The document is the hero.** Not the chat. Not the logo. The user's document occupies the
   largest, highest-fidelity region of the screen at all times, because it is the evidence.
3. **Show the machine working.** Parsing takes seconds; reasoning takes seconds. Hiding that
   behind a generic spinner reads as stalling. Naming the stage — *"Detecting table structure…"*,
   *"Retrieving billing policy…"* — reads as competence.

### Explicit anti-goals

- **No AI-slop aesthetic.** No purple→blue gradient hero. No glowing sparkle icons on every
  button. No "✨ AI-Powered ✨" badges. No animated gradient borders. This is a financial tool;
  it should look closer to a well-made analytics product than to a landing page.
- **No decoration that isn't information.** Every colored element encodes a state (validated,
  warning, cited, low-confidence). Color is a data channel here, not seasoning.
- **No fake confidence.** Low-confidence fields look different from high-confidence fields. A
  document that failed arithmetic validation says so, prominently.

---

## 2. Framework Decision Gate

Two viable zero-cost paths. **Both satisfy the design below** — the design system is expressed as
CSS custom properties either way.

### Option A — Streamlit + heavy custom CSS *(recommended for v1)*

| | |
|---|---|
| **Cost** | $0 |
| **Time to full spec** | ~1 development phase |
| **Strengths** | Pure Python — no second language, no API boundary. Native streaming (`st.write_stream`), file upload, dataframes. Instant integration with `src/`. |
| **Weaknesses** | Custom CSS requires `st.markdown(unsafe_allow_html=True)` and fighting default styling. Precise citation-highlight overlays on the PDF previewer need a custom component. Full rerun-on-interaction model needs careful `st.session_state` discipline. |
| **Verdict** | **Recommended.** Gets all four surfaces to a genuinely polished state fastest, and keeps the whole project in one language. Streamlit's multi-page + `st.columns` + custom CSS can absolutely look like a real product — the "Streamlit look" is a default, not a ceiling. |

### Option B — React + Vite + FastAPI

| | |
|---|---|
| **Cost** | $0 |
| **Time to full spec** | ~2–3× Option A |
| **Strengths** | Total layout control. `react-pdf` gives real page rendering with coordinate-accurate highlight overlays. SSE streaming is native. Genuinely production-grade result. |
| **Weaknesses** | Requires a FastAPI layer, CORS, build tooling, and state management. Two languages, two run commands. |
| **Verdict** | The right call **if** the pixel-accurate citation overlay proves to be the make-or-break feature, or if this becomes a portfolio centerpiece where the frontend itself is the demo. |

> **Decision:** Default to **Option A**, built so the switch is cheap. `architecture.md` AD-7 already
> forbids `src/` from importing the UI framework, so `src/` is reusable verbatim behind a FastAPI
> layer if we migrate. **This decision is confirmed at the start of Phase 5, not before** — by then
> we will know how hard the highlight overlay actually is.

---

## 3. Design System

### 3.1 Color

Semantic tokens, defined once, themed light/dark. **Financial-domain colors are reserved and never
used decoratively.**

```css
:root {
  /* Surfaces — near-neutral, very slightly cool. Documents are white; the app must not compete */
  --surface-base:      #FAFAFA;   /* app background */
  --surface-raised:    #FFFFFF;   /* cards, panels */
  --surface-sunken:    #F1F2F4;   /* wells, previewer backdrop, code */
  --surface-overlay:   #FFFFFF;   /* modals, popovers */

  /* Text */
  --text-primary:      #16181D;
  --text-secondary:    #5A6070;
  --text-tertiary:     #8A90A0;
  --text-inverse:      #FFFFFF;

  /* Borders */
  --border-subtle:     #E6E8EC;
  --border-default:    #D3D7DE;
  --border-strong:     #A8AEBB;

  /* Brand — a restrained slate-teal. Used for primary actions and active states ONLY */
  --brand-600:         #0F766E;
  --brand-500:         #14867C;
  --brand-100:         #D6F0EC;

  /* Semantic — these carry MEANING. Never decorative. */
  --state-validated:   #16794C;   /* arithmetic checks passed */
  --state-warning:     #B45309;   /* low confidence, validation mismatch */
  --state-error:       #B42318;   /* parse failure, API failure */
  --state-info:        #1D4ED8;   /* citations, retrieved context */
  --state-*-bg:        /* 12%-alpha companions for each of the above */;

  /* Financial semantics — currency direction, distinct from success/error */
  --amount-debit:      #B42318;   /* money out */
  --amount-credit:     #16794C;   /* money in */
  --amount-neutral:    var(--text-primary);

  /* Citation highlight — must read over white document pages */
  --citation-highlight: rgba(29, 78, 216, 0.16);
  --citation-border:    rgba(29, 78, 216, 0.55);
}

@media (prefers-color-scheme: dark) {
  :root {
    --surface-base:   #0F1115;
    --surface-raised: #171A21;
    --surface-sunken: #0B0D11;
    --text-primary:   #ECEEF3;
    --text-secondary: #A0A7B6;
    --text-tertiary:  #6B7280;
    --border-subtle:  #232833;
    --border-default: #2E3543;
    /* Semantic hues lighten ~15% for contrast on dark; document previewer keeps a light
       backdrop regardless — you do not invert someone's invoice. */
  }
}
```

**Contrast:** all text meets WCAG AA (4.5:1 body, 3:1 large). Semantic state is **never conveyed by
color alone** — every state pairs color with an icon and a text label. This is a table full of
numbers; red/green-only encoding fails ~8% of male users.

### 3.2 Typography

```css
--font-sans: "Inter", -apple-system, "Segoe UI", system-ui, sans-serif;
--font-mono: "JetBrains Mono", "SF Mono", "Cascadia Code", ui-monospace, monospace;
```

Both are free and open-source (Rule 1). Self-hosted or system-fallback — no CDN dependency.

| Token | Size / Line height | Weight | Use |
|---|---|---|---|
| `--type-display` | 30 / 36 px | 600 | Page title (one per view) |
| `--type-h1` | 22 / 28 px | 600 | Panel headers |
| `--type-h2` | 17 / 24 px | 600 | Card titles, section headers |
| `--type-body` | 14.5 / 22 px | 400 | Chat, descriptions, prose |
| `--type-label` | 13 / 18 px | 500 | Field labels, table headers |
| `--type-caption` | 12 / 16 px | 400 | Metadata, timestamps, confidence |
| `--type-mono-lg` | 16 / 22 px | 500 | **Total amount** |
| `--type-mono` | 13.5 / 20 px | 400 | All other amounts, IDs, token counts |

**Non-negotiable typographic rules:**
- **Every monetary amount is monospace with tabular figures** (`font-variant-numeric:
  tabular-nums`). Amounts in a column must align on the decimal point. Proportional digits in a
  financial table are a correctness problem, not a taste problem.
- Currency is always rendered with an explicit ISO code on totals: `$1,284.50 USD`. Ambiguous `$`
  in a multi-currency tool is a real failure mode.
- Negative amounts use a leading minus **and** `--amount-credit`/`--amount-debit` color **and**
  parenthesis convention is avoided (it reads as accounting jargon to non-accountants).

### 3.3 Spacing, Radius, Elevation

4px base scale: `--space-1: 4px` … `--space-2: 8`, `-3: 12`, `-4: 16`, `-5: 24`, `-6: 32`,
`-7: 48`, `-8: 64`.

```css
--radius-sm: 6px;    /* chips, badges, inputs */
--radius-md: 10px;   /* cards, buttons */
--radius-lg: 14px;   /* panels */
--radius-full: 999px;

--shadow-sm: 0 1px 2px rgba(16,18,29,.05), 0 1px 3px rgba(16,18,29,.04);
--shadow-md: 0 4px 12px rgba(16,18,29,.07), 0 1px 3px rgba(16,18,29,.05);
--shadow-lg: 0 12px 32px rgba(16,18,29,.12);
```

Elevation is **structural**: `sm` = card at rest, `md` = hovered/active card, `lg` = overlay only.
No shadow on flat text elements. Borders do most of the separation work; shadows are for genuine
layering.

### 3.4 Motion

```css
--ease-out: cubic-bezier(0.16, 1, 0.3, 1);
--dur-fast: 120ms;    /* hover, focus, chip press */
--dur-base: 200ms;    /* panel transitions, accordion */
--dur-slow: 320ms;    /* page/route change, drawer */
```

- Streaming text does **not** animate per token beyond a caret. Fading each token in is a common
  AI-app tic that makes text harder to read.
- The citation-scroll animation is the one deliberate flourish: clicking a chip smooth-scrolls the
  previewer and pulses the highlight region twice. It is the moment the product proves itself.
- `@media (prefers-reduced-motion: reduce)` collapses all durations to `1ms` and replaces
  smooth-scroll with instant jump.

---

## 4. Application Layout

### 4.1 Global Shell — Desktop ≥ 1280px

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  ◈ FinLens          Workspace · Extraction · Chat          [◐ theme]  [⚙ settings] │  56px
├──────────────────────────────────────────────────────────────────────────────────┤
│ ⚡ llama-3.3-70b-versatile │ ⏱ parse 4.2s · retrieve 0.3s · gen 1.8s │ ▤ 3,412 tok │  40px
├────────────────────────────┬─────────────────────────────────────────────────────┤
│                            │                                                     │
│   DOCUMENT WORKSPACE       │   EXTRACTION DASHBOARD                              │
│   (previewer, 42%)         │   (structured record, 33%)                          │
│                            │                                                     │
│   ┌────────────────────┐   │   ┌─────────────────────────────────────────────┐   │
│   │                    │   │   │ Amazon Web Services      invoice · 2 pages  │   │
│   │   page render      │   │   │ Billing 2026-06-01 → 06-30 · INV-7741820    │   │
│   │   (high-res)       │   │   └─────────────────────────────────────────────┘   │
│   │                    │   │   ┌─────────────────────────────────────────────┐   │
│   │  ░░ highlight ░░   │   │   │ ✓ Validated  Σ line items + tax = total     │   │
│   │                    │   │   └─────────────────────────────────────────────┘   │
│   │                    │   │   Line items                    [sort ▾] [export]   │
│   │                    │   │   ┌──────────────────┬─────┬────────┬──────────┐    │
│   └────────────────────┘   │   │ Description      │ Qty │  Unit  │   Amount │    │
│   ◀ 1 / 2 ▶   [−][100%][+] │   │ EC2 t3.medium    │ 730 │  0.042 │    30.66 │    │
│                            │   │ NAT Gateway data │   1 │ 412.90 │   412.90 │◀── │
├────────────────────────────┤   │ S3 Standard      │   1 │  18.44 │    18.44 │    │
│   RAG CHAT PANEL (25%)     │   └──────────────────┴─────┴────────┴──────────┘    │
│                            │   Subtotal 462.00 · Tax (8.5%) 39.27 · TOTAL 501.27 │
└────────────────────────────┴─────────────────────────────────────────────────────┘
```

The chat panel is a full-height right rail on wide screens; the ASCII above compresses it for
legibility. Actual desktop split: **Workspace 42% / Extraction 33% / Chat 25%**, with drag-resizable
dividers persisted to local storage.

### 4.2 Responsive Behavior

| Breakpoint | Layout |
|---|---|
| **≥ 1440px** | Three columns as above; observability bar shows all metrics inline |
| **1024–1439px** | Three columns, chat rail narrows to 22%; observability bar drops latency breakdown to a hover tooltip |
| **768–1023px** | **Two columns:** Workspace left (55%), right column becomes a tabbed panel `[Extraction | Chat]`; observability bar becomes a single-line summary |
| **< 768px** | **Single column, bottom tab bar:** `Document · Data · Chat`. Previewer gets full width; chat becomes a full-screen sheet. Observability collapses to a tappable pill showing model + total latency |

**Hard rule:** the page body never scrolls horizontally at any width. Wide tables scroll inside
their own `overflow-x: auto` container with the description column sticky-left.

---

## 5. Surface Specifications

### 5.1 Document Workspace

**Empty state — the first thing a new user sees. It must be excellent.**

```
        ┌─────────────────────────────────────────────┐
        │                                             │
        │              ⬒  (outlined doc icon)         │
        │                                             │
        │        Drop a financial document here       │
        │      PDF, PNG, JPG · up to 25 MB · parsed   │
        │            locally on your machine          │
        │                                             │
        │            [  Choose a file  ]              │
        │                                             │
        │  Or try a sample:                           │
        │  [AWS invoice] [Expense report] [Statement] │
        └─────────────────────────────────────────────┘
              2px dashed --border-default
```

- The line *"parsed locally on your machine"* is deliberate. It is the single most reassuring fact
  about this product and belongs in the first thing users read.
- **Sample documents are essential.** A user with no invoice at hand must still be able to
  experience the product in one click.
- Drag-over state: border → `--brand-500` solid, background → `--brand-100`, icon scales to 1.06.
  Transition 120ms. **The drop target is the entire panel**, not just the dashed box.

**Loaded state:**
- High-resolution page render (Docling page images at ≥ 150 DPI; upscale on zoom, never blur).
- Page navigation: `◀ 3 / 12 ▶`, keyboard `←`/`→`, plus a scrollable page-thumbnail strip when
  `page_count > 3`.
- Zoom: `−` / `fit` / `+`, range 50–300%, `Ctrl+scroll` support. Zoom level persists across pages.
- **Citation highlight overlay** — absolutely positioned translucent rectangles over the page
  image, keyed to chunk bounding boxes from Docling. Clicking a citation chip in chat:
  1. switches to the cited page,
  2. smooth-scrolls the region into view,
  3. pulses the highlight twice,
  4. leaves it lit until the next citation is selected.
- Document switcher: a compact list of previously uploaded documents in this session, with a
  per-item overflow menu (`Set as active`, `Remove from index`, `Download`).

**Loading state — narrate the pipeline, do not spin:**
```
  ▓▓▓▓▓▓▓▓▓▓░░░░░░░░  Detecting page layout…            (step 2 of 4)
  ✓ Rendered 12 pages   ✓ Layout detected
  ⟳ Extracting table structure…   ○ Building index
```

**Error state:** specific and recoverable. *"This PDF is password-protected. Remove the password
and re-upload."* — with a `Try another file` action. Never a stack trace, never "Something went
wrong."

### 5.2 Extraction Dashboard

**Vendor header card** — vendor name (`--type-h1`), document type badge, invoice number, billing
period, page count. Each field is inline-editable on click (FR-2 corrections).

**Validation banner** — always present, one of three states:

| State | Appearance |
|---|---|
| ✓ Validated | `--state-validated` left border, `--state-validated-bg`. *"Σ line items + tax = total ($501.27)"* |
| ⚠ Mismatch | `--state-warning`. *"Line items + tax = $498.11, but the document states $501.27 (difference $3.16). Review the highlighted rows."* Mismatching rows get a warning affordance in the table. |
| ⚠ Incomplete | `--state-warning`. *"Subtotal could not be read from this document."* — never a fabricated 0 (Rule 2.3). |

**Line items table:**
- Columns: Description · Category · Qty · Unit Price · Amount · (confidence dot).
- Sortable on every column; sort indicator is explicit (`▲`/`▼`), not a hover surprise.
- **Amount column: monospace, tabular figures, right-aligned, decimal-aligned.**
- Row hover raises `--surface-sunken` and reveals a `⌖ Show in document` action that highlights the
  source region in the previewer — **the reverse direction of the citation flow**, and just as
  important for trust.
- **Confidence encoding:** a small dot at the row end — filled `--state-validated` (≥0.85), hollow
  `--state-warning` (0.6–0.85), hollow `--state-error` (<0.6) — each with a tooltip stating the
  score and the source page. Never color-only: the fill/hollow shape carries it too.
- Sticky header on scroll; description column sticky-left on narrow viewports.
- Zebra striping is **off** — borders at `--border-subtle` are enough, and stripes fight the
  confidence and warning row states.
- Empty state: *"No line items detected. This document may be a summary statement — try asking a
  question about it directly."*

**Totals panel** — visually distinct (`--surface-sunken`, `--radius-lg`), right-aligned stack:
```
        Subtotal                    462.00
        Sales tax (8.5%)             39.27
        ─────────────────────────────────
        TOTAL                   $ 501.27 USD      ← --type-mono-lg, 600 weight
```

**Export** — `Copy as JSON` / `Download CSV`. Free, local, no dependency.

### 5.3 Interactive RAG Chat Panel

**Message rendering:**
- User messages: right-aligned, `--surface-sunken`, `--radius-md`, max-width 85%.
- Assistant messages: full-width, no bubble, on `--surface-raised`. Removing the bubble makes long
  cited explanations far more readable and signals "document analysis," not "chat toy."
- Markdown supported: bold, lists, and **tables** — the model will often answer with a small
  comparison table, and it must render properly.

**Citation chips** — inline, after the sentence they support:
> The $412.90 charge is NAT Gateway data processing. [`⧉ aws-invoice.pdf · p.1`] Your cloud policy
> caps NAT egress at $200/month. [`⧉ cloud-policy.md · p.3`]

- Style: `--radius-full`, `--state-info-bg`, `--state-info` text, `--type-caption`, monospace
  filename.
- Hover: tooltip with the retrieved snippet + relevance score.
- Click: drives the previewer (§5.1).
- **An answer with zero citations renders a warning strip above it:** *"⚠ This answer could not be
  traced to your documents. Treat it as unverified."* (FR-4.1). This must never be quietly omitted.
- **A numeric cross-check failure renders inline:** the figure gets a `--state-warning` underline
  and a tooltip: *"This figure does not match the extracted total ($501.27)."* (FR-4.4).

**Streaming:** a thin caret at the token frontier. Stage label above the message while working:
*"Retrieving from 2 documents…"* → *"Reasoning…"*. `Stop generating` button available throughout.

**Quick-prompt chips** — above the input, contextual to the loaded document:

```
[ Why was this charge deducted? ]  [ Explain this AWS charge ]
[ Verify against company travel policy ]  [ Compare to last month ]
[ Break down the tax ]  [ Find anything unusual ]
```

- Horizontally scrollable, no wrap-thrash.
- **Chips are generated from the extracted record**, not hardcoded: if `vendor_name == "Amazon Web
  Services"`, the chip reads *"Explain this AWS charge"*. If a policy corpus exists, the
  policy-verification chip appears. If a prior statement from the same vendor is indexed, the
  comparison chip appears. Contextual chips are the difference between a demo and a product.

**Input:** auto-growing textarea (1–6 rows), `Enter` sends / `Shift+Enter` newline, attach button
for adding a document mid-conversation, character-count hint only past 500 chars.

**Empty state:** *"Ask anything about this document. Every answer will cite the exact page it came
from."*

### 5.4 Observability Bar

A persistent 40px strip beneath the header. **This is a credibility feature** — showing cost and
latency signals a system with nothing to hide.

```
⚡ llama-3.3-70b-versatile ▾ │ ⏱ parse 4.2s · retrieve 0.31s · generate 1.84s │ ▤ 3,412 tokens · ~$0.00 │ ● Groq free tier
```

| Element | Behavior |
|---|---|
| **Model selector** | Dropdown listing configured models from `config.py` — reasoning / vision / utility. Shows which is active for the current operation. Switching persists to session state. |
| **Latency breakdown** | Per-stage, from `RunStats`. Segments color-code against NFR targets: within → `--text-secondary`, exceeding → `--state-warning`. |
| **Token counter** | Cumulative session estimate. **`~$0.00` is displayed deliberately and permanently** — it is the project's thesis (Rule 1) rendered as UI. |
| **Provider status** | `● Groq free tier` — green at rest; `● Rate limited · retrying in 12s` in `--state-warning` on 429, with a live countdown. This turns the most likely runtime failure into a legible, calm event. |
| **Trace link** | If LangSmith is configured, a `↗ trace` link to the current run. **Hidden entirely when unconfigured** — never a dead link, never a nag to sign up. |

Clicking the bar expands a detail drawer: per-stage timings, retrieved chunk count with scores,
the exact prompt token estimate, and the last 10 runs as a sparkline.

---

## 6. Component Inventory

| Component | States to implement |
|---|---|
| Button (primary/secondary/ghost/danger) | rest, hover, active, focus-visible, disabled, loading |
| Upload dropzone | rest, drag-over, uploading (%), success, error |
| Data table | rest, hover, sorted, empty, loading skeleton, error |
| Chat message | user, assistant, streaming, error, uncited-warning |
| Citation chip | rest, hover (tooltip), active/selected |
| Quick-prompt chip | rest, hover, pressed, disabled (no document loaded) |
| Validation banner | validated, mismatch, incomplete |
| Confidence dot | high, medium, low (fill + color + tooltip) |
| Page previewer | empty, loading, loaded, highlight-active, error |
| Observability metric | normal, exceeding-target, unavailable |
| Toast | info, success, warning, error — auto-dismiss 5s, manual close |
| Skeleton loader | table rows, message block, page render |

**Every component ships with its loading and error states in the same commit.** A component
implemented only in its happy path is not done.

---

## 7. Accessibility

- WCAG 2.1 AA contrast throughout.
- Full keyboard navigation: `Tab` order follows visual order; `/` focuses chat input; `Esc` closes
  overlays; `←`/`→` navigate pages when the previewer has focus.
- `:focus-visible` ring: 2px `--brand-500` + 2px offset. Never `outline: none` without a
  replacement.
- Semantic HTML: real `<table>` for data tables, real `<button>` for actions, `<main>`/`<aside>`
  landmarks.
- `aria-live="polite"` on the streaming message region; `aria-live="assertive"` on error toasts.
- Every icon-only button has an `aria-label`.
- **State is never color-only** — icon + text label accompany every semantic color.
- `prefers-reduced-motion` fully honored.

---

## 8. Definition of Visual Done

Phase 5A is complete only when all of the following are true:

- [ ] All four surfaces implemented with every state from §6
- [ ] Design tokens defined once in CSS custom properties; **zero hardcoded hex values** in
      component code
- [ ] Light and dark themes both correct; the document previewer backdrop stays light in both
- [ ] Correct and usable at 1440 / 1024 / 768 / 390 px; no horizontal body scroll at any width
- [ ] Citation click → correct page → correct region highlighted → pulse animation
- [ ] Reverse flow: table row `⌖ Show in document` → correct region highlighted
- [ ] All amounts monospace, tabular, decimal-aligned; totals carry an ISO currency code
- [ ] Every async operation shows a **named** stage, not a bare spinner
- [ ] Every error state is specific, actionable, and free of stack traces
- [ ] Uncited answers show the unverified warning; cross-check failures show the inline warning
- [ ] Quick-prompt chips are generated from the extracted record, not hardcoded
- [ ] Keyboard-only walkthrough of the full flow succeeds
- [ ] `prefers-reduced-motion` honored
- [ ] Zero AI-slop patterns (§1 anti-goals) — checked deliberately, not assumed

---

## 9. Open Design Questions

- **ODQ-1** Does Docling expose per-chunk bounding boxes with enough fidelity for pixel-accurate
  highlight overlays, or do we fall back to page-level highlighting? **Resolve in Phase 2** — this
  is the single input that decides §2's framework question.
- **ODQ-2** Should the Extraction Dashboard and Chat share a column with tabs even on desktop,
  giving the previewer more room? *Prototype both in Phase 5A.*
- **ODQ-3** Multi-document comparison view (side-by-side statements) — genuinely useful, but likely
  v1.1 scope.
