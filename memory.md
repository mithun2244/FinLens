# Project Memory — Multimodal AI Financial Assistant

> **Read this file before any code change (Rule 3). Update it in the same change set that
> completes a unit of work.**
>
> This is a **log**, not a specification. Durable requirements belong in `prd.md`,
> `architecture.md`, `rules.md`, `phases.md`, and `design.md`.

---

## Current State

| | |
|---|---|
| **Current phase** | **Phase 5 — Web Dashboard Frontend & Evaluation Suite** |
| **Phase status** | **In progress.** Eval suite and dashboard both built. Extraction targets met and exceeded; **Ragas metrics not yet measured — blocked on Groq's 100k tokens/day cap (D-27)** |
| **Last session** | 2026-07-25 |
| **Blockers** | None |
| **Environment** | Windows 11, Python 3.12.10, `venv/` populated (CPU-only torch), Groq key configured and verified |

**Verified working:** Docling 2.115.0 (layout + TableFormer models cached), ChromaDB persistent
client, local MiniLM embeddings (384-dim), Groq `llama-3.3-70b-versatile` and
`llama-3.1-8b-instant`. `mypy src/` clean.

---

## Next Immediate Actions

Ordered. Start at the top.

**The daily Groq token budget must reset before the two blocking items can be settled** (D-27).
Everything else can proceed now.

1. **Re-run `python -m src.evals --pace 3`** once the budget resets. This produces the Ragas
   numbers *and* settles the refusal question in one pass. Budget ~220k tokens; it will not
   complete on a day when other work has consumed the cap.
2. **Settle the refusal finding specifically.** 3 of 4 refusal cases did not set `refused`, cause
   undetermined. Diagnose with `scripts`-style isolation before changing anything: if the model
   genuinely answered, tighten the system prompt; if it refused in unrecognised wording, widen
   `_REFUSAL_MARKERS`. **Do not "fix" the detector without first reading the answers** — making
   the detector more permissive would hide a real grounding failure.
3. **Browser click-through** of the dashboard: upload → parse → extract → ask → click a citation
   → confirm the highlight lands on the right region of the right page. The app serves cleanly
   but this path has not been walked.
4. **Responsive checks** at 1440 / 1024 / 768 px, and a keyboard-only pass (design.md §8).
5. ~~`README.md`~~ — **done** (session 7). Includes an explicit "what is verified / what is not"
   section; keep it honest as the open items close.
6. **Cheap checks that need no tokens:** `python -m src.evals --extraction` runs the full
   deterministic suite at zero cost and can be run freely.
7. Update this file; close Phase 5 against its DoD in `phases.md`.

---

## Completed

| Date | Item |
|---|---|
| 2026-07-25 | **Phase 0 complete** — `prd.md`, `architecture.md`, `rules.md`, `phases.md`, `design.md`, `memory.md` created in project root |
| 2026-07-25 | **Phase 1 complete** — all DoD items met. `requirements.txt` (annotated with per-dependency cost), `.gitignore`, `.env.example`, `src/config.py`, `src/schemas.py`, `scripts/check_models.py`, `scripts/verify_setup.py`, `scripts/make_fixtures.py`. Dependencies installed with CPU-only torch. `verify_setup.py` green on every check; `check_models.py` confirms the reasoning and utility models are served; `mypy src/` clean. Five synthetic fixtures generated |

---

## Decision Log

Decisions are append-only. To reverse one, add a new entry that references and supersedes it —
never edit or delete the original (Rule 3).

