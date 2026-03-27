"""
Lightweight in-process TTL cache.

No external dependencies — plain dict with monotonic timestamps.
Module-level store is shared across all requests in the same process.

Usage:
    from services.cache import cache_get, cache_set, make_key

    key = make_key("github_profile", username)
    value = cache_get(key)
    if value is None:
        value = await expensive_call()
        cache_set(key, value, ttl_seconds=3600)
"""

import time
from typing import Any

# TTL constants (seconds)
GITHUB_PROFILE_TTL = 3600       # 1 hour  — GitHub profile data
HUNTER_DOMAIN_TTL  = 3600       # 1 hour  — Hunter domain search pattern
AGENT_VERIFY_TTL   = 86400      # 24 hours — on-chain agent verification

_store: dict[str, tuple[float, Any]] = {}  # key → (expires_at, value)


def cache_get(key: str) -> Any | None:
    """Return cached value if not expired, else None."""
    entry = _store.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        del _store[key]
        return None
    return value


def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    """Store value with a TTL."""
    _store[key] = (time.monotonic() + ttl_seconds, value)


def make_key(*parts: str) -> str:
    """Build a namespaced cache key from parts."""
    return ":".join(parts)
