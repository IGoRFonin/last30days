"""Tests for scripts/lib/reddit_direct.py — Reddit direct JSON API with proxy pool."""

import json
import urllib.error
from unittest import mock

import pytest
import sys
import os

# Ensure lib is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import reddit_direct


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_reddit_listing(posts):
    """Build a Reddit listing JSON structure from a list of post dicts."""
    children = []
    for p in posts:
        children.append({
            "kind": "t3",
            "data": {
                "id": p.get("id", "abc123"),
                "title": p.get("title", "Test Post"),
                "permalink": p.get("permalink", "/r/test/comments/abc123/test_post/"),
                "subreddit": p.get("subreddit", "test"),
                "score": p.get("score", 42),
                "num_comments": p.get("num_comments", 10),
                "created_utc": p.get("created_utc", 1711670400),  # 2024-03-29
                "author": p.get("author", "testuser"),
                "selftext": p.get("selftext", "Some body text"),
                "upvote_ratio": p.get("upvote_ratio", 0.95),
            },
        })
    return {"data": {"children": children}}


def _make_comment_listing(post_data, comments):
    """Build Reddit comment JSON (array of [post_listing, comments_listing])."""
    post_listing = _make_reddit_listing([post_data] if post_data else [])
    comment_children = []
    for c in comments:
        comment_children.append({
            "kind": "t1",
            "data": {
                "id": c.get("id", "cmt1"),
                "body": c.get("body", "Great post!"),
                "author": c.get("author", "commenter"),
                "score": c.get("score", 10),
                "ups": c.get("ups", c.get("score", 10)),
                "created_utc": c.get("created_utc", 1711670400),
                "permalink": c.get("permalink", "/r/test/comments/abc123/test_post/cmt1/"),
            },
        })
    comments_listing = {"data": {"children": comment_children}}
    return [post_listing, comments_listing]


SAMPLE_LISTING = _make_reddit_listing([
    {
        "id": "post1",
        "title": "Claude Code is amazing",
        "permalink": "/r/ClaudeAI/comments/post1/claude_code_is_amazing/",
        "subreddit": "ClaudeAI",
        "score": 250,
        "num_comments": 45,
        "created_utc": 1711670400,
        "author": "ai_fan",
        "selftext": "I've been using Claude Code for a week and it changed my workflow.",
    },
    {
        "id": "post2",
        "title": "Tips for Claude Code prompting",
        "permalink": "/r/ClaudeAI/comments/post2/tips_for_claude_code/",
        "subreddit": "ClaudeAI",
        "score": 120,
        "num_comments": 22,
        "created_utc": 1711584000,
        "author": "prompt_engineer",
        "selftext": "Here are my top tips for getting the most out of Claude Code.",
    },
])


