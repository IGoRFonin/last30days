"""Tests for scripts/lib/proxy_pool.py — round-robin proxy pool with cooldown."""

import sys
import os
import time
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib.proxy_pool import ProxyPool


class TestRoundRobin:
    """ProxyPool cycles through proxies in order."""

    def test_returns_proxies_in_order(self):
        pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
        assert pool.next() == "http://a:1"
        assert pool.next() == "http://b:2"
        assert pool.next() == "http://c:3"

    def test_wraps_around(self):
        pool = ProxyPool(["http://a:1", "http://b:2"])
        pool.next()
        pool.next()
        assert pool.next() == "http://a:1"

    def test_empty_pool_returns_none(self):
        pool = ProxyPool([])
        assert pool.next() is None


class TestCooldown:
    """Proxies on cooldown are skipped."""

    def test_skips_cooled_down_proxy(self):
        pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
        pool.next()  # a
        pool.cooldown("http://a:1", seconds=60)
        # Next round should skip a
        pool._index = 0
        assert pool.next() == "http://b:2"

    def test_cooldown_expires(self):
        pool = ProxyPool(["http://a:1", "http://b:2"])
        pool.cooldown("http://a:1", seconds=0.1)
        time.sleep(0.15)
        pool._index = 0
        assert pool.next() == "http://a:1"

    def test_all_on_cooldown_waits_and_returns(self):
        pool = ProxyPool(["http://a:1", "http://b:2"])
        pool.cooldown("http://a:1", seconds=0.1)
        pool.cooldown("http://b:2", seconds=0.1)
        # Should wait for shortest cooldown and return
        result = pool.next()
        assert result in ("http://a:1", "http://b:2")


class TestStatePersistence:
    """ProxyPool saves and loads index from state file."""

    def test_saves_state(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".state", delete=False) as f:
            state_file = Path(f.name)
        try:
            pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], state_file=state_file)
            pool.next()  # a, index -> 1
            pool.next()  # b, index -> 2
            pool.save_state()

            content = state_file.read_text().strip()
            assert content == "2"
        finally:
            state_file.unlink(missing_ok=True)

    def test_loads_state(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".state", delete=False) as f:
            f.write("2")
            state_file = Path(f.name)
        try:
            pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], state_file=state_file)
            assert pool.next() == "http://c:3"
        finally:
            state_file.unlink(missing_ok=True)

    def test_invalid_state_file_resets_to_zero(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".state", delete=False) as f:
            f.write("garbage")
            state_file = Path(f.name)
        try:
            pool = ProxyPool(["http://a:1", "http://b:2"], state_file=state_file)
            assert pool.next() == "http://a:1"
        finally:
            state_file.unlink(missing_ok=True)

    def test_missing_state_file_starts_at_zero(self):
        pool = ProxyPool(["http://a:1", "http://b:2"], state_file=Path("/tmp/nonexistent_proxy.state"))
        assert pool.next() == "http://a:1"


class TestThreadSafety:
    """ProxyPool is safe under concurrent access."""

    def test_concurrent_next_no_crash(self):
        pool = ProxyPool([f"http://p{i}:80" for i in range(10)])
        results = []

        def grab():
            for _ in range(20):
                results.append(pool.next())

        threads = [threading.Thread(target=grab) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 100
        assert all(r is not None for r in results)
