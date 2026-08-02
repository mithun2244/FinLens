"""Tests for the FastAPI routing surface.

Deliberately narrow. The business logic these endpoints call is covered in test_chain,
test_extractor, test_parser and test_vectorstore; duplicating it through HTTP would be
slower and would test FastAPI rather than the product. What is worth checking here is the
routing itself — the part that has no other test and fails in production rather than in a
unit.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import api


def test_root_redirects_to_the_docs() -> None:
    """The bare domain used to 404, which reads as a broken deployment.

    Not followed, deliberately: following the redirect would render the docs page and
    assert on Swagger's HTML, which is FastAPI's business. The contract this route owns is
    the status and the target.
    """
    with TestClient(api.app) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/docs"


def test_the_redirect_stays_out_of_the_schema() -> None:
    """A redirect listed among the endpoints it redirects to is noise, not documentation."""
    assert "/" not in api.app.openapi()["paths"]


def test_the_documented_endpoints_are_all_present() -> None:
    """Pins the set the README's endpoint table describes.

    That table is what a reviewer reads before touching the API, and it was written from
    the deployed openapi.json. A route renamed without the docs following turns it into a
    confident lie.
    """
    paths = set(api.app.openapi()["paths"])
    assert {
        "/api/health",
        "/api/upload",
        "/api/chat",
        "/api/samples",
        "/api/samples/{filename}",
        "/api/policies",
        "/api/documents/{document_id}",
        "/api/documents/{document_id}/pages/{page_number}",
    } <= paths