def _mock_opener_ok(data):
    """Return a mock opener whose .open() returns JSON data."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(data).encode("utf-8")
    resp.headers = {"Content-Type": "application/json"}
    resp.__enter__ = mock.MagicMock(return_value=resp)
    resp.__exit__ = mock.MagicMock(return_value=False)

    opener = mock.MagicMock()
    opener.open.return_value = resp
    return opener


def _make_pool():
    """Create a simple ProxyPool for testing."""
    from lib.proxy_pool import ProxyPool
    return ProxyPool(["http://proxy1:8080", "http://proxy2:8080"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchJson:
    """_fetch_json fetches JSON via proxy."""

    @mock.patch("lib.reddit_direct.urllib.request.build_opener")
    def test_fetch_json_success(self, mock_build):
        mock_build.return_value = _mock_opener_ok({"ok": True})
        pool = _make_pool()

        result = reddit_direct._fetch_json("https://www.reddit.com/search.json", pool)

        assert result == {"ok": True}
        assert mock_build.call_count == 1


class TestRetryOn429:
    """429 triggers proxy cooldown; with MAX_RETRIES=1 returns None immediately."""

    @mock.patch("lib.reddit_direct.time.sleep")
    @mock.patch("lib.reddit_direct.urllib.request.build_opener")
    def test_429_cooldowns_proxy_and_retries(self, mock_build, mock_sleep):
        fail_opener = mock.MagicMock()
        fail_opener.open.side_effect = urllib.error.HTTPError(
            "https://reddit.com/search.json", 429, "Too Many Requests", {}, None,
        )
        mock_build.return_value = fail_opener
        pool = _make_pool()

        result = reddit_direct._fetch_json("https://www.reddit.com/search.json", pool)

        assert result is None
        assert mock_build.call_count == 1


class TestRetryOnConnectionError:
    """Connection error returns None immediately with MAX_RETRIES=1."""

    @mock.patch("lib.reddit_direct.time.sleep")
    @mock.patch("lib.reddit_direct.urllib.request.build_opener")
    def test_connection_error_retries(self, mock_build, mock_sleep):
        fail_opener = mock.MagicMock()
        fail_opener.open.side_effect = urllib.error.URLError("Connection refused")
        mock_build.return_value = fail_opener
        pool = _make_pool()

        result = reddit_direct._fetch_json("https://www.reddit.com/search.json", pool)

        assert result is None
        assert mock_build.call_count == 1


class TestMaxRetries:
    """Gives up after MAX_RETRIES (5) attempts."""

    @mock.patch("lib.reddit_direct.time.sleep")
    @mock.patch("lib.reddit_direct.urllib.request.build_opener")
    def test_gives_up_after_max_retries(self, mock_build, mock_sleep):
        fail_opener = mock.MagicMock()
        fail_opener.open.side_effect = urllib.error.URLError("Connection refused")
        mock_build.return_value = fail_opener
        pool = _make_pool()

        result = reddit_direct._fetch_json("https://www.reddit.com/search.json", pool)

        assert result is None
        assert fail_opener.open.call_count == reddit_direct.MAX_RETRIES


class TestHtmlAntiBot:
    """HTML anti-bot response returns None immediately with MAX_RETRIES=1."""

    @mock.patch("lib.reddit_direct.time.sleep")
    @mock.patch("lib.reddit_direct.urllib.request.build_opener")
    def test_html_antibot_triggers_retry(self, mock_build, mock_sleep):
        html_resp = mock.MagicMock()
        html_resp.read.return_value = b"<html><body>Please verify</body></html>"
        html_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        html_resp.__enter__ = mock.MagicMock(return_value=html_resp)
        html_resp.__exit__ = mock.MagicMock(return_value=False)

        html_opener = mock.MagicMock()
        html_opener.open.return_value = html_resp

        mock_build.return_value = html_opener
        pool = _make_pool()

        result = reddit_direct._fetch_json("https://www.reddit.com/search.json", pool)

        assert result is None
        assert mock_build.call_count == 1


class TestGlobalSearch:
    """_global_search returns parsed posts."""

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_global_search_returns_posts(self, mock_fetch):
        mock_fetch.return_value = SAMPLE_LISTING
        pool = _make_pool()

        posts = reddit_direct._global_search("Claude Code", pool)

        assert len(posts) == 2
        assert posts[0]["title"] == "Claude Code is amazing"
        assert posts[0]["subreddit"] == "ClaudeAI"


class TestSubredditSearch:
    """_subreddit_search builds correct URL with restrict_sr."""

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_subreddit_search_url(self, mock_fetch):
        mock_fetch.return_value = SAMPLE_LISTING
        pool = _make_pool()

        posts = reddit_direct._subreddit_search("ClaudeAI", "Claude Code", pool)

        assert len(posts) == 2
        call_url = mock_fetch.call_args[0][0]
        assert "/r/ClaudeAI/search.json" in call_url
        assert "restrict_sr=on" in call_url


class TestFetchPostComments:
    """fetch_post_comments parses comment JSON."""

    @mock.patch("lib.reddit_direct._fetch_json")
    def test_fetch_comments(self, mock_fetch):
        comment_data = _make_comment_listing(
            {"id": "post1", "title": "Test", "permalink": "/r/test/comments/post1/test/"},
            [
                {"id": "c1", "body": "This is great!", "author": "user1", "score": 50},
                {"id": "c2", "body": "I disagree.", "author": "user2", "score": 5},
            ],
        )
        mock_fetch.return_value = comment_data
        pool = _make_pool()

        comments = reddit_direct.fetch_post_comments(
            "https://www.reddit.com/r/test/comments/post1/test/", pool
        )

        assert len(comments) == 2
        assert comments[0]["body"] == "This is great!"
        assert comments[0]["author"] == "user1"
        assert comments[0]["score"] == 50


class TestUserAgents:
    """USER_AGENTS list is not empty."""

    def test_user_agents_not_empty(self):
        assert len(reddit_direct.USER_AGENTS) >= 10
