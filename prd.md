# PRD — Multimodal AI Financial Assistant

**Project:** #4 — Multimodal AI Financial Assistant
**Status:** Draft v1.0
**Owner:** roym6363@gmail.com
**Last updated:** 2026-07-25

---

## 1. Problem Statement

Finance-adjacent knowledge workers (founders, ops leads, freelancers, accountants) receive a
constant stream of financial documents — credit card statements, SaaS invoices, cloud billing
PDFs, expense reports, receipts. When a charge looks wrong, answering *"why was this deducted?"*
requires three separate acts of labor:

1. **Reading** the document (often a screenshot or a scanned PDF with multi-column tables).
2. **Extracting** the structured facts (vendor, line items, tax, total, billing period).
3. **Reconciling** those facts against context that lives elsewhere — a prior month's bill, a
   company travel policy, a plan's overage terms.

Existing tools do exactly one of these. OCR tools extract but do not reason. Chatbots reason but
hallucinate numbers. Accounting SaaS does both but costs money, requires data upload to a
third party, and cannot answer open-ended "why" questions.

## 2. Product Vision

A **locally-parsed, cloud-reasoned** assistant. The user drops in a financial document, the system
extracts a verified structured record, indexes it alongside supporting policy/history documents,
and then answers natural-language questions with **grounded, citation-backed explanations** — every
number in the answer traceable to a specific region of a specific source document.

> **The North Star test:** a user uploads an AWS invoice, asks *"Why was this charge deducted?"*,
> and gets back an answer that names the line item, the amount, the reason (e.g. NAT Gateway
> data processing above the free tier), and cites both the invoice row and the relevant pricing
> policy document — with zero hallucinated figures.

## 3. Target Users & Jobs To Be Done

| Persona | Job To Be Done | Success looks like |
|---|---|---|
| **Solo founder / indie hacker** | "My cloud bill jumped 3x — what changed?" | Month-over-month line-item delta with an explanation |
| **Ops / finance associate** | "Does this expense report comply with our travel policy?" | Per-line-item compliance verdict with the policy clause cited |
| **Freelancer / contractor** | "What tax was applied to this invoice and is it right?" | Tax breakdown, rate derivation, subtotal reconciliation |
| **Accountant reviewing records** | "Give me these 40 receipts as structured rows" | Exportable table of normalized fields |

## 4. Core User Flows

### Flow A — Upload & Extract
1. User drags a PDF or image (PNG/JPG) into the Document Workspace.
2. System parses the document locally (layout-aware, table-aware).
3. Extraction Dashboard renders the structured record for review.
4. User can correct any field inline; corrections are persisted and fed back into the context.

### Flow B — Ask & Explain
1. User types (or clicks a quick-prompt chip) in the RAG Chat Panel.
2. System retrieves relevant chunks from the current document **and** the supporting corpus.
3. LLM streams a grounded explanation; citations are rendered as clickable chips.
4. Clicking a citation scrolls and highlights the source region in the document previewer.

### Flow C — Reconcile Against Policy
1. User uploads (once) a policy corpus: travel policy, prior statements, vendor pricing pages.
2. On any subsequent question, retrieval spans both the active document and the policy corpus.
3. Answer explicitly separates *what the document says* from *what the policy says*.

## 5. Functional Requirements

### FR-1 — Document Ingestion
- **FR-1.1** Accept `.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp` up to 25 MB per file.
- **FR-1.2** Accept multi-page PDFs; page count exposed in the UI.
- **FR-1.3** Support drag-and-drop and file-picker upload.
- **FR-1.4** Reject unsupported/corrupt files with a specific, actionable error message.
- **FR-1.5** Persist uploads to a local `data/uploads/` directory; never transmit raw files to a
  third-party service except as explicitly-scoped vision-model calls (see FR-2.3).

### FR-2 — Structured Extraction
- **FR-2.1** Extract the canonical financial record:

  | Field | Type | Required |
  |---|---|---|
  | `vendor_name` | `str` | yes |
  | `vendor_address` | `str \| None` | no |
  | `document_type` | `Literal["invoice","statement","receipt","expense_report"]` | yes |
  | `invoice_number` | `str \| None` | no |
  | `billing_date` | `date \| None` | yes* |
  | `due_date` | `date \| None` | no |
  | `billing_period_start` / `billing_period_end` | `date \| None` | no |
  | `currency` | `str` (ISO-4217) | yes |
  | `line_items` | `list[LineItem]` | yes |
  | `subtotal` | `Decimal \| None` | yes* |
  | `tax_lines` | `list[TaxLine]` | no |
  | `total_amount` | `Decimal` | yes |

  `LineItem` = `{description, quantity, unit_price, amount, category?}`
  `TaxLine` = `{label, rate?, amount}`

  \* Required if present in the source; `None` is permitted with a `low_confidence` flag rather
  than a fabricated value.

- **FR-2.2** Layout-aware table parsing — multi-column invoice tables must survive extraction as
  discrete rows, not as flattened text.
- **FR-2.3** For image-only / scanned documents where layout parsing yields insufficient text,
  fall back to **local OCR** (Docling + RapidOCR) over the page image. Escalation is automatic,
  per document, gated on `text_yield_ratio`. *Revised 2026-07-25 (decision D-15): this was
  specified as a cloud vision-LLM pass, but Groq serves no image-input model. Local OCR is free,
  deterministic, rate-limit-free, and keeps page images on the user's machine.*
