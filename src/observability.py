"""Per-request telemetry for the Observability Bar (design.md §5.4, phases.md Phase 4).

Populated **locally**, so latency and token counts are visible in the UI without a
LangSmith account and without any cloud dependency in the request path (decision D-11).
LangSmith, when configured, is a second and entirely optional destination.

Token counts are read from the provider's own usage metadata where it is available, and
estimated only when it is not — with :attr:`RunStats.tokens_estimated` set so the UI can
say so rather than presenting a guess as a measurement.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Literal

from langchain_core.callbacks import BaseCallbackHandler

from src.config import get_settings
from src.schemas import RunStats

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

__all__ = ["TokenCollector", "stage", "estimate_tokens", "tracing_config"]

Stage = Literal["parse", "retrieve", "generate"]

#: Rough characters-per-token for English prose. Used only as a fallback when the
#: provider returns no usage metadata — never to override a real count.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN) if text else 0


class TokenCollector(BaseCallbackHandler):
    """Accumulates token usage across every model call in one request.

    ``langchain-groq`` surfaces usage in two different places depending on whether the
    call streamed, so both are checked. Nothing here raises: a telemetry failure must
    never break an answer.
    """

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.measured = False

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:  # noqa: D102
        self.calls += 1
        try:
            self._read_llm_output(response)
            self._read_generation_metadata(response)
        except Exception:  # noqa: BLE001 - telemetry must never break the request path
            logger.debug("Could not read token usage from response", exc_info=True)

    def _read_llm_output(self, response: LLMResult) -> None:
        output = getattr(response, "llm_output", None) or {}
        usage = output.get("token_usage") or output.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt or completion:
            self.prompt_tokens += int(prompt or 0)
            self.completion_tokens += int(completion or 0)
            self.measured = True

    def _read_generation_metadata(self, response: LLMResult) -> None:
        if self.measured:
            return
        for batch in getattr(response, "generations", []) or []:
            for generation in batch:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) if message else None
                if not usage:
                    continue
                self.prompt_tokens += int(usage.get("input_tokens", 0) or 0)
                self.completion_tokens += int(usage.get("output_tokens", 0) or 0)
                self.measured = True

    def apply_to(self, stats: RunStats, *, fallback_text: str = "") -> None:
        """Write collected counts onto ``stats``, estimating only if nothing was measured."""
        if self.measured:
            stats.prompt_tokens = self.prompt_tokens
            stats.completion_tokens = self.completion_tokens
            stats.tokens_estimated = False
        elif fallback_text:
            stats.completion_tokens = estimate_tokens(fallback_text)
            stats.tokens_estimated = True


@contextmanager
def stage(stats: RunStats, name: Stage) -> Iterator[None]:
    """Time a pipeline stage and record it on ``stats``.

    Timings accumulate, so a stage entered more than once in a request (a retry, or the
    second retrieval pass) reports total time rather than only the last attempt.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        field = f"{name}_seconds"
        setattr(stats, field, getattr(stats, field) + elapsed)
        logger.debug("stage=%s elapsed=%.3fs", name, elapsed)


def tracing_config() -> dict[str, Any]:
    """LangSmith run config, or an empty dict when tracing is not configured.

    Returning ``{}`` rather than raising is the whole point of decision D-11: the app runs
    normally with no LangSmith account, and no code path needs to check first.
    """
    settings = get_settings()
    if not settings.tracing_enabled:
        return {}
    return {"run_name": "financial-assistant", "tags": ["rag", settings.langchain_project]}
