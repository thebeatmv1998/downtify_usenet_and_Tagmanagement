"""A cooperative stop switch for every download in progress.

Downloads run in ``DOWNLOAD_EXECUTOR`` threads, so cancelling the asyncio
task that waits for one does not stop the worker. A monotonically
increasing generation is shared instead: every download captures the
generation it started in, and :func:`stop_all` bumps it. Workers compare
their captured value at safe points and abort. Downloads started *after*
the stop capture the new generation and run normally, so stopping does not
leave a permanent global brake behind.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

_LOCK = threading.Lock()
_GENERATION = 0
_SLEEP_SLICE_SECONDS = 0.25


class DownloadCancelled(Exception):
    """Raised when Stop all downloads was pressed during a download."""


class SourceSkipRequested(Exception):
    """Raised when the user asks a running song to skip to the next source."""


_SKIP_REQUESTS: dict[str, int] = {}


def request_source_skip(key: str) -> bool:
    """Ask the worker for *key* to abandon its current audio source."""

    key = str(key or '').strip()
    if not key:
        return False
    with _LOCK:
        _SKIP_REQUESTS[key] = _SKIP_REQUESTS.get(key, 0) + 1
    return True


def source_skip_requested(key: str) -> bool:
    key = str(key or '').strip()
    if not key:
        return False
    with _LOCK:
        return _SKIP_REQUESTS.get(key, 0) > 0


def take_source_skip(key: str) -> bool:
    """Consume a pending skip request for *key*, if any."""

    key = str(key or '').strip()
    if not key:
        return False
    with _LOCK:
        return _SKIP_REQUESTS.pop(key, 0) > 0


def clear_source_skip(key: str) -> None:
    key = str(key or '').strip()
    if not key:
        return
    with _LOCK:
        _SKIP_REQUESTS.pop(key, None)


def current_generation() -> int:
    with _LOCK:
        return _GENERATION


def stop_all() -> int:
    """Cancel every download that started before this call.

    Returns the new generation, which new downloads will capture.
    """

    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        return _GENERATION


def is_cancelled(generation: int) -> bool:
    with _LOCK:
        return generation != _GENERATION


def sleep_interruptible(
    seconds: float, cancelled: Optional[Callable[[], bool]]
) -> bool:
    """Sleep, checking *cancelled* every 250 ms; True when stopped.

    Provider poll loops use this instead of a full-interval sleep, so a
    stop is noticed within a quarter second rather than after a poll
    interval of up to 30 seconds.
    """

    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if cancelled is not None and cancelled():
            return True
        step = min(_SLEEP_SLICE_SECONDS, remaining)
        time.sleep(step)
        remaining -= step
    return cancelled is not None and cancelled()
