# Engineering Rules — Multimodal AI Financial Assistant

**Status:** Binding. These are not suggestions.
**Last updated:** 2026-07-25

These rules govern every commit in this repository. A change that violates a rule does not get
merged, regardless of whether it works.

---

## Rule 1 — Zero Software and API Costs

**No component of this project may require a payment, a credit card, or a paid tier.**

### Permitted
- Open-source software running locally (Docling, ChromaDB, sentence-transformers, LangChain,
  Streamlit, React).
- Cloud APIs with a genuine free tier that requires **no credit card on file**: Groq Cloud,
  LangSmith free tier.
- Free model weights from HuggingFace Hub.

### Forbidden
- OpenAI, Anthropic, Google, Cohere, or any other metered LLM/embedding API. **This includes
  library defaults** — Ragas defaults to an OpenAI judge, LangChain examples default to
  `OpenAIEmbeddings`. These defaults must be explicitly overridden, and the override must be
  visible at the call site, not buried in an env var.
- Managed vector databases (Pinecone, Weaviate Cloud, Qdrant Cloud), managed OCR (AWS Textract,
  Google Document AI, Azure Form Recognizer), or the `unstructured` hosted API.
- Any dependency whose install prompts for a license key.
- Paid fonts, paid icon sets, or paid UI component libraries in the frontend.

### Enforcement
- `requirements.txt` is reviewed against this rule on every change.
- Before adding **any** new dependency, state in the PR/commit message: *what it costs*, *whether
  it phones home*, and *what free alternative was rejected and why*.
- `grep -rE "openai|OPENAI_API_KEY|anthropic|ANTHROPIC_API_KEY" src/` must return zero results.
- If a library silently falls back to a paid provider, that is a bug of the highest severity —
  it means we could be accruing cost without noticing.

### The reasoning
This constraint is a design forcing function, not just a budget. It pushes embeddings local
(which is also the privacy-correct choice for financial documents), pushes parsing local (which
is also faster and deterministic), and reserves cloud calls for the one thing that genuinely
requires a frontier model: reasoning. If a paid service ever looks necessary, that is a signal
the architecture is wrong — revisit `architecture.md` §8 before revisiting this rule.

---

## Rule 2 — Strict Modular Architecture, Type Hints, Robust Error Handling

### 2.1 Modularity
- One module, one responsibility. The boundaries are fixed in `architecture.md` §4.
- **Dependency direction is one-way:**
  `config → schemas → parser → extractor → vectorstore → chain → app`.
  A module never imports from a module to its right. Circular imports are a merge blocker.
- **The frontend consumes `src/`; `src/` never imports from the frontend.** No `import streamlit`
  inside `src/`. Not for caching, not for progress bars, not "just this once" — that single
  import is what makes the UI framework unswappable.
- Every module exposes a small, documented public surface. Helpers are `_`-prefixed.
- No module exceeds ~400 lines. If it does, it is doing more than one job.

### 2.2 Type Hints
- **Every** function signature is fully annotated — parameters and return type. No bare `def
  process(data):`.
- Public data structures are **Pydantic v2 models**, not dicts. `FinancialRecord`, not
  `dict[str, Any]`. A dict passed between modules is an untyped API and will drift.
- `from __future__ import annotations` at the top of every module.
- Prefer `X | None` over `Optional[X]`; prefer `Literal[...]` over free-form strings for closed
  sets (document types, chunk types, model roles).
- **All monetary values are `decimal.Decimal`.** Never `float`. This is not stylistic — float
  currency arithmetic silently produces wrong totals, and correct numbers are the entire product.
- `mypy src/` should pass. Where a third-party library lacks stubs, `# type: ignore[import]` with
  the specific error code — never a bare `# type: ignore`.

### 2.3 Error Handling
- **No bare `except:` and no bare `except Exception: pass`.** Catch the specific exception you
  expect and can handle.
