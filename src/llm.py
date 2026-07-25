"""Groq chat-model factory with uniform error translation.

Sits between ``config`` and ``schemas`` in the dependency chain (architecture.md §4):
it depends only on configuration, and ``extractor``, ``chain``, and ``evals`` all consume
it. Centralizing it here means retry policy, timeouts, and rate-limit handling are
defined once rather than re-implemented per caller.

**Rule 1 note:** this module constructs ``ChatGroq`` and nothing else. If a paid provider
client is ever constructed anywhere in ``src/``, it will be here, and it will be obvious.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeVar

from src import ConfigurationError, LLMError, RateLimitError
from src.config import MODELS_BY_ROLE, ModelRole, get_settings

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "get_chat_model",
    "invoke_with_translation",
    "invoke_with_retry",
    "translate_provider_error",
    "llm_available",
]

_CACHE: dict[tuple[ModelRole, float], BaseChatModel] = {}
_cache_configured = False


def _enable_response_cache() -> None:
    """Cache identical prompts to a local SQLite file.

    The free tier is rate-limited rather than metered, so the thing worth conserving is
    *requests*. Repeated eval runs and repeated demo questions should cost none.

    Applies to ``invoke``; streamed calls bypass it, which is correct — a cached stream
    would replay instantly and misrepresent latency in the Observability Bar.
    """
    global _cache_configured
    if _cache_configured or not get_settings().llm_cache_enabled:
        return

    try:
        from langchain_community.cache import SQLiteCache
        from langchain_core.globals import set_llm_cache

        from src.config import LLM_CACHE_PATH

        LLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        set_llm_cache(SQLiteCache(database_path=str(LLM_CACHE_PATH)))
        logger.info("LLM response cache enabled at %s", LLM_CACHE_PATH)
    except Exception as exc:  # noqa: BLE001 - caching is an optimization, never required
        logger.warning("Could not enable the LLM response cache: %s", type(exc).__name__)
    finally:
        _cache_configured = True


def llm_available() -> bool:
    """True when a Groq key is configured.

    Callers use this to degrade gracefully rather than crash: extraction still returns a
    record from deterministic parsing alone when no key is present.
    """
    return get_settings().groq_configured


def get_chat_model(role: ModelRole = "reasoning", *, temperature: float = 0.0) -> BaseChatModel:
    """Return a cached ``ChatGroq`` for the given role.

    Args:
        role: ``"reasoning"`` or ``"utility"``. The model ID comes from
            ``config.MODELS_BY_ROLE`` — never inlined at a call site (decision D-4).
        temperature: Defaults to 0. Financial extraction and grounded answering both
            want determinism; nothing in this product benefits from sampling variety.

    Raises:
        ConfigurationError: No Groq API key is configured.
    """
    settings = get_settings()
    if not settings.groq_configured:
        raise ConfigurationError(
            "No Groq API key configured. Add GROQ_API_KEY to .env — a free key is "
            "available at https://console.groq.com/keys (no credit card required)."
        )

    key = (role, temperature)
    if key in _CACHE:
        return _CACHE[key]

    _enable_response_cache()

    from langchain_groq import ChatGroq

    model_id = MODELS_BY_ROLE[role]
    logger.info("Creating ChatGroq role=%s model=%s", role, model_id)
    model = ChatGroq(
        model=model_id,
        api_key=settings.groq_api_key,  # type: ignore[arg-type]
        temperature=temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    _CACHE[key] = model
    return model


def invoke_with_translation(callable_: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a model call, converting provider exceptions into our own hierarchy.

    A 429 becomes :class:`RateLimitError` carrying ``retry_after_seconds``, so the
    Observability Bar can render a live countdown (design.md §5.4) instead of the UI
    showing a stack trace on the single most likely runtime failure.
    """
    try:
        return callable_(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed error below
        raise _translate(exc) from exc


#: Failures that are worth retrying rather than surfacing.
#:
#: ``tool_use_failed`` is the important one: Groq returns HTTP 400 when the model emits
#: malformed tool-call arguments. The SDK does not retry 400s — they are normally
#: permanent — but this particular one is a sampling artefact and succeeds on a retry.
#: Observed intermittently during Phase 2 structured extraction.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "tool_use_failed",
    "failed to call a function",
    "rate limit",
    "429",
    "timeout",
    "timed out",
    "temporarily",
    "503",
    "502",
    "overloaded",
)


def _is_transient(exc: Exception) -> bool:
    detail = str(exc).lower()
    return any(marker in detail for marker in _TRANSIENT_MARKERS)


def invoke_with_retry(
    callable_: Any, *args: Any, attempts: int = 3, base_delay: float = 0.6, **kwargs: Any
) -> Any:
    """Run a model call, retrying transient provider failures with backoff.

    Use this for structured-output calls in particular: a ``tool_use_failed`` 400 is a
    coin-flip, not a bug in the request, and failing the whole extraction over one is
    needlessly brittle.

    Raises:
        LLMError: The call failed for a non-transient reason, or every attempt failed.
    """
    import random
    import time

    error: LLMError = LLMError("The model call failed.")
    for attempt in range(1, attempts + 1):
        try:
            return callable_(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            error = _translate(exc)
            if not _is_transient(exc) or attempt == attempts:
                raise error from exc
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            logger.info(
                "Transient model failure (%s); retry %d/%d in %.1fs",
                type(exc).__name__,
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
    raise error


def translate_provider_error(exc: Exception) -> LLMError:
    """Convert a provider exception into our hierarchy.

    Public because streaming cannot use :func:`invoke_with_translation`: the exception
    surfaces while iterating the generator, not when the call is made. Without this, a
    ``groq.RateLimitError`` escapes untranslated and reaches the UI as a stack trace —
    which is exactly what the rate-limit handling is supposed to prevent.
    """
    return _translate(exc)


def _translate(exc: Exception) -> LLMError:
    name = type(exc).__name__
    detail = str(exc)
    lowered = detail.lower()

    if "rate" in lowered and "limit" in lowered or name == "RateLimitError" or "429" in detail:
        return RateLimitError(
            "Groq's free-tier rate limit was reached. The request will be retried shortly.",
            retry_after_seconds=_retry_after(detail),
        )
    if "decommission" in lowered or "does not exist" in lowered or "model_not_found" in lowered:
        return LLMError(
            "The configured Groq model is no longer served. Run "
            "`python scripts/check_models.py` to see what is available, then update "
            "src/config.py."
        )
    if "tool_use_failed" in lowered or "failed to call a function" in lowered:
        return LLMError(
            "The model did not return a valid structured response. This is usually "
            "transient — retrying normally succeeds."
        )
    if "auth" in lowered or "api key" in lowered or "401" in detail:
        return LLMError("Groq rejected the API key. Check GROQ_API_KEY in .env.")
    if "timeout" in lowered or "timed out" in lowered:
        return LLMError("The model call timed out. Try again, or reduce the document size.")

    logger.warning("Untranslated LLM failure: %s", name)
    return LLMError(f"The model call failed ({name}). Please try again.")


def _retry_after(detail: str) -> float | None:
    """Pull a retry hint out of a Groq rate-limit message, when it offers one."""
    import re

    match = re.search(r"(?:try again in|retry after)\s*([\d.]+)\s*(m|s|seconds|minutes)?", detail, re.I)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = (match.group(2) or "s").lower()
    return value * 60 if unit.startswith("m") else value
