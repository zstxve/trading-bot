"""Retry helper for flaky network/API calls: exponential backoff with jitter."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional, Tuple, Type, TypeVar

T = TypeVar("T")


def with_retries(
    fn: Callable[[], T],
    retriable: Tuple[Type[BaseException], ...],
    retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    logger: Optional[logging.Logger] = None,
) -> T:
    """Call `fn`; on a retriable exception back off and retry.

    Raises the last exception after `retries` failed attempts. Anything not
    in `retriable` (e.g. auth errors, insufficient funds) propagates
    immediately — retrying those would only mask real problems.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except retriable as exc:
            attempt += 1
            if attempt > retries:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= 0.5 + random.random()  # jitter, avoid thundering herd
            if logger:
                logger.warning("retriable error (attempt %d/%d): %s — sleeping %.1fs",
                               attempt, retries, exc, delay)
            time.sleep(delay)
