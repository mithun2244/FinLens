# Development Phases — Multimodal AI Financial Assistant

**Status:** Active plan
**Last updated:** 2026-07-25
**Current phase:** Phase 1 (not started)

Each phase has an explicit **Definition of Done**. A phase is not complete until every DoD item is
verifiably true and `memory.md` has been updated (Rule 3).

---

## Phase 0 — Documentation Scaffolding ✅

**Goal:** Establish persistent memory and specification before any code.

- [x] `prd.md` — requirements, user flows, success metrics
- [x] `architecture.md` — zero-cost stack, module layout, data contracts, decision log
- [x] `rules.md` — binding engineering rules
- [x] `phases.md` — this file
- [x] `design.md` — full web dashboard design specification
- [x] `memory.md` — session log initialized

**DoD:** All six documents exist in the project root and are internally consistent. ✅

---

## Phase 1 — Project Setup & Dependencies

**Goal:** A reproducible, verified environment. Every third-party dependency is installed,
importable, and confirmed reachable — *before* a line of business logic is written.

### Deliverables
| File | Purpose |
|---|---|
| `requirements.txt` | Pinned dependencies, grouped and commented with cost annotations |
| `.env.example` | Every required key, with a comment on where to get it free |
| `.gitignore` | `venv/`, `.env`, `data/`, `__pycache__/`, `.pytest_cache/`, HF cache |
| `src/__init__.py`, `src/config.py` | Env loading, **centralized model IDs**, paths, thresholds |
| `src/schemas.py` | Pydantic v2 models: `FinancialRecord`, `LineItem`, `TaxLine`, `Citation` |
| `scripts/check_models.py` | Groq `/models` liveness probe — reports which configured IDs are served |
| `scripts/verify_setup.py` | Imports every dependency, embeds a test string, opens Chroma, pings Groq |
| Directory skeleton | `src/ ui/ data/{uploads,chroma} evals/fixtures scripts/ tests/` |

### Key tasks
1. Activate the existing `venv/`; confirm Python 3.12.10.
2. Author `requirements.txt`. Pin major versions. Annotate each line with its cost ($0) and role.
3. Install. **Expect Docling's first import to download ~500 MB of layout/table models** — this is
   a one-time, free, local cache. Budget time for it and do it early, not mid-demo.
4. Write `config.py` with model IDs as named constants (`VISION_MODEL`, `REASONING_MODEL`,
   `UTILITY_MODEL`). Rule: **no model string is ever inlined elsewhere.**
5. Write `schemas.py`. All money fields are `Decimal`.
6. Run `check_models.py` — **confirm which Groq model IDs are actually live** before building on
   them (see `architecture.md` §3.5 on retired Llama 3.2 Vision endpoints).
7. Run `verify_setup.py` end to end.

### Definition of Done
- [ ] `python scripts/verify_setup.py` exits 0 with all green checks
- [ ] `python scripts/check_models.py` confirms the configured vision + reasoning models are served
- [ ] `python -c "import docling, chromadb, langchain_groq; print('ok')"` succeeds
- [ ] A test string embeds locally to a 384-dim vector with no network call
- [ ] `.env` exists locally, is gitignored, and is **not** in any commit
- [ ] `mypy src/` passes on `config.py` and `schemas.py`
- [ ] `memory.md` updated

### Risks
- Docling's model download may be slow or fail behind a proxy → retry, or pre-seed the HF cache.
- Groq free tier requires account signup → verify the key works *now*, not in Phase 4.

---

## Phase 2 — Document Extraction & Layout Parsing Engine

**Goal:** Any supported document in → a validated `FinancialRecord` out.

### Deliverables
- `src/parser.py` — Docling ingestion, page rendering, table extraction, vision fallback
- `src/extractor.py` — parsed document → `FinancialRecord`, plus arithmetic validation
- `tests/test_parser.py`, `tests/test_extractor.py`
- `evals/fixtures/` — 6–8 synthetic documents: clean digital invoice, multi-page statement,
  scanned/photographed receipt, multi-column expense report, a foreign-currency invoice, and a
  deliberately malformed file

