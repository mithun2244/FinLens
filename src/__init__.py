"""Multimodal AI Financial Assistant — core library.

Module dependency direction is strictly one-way (architecture.md §4, rules.md Rule 2.1):

    config -> schemas -> parser -> extractor -> vectorstore -> chain -> app
                                                                  \\-> evals

A module never imports from a module to its right, and **nothing in this package ever
imports a UI framework** (decision D-8) — that is what keeps the Streamlit/React choice
reversible.
"""

from __future__ import annotations

__version__ = "0.1.0"


class AssistantError(Exception):
    """Base class for every error raised by this package (rules.md Rule 2.3).

    Messages must be actionable and safe to show a user. They must never contain
    financial data (amounts, vendor names, line-item text).
    """


class ConfigurationError(AssistantError):
    """Missing or invalid configuration — absent API key, unwritable data directory."""


class ParsingError(AssistantError):
    """A document could not be parsed: unsupported, corrupt, or password-protected."""


class ExtractionError(AssistantError):
    """A parsed document could not be turned into a valid ``FinancialRecord``."""


class RetrievalError(AssistantError):
    """The vector store could not be read, written, or queried."""


class LLMError(AssistantError):
    """A model call failed."""


class RateLimitError(LLMError):
    """Provider rate limit hit. Carries the retry hint so the UI can count down."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


__all__ = [
    "__version__",
    "AssistantError",
    "ConfigurationError",
    "ParsingError",
    "ExtractionError",
    "RetrievalError",
    "LLMError",
    "RateLimitError",
]
