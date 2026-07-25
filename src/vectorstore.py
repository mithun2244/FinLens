"""Chunking, local embedding, and persistent retrieval (phases.md Phase 3).

Three rules govern this module, each learned the hard way:

**One chunk per table row, never split** (decision D-7). A split row is the dominant cause
of hallucinated invoice figures — half a row retrieved is a description attached to the
wrong amount. :meth:`TableBlock.to_serialized_rows` keeps the column name beside every
value so a question like "what was the NAT gateway charge" can match the right row.

**Embeddings are always passed explicitly** (decision D-14). Calling Chroma with
``query_texts`` makes it silently download and use its own bundled MiniLM — a second model
we do not control, producing vectors that need not agree with ours.

**Bounding boxes ride along in metadata** (decision D-16). Chroma metadata values must be
scalars, so a box is flattened to four floats and rebuilt on the way out. This is what lets
a citation chip drive the document previewer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from src import RetrievalError
from src.config import (
    CHROMA_DIR,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    COLLECTION_DOCUMENTS,
    COLLECTION_POLICIES,
    EMBEDDING_MODEL,
    get_settings,
)
from src.schemas import (
    BoundingBox,
    ChunkType,
    Citation,
    FinancialRecord,
    ParsedDocument,
)

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection
    from chromadb.api.types import Where
    from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

__all__ = [
    "Chunk",
    "warm_up",
    "build_chunks",
    "ingest_document",
    "ingest_text",
    "retrieve",
    "retrieve_with_policies",
    "delete_document",
    "collection_stats",
    "reset_collection",
]

#: Cosine distance, so ``score = 1 - distance`` is a similarity in [0, 1]. Chroma defaults
#: to L2, which with normalized vectors is monotonically equivalent but produces scores
#: that are awkward to show a user (design.md §5.3 renders the score in a tooltip).
_SPACE = {"hnsw:space": "cosine"}


class Chunk(BaseModel):
    """One indexable unit of a document, with everything needed to cite it later."""

    chunk_id: str
    text: str
    document_id: str
    filename: str
    page: int = Field(ge=1)
    chunk_type: ChunkType = "text"
    bbox: BoundingBox | None = None
    vendor: str | None = None
    billing_date: str | None = None

    def to_metadata(self) -> dict[str, str | int | float | bool]:
        """Flatten to Chroma-safe scalars. ``None`` values are omitted, not stringified."""
        metadata: dict[str, str | int | float | bool] = {
            "document_id": self.document_id,
            "filename": self.filename,
            "page": self.page,
            "chunk_type": self.chunk_type,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.vendor:
            metadata["vendor"] = self.vendor
        if self.billing_date:
            metadata["billing_date"] = self.billing_date
        if self.bbox is not None:
            metadata["bbox_left"] = self.bbox.left
            metadata["bbox_top"] = self.bbox.top
            metadata["bbox_right"] = self.bbox.right
            metadata["bbox_bottom"] = self.bbox.bottom
        return metadata


# ── Backing services ─────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _get_embeddings() -> HuggingFaceEmbeddings:
    """The one embedding model in this system. Local, CPU, no network after first load."""
    from langchain_huggingface import HuggingFaceEmbeddings

    logger.info("Loading local embedding model %s", EMBEDDING_MODEL)
    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def warm_up() -> None:
    """Load the embedding model ahead of the first document.

    Measured: the first ``ingest_document`` took 3.62 s and every later one 0.14 s — the
    difference is loading MiniLM. Doing it at startup moves that cost off the user's
    first upload, where it is most visible.
    """
    _get_embeddings().embed_query("warm up")


@lru_cache(maxsize=1)
def _get_client() -> Any:
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def _get_collection(name: str) -> Collection:
    try:
        return _get_client().get_or_create_collection(name=name, metadata=_SPACE)
    except Exception as exc:  # noqa: BLE001 - surfaced as our own typed error
        raise RetrievalError(
            f"Could not open the '{name}' collection at {CHROMA_DIR}. "
            f"If the store is corrupt, delete data/chroma/ and re-ingest."
        ) from exc


# ── Chunking ─────────────────────────────────────────────────────────────────


def _split_narrative(text: str) -> list[str]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return [part.strip() for part in splitter.split_text(text) if part.strip()]


def _record_summary(record: FinancialRecord) -> str:
    """A single chunk stating the document's headline facts.

    Questions like "what is the total on this invoice" match a compact summary far more
    reliably than a bare number sitting in a table cell.
    """
    parts = [
        f"Document: {record.document_type} from {record.vendor_name}",
        f"File: {record.filename}",
    ]
    if record.invoice_number:
        parts.append(f"Invoice number: {record.invoice_number}")
    if record.billing_date:
        parts.append(f"Billing date: {record.billing_date}")
    if record.billing_period_start and record.billing_period_end:
        parts.append(
            f"Billing period: {record.billing_period_start} to {record.billing_period_end}"
        )
    if record.subtotal is not None:
        parts.append(f"Subtotal: {record.subtotal} {record.currency}")
    for tax in record.tax_lines:
        rate = f" at {tax.rate * 100}%" if tax.rate is not None else ""
        parts.append(f"{tax.label}{rate}: {tax.amount} {record.currency}")
    if record.total_amount is not None:
        parts.append(f"Total amount: {record.total_amount} {record.currency}")
    parts.append(f"Line item count: {len(record.line_items)}")
    return "\n".join(parts)


def build_chunks(
    parsed: ParsedDocument, record: FinancialRecord | None = None
) -> list[Chunk]:
    """Turn a parsed document into indexable chunks.

    Produces, in order: a record summary (when a record is supplied), one chunk per table
    row, one summary chunk per table, and narrative text chunks.
    """
    vendor = record.vendor_name if record else None
    billing_date = str(record.billing_date) if record and record.billing_date else None
    chunks: list[Chunk] = []

    def add(text: str, page: int, chunk_type: ChunkType, bbox: BoundingBox | None) -> None:
        if not text.strip():
            return
        chunks.append(
            Chunk(
                chunk_id=f"{parsed.document_id}:{chunk_type}:{len(chunks):04d}",
                text=text.strip(),
                document_id=parsed.document_id,
                filename=parsed.filename,
                page=page,
                chunk_type=chunk_type,
                bbox=bbox,
                vendor=vendor,
                billing_date=billing_date,
            )
        )

    if record is not None:
        add(_record_summary(record), 1, "record_summary", None)

    for table in parsed.tables:
        for row_text in table.to_serialized_rows():
            # One row, one chunk. Never split (D-7).
            add(row_text, table.page_number, "table_row", table.bbox)

        if table.headers:
            summary = (
                f"Table on page {table.page_number} with columns: "
                f"{', '.join(table.headers)}. {table.row_count} rows."
            )
            if table.caption:
                summary += f" Caption: {table.caption}"
            add(summary, table.page_number, "table_summary", table.bbox)

    for page in parsed.pages:
        for part in _split_narrative(page.narrative_markdown):
            add(part, page.page_number, "text", None)

    logger.info(
        "document_id=%s produced %d chunks (%d table rows)",
        parsed.document_id,
        len(chunks),
        sum(1 for c in chunks if c.chunk_type == "table_row"),
    )
    return chunks


# ── Ingestion ────────────────────────────────────────────────────────────────


def _embed(texts: list[str]) -> list[Sequence[float]]:
    # Widened to Sequence: Chroma's signature accepts list[Sequence[float]], and list is
    # invariant, so list[list[float]] would not satisfy it.
    return list(_get_embeddings().embed_documents(texts))


def _add_chunks(chunks: list[Chunk], collection_name: str) -> int:
    if not chunks:
        return 0

    collection = _get_collection(collection_name)
    try:
        collection.add(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            embeddings=_embed([chunk.text for chunk in chunks]),  # D-14: always explicit
            metadatas=[chunk.to_metadata() for chunk in chunks],
        )
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(f"Could not write to the vector store: {exc}") from exc
    return len(chunks)


def ingest_document(
    parsed: ParsedDocument,
    record: FinancialRecord | None = None,
    *,
    collection: str = COLLECTION_DOCUMENTS,
) -> int:
    """Index a parsed document. Re-ingesting the same ``document_id`` replaces it.

    Deletion happens first rather than relying on upsert, because a re-parse can produce
    *fewer* chunks than before and upsert would leave the surplus behind as stale results.
    """
    delete_document(parsed.document_id, collection=collection)
    count = _add_chunks(build_chunks(parsed, record), collection)
    logger.info("Ingested document_id=%s: %d chunks into %s", parsed.document_id, count, collection)
    return count


def ingest_text(
    text: str,
    *,
    document_id: str,
    filename: str,
    collection: str = COLLECTION_POLICIES,
) -> int:
    """Index a plain-text document — policies, terms, prior correspondence.

    Policies are prose, not layout, so they skip Docling entirely.
    """
    if not text.strip():
        raise RetrievalError(f"{filename} contains no text to index.")

    delete_document(document_id, collection=collection)
    chunks = [
        Chunk(
            chunk_id=f"{document_id}:text:{index:04d}",
            text=part,
            document_id=document_id,
            filename=filename,
            page=1,
            chunk_type="text",
        )
        for index, part in enumerate(_split_narrative(text))
    ]
    count = _add_chunks(chunks, collection)
    logger.info("Ingested %s: %d chunks into %s", filename, count, collection)
    return count


def delete_document(document_id: str, *, collection: str = COLLECTION_DOCUMENTS) -> int:
    """Remove every chunk belonging to a document. Safe when nothing is there."""
    handle = _get_collection(collection)
    try:
        existing = handle.get(where={"document_id": document_id}, include=[])
        ids = existing.get("ids") or []
        if ids:
            handle.delete(ids=ids)
        return len(ids)
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(f"Could not remove {document_id} from the index: {exc}") from exc


# ── Retrieval ────────────────────────────────────────────────────────────────


def _bbox_from_metadata(metadata: dict[str, Any]) -> BoundingBox | None:
    keys = ("bbox_left", "bbox_top", "bbox_right", "bbox_bottom")
    if not all(key in metadata for key in keys):
        return None
    try:
        return BoundingBox(
            left=float(metadata["bbox_left"]),
            top=float(metadata["bbox_top"]),
            right=float(metadata["bbox_right"]),
            bottom=float(metadata["bbox_bottom"]),
        )
    except (TypeError, ValueError):
        return None


def _to_citations(results: Mapping[str, Any]) -> list[Citation]:
    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    citations: list[Citation] = []
    for text, metadata, distance in zip(documents, metadatas, distances):
        metadata = metadata or {}
        citations.append(
            Citation(
                document_id=str(metadata.get("document_id", "unknown")),
                filename=str(metadata.get("filename", "unknown")),
                page=int(metadata.get("page", 1) or 1),
                snippet=text,
                # Cosine distance in [0, 2]; clamped so a score is always a sane similarity.
                score=max(0.0, min(1.0, 1.0 - float(distance))),
                chunk_type=str(metadata.get("chunk_type", "text")),  # type: ignore[arg-type]
                bbox=_bbox_from_metadata(metadata),
            )
        )
    return citations


#: Chroma's wording when the HNSW index files are gone but the metadata database still
#: lists the collection. Seen after a process was killed mid-write, and after two
#: processes held the embedded store at once — the client is single-process only.
_CORRUPT_STORE_MARKERS = ("hnsw segment reader", "nothing found on disk", "no such file")


def _explain_search_failure(exc: Exception) -> str:
    """Turn a Chroma internal error into something a user can act on (Rule 2.3)."""
    detail = str(exc).lower()
    if any(marker in detail for marker in _CORRUPT_STORE_MARKERS):
        return (
            f"The local search index is corrupt and cannot be read. Delete the "
            f"'{CHROMA_DIR.name}' folder inside data/ and re-upload your documents to "
            f"rebuild it. This usually happens when the app is closed mid-indexing, or "
            f"when two processes use the index at the same time — the embedded store "
            f"supports only one."
        )
    return f"Search failed: {exc}"


def retrieve(
    query: str,
    *,
    collection: str = COLLECTION_DOCUMENTS,
    document_id: str | None = None,
    k: int | None = None,
) -> list[Citation]:
    """Find the chunks most relevant to a query.

    Args:
        query: Natural-language question or search phrase.
        collection: Which collection to search.
        document_id: Scope results to one document (FR-3.4). This is what makes "explain
            *this* charge" mean the open document rather than anything ever uploaded.
        k: Number of results. Defaults to the configured per-collection depth.

    Returns:
        Citations ordered best-first, each carrying page and (where available) a bbox.
    """
    if not query.strip():
        return []

    settings = get_settings()
    if k is None:
        k = (
            settings.retrieval_k_policy
            if collection == COLLECTION_POLICIES
            else settings.retrieval_k_document
        )
    if k <= 0:
        return []

    handle = _get_collection(collection)
    where = cast("Where", {"document_id": document_id}) if document_id else None
    query_vector: list[Sequence[float]] = [_get_embeddings().embed_query(query)]  # D-14

    try:
        results = handle.query(
            query_embeddings=query_vector,
            n_results=k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(_explain_search_failure(exc)) from exc

    citations = _to_citations(cast(Mapping[str, Any], results))
    logger.info(
        "retrieved %d chunks from %s (scoped=%s)", len(citations), collection, bool(document_id)
    )
    return citations


def retrieve_with_policies(
    query: str, *, document_id: str | None = None
) -> tuple[list[Citation], list[Citation]]:
    """Dual retrieval: the active document plus the policy corpus (architecture.md §6).

    Returned separately so the prompt — and the answer — can keep "what the document says"
    distinct from "what the policy says" (FR-3.3).
    """
    document_hits = retrieve(query, collection=COLLECTION_DOCUMENTS, document_id=document_id)
    policy_hits = retrieve(query, collection=COLLECTION_POLICIES)
    return document_hits, policy_hits


# ── Introspection ────────────────────────────────────────────────────────────


def collection_stats() -> dict[str, int]:
    """Chunk counts per collection, for the UI and for debugging retrieval quality."""
    return {
        name: _get_collection(name).count()
        for name in (COLLECTION_DOCUMENTS, COLLECTION_POLICIES)
    }


def reset_collection(name: str) -> None:
    """Drop a collection entirely. Used by tests and by an explicit user 'clear index'."""
    try:
        _get_client().delete_collection(name)
    except Exception:  # noqa: BLE001 - deleting something absent is not an error
        logger.debug("Collection %s did not exist at reset", name)


def ingest_policy_files(paths: Iterable[Any]) -> int:
    """Convenience loader for the policy corpus. Returns total chunks indexed."""
    from pathlib import Path

    total = 0
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            logger.warning("Skipping missing policy file %s", path.name)
            continue
        total += ingest_text(
            path.read_text(encoding="utf-8"),
            document_id=f"policy:{path.stem}",
            filename=path.name,
        )
    return total
