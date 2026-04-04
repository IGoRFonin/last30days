"""Round-robin proxy pool with cooldown and state persistence.

Rotates through a list of HTTP proxies. Tracks cooldowns per-proxy
(e.g. after 429 responses). Persists the current index to a state file
so consecutive runs distribute load evenly across the pool.
"""

import sys
import threading
import time
from pathlib import Path
from typing import List, Optional


def _log(msg: str):
    sys.stderr.write(f"[ProxyPool] {msg}\n")
    sys.stderr.flush()


class ProxyPool:
    """Thread-safe round-robin proxy pool with cooldown support."""

    def __init__(self, proxies: List[str], state_file: Optional[Path] = None):
        self._proxies = proxies
        self._state_file = state_file
        self._cooldowns: dict[str, float] = {}  # proxy -> timestamp when available
        self._lock = threading.Lock()
        self._index = self._load_state() if state_file else 0

    def _load_state(self) -> int:
        if not self._state_file or not self._state_file.exists():
            return 0
        try:
            value = int(self._state_file.read_text().strip())
            if 0 <= value < len(self._proxies):
                return value
            return 0
        except (ValueError, OSError):
            return 0

    def save_state(self):
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(str(self._index))
        except OSError as e:
            _log(f"Failed to save state: {e}")

    def next(self) -> Optional[str]:
        if not self._proxies:
            return None

        with self._lock:
            now = time.monotonic()
            n = len(self._proxies)

            # Try each proxy once
            for _ in range(n):
                proxy = self._proxies[self._index % n]
                self._index = (self._index + 1) % n
                cooldown_until = self._cooldowns.get(proxy, 0)
                if now >= cooldown_until:
                    return proxy

            # All on cooldown — wait for the shortest remaining
            if not self._cooldowns:
                return self._proxies[0]
            earliest = min(self._cooldowns.values())
            wait = max(0, earliest - now)
            _log(f"All proxies on cooldown, waiting {wait:.1f}s")

        time.sleep(wait)

        with self._lock:
            # After waiting, pick the first available
            now = time.monotonic()
            for _ in range(n):
                proxy = self._proxies[self._index % n]
                self._index = (self._index + 1) % n
                if now >= self._cooldowns.get(proxy, 0):
                    return proxy

        _log("Fallback: returning first proxy despite cooldown")
        return self._proxies[0]

    def cooldown(self, proxy: str, seconds: float = 30):
        with self._lock:
            self._cooldowns[proxy] = time.monotonic() + seconds
            _log(f"Cooldown {proxy} for {seconds}s")
