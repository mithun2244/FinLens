"""Tests for src.config.Settings.

Only the CORS origin handling so far. It earns tests where the other settings do not
because getting it wrong is a vulnerability rather than a misconfiguration: the API is
mounted with ``allow_credentials=True``, and under that setting a wildcard origin lets
any site a user has open make credentialed calls to this one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings


def _settings(**overrides: object) -> Settings:
    """Settings built from explicit values, ignoring any .env on the machine."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_default_origins_cover_the_local_dev_server() -> None:
    """Local development must need no configuration at all."""
    assert _settings().allowed_origin_list == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_origins_are_split_on_commas() -> None:
    settings = _settings(allowed_origins="https://finlens.vercel.app,https://finlens.app")
    assert settings.allowed_origin_list == [
        "https://finlens.vercel.app",
        "https://finlens.app",
    ]


def test_surrounding_whitespace_is_stripped() -> None:
    """A dashboard field entered as ``a, b`` must not yield an origin with a space."""
    settings = _settings(allowed_origins=" https://a.example , https://b.example ")
    assert settings.allowed_origin_list == ["https://a.example", "https://b.example"]


def test_a_trailing_comma_does_not_produce_an_empty_origin() -> None:
    """An empty string matches nothing while looking set, so it is dropped."""
    assert _settings(allowed_origins="https://a.example,").allowed_origin_list == [
        "https://a.example"
    ]


def test_a_wildcard_origin_is_rejected_at_startup() -> None:
    """The vulnerability this guards: wildcard plus credentials.

    Starlette answers ``allow_origins=["*"]`` under ``allow_credentials=True`` by echoing
    the caller's origin back, so any site could call this API on a signed-in user's
    behalf. Failing at startup is the point -- it cannot be deployed and discovered later.
    """
    with pytest.raises(ValidationError, match="cannot contain"):
        _settings(allowed_origins="*")


def test_a_wildcard_is_rejected_even_alongside_real_origins() -> None:
    """The dangerous entry does not become safe by having company."""
    with pytest.raises(ValidationError, match="cannot contain"):
        _settings(allowed_origins="https://finlens.app,*")
