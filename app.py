"""FinLens — Streamlit dashboard for the Multimodal AI Financial Assistant (Phase 5A).

    streamlit run app.py

Four surfaces, per design.md §5: Document Workspace, Extraction Dashboard, RAG Chat, and
the Observability Bar.

This module owns layout and session state only. Every piece of business logic lives in
``src/`` and is imported, never reimplemented — which is what makes the framework choice
reversible (decision D-8).
"""

# ruff: noqa: E402
#   Imports intentionally follow load_dotenv() below — see the comment there. Everything
#   in src/ reads configuration at import time, so the environment must be settled first.

from __future__ import annotations

# ── Environment first, before anything reads it ──────────────────────────────
#
# override=True is deliberate and load-bearing. Both `load_dotenv()` (which defaults to
# override=False) and pydantic-settings give an already-exported environment variable
# priority over the .env file. So a shell that exports GROQ_API_KEY="" — a stale export,
# a CI stub, a debugging session — silently wins, and the app reports "no API key
# configured" while a perfectly valid key sits in .env. Verified: with GROQ_API_KEY=""
# exported, only override=True recovers the key.
#
# This must run before `src.config` is imported so nothing reads the environment first.
from dotenv import load_dotenv

load_dotenv(override=True)

import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

import streamlit as st

from src import AssistantError
from src.chain import ChatTurn, stream_answer, suggested_prompts
from src.config import (
    COLLECTION_POLICIES,
    FIXTURES_DIR,
    MODELS_BY_ROLE,
    SUPPORTED_EXTENSIONS,
    configure_logging,
    ensure_directories,
)
from src.extractor import extract_record
from src.parser import parse_document, warm_up
from src.schemas import RunStats
from src.vectorstore import (
    collection_stats,
    ingest_document,
    ingest_policy_files,
)
from src.vectorstore import warm_up as warm_embeddings
from ui.components import (
    app_header,
    empty_workspace,
    line_item_table,
    observability_bar,
    page_view,
    render_answer_html,
    totals_panel,
    user_message,
    validation_banner,
    vendor_card,
    warning_strips,
)
from ui.styles import STYLESHEET

logger = logging.getLogger(__name__)

SAMPLES: tuple[tuple[str, str], ...] = (
    ("AWS invoice", "clean_invoice.pdf"),
    ("Card statement", "multipage_statement.pdf"),
    ("Scanned receipt", "scanned_receipt.png"),
    ("Unbalanced invoice", "unbalanced_invoice.pdf"),
)

st.set_page_config(page_title="FinLens — Financial Assistant", page_icon="◈", layout="wide")
st.markdown(STYLESHEET, unsafe_allow_html=True)


# ── Session state ────────────────────────────────────────────────────────────


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "documents": {},          # document_id -> {"parsed": ParsedDocument, "record": FinancialRecord}
        "active_id": None,
        "messages": [],           # [{"role", "content", "answer"}]
        "page": 1,
        "citation": None,
        "stats": RunStats(model=MODELS_BY_ROLE["reasoning"]),
        "policies_loaded": False,
        "pending": None,
        "warmed": False,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


@st.cache_resource(show_spinner=False)
def _warm_models() -> bool:
    """Load every heavy model once per process, before the first upload.

    ``st.cache_resource`` runs this exactly once and keeps the result across reruns and
    across browser sessions. The models themselves are held by ``lru_cache`` inside
    ``src/`` — verified staying resident across uploads — so this is about *when* the
    cost is paid, not whether the objects persist. ``src/`` never imports Streamlit
    (decision D-8), which is why the decorator lives here rather than next to the loaders.

    Measured on a cold start: Docling converter 8.6 s, MiniLM 3.6 s. Both used to land on
    whoever uploaded first.
    """
    warm_up(with_ocr=False)
    warm_embeddings()
    return True


def _active() -> dict[str, Any] | None:
    if not st.session_state.active_id:
        return None
    return st.session_state.documents.get(st.session_state.active_id)


# ── Ingestion ────────────────────────────────────────────────────────────────


