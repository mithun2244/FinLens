---
title: FinLens
emoji: 🔍
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# FinLens — Multimodal AI Financial Assistant

Upload a financial document — an invoice or a card statement — and ask *"why was this charge
deducted?"*. FinLens extracts the structured record, retrieves the policy that explains the
charge, and answers with every figure traceable to a source.

**It runs at $0.** Documents are parsed locally and vectors are stored locally, on free tiers
that need no credit card. Embeddings and reasoning are cloud calls; see
[The zero-cost stack](#the-zero-cost-stack) for where each piece runs.

![The dashboard](docs/dashboard.png)

---

## Live Demo

**The app:** <https://fin-lens-eta.vercel.app>

The Next.js workspace — upload a document, watch it parse, ask questions with citations you
can click back to the page region they came from.

**Backend API Documentation (Swagger UI):** <https://ai-finlens.onrender.com/docs>

Every endpoint below, callable from the browser with no client needed. The frontend is
hosted on Vercel and the API on Render, as two separate services.

> The API runs on Render's free tier, which **spins the service down when idle**. The first
> request after a quiet period wakes it and can take 30-60 seconds; everything after that is
> fast. That applies to the app too, since it calls the same API.

### Endpoints

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/health` | Liveness, plus whether the LLM and embedding endpoints are configured. Never fails on a missing key — it reports. |
| `POST` | `/api/upload` | Parse, extract and index a PDF (`multipart/form-data`, field `file`). Returns the full structured record. |
| `POST` | `/api/chat` | Ask a question. Streams newline-delimited JSON events: `stage`, `token`, `answer`, `error`. Takes `question` and an optional `document_id` to scope retrieval to one document. |
| `GET` | `/api/samples` | The bundled sample documents, so the API can be tried without uploading anything. |
| `POST` | `/api/samples/{filename}` | Load one of those samples as if it had been uploaded. |
| `POST` | `/api/policies` | Index the policy corpus that cross-document questions are answered against. |
| `GET` | `/api/documents/{id}/pages/{n}` | Rendered page image, used by the previewer for citation highlighting. |
| `DELETE` | `/api/documents/{id}` | Remove a document and its chunks from the index. |

### Try it in one command

```bash
# Upload a sample and read back the extracted record
curl -X POST https://ai-finlens.onrender.com/api/samples/clean_invoice.pdf

# Ask a question about it (use the document_id returned above)
curl -X POST https://ai-finlens.onrender.com/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"Why is the NAT Gateway charge so high?","document_id":"<id>"}'
```

Check `GET /api/health` first. `llm_configured` and `embeddings_configured` tell you whether
answering and retrieval are actually available — extraction works even when both are false.

---

## What it actually does

Ask *"Why is the NAT Gateway charge so high? Check it against our cloud billing policy."* and it
answers (verbatim, abridged at the ellipsis):

> The NAT Gateway charge is high because it is billed per GB of data processed, and the invoice
> shows a charge of "412.90" USD `[clean_invoice.pdf:1]` for "1" GB `[clean_invoice.pdf:1]`.
> According to the cloud billing policy, NAT Gateways are billed on two separate dimensions: an
> hourly charge for each gateway and a data processing charge per GB passed through it
> `[cloud_billing_policy.md:1]`. […] The charge of "412.90" USD exceeds the internal budget alert
> threshold of USD 200 per month `[cloud_billing_policy.md:1]`, indicating that this spend should
> be reviewed by the infrastructure lead.

Two documents, three citations, and every number checked against the extracted record before it
reaches the screen.

Ask it something the documents do not answer and it refuses:

> I cannot determine this from the provided documents. The documents provided are related to an
> invoice from Amazon Web Services and corporate travel and cloud billing policies, and do not
> mention the CEO's salary. To answer this question, a document containing information about the
> CEO's salary, such as a company's annual report or financial statements, would be needed.

---

## The zero-cost stack

| Layer | Technology | Cost | Runs |
|---|---|---|---|
| Orchestration | Python 3.12 + LangChain (LCEL) | Free (OSS) | Local |
| Document parsing | pdfplumber (text + tables) + PyMuPDF (rendering) | Free (OSS) | Local, CPU |
| Vector store | ChromaDB, embedded persistent client | Free (OSS) | Local, on disk |
| Embeddings | `all-MiniLM-L6-v2`, 384-dim, via HF Inference API | Free tier | Cloud |
| Reasoning | Groq — `llama-3.3-70b-versatile` | Free tier | Cloud |
| Query rewriting / eval judge | Groq — `llama-3.1-8b-instant` | Free tier | Cloud |
| Evaluation | Ragas | Free (OSS) | Local + Groq |
| Frontend | Streamlit + custom CSS | Free (OSS) | Local |

No component requires a credit card. See `rules.md` Rule 1 for what that constraint forbids and
why it is a design forcing function rather than a budget.

---

## Setup

**Requirements:** Python 3.10+ (developed on 3.12.10), CPU only. No model weights are
downloaded — the runtime install is small enough to fit a 512 MB container.

```bash
python -m venv venv
venv\Scripts\activate            # Windows;  source venv/bin/activate elsewhere

pip install -r requirements.txt                          # the API
pip install -r requirements.txt -r requirements-dev.txt  # + Streamlit, evals, tests
```

`requirements.txt` is runtime-only, so the deployed image stays inside 512 MB. Streamlit,
Ragas and the test tooling live in `requirements-dev.txt` — you need both to run the suite
or the dashboard.

**Get two free keys**, neither needing a credit card: a Groq key at
[console.groq.com/keys](https://console.groq.com/keys) for reasoning, and a Hugging Face token
at [hf.co/settings/tokens](https://hf.co/settings/tokens) for embeddings. Extraction works
without either; answering needs Groq and retrieval needs the HF token.

```bash
cp .env.example .env
# then edit .env and replace gsk_your_key_here with your key
```

**Verify the environment.** This checks every dependency, embeds a string locally, opens Chroma,
and pings Groq:

```bash
python scripts/verify_setup.py     # must exit 0
python scripts/check_models.py     # confirms the configured models are still served
```

`check_models.py` matters more than it looks — Groq retires models on short notice, and this
project has already been broken twice by it (see `memory.md`, decisions D-3 and D-15).

**Generate the sample documents.** No real financial documents are in this repository; every
fixture is generated from invented data:

```bash
python scripts/make_fixtures.py
```

---

## Running

```bash
streamlit run app.py
```

Launch is fast — there are no model weights to download or load. The app opens on the Document
Workspace; drop a PDF or click one of the sample documents. PDFs only: scanned images are
rejected, because OCR was removed to fit the deployment budget.

**Evaluation:**

```bash
python -m src.evals --extraction    # deterministic only — zero API calls, run freely
python -m src.evals --pace 3        # full suite including Ragas metrics
python -m src.evals --limit 8       # quick spot check
```

**Tests:**

```bash
pytest                    # 254 offline tests, no API calls, free
pytest -m integration     # 10 live-API tests (needs GROQ_API_KEY and HF_TOKEN)
mypy src app.py ui        # clean across 14 modules
```

---

## How it works

```
upload ─→ parser.py ──→ extractor.py ─→ vectorstore.py ──→ chain.py ─→ app.py
          pdfplumber    FinancialRecord   Chroma +          Groq RAG    Streamlit
          + PyMuPDF                       MiniLM over HTTP              / api.py
             │
             └─→ a PDF with no text layer is rejected, not OCR'd
```

Three design decisions shape everything else:

**Numbers are deterministic, prose is not.** Line items come from the PDF's own table structure and
totals from labelled-amount regexes. The model supplies vendor names and dates — things that vary
too much for patterns. Where the model *does* propose an amount, it is accepted only if that exact
figure appears in the document text. The model can read; it cannot invent.

**One chunk per table row, never split.** A row split across chunks retrieves a description
attached to the wrong amount, which is the dominant cause of hallucinated invoice figures.

**Grounding is checked, not requested.** The prompt asks for citations and refusals, and then the
chain verifies: citation markers are resolved against chunks actually retrieved (invented ones are
reported), and every monetary figure is traced to the record or to a retrieved snippet.

Bounding boxes from pdfplumber are normalized to top-left 0–1 coordinates, so clicking a citation
highlights the source region on the page image:

![Citation highlighting](docs/citation-highlight.png)

---

## Project layout

```
prd.md architecture.md rules.md phases.md design.md memory.md   ← read these first
app.py                    Streamlit dashboard
ui/styles.py              design tokens, one place, no hardcoded hex
ui/components.py          render helpers for the four surfaces
src/config.py             model IDs, paths, thresholds — model strings live ONLY here
src/llm.py                ChatGroq factory, provider-error translation, retries
src/schemas.py            FinancialRecord, Citation, Answer, RunStats — all money is Decimal
src/parser.py             pdfplumber ingestion, table/gutter detection, bbox normalization
src/extractor.py          ParsedDocument → validated FinancialRecord
src/vectorstore.py        chunking, local embedding, filtered retrieval
src/chain.py              LCEL RAG chain, streaming, citation + numeric verification
src/observability.py      per-request telemetry for the Observability Bar
src/evals.py              Ragas + deterministic extraction accuracy + grounding checks
scripts/                  setup verification, model liveness, fixture generation
evals/                    golden set (28 items), expectations, synthetic fixtures
```

The six markdown files at the root are the project's memory. `memory.md` in particular holds a
32-entry decision log explaining *why* things are the way they are — including several decisions
that were reversed when reality disagreed with the plan.

---

## What is verified, and what is not

Being straight about this matters more than a green badge.

**Verified:**

| | |
|---|---|
| `total_amount` extraction accuracy | **100%** (5/5 fixtures) — target 95% |
| Line-item recall | **100%** (17/17) — target 90% |
| Field accuracy | **100%** (40/40) |
| Tests | 198 offline + 8 live-API, `mypy` clean |
| Arithmetic validation | The deliberately unbalanced fixture is flagged, and **neither figure is adjusted** |
| OCR fallback | Fires on the scanned receipt and on nothing else |
| Responsive | No horizontal body scroll at 1440 / 1024 / 768 / 390 px |
| Citation highlighting | Lands on the correct region of the correct page |
| Graceful degradation | With no API key, extraction still works and says what is missing |

**Not yet verified:**

- **Ragas metrics (faithfulness, answer relevancy, context precision/recall) have not been
  measured.** The judge configuration is confirmed working in isolation — faithfulness scores 1.0
  on a grounded answer and 0.0 on a hallucinated one — but a full run needs ~220k tokens against
  Groq's 100k/day cap. Run `python -m src.evals --pace 3` on a fresh daily budget.
- **Refusal correctness is an open question.** One of four refusal cases is confirmed correct; the
  other three could not be re-tested before the token cap hit. They may be genuine grounding
  failures or a gap in the refusal detector — do not assume which without reading the answers.
- The ask → answer leg has not been walked in a browser (it needs tokens).

## Known limitations

- **Free-tier token budget is the binding constraint.** 100,000 tokens/day on the reasoning model.
  A full evaluation run costs roughly twice that, so complete runs are possible about every other
  day. `--extraction` is free and unlimited.
- **Citation highlighting is table-granular.** Citing one row highlights the whole table, because
  row chunks inherit the table's bounding box.
- **Comma-decimal amounts are rejected, not parsed.** `1.234,56` returns `None` rather than risk a
  1000× error. Latin-script, period-decimal documents only.
- **PDFs with a text layer only.** Scanned documents, photographed receipts and image uploads
  are rejected rather than parsed — OCR was removed to fit the 512 MB deployment. A PDF whose
  pages carry no extractable text is refused too, because parsing one yields a record with no
  line items and no total, which is indistinguishable from an empty invoice.
- **Tables are read from the PDF's own structure**, so a table drawn with neither ruling lines
  nor consistent column gutters may extract incompletely.
- **No handwriting recognition**, no multi-user auth, no bank integrations. See `prd.md` §7.
- Streamlit columns stack below 1200px rather than becoming the tabbed layout `design.md` §4.2
  sketches.

## Privacy

Documents are parsed and stored entirely on your machine, and `data/` is gitignored in full.
Page images never leave it.

**Text does.** Two things go out, and it is worth being precise about which:

- **Embeddings.** Every chunk of an indexed document is sent to the Hugging Face Inference
  API to be turned into a vector — at upload time, not only when you ask something. This
  reverses an earlier design decision (D-2) and was the price of fitting the service into
  512 MB, since computing embeddings locally means shipping torch. Set
  `EMBEDDING_ENDPOINT_URL` to a self-hosted [Text Embeddings Inference][tei] server to
  restore locality; the code path is identical.
- **Answering.** The text of retrieved chunks and your question go to Groq, and only when
  you ask one.

No real financial documents are in this repository, and `rules.md` Rule 4 forbids adding any.

[tei]: https://github.com/huggingface/text-embeddings-inference

---

## License

Synthetic fixtures and project code are provided as-is for educational use. Third-party
dependencies retain their own licenses — pdfplumber (MIT), ChromaDB (Apache-2.0),
Streamlit (Apache-2.0), PyMuPDF (AGPL).
