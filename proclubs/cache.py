"""Stale-while-revalidate caching for third-party API reads.

The home page reads the club's standing from EA and the roster's live status
from Twitch. Both used to be plain 30-second caches, which sounds harmless
but means that every 30 seconds one unlucky visitor pays the full upstream
round trip in their page load -- and EA's API is the slow, flaky,
undocumented kind, with a 10-second timeout and two calls behind a single
"what division are we in" question.

So: a cached value is served immediately even once it's gone stale, and the
refresh happens on a background thread. Upstream latency stops being page
latency for everyone except whoever arrives before the first successful
fetch -- and startup warms the cache so that's usually nobody.

A refresh that fails changes nothing: the last good value stays, and the
next request tries again. That's the point -- EA being down should mean the
standing band is a few minutes out of date, not that it disappears or that
the page hangs for ten seconds waiting to find out.
"""
from __future__ import annotations

import threading
import time


class SwrCache:
    """Keyed cache with a fresh window, a background refresh, and a ceiling
    on how stale a value may get before a caller waits for a real one."""

    def __init__(self, *, fresh_for: float, max_stale: float):
        self._fresh_for = fresh_for
        self._max_stale = max_stale
        self._entries: dict = {}          # key -> (stored_at, value)
        self._refreshing: set = set()     # keys with a refresh already in flight
        self._lock = threading.Lock()

    def get(self, key, loader):
        """Cached value for ``key``, calling ``loader()`` as needed.

        Fresh hit -> the value. Stale hit -> the stale value now, with a
        refresh started behind it. Miss (or staler than max_stale) -> the
        caller waits for ``loader()``, and any exception it raises is the
        caller's to handle, exactly as if the cache weren't here."""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                age = now - entry[0]
                if age < self._fresh_for:
                    return entry[1]
                if age < self._max_stale:
                    self._start_refresh(key, loader)
                    return entry[1]

        value = loader()
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
        return value

    def warm(self, key, loader) -> None:
        """Populate ``key`` off the request path (see the startup hook in
        app.py), so the first visitor after a restart doesn't become the one
        who waits for EA."""
        with self._lock:
            self._start_refresh(key, loader)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _start_refresh(self, key, loader) -> None:
        """Caller must hold the lock. At most one refresh per key is in
        flight -- without that, a burst of traffic on a stale key would
        start a thread per request against an API that's already slow."""
        if key in self._refreshing:
            return
        self._refreshing.add(key)
        threading.Thread(target=self._refresh, args=(key, loader), daemon=True).start()

    def _refresh(self, key, loader) -> None:
        try:
            value = loader()
        except Exception:
            # Keep serving the last good value; the next request retries.
            pass
        else:
            with self._lock:
                self._entries[key] = (time.monotonic(), value)
        finally:
            with self._lock:
                self._refreshing.discard(key)
