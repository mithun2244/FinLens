"""Shared test fixtures.

Exists because of the 512 MB refactor. Embeddings used to be computed locally by
sentence-transformers, so the whole suite ran offline and free by construction. They are
now an HTTP call, which would make every retrieval test require a network round trip and
an HF_TOKEN — turning the "Offline test suite" CI job into something that fails on a
missing secret or a rate limit, and testing HuggingFace's availability rather than our
retrieval.

So embeddings are stubbed for the entire suite. What is being tested here is chunking,
metadata round-tripping, scoping and ranking mechanics, none of which depend on the
vectors being semantically meaningful — only on their being deterministic, correctly
shaped, and different for different text.

The one thing this cannot cover is whether the real endpoint returns 384-dimensional
vectors. That is what ``_check_dimensions`` is for, and it is exercised directly in
tests/test_vectorstore.py rather than implied by these stubs.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator

import pytest

from src.config import EMBEDDING_DIMENSIONS


def _deterministic_vector(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """A stable unit vector derived from the text itself.

    Hash-derived rather than random so the same text always embeds identically — a test
    that ingests a document and then queries it needs the query vector to be reproducible.
    Normalized because the collections use cosine distance, and Chroma reports scores as
    ``1 - distance``; unnormalized vectors would produce similarities outside [0, 1] and
    make ``test_scores_are_similarities_not_distances`` meaningless.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Repeat the digest to fill the dimension count, then centre it on zero so vectors are
    # spread across the space rather than crowded into one orthant.
    raw = [(digest[index % len(digest)] - 127.5) / 127.5 for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


#: The genuine ``_get_embeddings``, stashed when the stub is installed so a test that
#: needs the real endpoint can borrow it back. See ``live_policies``.
_REAL_EMBEDDINGS = None


class _StubEmbeddings:
    """Stands in for HuggingFaceEndpointEmbeddings. No network, no token, no model."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_deterministic_vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return _deterministic_vector(text)


@pytest.fixture(scope="session", autouse=True)
def _offline_embeddings() -> Iterator[None]:
    """Point the vector store at the stub for the whole session.

    Session-scoped and patched by hand rather than with ``monkeypatch``, which is
    function-scoped. pytest instantiates higher-scoped fixtures first, so a function-scoped
    patch installs *after* module-scoped fixtures like ``populated`` have already ingested
    documents — and those ingests would go to the real endpoint, or fail without a token.

    Autouse and unconditional: a test that quietly reached the real endpoint would pass
    for whoever had a token exported and fail in CI, which is the failure mode this exists
    to remove.
    """
    from src import vectorstore

    global _REAL_EMBEDDINGS
    _REAL_EMBEDDINGS = original = vectorstore._get_embeddings
    vectorstore._get_embeddings = lambda: _StubEmbeddings()  # type: ignore[assignment]
    try:
        yield
    finally:
        vectorstore._get_embeddings = original  # type: ignore[assignment]
        original.cache_clear()


@pytest.fixture
def live_policies() -> Iterator[str]:
    """A policy collection embedded by the real endpoint, for semantic retrieval tests.

    Undoes the session-wide stub for the duration of the test and ingests into a
    collection of its own. Both are necessary: real query vectors against a
    stub-embedded collection would be comparing noise to noise, and would pass or fail
    for reasons unrelated to retrieval.

    Skips rather than fails when no endpoint is configured — an offline machine should
    not report a red suite for a test it was never able to run.
    """
    from src import vectorstore
    from src.config import FIXTURES_DIR, get_settings

    if not get_settings().embeddings_configured:
        pytest.skip("no embedding endpoint configured (set HF_TOKEN)")

    stub = vectorstore._get_embeddings
    vectorstore._get_embeddings = _REAL_EMBEDDINGS  # type: ignore[assignment]
    collection = "test-live-policy-corpus"
    try:
        vectorstore.reset_collection(collection)
        for path in sorted((FIXTURES_DIR / "policies").glob("*.md")):
            vectorstore.ingest_text(
                path.read_text(encoding="utf-8"),
                document_id=f"policy:{path.stem}",
                filename=path.name,
                collection=collection,
            )
        yield collection
    finally:
        try:
            vectorstore.reset_collection(collection)
        finally:
            vectorstore._get_embeddings = stub  # type: ignore[assignment]