- **FR-2.4** **Arithmetic validation:** assert `sum(line_items.amount) + sum(tax_lines.amount) ==
  total_amount` within a ±0.02 tolerance. On mismatch, surface a warning banner rather than
  silently accepting.
- **FR-2.5** Every extracted field carries a `confidence` score and a `source_page`.

### FR-3 — Retrieval (RAG)
- **FR-3.1** Chunk parsed documents with layout awareness — a table row is never split across
  chunks.
- **FR-3.2** Embed chunks locally; store in a local persistent vector store.
- **FR-3.3** Maintain two logical collections: `documents` (uploaded financial docs) and
  `policies` (supporting context: prior bills, travel policy, vendor pricing terms).
- **FR-3.4** Retrieval must be filterable by `document_id` so a question can be scoped to the
  active document.
- **FR-3.5** Every retrieved chunk returns `{text, document_id, page, score}` for citation.

### FR-4 — Grounded Reasoning
- **FR-4.1** Answers must cite at least one retrieved chunk; an answer with zero citations is
  rendered with an explicit "unverified" warning.
- **FR-4.2** The model must be instructed to answer *"I cannot determine this from the provided
  documents"* rather than infer a figure not present in context.
- **FR-4.3** Responses stream token-by-token to the UI.
- **FR-4.4** Numeric claims in the answer are cross-checked against the extracted structured
  record where the field exists.
- **FR-4.5** Multi-turn conversation with history, scoped per document session.

### FR-5 — Web Dashboard
See `design.md` for the full specification. Summary requirements:
- **FR-5.1** Document Workspace — drag-and-drop upload + high-resolution page previewer with
  zoom, page navigation, and citation highlighting overlays.
- **FR-5.2** Extraction Dashboard — sortable data tables for line items, a vendor detail card,
  and a tax/subtotal/total breakdown panel.
- **FR-5.3** Interactive RAG Chat Panel — streaming responses, inline citation chips,
  quick-prompt suggestion chips.
- **FR-5.4** Observability Bar — estimated token usage, per-stage processing latency, active
  model selection.
- **FR-5.5** Fully responsive: usable at 1440px, 1024px, and 768px widths.

### FR-6 — Evaluation
- **FR-6.1** A golden set of ≥ 20 (document, question, expected-answer) triples.
- **FR-6.2** Retrieval metrics: context precision, context recall.
- **FR-6.3** Generation metrics: faithfulness, answer relevancy.
- **FR-6.4** Extraction metrics: per-field exact-match accuracy, total-amount accuracy.
- **FR-6.5** Traces exported to an observability platform for every chain invocation.

## 6. Non-Functional Requirements

| ID | Requirement | Target |
|---|---|---|
| NFR-1 | **Zero cost** — no paid software, no paid API tier | Hard constraint; see `rules.md` Rule 1 |
| NFR-2 | Single-page parse latency (text PDF) | < 8 s |
| NFR-3 | Single-page parse latency (scanned image, local OCR fallback) | < 25 s |
| NFR-4 | First streamed token after question submit | < 3 s |
| NFR-5 | Local-first — documents and embeddings never leave the machine except for scoped LLM calls | Hard constraint |
| NFR-6 | Runs on CPU-only hardware, 8 GB RAM | Hard constraint |
| NFR-7 | Type-hinted, modular, unit-testable source modules | See `rules.md` Rule 2 |

## 7. Out of Scope (v1)

- Multi-user auth, accounts, or role-based access.
- Direct bank / accounting-software integrations (Plaid, QuickBooks, Xero).
- Automated dispute filing or any write action against a financial provider.
- Handwriting recognition.
- Non-Latin-script documents.
- Mobile-native apps (the web dashboard is responsive; that is the mobile story).

## 8. Success Metrics

| Metric | Target |
|---|---|
| Total-amount extraction accuracy on the golden set | ≥ 95% |
| Line-item row recall on the golden set | ≥ 90% |
| Ragas faithfulness | ≥ 0.85 |
| Ragas answer relevancy | ≥ 0.80 |
| Context recall | ≥ 0.85 |
| Hallucinated-figure rate (manual audit, 50 answers) | 0 |
| Monthly infrastructure cost | $0.00 |

## 9. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Groq free-tier rate limits during demo/eval | High | Request batching, exponential backoff, local response cache keyed on prompt hash. Mitigated further by D-15: parsing now consumes zero quota, so only reasoning calls count |
| OCR misreads figures on poor-quality scans | High | Layout parser is primary; OCR is fallback only, with a confidence floor (`OCR_TEXT_SCORE`). Arithmetic validation (FR-2.4) catches the errors that slip through |
| Groq model deprecation mid-project | **Materialized** | Happened twice before a line of chain code was written (D-3, D-15). Model IDs are centralized in `src/config.py` and `scripts/check_models.py` probes liveness against the live account |
| Docling first-run model download is large | Medium | Documented one-time setup step; models cached locally |
| PII in uploaded financial documents | High | Local-first storage; `data/` gitignored; redaction pass before any cloud call is a v1.1 candidate |
| Ragas evaluation itself requires an LLM (cost) | Medium | Point Ragas at the same free Groq endpoint; local embeddings for the embedding-based metrics |

## 10. Open Questions

- **OQ-1** Streamlit vs. React for the dashboard — resolved in `design.md` §2 (decision gate).
- **OQ-2** Should corrected extraction fields be written back into the vector store as an
  authoritative chunk? *(Leaning yes — deferred to Phase 5.)*
- **OQ-3** Do we persist chat history across app restarts? *(Leaning yes, SQLite — Phase 5.)*