### Key tasks
1. `parse_document(path) -> ParsedDocument` — markdown, per-page tables as DataFrames, rendered
   page images, `page_count`, `text_yield_ratio`.
2. Table fidelity is the hard part. Verify against fixtures that a 12-row invoice table yields 12
   rows — not 1 flattened string, not 12 misaligned columns.
3. **OCR fallback** *(revised — decision D-15)*: when `text_yield_ratio < THRESHOLD`, re-parse
   through Docling's local RapidOCR engine. **Fallback only** — a digital PDF is never sent
   through OCR, which would be slower and less accurate than reading its text layer. Image files
   skip the non-OCR pass entirely, since the extension already tells us there is no text layer.
4. `extract_record(parsed) -> FinancialRecord` using structured output. Prefer deterministic
   parsing of Docling tables for line items; use the LLM for the header fields (vendor, dates,
   document type) and for reconciling ambiguity.
5. **Arithmetic validation (FR-2.4):** `Σ line_items + Σ tax == total` within ±0.02. Mismatch
   appends to `extraction_warnings` — it never mutates a number to force a balance.
6. Missing fields become `None` + a warning. **Never a fabricated default** (Rule 2.3).

### Definition of Done
- [x] All fixtures parse without exception; the malformed file raises `ParsingError` with an
      actionable message
- [x] Line-item row recall ≥ 90% across fixtures — **3/3 rows on `clean_invoice.pdf`**, correct
      column alignment, no split rows
- [x] OCR fallback triggers on the scanned fixture and on nothing else; `force_ocr=False` honoured
- [x] Provenance available — normalized top-left `BoundingBox` on tables (resolves ODQ-1)
- [x] `mypy src/` passes; `tests/test_parser.py` 25/25 green in ~30 s, zero LLM calls
- [x] `total_amount` correct on 100% of digital-PDF fixtures — 501.27, 528.40, 1951.87, 38.52
- [x] Arithmetic validation flags the deliberately-unbalanced fixture, and **neither figure is
      adjusted** (528.40 stated vs 501.27 computed both preserved)
- [x] `memory.md` updated

**Phase 2 complete (2026-07-25).** 75 tests: 73 offline + 2 live-API. `mypy src/` clean across
6 modules.

---

## Phase 3 — Local RAG Pipeline & Vector Store

**Goal:** Layout-aware chunking, local embedding, persistent Chroma storage, filtered retrieval.

### Deliverables
- `src/vectorstore.py` — chunking, embedding, ingest, retrieval, deletion, collection stats
- `tests/test_vectorstore.py`
- A seeded `policy_corpus`: a sample company travel policy, a sample cloud-vendor pricing/overage
  document, and a prior-month statement (all synthetic)

### Key tasks
1. **Chunking (this is where RAG quality is won or lost):**
   - Narrative text → `RecursiveCharacterTextSplitter(800 / 120)`.
   - Tables → **one chunk per row**, serialized `"Description: … | Qty: … | Unit: … | Amount: …"`.
   - Plus one table-summary chunk carrying headers and totals.
   - **A table row is never split across chunks** (FR-3.1). A split row is the single dominant
     cause of hallucinated invoice figures.
2. Local `HuggingFaceEmbeddings(all-MiniLM-L6-v2)` — assert no network call after first load.
3. `chromadb.PersistentClient("data/chroma")`, two collections: `financial_documents`,
   `policy_corpus`.
4. Full metadata per chunk (`architecture.md` §3.3). Chroma metadata values must be scalars —
   dates stored as ISO-8601 strings.
5. Retrieval API: `retrieve(query, document_id=None, collection=..., k=6) -> list[Citation]`,
   supporting `where={"document_id": ...}` scoping (FR-3.4).
6. Idempotent ingest: re-uploading the same document replaces rather than duplicates its chunks.

### Definition of Done
- [x] Ingesting a fixture produces the expected chunk count with correct metadata on every chunk —
      6 chunks per invoice (1 record summary, 3 table rows, 1 table summary, 1 narrative)
- [x] `where={"document_id": …}` filtering verifiably scopes results to one document
- [x] Every retrieval result carries `{document_id, filename, page, score}` for citation, plus a
      `BoundingBox` on table rows (D-16)