- Every module defines its own exception types deriving from a shared base:
  ```python
  class AssistantError(Exception): ...
  class ParsingError(AssistantError): ...
  class ExtractionError(AssistantError): ...
  class RetrievalError(AssistantError): ...
  class LLMError(AssistantError): ...      # incl. RateLimitError subclass
  ```
- Error messages are **actionable and user-facing-safe**. `"Could not parse page 3 of
  invoice.pdf — the page appears to be a scanned image with no extractable text. Enable the
  vision fallback in Settings."` — not `"NoneType has no attribute 'text'"`.
- **External calls are wrapped:** every Groq call has a timeout, a retry policy with exponential
  backoff + jitter, and a specific handler for HTTP 429. A rate-limit hit must degrade
  gracefully with a clear UI message, never a stack trace.
- **Never fabricate a value to recover from an error.** If `total_amount` cannot be extracted,
  the field is `None` with a warning appended to `extraction_warnings` — it is *never* defaulted
  to `0` or inferred. A silently-wrong number is worse than a visible failure.
- Logging via the stdlib `logging` module with module-level loggers. No `print()` in `src/`.
- **Financial data must never be logged.** Log document IDs, page numbers, timings, and token
  counts — never line-item descriptions, amounts, vendor names, or raw parsed text.

### 2.4 Testing
- Every `src/` module has a corresponding `tests/test_<module>.py`.
- Parser, extractor, and validation logic are tested against fixtures in `evals/fixtures/` —
  **synthetic documents only, never real financial records.**
- LLM calls are mocked in unit tests. Live-API tests are marked `@pytest.mark.integration` and
  excluded from the default run so the suite is free and offline.

---

## Rule 3 — Always Reference `memory.md` Before Code Changes

`memory.md` is the project's working state. It is authoritative on *what is done*, *what is in
flight*, and *what was decided and why*.

### The protocol
1. **Before starting any work:** read `memory.md`. Confirm the current phase, check the "Next
   Immediate Actions" list, and check the Decision Log for anything that constrains the task.
2. **Before writing code that touches an existing module:** check the Decision Log for a prior
   decision about that module. Do not silently reverse a decision — if a past decision now looks
   wrong, log the reversal *with its reasoning* rather than quietly overwriting the code.
3. **After completing a unit of work:** update `memory.md` in the same change set:
   - Move the item to "Completed" with a date.
   - Add any new decision to the Decision Log (with rationale, not just the outcome).
   - Add any new blocker or open question.
   - Rewrite "Next Immediate Actions" so the next session can start cold.
4. **`memory.md` is never allowed to go stale.** A commit that changes phase status but not
   `memory.md` is incomplete.

### Companion rule
`prd.md`, `architecture.md`, `rules.md`, `phases.md`, and `design.md` are **specifications** — they
change deliberately, via an explicit decision. `memory.md` is a **log** — it changes constantly.
Do not put durable specification into `memory.md`, and do not put session state into the specs.

---

## Rule 4 — Secrets and Data Hygiene

- API keys live in `.env`. `.env` is gitignored. `.env.example` is committed with placeholder
  values and a comment for each key explaining where to obtain it free.
- `data/` is gitignored in full — uploads, Chroma store, and LLM cache never enter version
  control.
- **No real financial documents in the repository, ever.** Fixtures are synthetic.
- A secret that is ever committed is treated as compromised: rotate it, do not just remove it.

---

## Rule 5 — Grounding Is Non-Negotiable

The product's value is that its numbers are trustworthy. Therefore:

- The system prompt must instruct the model to refuse rather than infer (FR-4.2).
- Every answer surfaces its citations; an uncited answer is rendered with an explicit warning.
- Numeric claims are cross-checked against the extracted `FinancialRecord` where the field exists
  (FR-4.4), and mismatches are surfaced, not suppressed.
- Extraction arithmetic is validated (FR-2.4); failures produce a visible warning banner.
- **We do not ship a feature that makes a confident-sounding claim we cannot trace to a source.**