def _ingest(path: Path, display_name: str) -> None:
    """Parse, extract, index. Each stage is named in the UI rather than hidden behind a spinner."""
    document_id = str(uuid.uuid4())[:8]
    progress = st.status(f"Reading {display_name}…", expanded=True)
    try:
        progress.write("Detecting page layout and table structure…")
        parsed = parse_document(path, document_id=document_id)

        if parsed.used_ocr:
            progress.write("No text layer found — read with local OCR.")
        progress.write(f"Found {parsed.page_count} page(s), {len(parsed.tables)} table(s).")

        progress.write("Extracting the financial record…")
        record = extract_record(parsed)
        progress.write(
            f"Extracted {len(record.line_items)} line item(s); "
            f"validation: {record.validation_state}."
        )

        progress.write("Building the search index…")
        chunks = ingest_document(parsed, record)
        progress.write(f"Indexed {chunks} chunks.")

        st.session_state.documents[document_id] = {"parsed": parsed, "record": record}
        st.session_state.active_id = document_id
        st.session_state.page = 1
        st.session_state.citation = None
        st.session_state.messages = []
        st.session_state.stats = RunStats(
            model=MODELS_BY_ROLE["reasoning"], parse_seconds=parsed.parse_seconds
        )
        progress.update(label=f"{display_name} ready", state="complete", expanded=False)
    except AssistantError as exc:
        progress.update(label="Could not read this document", state="error", expanded=True)
        progress.write(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected failure must still be legible
        logger.exception("Unexpected ingestion failure")
        progress.update(label="Something went wrong", state="error", expanded=True)
        progress.write(f"{type(exc).__name__}: {exc}")


def _load_policies() -> None:
    paths = sorted((FIXTURES_DIR / "policies").glob("*.md"))
    if not paths:
        st.warning("No policy documents found. Run `python scripts/make_fixtures.py` first.")
        return
    with st.spinner("Indexing policy corpus…"):
        ingest_policy_files(paths)
    st.session_state.policies_loaded = True


# ── Surfaces ─────────────────────────────────────────────────────────────────


def _render_workspace() -> None:
    st.markdown('<div class="fl-panel-title">Document workspace</div>', unsafe_allow_html=True)

    uploaded = st.file_uploader(
        "Drop a financial document",
        type=[ext.lstrip(".") for ext in sorted(SUPPORTED_EXTENSIONS)],
        label_visibility="collapsed",
    )
    if uploaded is not None and uploaded.name != st.session_state.get("last_upload"):
        st.session_state.last_upload = uploaded.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(uploaded.name).suffix) as handle:
            handle.write(uploaded.getbuffer())
            temp_path = Path(handle.name)
        target = temp_path.with_name(uploaded.name)
        shutil.move(temp_path, target)
        _ingest(target, uploaded.name)
        st.rerun()

    active = _active()
    if active is None:
        st.markdown(empty_workspace(), unsafe_allow_html=True)
        st.caption("Or try a sample:")
        columns = st.columns(len(SAMPLES))
        for column, (label, filename) in zip(columns, SAMPLES):
            if column.button(label, use_container_width=True, key=f"sample-{filename}"):
                _ingest(FIXTURES_DIR / filename, filename)
                st.rerun()
        return

    parsed = active["parsed"]

    if len(st.session_state.documents) > 1:
        options = {
            doc_id: data["record"].filename for doc_id, data in st.session_state.documents.items()
        }
        chosen = st.selectbox(
            "Active document",
            options=list(options),
            format_func=lambda key: options[key],
            index=list(options).index(st.session_state.active_id),
        )
        if chosen != st.session_state.active_id:
            st.session_state.active_id = chosen
            st.session_state.page = 1
            st.session_state.citation = None
            st.rerun()

    if parsed.page_count > 1:
        left, middle, right = st.columns([1, 3, 1])
        if left.button("◀", use_container_width=True, disabled=st.session_state.page <= 1):
            st.session_state.page -= 1
            st.rerun()
        middle.markdown(
            f'<div style="text-align:center;padding-top:6px;font-size:12px;">'
            f"Page {st.session_state.page} of {parsed.page_count}</div>",
            unsafe_allow_html=True,
        )
        if right.button(
            "▶", use_container_width=True, disabled=st.session_state.page >= parsed.page_count
        ):
            st.session_state.page += 1
            st.rerun()

    st.markdown(
        page_view(parsed, st.session_state.page, st.session_state.citation),
        unsafe_allow_html=True,
    )

    citation = st.session_state.citation
    if citation is not None:
        st.caption(f"Highlighting {citation.label} · relevance {citation.score:.2f}")
        if st.button("Clear highlight"):
            st.session_state.citation = None
            st.rerun()


def _render_extraction() -> None:
    st.markdown('<div class="fl-panel-title">Extraction dashboard</div>', unsafe_allow_html=True)
    active = _active()
    if active is None:
        st.markdown(
            '<div class="fl-panel"><div class="fl-meta">Upload a document to see its '
            "extracted vendor, line items, and totals.</div></div>",
            unsafe_allow_html=True,
        )
        return

    record = active["record"]
    st.markdown(vendor_card(record), unsafe_allow_html=True)
    st.markdown(validation_banner(record), unsafe_allow_html=True)
    st.markdown(
        f'<div class="fl-panel">{line_item_table(record.line_items, record.currency)}'
        f"{totals_panel(record)}</div>",
        unsafe_allow_html=True,
    )

    if record.line_items:
        payload = record.model_dump_json(indent=2)
        st.download_button(
            "Download record as JSON",
            payload,
            file_name=f"{Path(record.filename).stem}-record.json",
            mime="application/json",
            use_container_width=True,
        )


