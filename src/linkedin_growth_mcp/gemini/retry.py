"""Retry transient Gemini failures (overload, server error, network timeout) a couple of times, gently.

A 429 means the key's quota is spent: retrying it would only burn more of that key, so it is raised at once."""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

import httpx
from google.genai import errors

T = TypeVar("T")
TRANSIENT = {500, 503, 504}


def with_retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 3.0) -> T:
    for i in range(attempts):
        try:
            return fn()
        except errors.APIError as e:
            if e.code not in TRANSIENT or i == attempts - 1:
                raise
        except (httpx.TimeoutException, httpx.TransportError):
            if i == attempts - 1:
                raise
        time.sleep(base_delay * 2**i + random.uniform(0, 1))
    raise AssertionError("unreachable")
