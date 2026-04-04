# Reddit Direct Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ScrapeCreators Reddit API with direct Reddit JSON API requests routed through a rotating proxy pool.

**Architecture:** New `reddit_direct.py` module with `ProxyPool` (round-robin, cooldown, persistent state) and Reddit JSON API wrappers. Drop-in replacement for `reddit.search_and_enrich()`. Old `reddit.py` (ScrapeCreators) deleted.

**Tech Stack:** Python stdlib (`urllib`, `json`, `threading`), no new dependencies.

---

### Task 1: ProxyPool class

**Files:**
- Create: `scripts/lib/proxy_pool.py`
- Test: `tests/test_proxy_pool.py`

- [ ] **Step 1: Write failing tests for ProxyPool**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_proxy_pool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.proxy_pool'`

- [ ] **Step 3: Implement ProxyPool**

```python
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

        return self._proxies[0]  # fallback

    def cooldown(self, proxy: str, seconds: float = 30):
        with self._lock:
            self._cooldowns[proxy] = time.monotonic() + seconds
            _log(f"Cooldown {proxy} for {seconds}s")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_proxy_pool.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/proxy_pool.py tests/test_proxy_pool.py
git commit -m "feat: add ProxyPool with round-robin, cooldown, and state persistence"
```

---

### Task 2: reddit_direct.py — fetch layer and search functions

**Files:**
- Create: `scripts/lib/reddit_direct.py`
- Test: `tests/test_reddit_direct.py`

- [ ] **Step 1: Write failing tests for _fetch_json and search functions**

