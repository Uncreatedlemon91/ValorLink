"""SwrCache, and the rule that the home page never waits on EA.

The site-is-slow complaint this cache exists for has two shapes: an upstream
that is slow, and an upstream that is slow *and* was down when the app
started, so the startup warm never filled anything. The second is the one
that hurts -- without a non-blocking read every visitor pays the full
timeout, twice, for as long as EA stays down.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402


def _counting_loader(value="v", delay=0.0):
    calls = []

    def loader():
        calls.append(1)
        if delay:
            time.sleep(delay)
        return value

    return loader, calls


def _settle(calls, expected, timeout=2.0):
    """Wait for background refreshes to land rather than sleeping blind."""
    deadline = time.time() + timeout
    while len(calls) < expected and time.time() < deadline:
        time.sleep(0.01)


# --- get(): the blocking read ----------------------------------------------- #
def test_a_fresh_value_is_served_without_calling_upstream_again():
    c = cache.SwrCache(fresh_for=60, max_stale=600)
    loader, calls = _counting_loader()
    assert c.get("k", loader) == "v"
    assert c.get("k", loader) == "v"
    assert len(calls) == 1


def test_a_stale_value_is_served_immediately_and_refreshed_behind_it():
    c = cache.SwrCache(fresh_for=0, max_stale=600)
    loader, calls = _counting_loader(delay=0.2)
    c.get("k", loader)               # cold: pays for it
    started = time.time()
    assert c.get("k", loader) == "v"  # stale: must not pay for it
    assert time.time() - started < 0.1
    _settle(calls, 2)
    assert len(calls) == 2


def test_a_failed_refresh_keeps_the_last_good_value():
    """EA being down should mean a slightly old standing band, not a
    missing one."""
    c = cache.SwrCache(fresh_for=0, max_stale=600)
    c.get("k", lambda: "good")

    def broken():
        raise RuntimeError("EA is down")

    assert c.get("k", broken) == "good"
    time.sleep(0.1)
    assert c.get("k", broken) == "good"


# --- get_if_cached(): the read that never waits ----------------------------- #
def test_a_cold_miss_returns_none_instead_of_waiting():
    c = cache.SwrCache(fresh_for=60, max_stale=600)
    loader, calls = _counting_loader(delay=1.0)
    started = time.time()
    assert c.get_if_cached("k", loader) is None
    assert time.time() - started < 0.1, "a cold miss must not block the caller"
    _settle(calls, 1)
    assert len(calls) == 1, "it should still fetch, just not in front of the request"


def test_the_background_fetch_fills_the_cache_for_the_next_caller():
    c = cache.SwrCache(fresh_for=60, max_stale=600)
    loader, calls = _counting_loader(value="filled")
    assert c.get_if_cached("k", loader) is None
    # Poll for the value, not for the call: the loader records the call
    # before it returns, so waiting on the counter races the store.
    deadline = time.time() + 2.0
    while c.get_if_cached("k", loader) is None and time.time() < deadline:
        time.sleep(0.01)
    assert c.get_if_cached("k", loader) == "filled"


def test_a_burst_on_a_cold_key_starts_one_fetch_not_one_per_caller():
    """Otherwise a busy moment on a cold cache becomes a thread-per-visitor
    stampede against an API that is already struggling."""
    c = cache.SwrCache(fresh_for=60, max_stale=600)
    loader, calls = _counting_loader(delay=0.3)
    for _ in range(10):
        assert c.get_if_cached("k", loader) is None
    _settle(calls, 1)
    time.sleep(0.2)
    assert len(calls) == 1


def test_a_cached_value_is_returned_without_waiting():
    c = cache.SwrCache(fresh_for=60, max_stale=600)
    c.get("k", lambda: "v")
    loader, calls = _counting_loader(delay=5.0)
    started = time.time()
    assert c.get_if_cached("k", loader) == "v"
    assert time.time() - started < 0.1
    assert calls == []


def test_a_value_past_max_stale_is_not_served():
    """Too old to be worth showing: treated as a miss, so the caller gets
    None now and a fresh value next time rather than something misleading."""
    c = cache.SwrCache(fresh_for=0, max_stale=0)
    c.get("k", lambda: "ancient")
    loader, calls = _counting_loader(value="new")
    assert c.get_if_cached("k", loader) is None
    _settle(calls, 1)
    assert len(calls) == 1
