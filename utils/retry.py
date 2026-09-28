"""
GhostDirector — Retry Utility

Exponential backoff retry decorator for API calls, with first-class handling
of provider quota windows (HTTP 429 + Google's `retryDelay` hint) and a
cross-module min-interval pacer for LLM calls on constrained API keys.

Found by the first real production run (2026-09-18): the free tier of
gemini-3.6-flash allows 5 requests/minute; the old blind backoff gave up
after ~45s while Google was saying "retry in 58s", killing mid-run pipelines
at the script stage.
"""

import asyncio
import functools
import re
import time
from typing import Type

from utils.logger import get_logger

log = get_logger("retry")


# ──────────────────────────────────────────────
# Provider quota windows + call pacing
# ──────────────────────────────────────────────
def _quota_retry_delay(err: Exception) -> float | None:
    """Extract Google's retryDelay (seconds) from a 429/503 error, if any."""
    text = str(err)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text.upper() \
            and "503" not in text and "UNAVAILABLE" not in text.upper():
        return None
    m = re.search(r"retry in ([\d.]+)s", text, flags=re.IGNORECASE)
    if m:
        return float(m.group(1)) + 1.0  # small safety margin
    return None


_LLM_MIN_INTERVAL_S = 13.0     # >5 req/min → stay under free-tier RPM
_last_llm_call = [0.0]


def pace_llm_call(min_interval_s: float | None = None) -> None:
    """Block until at least `min_interval_s` has passed since the last LLM call.

    A module-level rate limiter: research → script → humanize each make one
    call, but back-to-back runs (batch mode, retries) would otherwise exceed
    the per-minute quota immediately.
    """
    interval = _LLM_MIN_INTERVAL_S if min_interval_s is None else min_interval_s
    wait = interval - (time.monotonic() - _last_llm_call[0])
    if wait > 0.05:
        log.info(f"LLM pacing: waiting {wait:.0f}s to respect provider rate limit")
        time.sleep(max(wait, 0.0))
    _last_llm_call[0] = time.monotonic()


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
):
    """
    Decorator for retrying a function with exponential backoff.

    Works with both sync and async functions.
    """
    def decorator(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts:
                        log.error(f"[{func.__name__}] Failed after {max_attempts} attempts: {e}")
                        raise
                    # Respect the provider's own retry window when it gives one
                    # (Google 429s carry "Please retry in Ns" hints).
                    quota_delay = _quota_retry_delay(e)
                    delay = max(
                        quota_delay if quota_delay else 0.0,
                        min(base_delay * (2 ** (attempt - 1)), max_delay),
                    )
                    log.warning(
                        f"[{func.__name__}] Attempt {attempt}/{max_attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts:
                        log.error(f"[{func.__name__}] Failed after {max_attempts} attempts: {e}")
                        raise
                    quota_delay = _quota_retry_delay(e)
                    delay = max(
                        quota_delay if quota_delay else 0.0,
                        min(base_delay * (2 ** (attempt - 1)), max_delay),
                    )
                    log.warning(
                        f"[{func.__name__}] Attempt {attempt}/{max_attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