```python
"""Tests for scripts/lib/reddit_direct.py — direct Reddit API with proxy pool."""

import json
import sys
import os
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import reddit_direct
from lib.proxy_pool import ProxyPool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_reddit_listing(posts):
    children = []
    for p in posts:
        children.append({
            "kind": "t3",
            "data": {
                "title": p.get("title", "Test Post"),
                "permalink": p.get("permalink", "/r/test/comments/abc123/test_post/"),
                "subreddit": p.get("subreddit", "test"),
                "score": p.get("score", 42),
                "num_comments": p.get("num_comments", 10),
                "created_utc": p.get("created_utc", 1711670400),
                "author": p.get("author", "testuser"),
                "selftext": p.get("selftext", "Some body text"),
                "upvote_ratio": p.get("upvote_ratio", 0.95),
            },
        })
    return {"data": {"children": children}}


def _make_comments_listing(comments):
    children = []
    for c in comments:
        children.append({
            "kind": "t1",
            "data": {
                "body": c.get("body", "A comment"),
                "score": c.get("score", 10),
                "author": c.get("author", "commenter"),
                "created_utc": c.get("created_utc", 1711670400),
                "permalink": c.get("permalink", "/r/test/comments/abc123/test/def456/"),
            },
        })
    # Reddit returns [post_listing, comments_listing]
    post_listing = {"data": {"children": [{"kind": "t3", "data": {"title": "Test"}}]}}
    return [post_listing, {"data": {"children": children}}]


SAMPLE_LISTING = _make_reddit_listing([
    {
        "title": "Claude Code tips",
        "permalink": "/r/ClaudeAI/comments/abc123/claude_code_tips/",
        "subreddit": "ClaudeAI",
        "score": 250,
        "num_comments": 45,
    },
    {
        "title": "AI workflow automation",
        "permalink": "/r/artificial/comments/def456/ai_workflow/",
        "subreddit": "artificial",
        "score": 120,
        "num_comments": 22,
    },
])

SAMPLE_COMMENTS = _make_comments_listing([
    {"body": "This is really useful, thanks for sharing!", "score": 50, "author": "helpful_user"},
    {"body": "I tried this and it works great.", "score": 30, "author": "confirmer"},
    {"body": "[deleted]", "score": 0, "author": "[deleted]"},
])


def _mock_urlopen_ok(data):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(data).encode("utf-8")
    resp.headers = {"Content-Type": "application/json"}
    resp.__enter__ = mock.MagicMock(return_value=resp)
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


class TestFetchJsonWithProxy:
    """_fetch_json routes requests through proxy pool."""

    @mock.patch("lib.reddit_direct.urllib.request.urlopen")
    def test_uses_proxy_from_pool(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_ok(SAMPLE_LISTING)
        pool = ProxyPool(["http://proxy1:8080"])

        result = reddit_direct._fetch_json("https://www.reddit.com/search.json?q=test", pool)

        assert result is not None
        # Verify ProxyHandler was used
        mock_urlopen.assert_called_once()

    @mock.patch("lib.reddit_direct.urllib.request.urlopen")
    def test_retries_with_different_proxy_on_429(self, mock_urlopen):
        error_429 = urllib.error.HTTPError(
            "https://reddit.com/search.json", 429, "Too Many Requests", {}, None,
        )
        success_resp = _mock_urlopen_ok(SAMPLE_LISTING)
        mock_urlopen.side_effect = [error_429, success_resp]

        pool = ProxyPool(["http://proxy1:8080", "http://proxy2:8080"])
        result = reddit_direct._fetch_json("https://www.reddit.com/search.json?q=test", pool)

        assert result is not None
        assert mock_urlopen.call_count == 2

    @mock.patch("lib.reddit_direct.urllib.request.urlopen")
    def test_retries_on_connection_error(self, mock_urlopen):
        conn_error = urllib.error.URLError("Connection refused")
        success_resp = _mock_urlopen_ok(SAMPLE_LISTING)
        mock_urlopen.side_effect = [conn_error, success_resp]

        pool = ProxyPool(["http://proxy1:8080", "http://proxy2:8080"])
        result = reddit_direct._fetch_json("https://www.reddit.com/search.json?q=test", pool)

        assert result is not None

    @mock.patch("lib.reddit_direct.urllib.request.urlopen")
    def test_gives_up_after_max_retries(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        pool = ProxyPool([f"http://proxy{i}:8080" for i in range(10)])
        result = reddit_direct._fetch_json("https://www.reddit.com/search.json?q=test", pool)

        assert result is None
        assert mock_urlopen.call_count == 5  # MAX_RETRIES

    @mock.patch("lib.reddit_direct.urllib.request.urlopen")
    def test_html_antibot_triggers_retry(self, mock_urlopen):
        html_resp = mock.MagicMock()
        html_resp.read.return_value = b"<html>Please verify</html>"
        html_resp.headers = {"Content-Type": "text/html"}
        html_resp.__enter__ = mock.MagicMock(return_value=html_resp)
        html_resp.__exit__ = mock.MagicMock(return_value=False)

        success_resp = _mock_urlopen_ok(SAMPLE_LISTING)
        mock_urlopen.side_effect = [html_resp, success_resp]

        pool = ProxyPool(["http://proxy1:8080", "http://proxy2:8080"])
        result = reddit_direct._fetch_json("https://www.reddit.com/search.json?q=test", pool)

        assert result is not None


class TestGlobalSearch:
    """_global_search returns normalized posts."""

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_returns_parsed_posts(self, mock_fetch):
        mock_fetch.return_value = SAMPLE_LISTING
        pool = ProxyPool(["http://proxy1:8080"])

        posts = reddit_direct._global_search("claude code", pool, sort="relevance", timeframe="month")

        assert len(posts) == 2
        assert posts[0]["title"] == "Claude Code tips"

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_returns_empty_on_failure(self, mock_fetch):
        mock_fetch.return_value = None
        pool = ProxyPool(["http://proxy1:8080"])

        posts = reddit_direct._global_search("test", pool)
        assert posts == []


class TestSubredditSearch:
    """_subreddit_search scopes to a subreddit."""

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_builds_correct_url(self, mock_fetch):
        mock_fetch.return_value = SAMPLE_LISTING
        pool = ProxyPool(["http://proxy1:8080"])

        reddit_direct._subreddit_search("ClaudeAI", "tips", pool)

        call_url = mock_fetch.call_args[0][0]
        assert "/r/ClaudeAI/search.json" in call_url
        assert "restrict_sr=on" in call_url


class TestFetchPostComments:
    """fetch_post_comments parses Reddit comment JSON."""

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_returns_parsed_comments(self, mock_fetch):
        mock_fetch.return_value = SAMPLE_COMMENTS
        pool = ProxyPool(["http://proxy1:8080"])

        comments = reddit_direct.fetch_post_comments(
            "https://www.reddit.com/r/test/comments/abc123/test/", pool
        )

        assert len(comments) == 3
        assert comments[0]["body"] == "This is really useful, thanks for sharing!"
        assert comments[0]["score"] == 50

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_returns_empty_on_failure(self, mock_fetch):
        mock_fetch.return_value = None
        pool = ProxyPool(["http://proxy1:8080"])

        comments = reddit_direct.fetch_post_comments("https://reddit.com/r/test/comments/abc/t/", pool)
        assert comments == []


class TestUserAgentRotation:
    """Requests use different User-Agent strings."""

    def test_user_agents_list_not_empty(self):
        assert len(reddit_direct.USER_AGENTS) >= 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reddit_direct.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.reddit_direct'`