| ID | Date | Decision | Rationale |
|---|---|---|---|
| **D-1** | 2026-07-25 | **Docling is the primary parser; the vision LLM is a fallback only**, gated on `text_yield_ratio` | Layout parsing is deterministic, local, and free. Vision is non-deterministic and token-expensive against a free-tier quota. Also see `architecture.md` §8 for the signal that would invalidate this |
| **D-2** | 2026-07-25 | **Embeddings local (MiniLM), reasoning cloud (Groq)** | Embeddings are high-volume/low-complexity → local, free, private. Reasoning is low-volume/high-complexity → cloud. This split is what makes the $0 constraint achievable without gutting answer quality |
| **D-3** | 2026-07-25 | **Vision model is `meta-llama/llama-4-scout-17b-16e-instruct`, not Llama 3.2 Vision** | The brief specified Llama 3.2 Vision, but Groq's `llama-3.2-*-vision-preview` endpoints were preview models that have since been **retired** — calls fail with a decommissioned-model error. Llama 4 Scout is the current free-tier multimodal model on Groq, same API shape, same $0 cost. `llama-3.3-70b-versatile` for text reasoning is unaffected. **Verify with `scripts/check_models.py` in Phase 1 before building on it.** |
| **D-4** | 2026-07-25 | **All model IDs centralized in `src/config.py`; never inlined** | Direct mitigation for D-3 — Groq retires preview models on short notice. Swapping one must be a one-line change |
| **D-5** | 2026-07-25 | **ChromaDB over FAISS** | Metadata filtering (`where={"document_id": ...}`) is a functional requirement (FR-3.4), not an optimization. FAISS lacks it out of the box |
| **D-6** | 2026-07-25 | **All monetary values are `Decimal`, never `float`** | Float currency arithmetic silently produces wrong totals. The product's entire value is that its numbers are exactly right |
| **D-7** | 2026-07-25 | **One chunk per table row; a row is never split** | Split table rows are the dominant cause of hallucinated invoice figures in RAG systems |
| **D-8** | 2026-07-25 | **`src/` never imports the UI framework** | Keeps the Streamlit↔React decision (`design.md` §2) reversible. Enforced at review |
| **D-9** | 2026-07-25 | **Frontend framework decision deferred to the start of Phase 5** | It hinges on ODQ-1 — whether Docling exposes bounding boxes precise enough for pixel-accurate citation overlays. That answer arrives in Phase 2. Deciding now would be guessing. Default lean: Streamlit + heavy custom CSS |
| **D-10** | 2026-07-25 | **Ragas judge points at Groq; embeddings point at local MiniLM** | Ragas defaults to a paid OpenAI judge. That default must be explicitly overridden, visibly, at the call site (Rule 1) |
| **D-11** | 2026-07-25 | **LangSmith is optional; in-app `RunStats` is mandatory** | Observability must not be a hard dependency of the request path, nor require a second account to see latency and token counts |
| **D-12** | 2026-07-25 | **Never fabricate a value to recover from an extraction failure** | A missing `total_amount` becomes `None` + a warning, never `0` and never inferred. A silently-wrong number is worse than a visible failure (Rule 2.3, Rule 5) |
| **D-13** | 2026-07-25 | **Ragas' transitive OpenAI SDK stack is tolerated, but no paid credential may ever exist in this environment** | Discovered during the Phase 1 install: `ragas` **hard-requires** `openai`, `langchain-openai`, `tiktoken`, and `instructor` — they install regardless of which judge we configure. This is exactly the Rule 1 risk of "a library silently defaulting to a paid provider". Removing them means dropping Ragas entirely, which costs us FR-6. **Resolution:** keep them, but make an accidental fallback *impossible to pay for* rather than merely discouraged — `OPENAI_API_KEY` and every other paid-provider key must be absent from the environment, so any silent default raises an auth error instead of billing. `verify_setup.py` now audits installed packages and environment credentials, not just `requirements.txt`. Phase 5 adds a runtime assertion that the Ragas judge is a `ChatGroq` instance |