def _render_chat() -> None:
    st.markdown('<div class="fl-panel-title">Ask about this document</div>', unsafe_allow_html=True)
    active = _active()
    record = active["record"] if active else None

    if not st.session_state.policies_loaded:
        if st.button("Load sample policy corpus", use_container_width=True):
            _load_policies()
            st.rerun()

    for message in st.session_state.messages:
        if message["role"] == "user":
            st.markdown(user_message(message["content"]), unsafe_allow_html=True)
            continue

        answer = message.get("answer")
        citations = answer.citations if answer else []
        st.markdown(render_answer_html(message["content"], citations), unsafe_allow_html=True)
        if answer is not None:
            strips = warning_strips(answer)
            if strips:
                st.markdown(strips, unsafe_allow_html=True)
            if citations:
                columns = st.columns(min(len(citations), 3))
                for index, citation in enumerate(citations):
                    column = columns[index % len(columns)]
                    if column.button(
                        f"⧉ {citation.label}",
                        key=f"cite-{message['id']}-{index}",
                        use_container_width=True,
                    ):
                        if citation.document_id in st.session_state.documents:
                            st.session_state.active_id = citation.document_id
                        st.session_state.page = citation.page
                        st.session_state.citation = citation
                        st.rerun()

    if active is not None and not st.session_state.messages:
        st.caption("Try one of these:")
        chips = suggested_prompts(record, has_policies=st.session_state.policies_loaded)
        for index, chip in enumerate(chips[:6]):
            if st.button(chip, key=f"chip-{index}", use_container_width=True):
                st.session_state.pending = chip
                st.rerun()

    question = st.chat_input(
        "Ask why a charge was deducted…" if active else "Upload a document first",
        disabled=active is None,
    )
    if question:
        st.session_state.pending = question
        st.rerun()

    if st.session_state.pending:
        _answer(st.session_state.pending, record)
        st.session_state.pending = None
        st.rerun()


def _answer(question: str, record: Any) -> None:
    """Stream one answer, rendering named stages then tokens (design.md §5.3)."""
    st.session_state.messages.append({"role": "user", "content": question, "id": len(st.session_state.messages)})
    st.markdown(user_message(question), unsafe_allow_html=True)

    history = [
        ChatTurn(role=m["role"], content=m["content"])
        for m in st.session_state.messages[:-1]
        if m["role"] in ("user", "assistant")
    ]
    stats = st.session_state.stats
    stats.retrieve_seconds = 0.0
    stats.generate_seconds = 0.0

    stage_slot = st.empty()
    text_slot = st.empty()
    pieces: list[str] = []
    answer = None
    error = None

    for event in stream_answer(
        question,
        record=record,
        document_id=st.session_state.active_id,
        history=history,
        stats=stats,
    ):
        if event.type == "stage":
            stage_slot.markdown(f'<div class="fl-stage">{event.stage}</div>', unsafe_allow_html=True)
        elif event.type == "token":
            pieces.append(event.token or "")
            text_slot.markdown(
                render_answer_html("".join(pieces) + "▌", []), unsafe_allow_html=True
            )
        elif event.type == "answer":
            answer = event.answer
        elif event.type == "error":
            error = event.message

    stage_slot.empty()

    if error is not None:
        text_slot.markdown(f'<div class="fl-warn-strip">{error}</div>', unsafe_allow_html=True)
        st.session_state.messages.append(
            {"role": "assistant", "content": error, "answer": None, "id": len(st.session_state.messages)}
        )
        return

    text = answer.text if answer else "".join(pieces)
    text_slot.markdown(
        render_answer_html(text, answer.citations if answer else []), unsafe_allow_html=True
    )
    st.session_state.messages.append(
        {"role": "assistant", "content": text, "answer": answer, "id": len(st.session_state.messages)}
    )


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    configure_logging()
    ensure_directories()
    _init_state()

    if not st.session_state.warmed:
        with st.spinner("Loading local document and embedding models (one-time, ~12s)…"):
            _warm_models()
        st.session_state.warmed = True

    try:
        counts = collection_stats()
    except AssistantError:
        counts = {}

    st.markdown(
        app_header(len(st.session_state.documents), counts.get(COLLECTION_POLICIES, 0)),
        unsafe_allow_html=True,
    )
    st.markdown(
        observability_bar(st.session_state.stats, model=MODELS_BY_ROLE["reasoning"]),
        unsafe_allow_html=True,
    )

    workspace, extraction, chat = st.columns([42, 33, 25], gap="medium")
    with workspace:
        _render_workspace()
    with extraction:
        _render_extraction()
    with chat:
        _render_chat()


if __name__ == "__main__":
    main()