- [ ] **Step 3: Implement reddit_direct.py**

```python
"""Reddit search via direct JSON API with proxy pool rotation.

Replaces ScrapeCreators-based reddit.py. Uses the free public Reddit JSON
endpoints routed through a rotating proxy pool for rate limit avoidance.

Endpoints:
- Global: https://www.reddit.com/search.json?q={query}&sort=relevance&t=month
- Subreddit: https://www.reddit.com/r/{sub}/search.json?q={query}&restrict_sr=on&sort=relevance&t=month
- Comments: https://www.reddit.com/comments/{post_id}.json

Requires REDDIT_PROXIES_FILE in config pointing to a file with one
http://user:pass@host:port per line.
"""

import json
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .proxy_pool import ProxyPool
from .query import extract_core_subject as _query_extract
from .query_type import detect_query_type
from .relevance import token_overlap_relevance

MAX_RETRIES = 5

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Depth configurations (same structure as old reddit.py)
DEPTH_CONFIG = {
    "quick": {
        "global_searches": 1,
        "subreddit_searches": 2,
        "comment_enrichments": 3,
        "timeframe": "week",
    },
    "default": {
        "global_searches": 2,
        "subreddit_searches": 3,
        "comment_enrichments": 5,
        "timeframe": "month",
    },
    "deep": {
        "global_searches": 3,
        "subreddit_searches": 5,
        "comment_enrichments": 8,
        "timeframe": "month",
    },
}

NOISE_WORDS = frozenset({
    'best', 'top', 'good', 'great', 'awesome', 'killer',
    'latest', 'new', 'news', 'update', 'updates',
    'trending', 'hottest', 'popular',
    'practices', 'features', 'tips',
    'recommendations', 'advice',
    'prompt', 'prompts', 'prompting',
    'methods', 'strategies', 'approaches',
    'how', 'to', 'the', 'a', 'an', 'for', 'with',
    'of', 'in', 'on', 'is', 'are', 'what', 'which',
    'guide', 'tutorial', 'using',
})

UTILITY_SUBS = frozenset({
    'namethatsong', 'findthatsong', 'tipofmytongue',
    'whatisthissong', 'helpmefind', 'whatisthisthing',
    'whatsthissong', 'findareddit', 'subredditdrama',
})


def _log(msg: str):
    sys.stderr.write(f"[RedditDirect] {msg}\n")
    sys.stderr.flush()


def _extract_core_subject(topic: str) -> str:
    return _query_extract(topic, noise=NOISE_WORDS)


def _fetch_json(
    url: str,
    pool: ProxyPool,
    timeout: int = 15,
) -> Optional[Any]:
    """Fetch JSON from URL via proxy pool with retry on failure.

    Tries up to MAX_RETRIES proxies. On 429 the proxy gets a 30s cooldown.
    On connection error or HTML anti-bot, moves to next proxy without cooldown.
    """
    for attempt in range(MAX_RETRIES):
        proxy = pool.next()
        if not proxy:
            _log("No proxy available")
            return None

        ua = random.choice(USER_AGENTS)
        headers = {
            "User-Agent": ua,
            "Accept": "application/json",
        }

        # Build opener with proxy
        proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(url, headers=headers)

        try:
            with opener.open(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "json" not in content_type and "text/html" in content_type:
                    _log(f"Anti-bot HTML from {proxy}, retry {attempt + 1}/{MAX_RETRIES}")
                    continue

                body = resp.read().decode("utf-8")
                return json.loads(body)

        except urllib.error.HTTPError as e:
            if e.code == 429:
                pool.cooldown(proxy, seconds=30)
                _log(f"429 from {proxy}, cooldown 30s, retry {attempt + 1}/{MAX_RETRIES}")
                continue
            elif e.code in (403, 451):
                _log(f"HTTP {e.code} from {proxy}, retry {attempt + 1}/{MAX_RETRIES}")
                continue
            else:
                _log(f"HTTP {e.code} from {proxy}: {e.reason}")
                continue

        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(f"Connection error via {proxy}: {e}, retry {attempt + 1}/{MAX_RETRIES}")
            continue

        except json.JSONDecodeError as e:
            _log(f"JSON decode error via {proxy}: {e}")
            continue

    _log(f"All {MAX_RETRIES} retries exhausted for {url}")
    return None


def _parse_posts(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse Reddit listing JSON into raw post dicts."""
    if not data:
        return []
    children = data.get("data", {}).get("children", [])
    posts = []
    for child in children:
        if child.get("kind") != "t3":
            continue
        posts.append(child.get("data", {}))
    return posts


def _parse_date(created_utc) -> Optional[str]:
    if not created_utc:
        return None
    try:
        dt = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return None


def _compute_post_relevance(query: str, title: str, selftext: str) -> float:
    title_score = token_overlap_relevance(query, title)
    if not selftext.strip():
        return title_score
    body_score = token_overlap_relevance(query, selftext)
    support_score = max(title_score, body_score)
    return round(0.75 * title_score + 0.25 * support_score, 2)


def _normalize_post(post: Dict[str, Any], idx: int, source_label: str = "global", query: str = "") -> Dict[str, Any]:
    permalink = post.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink and "/comments/" in permalink else ""
    title = str(post.get("title", "")).strip()
    selftext = str(post.get("selftext", ""))
    relevance = _compute_post_relevance(query, title, selftext) if query else 0.7

    return {
        "id": f"R{idx}",
        "reddit_id": post.get("id", ""),
        "title": title,
        "url": url,
        "subreddit": str(post.get("subreddit", "")).strip(),
        "date": _parse_date(post.get("created_utc")),
        "engagement": {
            "score": post.get("score", 0) or 0,
            "num_comments": post.get("num_comments", 0) or 0,
            "upvote_ratio": post.get("upvote_ratio"),
        },
        "relevance": relevance,
        "why_relevant": f"Reddit {source_label} search",
        "selftext": selftext[:500],
    }


def expand_reddit_queries(topic: str, depth: str) -> List[str]:
    core = _extract_core_subject(topic)
    queries = [core]
    original_clean = topic.strip().rstrip('?!.')
    if core.lower() != original_clean.lower() and len(original_clean.split()) <= 8:
        queries.append(original_clean)
    qtype = detect_query_type(topic)
    if depth in ("default", "deep") and qtype in ("product", "opinion"):
        queries.append(f"{core} worth it OR thoughts OR review")
    if depth == "deep" and qtype in ("product", "opinion", "how_to"):
        queries.append(f"{core} issues OR problems OR bug OR broken")
    return queries


def discover_subreddits(results: List[Dict[str, Any]], topic: str = "", max_subs: int = 5) -> List[str]:
    core = _extract_core_subject(topic) if topic else ""
    core_words = set(core.lower().split()) if core else set()
    scores = Counter()
    for post in results:
        sub = post.get("subreddit", "")
        if not sub:
            continue
        base = 1.0
        sub_lower = sub.lower()
        if core_words and any(w in sub_lower for w in core_words if len(w) > 2):
            base += 2.0
        if sub_lower in UTILITY_SUBS:
            base *= 0.3
        ups = post.get("score", 0) or 0
        if ups > 100:
            base += 0.5
        scores[sub] += base
    return [sub for sub, _ in scores.most_common(max_subs)]


def _global_search(
    query: str,
    pool: ProxyPool,
    sort: str = "relevance",
    timeframe: str = "month",
    limit: int = 25,
) -> List[Dict[str, Any]]:
    encoded = urllib.parse.quote_plus(query)
    url = (
        f"https://www.reddit.com/search.json"
        f"?q={encoded}&sort={sort}&t={timeframe}&limit={limit}&raw_json=1"
    )
    data = _fetch_json(url, pool)
    return _parse_posts(data)


def _subreddit_search(
    subreddit: str,
    query: str,
    pool: ProxyPool,
    sort: str = "relevance",
    timeframe: str = "month",
    limit: int = 25,
) -> List[Dict[str, Any]]:
    sub = subreddit.lstrip("r/").strip()
    encoded = urllib.parse.quote_plus(query)
    url = (
        f"https://www.reddit.com/r/{sub}/search.json"
        f"?q={encoded}&restrict_sr=on&sort={sort}&t={timeframe}&limit={limit}&raw_json=1"
    )
    data = _fetch_json(url, pool)
    return _parse_posts(data)


def _parse_comments(data: Optional[Any]) -> List[Dict[str, Any]]:
    """Parse Reddit comments JSON. Reddit returns [post_listing, comments_listing]."""
    if not data or not isinstance(data, list) or len(data) < 2:
        return []
    children = data[1].get("data", {}).get("children", [])
    comments = []
    for child in children:
        if child.get("kind") != "t1":
            continue
        c = child.get("data", {})
        comments.append({
            "body": c.get("body", ""),
            "score": c.get("score", 0) or 0,
            "author": c.get("author", "[deleted]"),
            "created_utc": c.get("created_utc"),
            "permalink": c.get("permalink", ""),
        })
    return comments


def fetch_post_comments(url: str, pool: ProxyPool) -> List[Dict[str, Any]]:
    # Extract post path and convert to .json endpoint
    # e.g. https://www.reddit.com/r/ClaudeAI/comments/abc123/title/ -> .json
    json_url = url.rstrip("/") + ".json?raw_json=1&limit=10"
    data = _fetch_json(json_url, pool)
    return _parse_comments(data)


def _dedupe_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_ids = set()
    seen_urls = set()
    unique = []
    for post in posts:
        rid = post.get("reddit_id", "")
        url = post.get("url", "")
        if rid and rid in seen_ids:
            continue
        if url and url in seen_urls:
            continue
        if rid:
            seen_ids.add(rid)
        if url:
            seen_urls.add(url)
        unique.append(post)
    return unique


def load_proxies(proxies_file: str) -> List[str]:
    """Load proxy list from file. One http://user:pass@host:port per line."""
    path = Path(proxies_file).expanduser()
    if not path.exists():
        _log(f"Proxies file not found: {path}")
        return []
    proxies = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            proxies.append(line)
    _log(f"Loaded {len(proxies)} proxies from {path}")
    return proxies


def make_pool(proxies_file: str) -> ProxyPool:
    """Create a ProxyPool from a proxies file with state persistence."""
    proxies = load_proxies(proxies_file)
    state_file = Path("~/.config/last30days/proxies.state").expanduser()
    return ProxyPool(proxies, state_file=state_file)


def search_reddit(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    pool: ProxyPool = None,
) -> Dict[str, Any]:
    if not pool or not pool._proxies:
        return {"items": [], "error": "No proxies configured (REDDIT_PROXIES_FILE)"}

    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    timeframe = config["timeframe"]

    # Phase 1: Query Expansion
    queries = expand_reddit_queries(topic, depth)
    _log(f"Expanded '{topic}' into {len(queries)} queries: {queries}")

    # Phase 2: Global Discovery
    all_raw_posts = []
    max_global = config["global_searches"]
    for i, query in enumerate(queries[:max_global]):
        sort = "relevance" if i == 0 else "top"
        _log(f"Global search {i+1}/{max_global}: '{query}' (sort={sort})")
        posts = _global_search(query, pool, sort=sort, timeframe=timeframe)
        _log(f"  -> {len(posts)} results")
        all_raw_posts.extend(posts)

    # Normalize
    core = _extract_core_subject(topic)
    all_items = []
    for i, post in enumerate(all_raw_posts):
        item = _normalize_post(post, i + 1, "global", query=core)
        all_items.append(item)

    # Phase 3: Subreddit Discovery + Targeted Search
    discovered_subs = discover_subreddits(all_raw_posts, topic=topic, max_subs=config["subreddit_searches"])
    _log(f"Discovered subreddits: {discovered_subs}")

    for sub in discovered_subs[:config["subreddit_searches"]]:
        _log(f"Subreddit search: r/{sub} for '{core}'")
        sub_posts = _subreddit_search(sub, core, pool, sort="relevance", timeframe=timeframe)
        _log(f"  -> {len(sub_posts)} results from r/{sub}")
        for j, post in enumerate(sub_posts):
            item = _normalize_post(post, len(all_items) + j + 1, f"r/{sub}", query=core)
            all_items.append(item)

    # Phase 4: Deduplicate
    all_items = _dedupe_posts(all_items)
    _log(f"After dedup: {len(all_items)} unique posts")

    # Phase 5: Date filter
    in_range = []
    out_of_range = 0
    for item in all_items:
        if item["date"] and from_date <= item["date"] <= to_date:
            in_range.append(item)
        elif item["date"] is None:
            in_range.append(item)
        else:
            out_of_range += 1

    if in_range:
        all_items = in_range
        if out_of_range:
            _log(f"Filtered {out_of_range} posts outside date range")
    else:
        _log(f"No posts within date range, keeping all {len(all_items)}")

    # Phase 6: Sort by engagement
    all_items.sort(key=lambda x: (x.get("engagement", {}).get("score", 0) or 0), reverse=True)

    for i, item in enumerate(all_items):
        item["id"] = f"R{i+1}"

    _log(f"Final: {len(all_items)} Reddit posts")
    return {"items": all_items}


def enrich_with_comments(
    items: List[Dict[str, Any]],
    pool: ProxyPool,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    max_comments = config["comment_enrichments"]

    if not items or not pool:
        return items

    top_items = items[:max_comments]
    _log(f"Enriching comments for {len(top_items)} posts")

    for item in top_items:
        url = item.get("url", "")
        if not url:
            continue

        raw_comments = fetch_post_comments(url, pool)
        if not raw_comments:
            continue

        top_comments = []
        insights = []

        for ci, c in enumerate(raw_comments[:10]):
            body = c.get("body", "")
            if not body or body in ("[deleted]", "[removed]"):
                continue

            score = c.get("score", 0)
            author = c.get("author", "[deleted]")
            permalink = c.get("permalink", "")
            comment_url = f"https://reddit.com{permalink}" if permalink else ""

            max_excerpt = 400 if ci == 0 else 300
            top_comments.append({
                "score": score,
                "date": _parse_date(c.get("created_utc")),
                "author": author,
                "excerpt": body[:max_excerpt],
                "url": comment_url,
            })

            if len(body) >= 30 and author not in ("[deleted]", "[removed]", "AutoModerator"):
                insight = body[:150]
                if len(body) > 150:
                    for i, char in enumerate(insight):
                        if char in '.!?' and i > 50:
                            insight = insight[:i+1]
                            break
                    else:
                        insight = insight.rstrip() + "..."
                insights.append(insight)

        top_comments.sort(key=lambda c: c.get("score", 0), reverse=True)
        item["top_comments"] = top_comments[:10]
        item["comment_insights"] = insights[:10]

    return items


def search_and_enrich(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    pool: ProxyPool = None,
) -> Dict[str, Any]:
    """Full Reddit pipeline: search + comment enrichment.

    Drop-in replacement for reddit.search_and_enrich().
    """
    result = search_reddit(topic, from_date, to_date, depth, pool)
    items = result.get("items", [])

    if items and pool:
        items = enrich_with_comments(items, pool, depth)
        result["items"] = items

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reddit_direct.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/reddit_direct.py tests/test_reddit_direct.py
git commit -m "feat: add reddit_direct module with proxy pool integration"
```