- [x] Embedding a chunk makes zero network requests — local MiniLM only
- [x] Re-ingesting the same file does not duplicate chunks (delete-then-add, not upsert)
- [x] A targeted query ("what was the NAT gateway charge") returns the correct table row at rank 1
- [x] **Bonus:** the policy corpus answers "why is my NAT gateway charge so high" with the
      NAT Gateway Charges clause at rank 1 (0.534) — the cross-document grounding flow works
- [x] `memory.md` updated

**Phase 3 complete (2026-07-25).** 97 tests offline + 2 live-API. `mypy src/` clean across
7 modules.

---

## Phase 4 — Multimodal LLM Reasoning Chain

**Goal:** Grounded, streaming, cited answers. This is the phase where the product becomes real.

### Deliverables
- `src/chain.py` — LCEL RAG chain, query rewriting, prompt assembly, streaming, citation parsing,
  numeric cross-check
- `src/observability.py` — `RunStats` callback: token estimates, per-stage latency, active model
- `tests/test_chain.py`

### Key tasks
1. **Query rewriting** with the utility model: resolve pronouns against chat history
   ("*this* charge" → "the AWS NAT Gateway charge on the July invoice").
2. **Dual retrieval:** document-scoped (k=6) + policy corpus (k=4).
3. **Prompt assembly:** retrieved chunks **plus** the validated `FinancialRecord` as compact JSON.
   Including the structured record means the model reads totals rather than re-deriving them —
   a major hallucination reduction.
4. **System prompt hard requirements:**
   - Cite every claim as `[filename:page]`.
   - State *"I cannot determine this from the provided documents"* rather than infer any figure
     not present in context (FR-4.2).
   - Separate what the *document* says from what the *policy* says.
5. **Streaming** via LCEL `.astream()` with a token callback.
6. **Post-processing:** parse `[file:page]` markers → `Citation` objects; regex every `$`-figure in
   the answer and cross-check against `FinancialRecord` (FR-4.4); flag mismatches visibly.
7. **Resilience:** timeout, exponential backoff + jitter on 429, local prompt-hash response cache
   at `data/llm_cache.db`. A rate-limit hit produces a clear UI message, never a stack trace.
8. LangSmith tracing enabled if `LANGCHAIN_API_KEY` is present; **the app runs normally without it.**

### Definition of Done
- [x] "Why was this charge deducted?" against the AWS fixture returns a correct, cited answer
      citing both the invoice and the billing policy
- [x] Every answer carries ≥ 1 parsed citation, or is a refusal; `Answer.is_grounded` drives the
      unverified warning strip
- [x] A question whose answer is genuinely absent from context produces a refusal, **not a guess** —
      verified with "What was the CEO's salary last year?"
- [x] Tokens stream incrementally (asserted, not assumed: `test_tokens_stream_incrementally`)
- [x] Rate limits degrade gracefully — `RateLimitError` carries `retry_after_seconds` and becomes
      an error `StreamEvent`, never a stack trace.
      ⚠ **Corrected 2026-07-25:** this was checked only on the non-streaming path when first
      marked done. A provider 429 raised *while iterating* `model.stream()` escaped
      untranslated and crashed the caller — found by the Phase 5B eval run, not by the Phase 4
      tests. Fixed via `translate_provider_error` (D-28) and now covered by three regression
      tests
- [x] `RunStats` reports real token counts from provider usage metadata, with `tokens_estimated`
      set when it has to fall back to estimation
- [x] Multi-turn follow-up ("and what was it the month before?") resolves via query rewriting and
      correctly retrieves the prior invoice (98.03)
- [x] **Beyond the DoD:** invented citation markers are dropped and reported; every figure is
      traced to the record or to retrieved context
- [x] `memory.md` updated

**Phase 4 complete (2026-07-25).** 133 tests offline + 8 live-API. `mypy src/` clean across
9 modules.

---

## Phase 5 — Full Web Dashboard Frontend & Evaluation Suite

**Goal:** A polished, responsive, production-feel web application — plus the evidence that it
works.

