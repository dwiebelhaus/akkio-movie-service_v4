"""Shared response cache in Redis (DESIGN.md §13).

Fail-open: any Redis error is logged and treated as a miss, and Redis is then skipped for
`cache_retry_seconds` so a dead Redis costs requests nothing. Keys include the dataset
version, so they never need explicit invalidation; the TTL reclaims memory.
"""

import logging
import time
from typing import Literal

import redis.asyncio as redis

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 0.25


class ResponseCache:
    def __init__(self, url: str, ttl_seconds: int, retry_seconds: float):
        self._redis = (
            redis.from_url(url, socket_timeout=_TIMEOUT_SECONDS, socket_connect_timeout=_TIMEOUT_SECONDS)
            if url
            else None
        )
        self._ttl = ttl_seconds
        self._retry = retry_seconds
        self._skip_until = 0.0

    def _available(self) -> bool:
        return self._redis is not None and time.monotonic() >= self._skip_until

    def _failed(self, op: str, exc: Exception) -> None:
        logger.warning("cache %s failed (%s); serving without cache for %.0fs", op, exc, self._retry)
        self._skip_until = time.monotonic() + self._retry

    async def get(self, key: str) -> bytes | None:
        if not self._available():
            return None
        try:
            return await self._redis.get(key)
        except Exception as exc:
            self._failed("get", exc)
            return None

    async def set(self, key: str, value: bytes) -> None:
        if not self._available():
            return
        try:
            await self._redis.set(key, value, ex=self._ttl)
        except Exception as exc:
            self._failed("set", exc)

    async def status(self) -> Literal["ok", "unavailable", "disabled"]:
        if self._redis is None:
            return "disabled"
        try:
            await self._redis.ping()
            return "ok"
        except Exception:
            return "unavailable"

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