---

### Task 3: Wire into env.py and last30days.py

**Files:**
- Modify: `scripts/lib/env.py:353-378` (add REDDIT_PROXIES_FILE to config keys)
- Modify: `scripts/lib/env.py:421-442` (update is_reddit_available, get_reddit_source)
- Modify: `scripts/last30days.py:140-160` (imports)
- Modify: `scripts/last30days.py:179-310` (_search_reddit function)

- [ ] **Step 1: Add REDDIT_PROXIES_FILE to env.py config keys**

In `scripts/lib/env.py`, add to the `keys` list (after `SCRAPECREATORS_API_KEY` line):

```python
        ('REDDIT_PROXIES_FILE', None),
```

- [ ] **Step 2: Update is_reddit_available and get_reddit_source in env.py**

Replace `is_reddit_available`:

```python
def is_reddit_available(config: Dict[str, Any]) -> bool:
    """Check if Reddit search is available.

    Reddit can use direct proxies (preferred), public JSON, or OpenAI.
    """
    has_proxies = bool(config.get('REDDIT_PROXIES_FILE'))
    has_openai = bool(config.get('OPENAI_API_KEY')) and config.get('OPENAI_AUTH_STATUS') == AUTH_STATUS_OK
    return has_proxies or has_openai or True  # public JSON always available
```