### 5A — Frontend (per `design.md`)
- `app.py` + `ui/` — the four surfaces:
  1. **Document Workspace** — drag-and-drop upload, high-resolution page previewer with zoom,
     page navigation, and citation-highlight overlays.
  2. **Extraction Dashboard** — sortable line-item table, vendor detail card, tax/subtotal/total
     breakdown, inline field correction, validation-warning banner.
  3. **RAG Chat Panel** — streaming responses, clickable citation chips (click → scroll +
     highlight in the previewer), quick-prompt suggestion chips.
  4. **Observability Bar** — estimated token usage, per-stage latency, active model selection.
- Full design-token implementation, light/dark themes, responsive at 1440 / 1024 / 768 px.
- Every async operation has a real loading state; every failure has a designed error state.

### 5B — Evaluation Suite
- `src/evals.py` — Ragas (`faithfulness`, `answer_relevancy`, `context_precision`,
  `context_recall`) with a **Groq judge and local embeddings** (never the OpenAI default — Rule 1).
- Deterministic extraction accuracy comparator (per-field exact match, `Decimal`-aware) — no LLM,
  no tokens.
- `evals/golden_set.jsonl` — ≥ 20 (document, question, expected-answer) triples.
- `python -m src.evals` prints a metrics table and writes a timestamped JSON report.

### Definition of Done
- [x] All four UI surfaces implemented — `app.py`, `ui/styles.py`, `ui/components.py`
- [x] App boots and serves cleanly (`streamlit run app.py`, HTTP 200, no errors in the log)
- [x] Golden set ≥ 20 entries — 28 items across document / policy / cross-document / refusal
- [x] Eval suite runs at $0 — Groq judge + local MiniLM, with a Rule 1 assertion on the endpoint
- [x] **Total-amount extraction accuracy 100%** (5/5 fixtures) — target 95%
- [x] **Line-item recall 100%** (17/17) — target 90%
- [x] Upload → parse → extract verified in a real browser: sample loads, page image renders,
      vendor card / validation banner / line-item table / totals all correct, quick-prompt chips
      generated from the record. **The ask → cited answer leg is unverified** — the daily token
      cap blocks it
- [x] Citation highlight lands on the correct region of the correct page — verified by rendering
      a real retrieved `Citation` over the real page image (bbox → CSS → correct region).
      **Granularity is table-level, not row-level** (D-32)
- [x] Responsive at 1440 / 1024 / 768 / 390 px — **no horizontal body scroll at any width**;
      table unclipped at 1440/1024/768 and scrolling inside its own container at 390
- [x] Graceful degradation with no API key: extraction still works, and the missing metadata is
      stated as an advisory note rather than silently omitted
- [ ] Faithfulness ≥ 0.85, answer relevancy ≥ 0.80, context recall ≥ 0.85 (PRD §8)
      — **blocked**: Groq's 100k tokens/day cap was exhausted mid-run (D-27)
- [ ] Refusal correctness — 1 of 4 cases verified correct; the other 3 are undetermined,
      not yet shown to be either a model failure or a detector gap
- [x] `README.md` written: setup, free-key acquisition, run instructions, screenshots, and an
      explicit "what is verified / what is not" section rather than a green badge
- [ ] `memory.md` updated with final state

**Phase 5 partially complete (2026-07-25).** 163 tests offline + 8 live-API; `mypy` clean across
14 modules. Deterministic extraction targets are met and exceeded. **The Ragas numbers are not
yet measured** — see the Session 6 log in `memory.md`.

---

## Cross-Phase Standing Requirements

Applies to every phase, checked at every DoD:

- [ ] Rule 1 — no paid dependency introduced; no library silently defaulting to a paid provider
- [ ] Rule 2 — full type hints, Pydantic models across module boundaries, specific exceptions,
      no bare `except`, no financial data in logs
- [ ] Rule 3 — `memory.md` read before starting, updated before finishing
- [ ] Rule 4 — no secrets, no real financial documents committed
- [ ] Rule 5 — no feature ships that makes an untraceable claim

## Sequencing Note

Phases 1–4 are strictly sequential; each depends on the contracts established by the last.
**Phase 5A (frontend) can begin against mocked `src/` responses as soon as Phase 2's schemas are
frozen** — this is the recommended parallelization if UI polish becomes the long pole.
