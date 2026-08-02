"""Tests for src.vectorstore (phases.md Phase 3 DoD).

Entirely free and offline: embeddings are local and Chroma is embedded, so nothing here
touches a paid or remote service.

Tests write to dedicated ``test_*`` collections and drop them afterwards, so running the
suite never disturbs a real index sitting in ``data/chroma/``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src import RetrievalError
from src.config import FIXTURES_DIR
from src.extractor import extract_record
from src.parser import parse_document
from src.schemas import FinancialRecord, ParsedDocument
from src.vectorstore import (
    build_chunks,
    delete_document,
    ingest_document,
    ingest_text,
    reset_collection,
    retrieve,
)

TEST_DOCS = "test-financial-documents"
TEST_POLICIES = "test-policy-corpus"

CURRENT = FIXTURES_DIR / "clean_invoice.pdf"
PRIOR = FIXTURES_DIR / "prior_invoice.pdf"
POLICY_DIR = FIXTURES_DIR / "policies"

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def current_doc() -> tuple[ParsedDocument, FinancialRecord]:
    parsed = parse_document(CURRENT, document_id="t-current", persist_source=False)
    return parsed, extract_record(parsed, use_llm=False)


@pytest.fixture(scope="module")
def prior_doc() -> tuple[ParsedDocument, FinancialRecord]:
    parsed = parse_document(PRIOR, document_id="t-prior", persist_source=False)
    return parsed, extract_record(parsed, use_llm=False)


@pytest.fixture(scope="module")
def populated(
    current_doc: tuple[ParsedDocument, FinancialRecord],
    prior_doc: tuple[ParsedDocument, FinancialRecord],
) -> Iterator[None]:
    """A live index holding both invoices and the policy corpus."""
    reset_collection(TEST_DOCS)
    reset_collection(TEST_POLICIES)

    for parsed, record in (current_doc, prior_doc):
        ingest_document(parsed, record, collection=TEST_DOCS)
    for path in sorted(POLICY_DIR.glob("*.md")):
        ingest_text(
            path.read_text(encoding="utf-8"),
            document_id=f"policy:{path.stem}",
            filename=path.name,
            collection=TEST_POLICIES,
        )
    yield
    reset_collection(TEST_DOCS)
    reset_collection(TEST_POLICIES)


# ── Chunking: where RAG quality is won or lost ───────────────────────────────


def test_every_table_row_becomes_exactly_one_chunk(
    current_doc: tuple[ParsedDocument, FinancialRecord],
) -> None:
    """Decision D-7. A split row attaches a description to the wrong amount."""
    parsed, record = current_doc
    rows = [c for c in build_chunks(parsed, record) if c.chunk_type == "table_row"]
    assert len(rows) == 3


def test_table_row_chunks_are_self_contained(
    current_doc: tuple[ParsedDocument, FinancialRecord],
) -> None:
    """Description and amount must live in the same chunk, with column names attached."""
    parsed, record = current_doc
    nat = next(
        c for c in build_chunks(parsed, record)
        if c.chunk_type == "table_row" and "NAT Gateway" in c.text
    )
    assert "Amount: 412.90" in nat.text
    assert "Description:" in nat.text


def test_table_rows_are_not_duplicated_by_narrative_chunks(
    current_doc: tuple[ParsedDocument, FinancialRecord],
) -> None:
    """Narrative chunking uses narrative_markdown, so rows are indexed once, not twice."""
    parsed, record = current_doc
    text_chunks = [c for c in build_chunks(parsed, record) if c.chunk_type == "text"]
    assert not any("Amount: 412.90" in c.text for c in text_chunks)


def test_record_summary_chunk_carries_the_headline_figures(
    current_doc: tuple[ParsedDocument, FinancialRecord],
) -> None:
    parsed, record = current_doc
    summary = next(
        c for c in build_chunks(parsed, record) if c.chunk_type == "record_summary"
    )
    assert "501.27" in summary.text
    assert "Amazon Web Services" in summary.text


def test_chunks_without_a_record_still_index_the_tables(
    current_doc: tuple[ParsedDocument, FinancialRecord],
) -> None:
    parsed, _ = current_doc
    chunks = build_chunks(parsed, None)
    assert any(c.chunk_type == "table_row" for c in chunks)
    assert not any(c.chunk_type == "record_summary" for c in chunks)


def test_chunk_ids_are_unique(current_doc: tuple[ParsedDocument, FinancialRecord]) -> None:
    parsed, record = current_doc
    ids = [c.chunk_id for c in build_chunks(parsed, record)]
    assert len(ids) == len(set(ids))


# ── Metadata and provenance ──────────────────────────────────────────────────


def test_table_row_metadata_is_chroma_safe(
    current_doc: tuple[ParsedDocument, FinancialRecord],
) -> None:
    """Chroma rejects None and nested values; every value must be a scalar."""
    parsed, record = current_doc
    for chunk in build_chunks(parsed, record):
        for key, value in chunk.to_metadata().items():
            assert isinstance(value, (str, int, float, bool)), f"{key}={value!r}"


def test_bbox_is_flattened_into_four_floats(
    current_doc: tuple[ParsedDocument, FinancialRecord],
) -> None:
    parsed, record = current_doc
    row = next(c for c in build_chunks(parsed, record) if c.chunk_type == "table_row")
    metadata = row.to_metadata()
    for key in ("bbox_left", "bbox_top", "bbox_right", "bbox_bottom"):
        assert isinstance(metadata[key], float)


def test_bbox_survives_the_round_trip(populated: None) -> None:
    """Decision D-16: without this, citation chips cannot drive the previewer overlay."""
    hits = retrieve("NAT gateway data processing", collection=TEST_DOCS, k=3)
    row = next(c for c in hits if c.chunk_type == "table_row")
    assert row.bbox is not None
    assert 0.0 <= row.bbox.left < row.bbox.right <= 1.0
    assert 0.0 <= row.bbox.top < row.bbox.bottom <= 1.0


def test_citations_carry_filename_and_page(populated: None) -> None:
    for citation in retrieve("S3 storage", collection=TEST_DOCS, k=3):
        assert citation.filename.endswith((".pdf", ".png", ".md"))
        assert citation.page >= 1
        assert citation.label


# ── Retrieval quality ────────────────────────────────────────────────────────


def test_targeted_question_retrieves_the_right_row(populated: None) -> None:
    """Phase 3 DoD: 'what was the NAT gateway charge' must hit the correct row in top-3."""
    hits = retrieve("what was the NAT gateway charge", collection=TEST_DOCS, k=3)
    assert any("NAT Gateway" in c.snippet and "412.90" in c.snippet for c in hits[:3])


def test_retrieval_distinguishes_between_line_items(populated: None) -> None:
    hits = retrieve("how much was S3 storage", collection=TEST_DOCS, k=1)
    assert "S3 Standard storage" in hits[0].snippet


def test_scores_are_similarities_not_distances(populated: None) -> None:
    hits = retrieve("S3 standard storage", collection=TEST_DOCS, k=3)
    assert all(0.0 <= c.score <= 1.0 for c in hits)
    assert hits[0].score >= hits[-1].score, "results are not ordered best-first"


# These two measure semantic retrieval quality, which is the one thing the offline stub
# cannot stand in for: a hash-derived vector has no meaning, so "why is my NAT gateway
# charge so high" cannot land on the NAT Gateway policy. They need the real embedding
# endpoint, and their own store — querying real vectors against a stub-embedded collection
# would compare noise to noise and pass for the wrong reason.
#
# Marked integration, so the default offline run excludes them:
#     pytest -m integration


@pytest.mark.integration
def test_policy_corpus_answers_a_why_question(live_policies: str) -> None:
    """The cross-document grounding the product exists to do."""
    hits = retrieve("why is my NAT gateway charge so high", collection=live_policies, k=3)
    assert "NAT Gateway" in hits[0].snippet
    assert "per GB" in hits[0].snippet


@pytest.mark.integration
def test_policy_corpus_finds_the_hotel_cap(live_policies: str) -> None:
    hits = retrieve("what is the nightly hotel spending limit", collection=live_policies, k=3)
    assert any("200 per night" in c.snippet for c in hits)


# ── Scoping (FR-3.4) ─────────────────────────────────────────────────────────


def test_document_scoping_excludes_other_documents(populated: None) -> None:
    """'Explain *this* charge' must mean the open document, not anything ever uploaded."""
    hits = retrieve(
        "NAT gateway data processing", collection=TEST_DOCS, document_id="t-prior", k=5
    )
    assert hits
    assert {c.document_id for c in hits} == {"t-prior"}


def test_unscoped_retrieval_spans_documents(populated: None) -> None:
    hits = retrieve("NAT gateway data processing", collection=TEST_DOCS, k=5)
    assert len({c.document_id for c in hits}) > 1


def test_scoping_to_an_unknown_document_returns_nothing(populated: None) -> None:
    assert retrieve("anything", collection=TEST_DOCS, document_id="does-not-exist") == []


# ── Lifecycle ────────────────────────────────────────────────────────────────


def test_reingest_replaces_rather_than_duplicates(
    populated: None, current_doc: tuple[ParsedDocument, FinancialRecord]
) -> None:
    parsed, record = current_doc
    before = len(retrieve("NAT gateway", collection=TEST_DOCS, document_id="t-current", k=50))
    ingest_document(parsed, record, collection=TEST_DOCS)
    after = len(retrieve("NAT gateway", collection=TEST_DOCS, document_id="t-current", k=50))
    assert before == after


def test_delete_removes_only_the_named_document(
    populated: None, prior_doc: tuple[ParsedDocument, FinancialRecord]
) -> None:
    parsed, record = prior_doc
    removed = delete_document("t-prior", collection=TEST_DOCS)
    assert removed > 0
    assert retrieve("anything", collection=TEST_DOCS, document_id="t-prior") == []
    assert retrieve("NAT gateway", collection=TEST_DOCS, document_id="t-current", k=5)
    ingest_document(parsed, record, collection=TEST_DOCS)  # restore for other tests


def test_deleting_an_absent_document_is_not_an_error() -> None:
    assert delete_document("never-existed", collection=TEST_DOCS) == 0


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_empty_query_returns_no_results(populated: None) -> None:
    assert retrieve("   ", collection=TEST_DOCS) == []


def test_zero_k_returns_no_results(populated: None) -> None:
    assert retrieve("NAT gateway", collection=TEST_DOCS, k=0) == []


def test_ingesting_blank_text_is_rejected() -> None:
    with pytest.raises(RetrievalError, match="no text"):
        ingest_text("   ", document_id="blank", filename="blank.md", collection=TEST_POLICIES)


# ── Corrupt-store diagnostics ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Error creating hnsw segment reader: Nothing found on disk",
        "Internal error: no such file or directory",
    ],
)
def test_corrupt_store_error_is_actionable(message: str) -> None:
    """Regression: the raw Chroma error told the user nothing they could act on."""
    from src.vectorstore import _explain_search_failure

    explained = _explain_search_failure(RuntimeError(message))
    assert "corrupt" in explained.lower()
    assert "re-upload" in explained.lower()
    assert "hnsw" not in explained.lower()


def test_unrecognised_search_failure_still_surfaces_detail() -> None:
    from src.vectorstore import _explain_search_failure

    assert "boom" in _explain_search_failure(RuntimeError("boom"))