Replace `get_reddit_source`:

```python
def get_reddit_source(config: Dict[str, Any]) -> Optional[str]:
    """Determine which Reddit backend to use.

    Priority: direct proxies > public JSON > OpenAI (legacy)

    Returns: 'direct', 'public', 'openai', or None
    """
    if config.get('REDDIT_PROXIES_FILE'):
        return 'direct'
    return 'public'
```

- [ ] **Step 3: Update imports in last30days.py**

In `scripts/last30days.py`, add `reddit_direct` to the import block (around line 140-160) and remove `reddit` import:

Replace:
```python
    reddit,
```
With:
```python
    reddit_direct,
```

- [ ] **Step 4: Replace _search_reddit in last30days.py**

Replace the `_search_reddit` function (lines ~179-310). The new version:

```python
def _search_reddit(
    topic: str,
    config: dict,
    selected_models: dict,
    from_date: str,
    to_date: str,
    depth: str,
    mock: bool,
) -> tuple:
    """Search Reddit (runs in thread).

    Hierarchy:
    1. Direct Reddit JSON via proxy pool (if REDDIT_PROXIES_FILE exists)
    2. Public Reddit JSON (always available) — free, no proxy
    3. OpenAI Responses API — legacy fallback

    Returns:
        Tuple of (reddit_items, raw_response, error, used_direct)
    """
    raw_response = None
    reddit_error = None
    used_direct = False

    proxies_file = config.get("REDDIT_PROXIES_FILE")

    if mock:
        raw_response = load_fixture("openai_sample.json")
    elif proxies_file:
        # === Tier 1: Direct Reddit with proxy pool ===
        used_direct = True
        try:
            sys.stderr.write("[Reddit] Using direct API with proxy pool\n")
            sys.stderr.flush()
            pool = reddit_direct.make_pool(proxies_file)
            result = reddit_direct.search_and_enrich(
                topic, from_date, to_date,
                depth=depth, pool=pool,
            )
            pool.save_state()
            reddit_items = result.get("items", [])
            if result.get("error"):
                reddit_error = result["error"]
            return reddit_items, result, reddit_error, used_direct
        except Exception as e:
            reddit_error = f"RedditDirect: {type(e).__name__}: {e}"
            sys.stderr.write(f"[Reddit] Direct API failed: {e}\n")
            sys.stderr.flush()
            used_direct = False
            # Fall through to Tier 2

    # === Tier 2: Public Reddit JSON (free, always available) ===
    if not mock:
        try:
            sys.stderr.write("[Reddit] Trying public Reddit JSON\n")
            sys.stderr.flush()
            reddit_items = reddit_public.search_reddit_public(
                topic, from_date, to_date, depth=depth,
            )
            if reddit_items:
                raw_response = {"source": "reddit_public", "items": reddit_items}
                sys.stderr.write(f"[Reddit] Public JSON returned {len(reddit_items)} results\n")
                sys.stderr.flush()
                return reddit_items, raw_response, None, False
            sys.stderr.write("[Reddit] Public JSON returned 0 results, trying OpenAI\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[Reddit] Public JSON failed: {e}\n")
            sys.stderr.flush()

    # === Tier 3: OpenAI Responses API (legacy fallback) ===
    if not mock and config.get("OPENAI_API_KEY"):
        try:
            sys.stderr.write("[Reddit] Falling back to OpenAI Responses API\n")
            sys.stderr.flush()
            raw_response = openai_reddit.search_reddit(
                config["OPENAI_API_KEY"],
                selected_models["openai"],
                topic,
                from_date,
                to_date,
                depth=depth,
                auth_source=config.get("OPENAI_AUTH_SOURCE", "api_key"),
                account_id=config.get("OPENAI_CHATGPT_ACCOUNT_ID"),
            )
        except http.HTTPError as e:
            raw_response = {"error": str(e)}
            reddit_error = f"API error: {e}"
        except Exception as e:
            raw_response = {"error": str(e)}
            reddit_error = f"{type(e).__name__}: {e}"

    reddit_items = openai_reddit.parse_reddit_response(raw_response or {})

    if len(reddit_items) < 5 and not mock and not reddit_error and config.get("OPENAI_API_KEY"):
        core = openai_reddit._extract_core_subject(topic)
        if core.lower() != topic.lower():
            try:
                retry_raw = openai_reddit.search_reddit(
                    config["OPENAI_API_KEY"],
                    selected_models["openai"],
                    core,
                    from_date, to_date,
                    depth=depth,
                    auth_source=config.get("OPENAI_AUTH_SOURCE", "api_key"),
                    account_id=config.get("OPENAI_CHATGPT_ACCOUNT_ID"),
                )
                retry_items = openai_reddit.parse_reddit_response(retry_raw)
                existing_urls = {item.get("url") for item in reddit_items}
                for item in retry_items:
                    if item.get("url") not in existing_urls:
                        reddit_items.append(item)
            except Exception:
                pass

    if len(reddit_items) < 3 and not mock and not reddit_error and config.get("OPENAI_API_KEY"):
        sub_query = openai_reddit._build_subreddit_query(topic)
        try:
            sub_raw = openai_reddit.search_reddit(
                config["OPENAI_API_KEY"],
                selected_models["openai"],
                sub_query,
                from_date, to_date,
                depth=depth,
            )
            sub_items = openai_reddit.parse_reddit_response(sub_raw)
            existing_urls = {item.get("url") for item in reddit_items}
            for item in sub_items:
                if item.get("url") not in existing_urls:
                    reddit_items.append(item)
        except Exception:
            pass

    return reddit_items, raw_response, reddit_error, False
```