| **D-37** | 2026-07-25 | **An evaluation run that measured nothing must FAIL, not PASS** | A run where all 28 questions errored on a corrupt vector store printed **"PASS — all targets met"**. Every grounding rate was vacuously 0% and every Ragas mean was `None`, and unmeasured metrics rendered as "n/a" rather than FAIL, so nothing tripped the verdict. This is the second variant of the same bug (the first: field accuracy had no target). Two guards added: any errored item fails the run, and any unmeasured Ragas metric fails the run when answering was attempted — with `--extraction` exempt, since it legitimately skips scoring. Four regression tests. **An evaluator that manufactures confidence is worse than no evaluator** |
| **D-36** | 2026-07-25 | **Ragas retrieval metrics must be scored against what retrieval returned, not what the model cited** | The first real run produced faithfulness **0.15**, context_precision **0.077** and context_recall **0.077** — on a pipeline whose extraction accuracy is 100%. Not a quality finding: `_score_item` built `retrieved_contexts` from `answer.citations`. An answer typically cites 1–2 of the 10 retrieved chunks, so recall against a full reference answer is near-zero *by construction*, and faithfulness judged against two snippets makes a correct answer look unfaithful because the supporting facts were never shown to the judge. `answer_relevancy` (0.963) was unaffected — its signature takes no contexts, which is why the pattern was incoherent enough to notice. `Answer` now carries `retrieved` alongside `citations`; the two are different sets and conflating them corrupts evaluation |
| **D-35** | 2026-07-25 | **Parsing speed comes from `AcceleratorOptions.num_threads=8`, a PyMuPDF triage pass, and a content-hash cache — not from the levers that looked obvious** | Profiled before changing anything. Findings: (a) Docling's `AcceleratorOptions` defaults to **4 threads** and sets torch's count itself, so `torch.set_num_threads()` from application code is silently overridden — measured no effect. Setting it through Docling gave **3.04 s → 1.99 s (−35%)**; 12 threads was *worse* (2.75 s, high variance) than 8 on a 12-core box, so the cap is `min(8, cpu_count)`. (b) `generate_page_images=False` saves **nothing** (3.63 s off vs 3.22 s on, inside noise) and would break the previewer and citation highlighting — rejected. (c) A PyMuPDF triage pass costs 1–10 ms and decides OCR *before* Docling runs, so a scanned PDF is no longer parsed twice. (d) A parse cache keyed on content MD5 makes a re-upload 3.0 s → **0.027 s**. **Rejected outright:** replacing Docling text extraction with PyMuPDF for digital PDFs. PyMuPDF returns a flat character stream with no table structure; line items come from TableFormer rows (D-7), so that swap would push every invoice onto the 0.55-confidence narrative fallback and cost the 100% line-item recall the product is built on. Triage reads the text only to count it, then discards it |
| **D-34** | 2026-07-25 | **The embedded Chroma store is single-process, and corruption must be reported in plain language** | Two processes holding `data/chroma` at once (the app plus a script) left the store unreadable: the HNSW segment files were gone while the metadata database still listed the collections. Chroma reported `Error creating hnsw segment reader: Nothing found on disk`, which tells a user nothing. `_explain_search_failure()` now detects this and says to delete the folder and re-upload. **Operational rule: do not run the app and an eval at the same time** — the embedded client supports one process |
| **D-33** | 2026-07-25 | **`load_dotenv(override=True)` runs before every other import in `app.py`** | An exported `GROQ_API_KEY=""` silently beats the `.env` file in **both** pydantic-settings and the default `load_dotenv()` (which is `override=False`). The app then reports "no API key configured" while a perfectly valid key sits in `.env`. Measured all three: polluted env → pydantic-settings `groq_configured=False`, `load_dotenv()` `False`, `load_dotenv(override=True)` `True`. The file carries a `# ruff: noqa: E402` with the reason, because every `src/` import must follow the environment being settled. **This was self-inflicted** — the browser-QA session launched Streamlit with an empty key to work around the token cap and left the process running |
| **D-32** | 2026-07-25 | **Citation highlighting is table-granular, not row-granular** | Verified visually: a cited table row highlights the **whole table**, because every row chunk inherits `TableBlock.bbox` in `build_chunks`. The geometry is correct and lands on the right region — it is just coarser than design.md §5.1 implies. Acceptable for v1 (the user's eye still goes to the right part of the page). To tighten it, per-row provenance would have to come from Docling's `TableItem` cell prov, which has not been investigated |
| **D-31** | 2026-07-25 | **Streamlit columns need explicit CSS to collapse; they do not respond to viewport** | `st.columns` ratios are fixed server-side, so three columns stayed three-abreast at every width. At 1024px the line-item table was squeezed until the **Amount column clipped off the right edge**, and at 768px it disappeared entirely — the single most important column in the product, invisible, with no error anywhere. Added a `max-width: 1200px` media query stacking `[data-testid="stColumn"]` full-width. Measured after: no clipping at 1440/1024/768, and at 390px the table scrolls inside its own container while the body never scrolls sideways. **Deviation from design.md §4.2:** that spec called for a tabbed two-column variant at 768–1023px; stacking is simpler and meets the hard rule, so the spec is the aspiration and this is what shipped |
| **D-30** | 2026-07-25 | **HTML passed to `st.markdown` must be dedented** | Indented HTML inside triple-quoted strings reached the page as **literal `<div class="fl-banner incomplete">` text**. Streamlit renders through a markdown parser, and markdown turns any line indented four or more spaces into a code block. Every emitted block now goes through `_block()`, which strips leading whitespace per line, and a test asserts no rendered block contains a four-space-indented line. Two sibling formatting bugs were found in the same pass: `Sales Tax (8.500%)` (Decimal not normalized) and a `0.0420` unit price rendered as `0.04` by the two-decimal money formatter, which reads as a different rate |
| **D-29** | 2026-07-25 | **OQ-1 RESOLVED: Streamlit + custom CSS, not React** | The deciding input was ODQ-1/D-16: `BoundingBox` arrives normalized to 0–1 top-left with `as_css_percent()`, so citation highlighting is an absolutely-positioned div over a page image — no PDF rendering library, which was the entire argument for `react-pdf`. `ui/components.page_view()` does it in ~20 lines. Streamlit keeps the project in one language and `src/` untouched. **Supersedes D-9's deferral** |
| **D-28** | 2026-07-25 | **Provider errors raised *during* streaming need their own translation** | A `groq.RateLimitError` raised while iterating `model.stream()` escaped untranslated and crashed the eval with a raw traceback. `invoke_with_translation` cannot cover this: the exception surfaces during iteration, not at call time. Added public `translate_provider_error()` in `src/llm.py` and wrapped the stream loop. **This invalidated a Phase 4 DoD item that had been marked complete** — the rate-limit path was only ever exercised non-streaming, and streaming is the path the UI actually uses. Corrected in `phases.md` with three regression tests |
| **D-27** | 2026-07-25 | **The free tier's binding constraint is tokens per DAY, not per minute** | Measured, not assumed: `llama-3.3-70b-versatile` is capped at **100,000 tokens/day** and `llama-3.1-8b-instant` at **6,000 tokens/minute**. A full eval run is ~28 answers plus ~112 judge calls at ~1,600 tokens each — roughly 220k tokens, which is **over twice the daily budget**. Mitigations: `--pace` between calls (default 2 s), judge contexts trimmed to 4 × 500 chars, and 429 retries that honour Groq's own `try again in Ns` hint (instructor otherwise gives up after one attempt). **A full eval cannot be run more than about once every two days on the free tier** — this is a real project constraint, recorded in the Risk Watchlist |
| **D-26** | 2026-07-25 | **The Ragas judge reaches Groq through the OpenAI-compatible client, and D-13 is amended accordingly** | `llm_factory(provider="groq", client=Groq(...))` is broken in ragas 0.4.3 — the instructor adapter mis-patches the client and raises `'Groq' object has no attribute 'messages'`. The working configuration is `AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")` with `provider="openai"`. **This amends D-13:** the `openai` package is no longer merely installed-and-never-called — it is used as an HTTP transport. It is *not* OpenAI: the base URL is Groq, the credential is the Groq key, no request reaches OpenAI, and the cost stays $0. `_build_judge()` asserts the base URL before returning, and a test pins it. The Rule 1 safeguard is unchanged — with no `OPENAI_API_KEY` in the environment, a misconfiguration fails loudly rather than billing |
| **D-25** | 2026-07-25 | **Refusal detection only looks at the first 160 characters** | Substring matching over the whole answer flagged a well-cited, substantive answer as a refusal because it ended with the caveat "the documents do not provide further information on why…". A genuine refusal *opens* with the phrase — the system prompt requires it verbatim and instructs the model to lead with its answer. The same wording appearing late is a caveat, and labelling that a refusal would mislabel a good answer in the UI |
| **D-24** | 2026-07-25 | **A figure found verbatim in retrieved context is grounded, even if it contradicts the active record** | Multi-document questions broke the naive check: asked "and what was it the month before?", the model correctly answered 98.03 from the prior invoice, and the cross-checker flagged it as contradicting the *current* invoice's total of 501.27. Order is now: (1) matches a record field → supported; (2) appears verbatim in a retrieved chunk → supported; (3) neither, and the wording names a field → contradiction; (4) neither → unsupported. Checking value-match *before* the label heuristic also fixed a second false positive where "a subtotal of 462.00" was judged against `total_amount` |
| **D-23** | 2026-07-25 | **`NumericCheck` distinguishes "unsupported" from "contradicting"** | The original model could not express the more serious finding: a figure matching no record field returned `is_consistent == True`. Now `is_supported` (was it read from the record or from retrieved context, per Rule 5) is separate from `contradicts_record` (does it disagree with a field it names, per FR-4.4). `Answer` exposes `unsupported_figures`, `contradicting_figures`, `dropped_citations`, and `is_trustworthy` for the UI warning strips |
| **D-22** | 2026-07-25 | **Chroma collections use cosine space explicitly** | Created with `metadata={"hnsw:space": "cosine"}` so `score = 1 - distance` is a similarity in [0, 1]. Chroma defaults to L2, which with normalized vectors is monotonically equivalent but yields scores that are meaningless to show a user — and design.md §5.3 renders the score in the citation tooltip |
| **D-21** | 2026-07-25 | **`ParsedPage` carries narrative text separately from table rows** | `ParsedPage.markdown` combined both, so chunking it would index every table row twice — once as its own chunk (D-7) and again inside a narrative chunk, letting one row outvote the rest of a document during retrieval. Added `narrative_markdown` (text only) for chunking, while `markdown` stays combined so `text_yield_ratio` is not skewed by a table-heavy page with a sparse header being mistaken for a scan. `extractor`'s narrative line-item fallback also switched to the narrative field |
| **D-20** | 2026-07-25 | **Groq's `tool_use_failed` 400 is transient and must be retried** | Structured extraction failed intermittently with HTTP 400 `tool_use_failed` — Groq returns this when the model emits malformed tool-call arguments. The SDK does not retry 400s (normally permanent), but this one succeeds on retry: 3/3 on re-run of the same input. `src/llm.py` now has `invoke_with_retry` with a transient-marker list and exponential backoff. **Also ruled out:** `method="json_schema"` is rejected outright by `llama-3.3-70b-versatile` on Groq, and `method="json_mode"` works but is lossy (dropped the address and both dates on the same input). Default tool-calling plus retry is the right configuration |
| **D-19** | 2026-07-25 | **`validation_state` is independent of `extraction_warnings`** | Advisory notes ("these items were read from text, please verify"; "offline mode") were flipping `is_validated` to `False` on documents whose arithmetic was perfectly correct — the receipt reconciled at 38.52 exactly and still showed as failed. The banner now reports **only** the maths (`validated` / `mismatch` / `incomplete`, matching design.md §5.2's three states); warnings are a separate channel. Conflating "something is worth mentioning" with "the numbers are wrong" would train users to ignore the one signal that matters |
| **D-18** | 2026-07-25 | **Line items fall back to narrative text parsing when no table is detected** | The OCR'd receipt produced zero line items: receipts have no ruled table for TableFormer to find, so the structural path yielded nothing while subtotal/tax/total all extracted fine — leaving a document that could not reconcile. A `description amount` pattern scan now runs **only when no line-item table exists**, excluding summary rows (subtotal/tax/total/payment labels). Items found this way get confidence 0.55 so the UI renders them as low-confidence (design.md §5.2), because this is pattern-matching over prose, not structure. Recovered all 3 receipt items; the record now reconciles exactly |
| **D-17** | 2026-07-25 | **The LLM may propose an amount, but only a figure written in the document is accepted** | Numbers come from table structure and labelled-amount regexes. Where the model fills a gap those missed, `_amount_appears_in_document` requires the value to appear verbatim (digit-normalized, so `1,284.50` matches `1284.50`) or it is discarded with a warning. This is Rule 5 made mechanical: the model can read, it cannot invent. Verified by `test_llm_does_not_override_deterministic_totals` |
| **D-16** | 2026-07-25 | **ODQ-1 RESOLVED: pixel-accurate citation highlighting is feasible** | Empirically verified on `clean_invoice.pdf`: every Docling item carries `prov[0].bbox` (`l/t/r/b` in absolute points, `coord_origin=BOTTOMLEFT`) plus `page_no`, and page images come back as `ImageRef.pil_image` at 1190×1684 for a 595×842pt page. `src/parser.py` normalizes these to 0–1 **top-left** coordinates (`BoundingBox`) so they drop straight into CSS. **Consequence for OQ-1:** the strongest argument for React (`react-pdf` overlays) was that Streamlit could not do region highlighting — that argument is now weaker, since we supply the geometry ourselves and only need absolutely-positioned divs over a page image. Streamlit remains the Phase 5 default |
| **D-15** | 2026-07-25 | **The cloud vision fallback is replaced by Docling's LOCAL OCR engine (RapidOCR)** | `check_models.py` against the live account proved Groq serves **no image-input model at all** — not Llama 4 Scout, not Maverick, not the retired Llama 3.2 Vision (D-3). Groq's 15 served models are text, agentic, safety-classifier, speech, and TTS only. Rather than adding a second cloud provider (which would require amending Rule 1) or a local VLM (~30–90s/page on CPU, breaking NFR-3), scanned pages now go through Docling's built-in RapidOCR — already installed via `docling-slim`, runs on onnxruntime CPU, $0, deterministic, no rate limits, and **page images never leave the machine**. This strengthens Rule 1 rather than bending it. Costs: OCR is weaker than a VLM on poor-quality photographs, and we lose the ability to answer semantic questions *about* an image. Both acceptable for v1 — see the Risk Watchlist for the signal that would force a revisit |
| **D-14** | 2026-07-25 | **Embeddings are always passed to Chroma explicitly; never rely on its default embedding function** | Found during Phase 1 verification: calling `collection.query(query_texts=...)` without an embedding function made Chroma download and cache its **own** bundled ONNX `all-MiniLM-L6-v2` (79 MB, at `~/.cache/chroma/onnx_models/`) — a second copy of a model we already load, produced by a component we do not control. **Phase 3 rule:** every `upsert` passes `embeddings=`, every `query` passes `query_embeddings=`, or we go through `langchain_chroma.Chroma(embedding_function=...)`. Never `query_texts` on a raw collection |

---

## Open Questions

| ID | Question | Resolve by |
|---|---|---|
| **OQ-1** | Streamlit vs. React for the dashboard | Start of Phase 5 (see D-9) |
| **OQ-2** | Do corrected extraction fields get written back into the vector store as authoritative chunks? *(leaning yes)* | Phase 5 |
| **OQ-3** | Persist chat history across app restarts? *(leaning yes, SQLite)* | Phase 5 |
| **ODQ-1** | Does Docling expose per-chunk bounding boxes precise enough for pixel-accurate highlight overlays, or do we fall back to page-level highlighting? **This gates OQ-1.** | Phase 2 |
| **ODQ-2** | Should Extraction and Chat share a tabbed column even on desktop, giving the previewer more room? | Phase 5A prototype |
| **OQ-4** | Is MiniLM's 256-token window sufficient for retrieval quality, or do we move to `bge-small-en-v1.5`? | Phase 3, measured against the golden set |

---

## Risk Watchlist

Signals that would invalidate a core architectural assumption (`architecture.md` §8). Check these
at every phase boundary.

| Signal | Threshold | Implication |
|---|---|---|
| `text_yield_ratio` below threshold on uploads | > 40% of documents | OCR becomes the main path, not a fallback → extraction quality is capped by RapidOCR rather than Docling's layout model, and D-15's trade-off stops being cheap. Contingency: local VLM via Ollama, accepting the latency cost |
| OCR-derived records failing FR-2.4 arithmetic validation | Materially more often than digital ones | Same as above — D-15 needs revisiting. Measure this once `extractor.py` exists |
| Context recall on multi-document questions | < 0.70 | Chroma embedded mode + 384-dim MiniLM insufficient (invalidates D-2/D-5 at scale) |
| Groq free-tier tightening | Any material change | Reasoning must move local (Ollama + Llama 3.1 8B) at significant quality cost. Contingency documented, not built |
| **Daily token budget exhausted** | **Materialized 2026-07-25** | 100k tokens/day on the reasoning model; one full eval run costs ~220k. A complete eval is possible roughly every two days. Mitigate by running `--extraction` freely (zero tokens), `--limit N` for spot checks, and reserving full runs for release gates (D-27) |
| Groq model deprecation | Any configured ID stops being served | One-line fix in `config.py` (this is why D-4 exists) — but re-verify vision quality after any swap |

---

## Session Log

### 2026-07-25 — Session 1: Documentation scaffolding

**Done:** Created all six root documents (`prd.md`, `architecture.md`, `rules.md`, `phases.md`,
`design.md`, `memory.md`). Established the zero-cost stack, the one-way module dependency chain,
the design system, and Decisions D-1 through D-12.

**Notable:** Flagged that the brief's specified Llama 3.2 Vision model has been retired on Groq
(D-3). Architecture is unchanged — same provider, same API, same $0 — but the model string differs
and must be verified live in Phase 1.

**Next:** Phase 1, starting with `requirements.txt`.

### 2026-07-25 — Session 2: Phase 1 complete, Phase 2 parser built

**Done:**
- Phase 1 closed against its full DoD. Dependencies installed with CPU-only torch;
  `verify_setup.py` green on every check; `mypy src/` clean.
- `scripts/make_fixtures.py` — five synthetic fixtures, arithmetic self-asserted at generation
  time so a bad fixture cannot teach a wrong lesson.
- `src/parser.py` — Docling ingestion with cached converters, per-page text-yield computation,
  local OCR fallback, table extraction with row structure intact, page-image export, and
  bottom-left → top-left bbox normalization.
- `tests/test_parser.py` — 25 tests, all passing in ~30 s against real Docling conversion.

**Three findings that changed the design, none of which were visible from documentation:**
1. **D-13** — `ragas` hard-requires the OpenAI SDK stack. Guard changed from discipline to
   impossibility: no paid credential may exist in the environment.
2. **D-14** — Chroma silently downloads its own MiniLM when queried with `query_texts`.
   Embeddings must always be passed explicitly.
3. **D-15** — Groq serves **no image-input model at all**. The cloud vision fallback is replaced
   by local RapidOCR, verified reading a rasterized receipt correctly (`TOTAL 38.52`).

**Also resolved:** ODQ-1 (D-16) — Docling exposes usable bounding boxes, so pixel-accurate
citation highlighting is feasible, which weakens the main argument for React over Streamlit.

**Measured:** `clean_invoice.pdf` → 1 table, 3 rows, correct column alignment, no OCR.
`scanned_receipt.png` → OCR fires, figures recovered. Cold parse ~20 s (model load), warm ~2 s.
Converters are `lru_cache`d and `warm_up()` exists so the first upload is not the slow one.

**Next:** `src/extractor.py` — `ParsedDocument` → `FinancialRecord`, then verify
`unbalanced_invoice.pdf` trips `validate_arithmetic()` without any figure being repaired.

### 2026-07-25 — Session 3: Phase 2 complete

**Done:** `src/llm.py` (ChatGroq factory, provider-error translation, transient retry),
`src/extractor.py`, `tests/test_extractor.py`. **75 tests: 73 offline + 2 live-API.**
`mypy src/` clean across 6 modules. Phase 2 closed against its full DoD.

**Extraction accuracy on all four fixtures (100% on totals):**

| Fixture | Line items | Subtotal | Tax | Total | State |
|---|---|---|---|---|---|
| `clean_invoice.pdf` | 3/3 with qty + unit price | 462.00 | 39.27 @ 8.5% | 501.27 | validated |
| `unbalanced_invoice.pdf` | 3/3 | 462.00 | 39.27 | 528.40 stated **vs 501.27 computed** | **mismatch, neither figure altered** |
| `multipage_statement.pdf` | 5/5 across 2 pages | 1951.87 | — | 1951.87 | validated |
| `scanned_receipt.png` (OCR) | 3/3 via text fallback | 35.50 | 3.02 | 38.52 | validated |

**Two real bugs the tests caught that review would not have:**
1. `_to_decimal("1.234,56")` returned `Decimal("1.23456")` — comma-stripping silently turned a
   European-format amount into a number ~1000x too small. Now explicitly rejected. This is
   exactly the class of failure D-6 exists to prevent, and it was in the guard itself.
2. The money regex required a thousands separator, so **every four-figure total failed to
   parse** — `1951.87` was invisible to it. Caught only because the multi-page fixture crosses
   1000. The digital-PDF total accuracy target would have quietly missed on real invoices.

**One test was wrong, not the code:** it asserted a "No Groq API key" warning under
`use_llm=False`, but the key *is* configured — offline mode simply skipped the call. The code now
emits an explicit offline-mode notice and the test checks for that instead.

**New decisions:** D-17 (verified-amounts-only), D-18 (narrative line-item fallback),
D-19 (validation state vs advisories), D-20 (Groq `tool_use_failed` retry).

**Next:** Phase 3 — `src/vectorstore.py`.

### 2026-07-25 — Session 4: Phase 3 complete

**Done:** `src/vectorstore.py`, `tests/test_vectorstore.py` (24 new tests), policy corpus added
to `scripts/make_fixtures.py`. **97 tests offline + 2 live-API.** `mypy src/` clean across
7 modules. Phase 3 closed against its full DoD.

**Chunk shape per invoice (6 chunks):** 1 record summary, 3 table rows, 1 table summary,
1 narrative. The record-summary chunk is new and was not in the original plan — a compact
statement of vendor/dates/subtotal/tax/total. Questions like "what is the total on this invoice"
match it far more reliably than a bare number sitting in a table cell.

**Retrieval verified against the fixtures:**

| Query | Rank 1 | Score |
|---|---|---|
| "what was the NAT gateway charge" | correct table row, `Amount: 412.90` | 0.526 |
| "how much was S3 storage" | correct table row, `Amount: 18.44` | 0.799 |
| "why is my NAT gateway charge so high" (policy corpus) | **NAT Gateway Charges clause** | 0.534 |
| "what is the nightly hotel spending limit" (policy corpus) | USD 200/night cap | — |

That third row is the product working end to end: a question about a charge on an invoice
retrieving the policy clause that explains *why* it exists.

**One design flaw caught before it shipped (D-21):** `ParsedPage.markdown` combined narrative
text and serialized table rows, so chunking it would have indexed every table row **twice** —
once as its own chunk and again inside a narrative chunk. A single row could then outvote the
rest of a document during retrieval. Split into `narrative_markdown` for chunking while keeping
`markdown` combined for `text_yield_ratio`, so a table-heavy page with a sparse header is still
not mistaken for a scan.

**Also:** cosine space set explicitly on collections (D-22) so scores are user-showable
similarities; four mypy errors in the Chroma calls fixed by widening to `Sequence[float]` and
casting `Where`, rather than suppressing.

**Next:** Phase 4 — `src/chain.py` and `src/observability.py`.

### 2026-07-25 — Session 5: Phase 4 complete

**Done:** `src/chain.py`, `src/observability.py`, `tests/test_chain.py` (36 new tests), SQLite
response cache wired into `src/llm.py`. **133 tests offline + 8 live-API.** `mypy src/` clean
across 9 modules. Phase 4 closed against its full DoD.

**The product now works end to end.** Measured against the live model:

| Question | Result |
|---|---|
| "Why was this charge deducted?" | Cited answer naming all 3 line items, citing invoice **and** billing policy. 5 figures, all traced |
| "Why is the NAT Gateway charge so high? Check against our cloud billing policy" | Explained per-GB billing, flagged that 412.90 exceeds the USD 200 policy threshold — both cited |
| "What was the CEO's salary last year?" | **Refused**, named what would be needed. No guess |
| "And what was it the month before?" | Resolved via query rewriting, retrieved the prior invoice, answered 98.03 |

Latency 0.4–1.6 s per answer; token counts are real (from provider usage metadata, not estimated).

**Grounding is now enforced, not requested.** Beyond the prompt instructions, every answer is
checked: citation markers are resolved against chunks actually retrieved (invented ones are
dropped into `Answer.dropped_citations`), and every monetary figure is traced to a record field
or a retrieved snippet.

**Three false positives the smoke run caught — none would have raised an error:**
1. `"a subtotal of 462.00"` reported as a contradicting **total**. `"subtotal"` contains
   `"total"`, and `rfind` scored the inner match higher (D-24).
2. `"the previous invoice was 98.03"` reported as contradicting the *current* invoice's total —
   the cross-checker judged a figure about one document against another's record (D-24).
3. A fully-cited answer flagged `refused=True` because it ended with a caveat containing refusal
   wording (D-25).

All three would have shown false warning strips on correct answers — the fastest way to train
users to ignore the warnings that matter. Fixed and regression-tested.

**Also:** `NumericCheck` reworked (D-23) so "unsupported" and "contradicting" are distinct —
the original could not express the more serious of the two.

**Next:** Phase 5 — `src/evals.py` first, then the dashboard.

### 2026-07-25 — Session 6: eval suite + dashboard built; Ragas run blocked

**Done:** `src/evals.py`, `evals/golden_set.jsonl` (28 items), `evals/extraction_expectations.jsonl`,
`tests/test_evals.py` (27 tests), `app.py`, `ui/styles.py`, `ui/components.py`.
**163 tests offline + 8 live-API.** `mypy` clean across 14 modules. The dashboard boots and
serves cleanly (HTTP 200, no errors).

**Extraction accuracy — targets exceeded:**

| Metric | Result | Target |
|---|---|---|
| total_amount accuracy | **100%** (5/5) | 95% |
| line-item recall | **100%** (17/17) | 90% |

**Ragas metrics: NOT MEASURED.** The judge configuration is verified working in isolation
(faithfulness scored 1.0 on a grounded answer and 0.0 on a hallucinated one, so the metric
discriminates), but a full run needs ~220k tokens against a 100k/day cap (D-27). All 28 golden
questions *were* answered successfully in the first run; only the scoring phase failed.

**Open finding — refusal correctness (unresolved):** in the completed answering phase,
3 of the 4 refusal cases did not set `refused`. q25 was re-tested in isolation and is correct
(refuses cleanly, detector catches it); q26–q28 could not be re-tested before the daily cap hit.
**Cause is undetermined** — either the model answered when it should not have (a grounding
failure), or it refused in wording the 160-character detector misses (a detector gap). Do not
assume which. `q28` returning 3 citations hints at the former, since a refusal cites nothing.

**Bug found by the eval that the Phase 4 tests missed (D-28):** a provider 429 raised *while
iterating* `model.stream()` escaped untranslated and crashed with a raw traceback. The Phase 4
DoD item claiming graceful rate-limit handling had only ever been exercised on the
non-streaming path — and streaming is the path the UI uses. Fixed, regression-tested, and the
DoD item corrected in `phases.md` rather than left standing.

**Four bugs in the evaluator itself,** all found by running it: LLM-only fields were checked
during a deterministic-only run (marking every fixture failed for fields never attempted); the
summary printed "PASS" while individual fixtures showed FAIL, because field accuracy had no
target; judge calls were unpaced; and retries reused an already-awaited coroutine.

**Decisions:** D-26 (judge transport, amends D-13), D-27 (daily token cap), D-28 (streaming
error translation), **D-29 (OQ-1 resolved — Streamlit, superseding D-9)**.

**Next:** re-run `python -m src.evals --pace 3` once the daily budget resets to obtain the Ragas
numbers and settle the refusal question; then the browser click-through, responsive checks, and
`README.md`.

### 2026-07-25 — Session 7: browser QA and responsive pass

**Done:** drove the running app with a real headless browser at 1440 / 1024 / 768 / 390 px.
**198 tests offline + 8 live-API** (35 new UI tests). `mypy` clean across 14 modules.
`.streamlit/config.toml` added.

**Four real bugs, none of which raised an error anywhere:**

| Bug | Symptom | Decision |
|---|---|---|
| Indented HTML in `st.markdown` | Advisory notes rendered as literal `<div class="fl-banner incomplete">` text on screen | D-30 |
| Decimal not normalized | Tax line read `Sales Tax (8.500%)` | D-30 |
| Two-decimal money format on unit prices | `0.0420`/hour displayed as `0.04` — a different rate | D-30 |
| Streamlit columns don't respond to viewport | At 1024px the **Amount column clipped off the right edge**; at 768px it vanished entirely | D-31 |

The last one is the serious one: the most important column in a financial table, invisible at a
common laptop width, with nothing failing. Fixed with a `max-width: 1200px` media query that
stacks the columns; measured after at four widths with no clipping and no horizontal body scroll.

**Also fixed:** Streamlit's default 200 MB upload cap contradicted our own "up to 25 MB" copy and
`MAX_UPLOAD_BYTES` — a user could have waited through a long upload only to hit our validation
error. Now pinned in `.streamlit/config.toml`.

**Verified working in the browser:** sample document loads, page image renders legibly, vendor
card + validation banner + line-item table + totals all correct, confidence dots render as
fill-and-colour, quick-prompt chips generated from the record ("Explain this AWS charge", "Why is
'NAT Gateway data processing (GB)' so expensive?"), observability bar live, and graceful
degradation with no API key (extraction still runs; missing metadata stated as an advisory).

**Citation highlight verified end-to-end** by rendering a real retrieved `Citation` over the real
page image: the overlay lands on the correct region. Granularity is table-level rather than
row-level (D-32) — coarser than design.md §5.1 implies, but correct.

**Still unverified:** the ask → cited answer leg in the browser, which needs tokens.

**Next:** the eval re-run (Ragas + refusal question), then `README.md`.