- [ ] **Step 5: Update reddit_enrich.py reference**

In `scripts/lib/reddit_enrich.py:281`, change:

```python
    from . import reddit as reddit_mod
```
to:
```python
    from . import reddit_direct as reddit_mod
```

And update the `fetch_post_comments` call at line ~287 to pass a proxy pool. Check the calling context — if `reddit_enrich` is called from `last30days.py` with a token, we need to pass the pool instead. Read the full function to determine exact changes needed.

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: All existing tests that don't depend on `reddit.py` pass. Tests in `test_reddit_sc.py` will fail (expected — we'll handle in Task 4).

- [ ] **Step 7: Commit**

```bash
git add scripts/lib/env.py scripts/last30days.py scripts/lib/reddit_enrich.py
git commit -m "feat: wire reddit_direct into search pipeline, replace ScrapeCreators"
```

---

### Task 4: Delete reddit.py and update tests

**Files:**
- Delete: `scripts/lib/reddit.py`
- Modify: `tests/test_reddit_sc.py` — repoint to `reddit_direct`
- Modify: any other files still referencing `reddit.` for Reddit search

- [ ] **Step 1: Delete scripts/lib/reddit.py**

```bash
rm scripts/lib/reddit.py
```

- [ ] **Step 2: Update test_reddit_sc.py to test reddit_direct**

Rename file to `tests/test_reddit_direct_unit.py` and replace imports:

Replace:
```python
from lib import reddit
```
With:
```python
from lib import reddit_direct as reddit
```

All test method bodies stay the same — `reddit_direct` exports the same functions (`expand_reddit_queries`, `discover_subreddits`, `_parse_date`, `_compute_post_relevance`, `DEPTH_CONFIG`, `_extract_core_subject`).

- [ ] **Step 3: Remove SCRAPECREATORS references from env.py for Reddit**

In `scripts/lib/env.py`, remove `('SCRAPECREATORS_API_KEY', None)` from the keys list.

**Important:** `SCRAPECREATORS_API_KEY` is still used by TikTok and Instagram (`is_tiktok_available`, `is_instagram_available`). Only remove it if TikTok/Instagram don't need it. Check first — if they still need it, keep the key but remove Reddit-specific references only.

- [ ] **Step 4: Grep for remaining reddit.py references**

```bash
grep -rn "from.*lib.*import reddit\b" scripts/ tests/ --include="*.py" | grep -v reddit_direct | grep -v reddit_public | grep -v reddit_enrich
```

Fix any remaining references.

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: remove ScrapeCreators reddit.py, migrate tests to reddit_direct"
```

---

### Task 5: Integration smoke test

**Files:**
- No new files — manual verification

- [ ] **Step 1: Create a test proxies file**

```bash
echo "http://your-proxy1:port" > ~/.config/last30days/proxies.txt
# Add your real proxies here
```

- [ ] **Step 2: Set REDDIT_PROXIES_FILE in config**

Add to `~/.config/last30days/.env`:
```
REDDIT_PROXIES_FILE=~/.config/last30days/proxies.txt
```

- [ ] **Step 3: Run a real search**

```bash
python3 scripts/last30days.py "claude code tips" --emit=compact
```

Expected: Reddit results appear, stderr shows `[RedditDirect]` logs with proxy usage.

- [ ] **Step 4: Verify proxy rotation in logs**

Check stderr output for different proxy addresses being used across requests.

- [ ] **Step 5: Verify proxies.state was created**

```bash
cat ~/.config/last30days/proxies.state
```

Expected: A number (the index of the last used proxy).

- [ ] **Step 6: Deploy**

```bash
bash scripts/sync.sh
```
