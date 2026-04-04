"""Reddit search via direct JSON API with proxy pool.

Drop-in replacement for reddit.py (ScrapeCreators-based).
Uses Reddit's public JSON endpoints routed through a ProxyPool.

Endpoints:
- Global: https://www.reddit.com/search.json?q={query}&sort=relevance&t=month&limit=25&raw_json=1
- Subreddit: https://www.reddit.com/r/{sub}/search.json?q={query}&restrict_sr=on&sort=relevance&t=month&limit=25&raw_json=1
- Comments: {post_url}.json?raw_json=1&limit=10
"""

import json
import random
import sys
import time
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
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 OPR/109.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Depth configurations: how many API calls per phase
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

# Reddit-specific noise words
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

# Known utility/meta subreddits that get a 0.3x penalty
UTILITY_SUBS = frozenset({
    'namethatsong', 'findthatsong', 'tipofmytongue',
    'whatisthissong', 'helpmefind', 'whatisthisthing',
    'whatsthissong', 'findareddit', 'subredditdrama',
})


def _log(msg: str):
    """Log to stderr."""
    sys.stderr.write(f"[RedditDirect] {msg}\n")
    sys.stderr.flush()


def _extract_core_subject(topic: str) -> str:
    """Extract core subject from verbose query."""
    return _query_extract(topic, noise=NOISE_WORDS)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _fetch_json(url: str, pool: ProxyPool, timeout: int = 15) -> Optional[Dict[str, Any]]:
    """Fetch JSON via proxy with retry (max MAX_RETRIES attempts).

    On 429 -> cooldown proxy 30s, try next.
    On connection error / 403 / HTML anti-bot -> next proxy.
    Rotates User-Agent each attempt.
    """
    for attempt in range(MAX_RETRIES):
        proxy = pool.next()
        ua = random.choice(USER_AGENTS)

        headers = {
            "User-Agent": ua,
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, headers=headers)

        # Build per-request opener with proxy (thread-safe, no global state)
        if proxy:
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy,
                "https": proxy,
            })
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()

        try:
            with opener.open(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "json" not in content_type and "text/html" in content_type:
                    _log(f"Anti-bot HTML response via {proxy}, attempt {attempt + 1}/{MAX_RETRIES}")
                    time.sleep(1)
                    continue

                body = resp.read().decode("utf-8")
                return json.loads(body)

        except urllib.error.HTTPError as e:
            if e.code == 429:
                _log(f"429 via {proxy}, cooling down, attempt {attempt + 1}/{MAX_RETRIES}")
                pool.cooldown(proxy, 30)
                time.sleep(1)
                continue
            elif e.code == 403:
                _log(f"403 via {proxy}, trying next, attempt {attempt + 1}/{MAX_RETRIES}")
                time.sleep(0.5)
                continue
            else:
                _log(f"HTTP {e.code} via {proxy}: {e.reason}")
                time.sleep(0.5)
                continue

        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(f"Connection error via {proxy}: {e}, attempt {attempt + 1}/{MAX_RETRIES}")
            time.sleep(0.5)
            continue

        except json.JSONDecodeError as e:
            _log(f"JSON decode error: {e}")
            return None

    _log(f"All {MAX_RETRIES} attempts exhausted for {url}")
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_posts(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse Reddit listing JSON into raw post dicts.

    Expects data.children[].data where kind == 't3'.
    """
    if not data:
        return []

    children = data.get("data", {}).get("children", [])
    posts = []
    for child in children:
        if child.get("kind") != "t3":
            continue
        post = child.get("data", {})
        permalink = str(post.get("permalink", "")).strip()
        if not permalink or "/comments/" not in permalink:
            continue
        posts.append(post)
    return posts


def _parse_comments(data: Optional[Any]) -> List[Dict[str, Any]]:
    """Parse Reddit comments JSON.

    Reddit returns [post_listing, comments_listing].
    Comments are in data[1].data.children[].data where kind == 't1'.
    """
    if not data or not isinstance(data, list) or len(data) < 2:
        return []

    comments_listing = data[1]
    children = comments_listing.get("data", {}).get("children", [])
    comments = []
    for child in children:
        if child.get("kind") != "t1":
            continue
        comments.append(child.get("data", {}))
    return comments


def _parse_date(created_utc) -> Optional[str]:
    """Convert Unix timestamp to YYYY-MM-DD."""
    if not created_utc:
        return None
    try:
        dt = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return None


def _compute_post_relevance(query: str, title: str, selftext: str) -> float:
    """Compute Reddit relevance with title-first weighting."""
    title_score = token_overlap_relevance(query, title)
    if not selftext.strip():
        return title_score
    body_score = token_overlap_relevance(query, selftext)
    support_score = max(title_score, body_score)
    return round(0.75 * title_score + 0.25 * support_score, 2)


def _normalize_post(
    post: Dict[str, Any],
    idx: int,
    source_label: str = "global",
    query: str = "",
) -> Dict[str, Any]:
    """Normalize a raw Reddit post dict to internal format."""
    permalink = post.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")
    if url and "reddit.com" not in url:
        url = ""

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
            "score": post.get("ups") or post.get("score", 0),
            "num_comments": post.get("num_comments", 0),
            "upvote_ratio": post.get("upvote_ratio"),
        },
        "relevance": relevance,
        "why_relevant": f"Reddit {source_label} search",
        "selftext": selftext[:500],
    }


# ---------------------------------------------------------------------------
# Search functions
# ---------------------------------------------------------------------------

def _global_search(
    query: str,
    pool: ProxyPool,
    sort: str = "relevance",
    timeframe: str = "month",
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Search across all of Reddit via public JSON endpoint."""
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
    """Search within a specific subreddit via public JSON endpoint."""
    sub = subreddit.lstrip("r/").strip()
    encoded = urllib.parse.quote_plus(query)
    url = (
        f"https://www.reddit.com/r/{sub}/search.json"
        f"?q={encoded}&restrict_sr=on&sort={sort}&t={timeframe}&limit={limit}&raw_json=1"
    )
    data = _fetch_json(url, pool)
    return _parse_posts(data)


def fetch_post_comments(
    url: str,
    pool: ProxyPool,
) -> List[Dict[str, Any]]:
    """Fetch comments for a Reddit post.

    Appends .json?raw_json=1&limit=10 to the post URL.
    Returns list of comment dicts with body, author, score, etc.
    """
    # Clean the URL
    clean = url.rstrip("/")
    # Remove existing .json suffix if present
    if clean.endswith(".json"):
        clean = clean[:-5]
    json_url = f"{clean}.json?raw_json=1&limit=10"

    data = _fetch_json(json_url, pool)
    return _parse_comments(data)


# ---------------------------------------------------------------------------
# Query expansion and subreddit discovery (same logic as reddit.py)
# ---------------------------------------------------------------------------

def expand_reddit_queries(topic: str, depth: str) -> List[str]:
    """Generate multiple Reddit search queries from a topic.

    1. Extract core subject (strip noise words)
    2. Include original topic if different from core
    3. For default/deep: add casual/review variant
    4. For deep: add problem/issues variant

    Returns 1-4 query strings depending on depth.
    """
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


def discover_subreddits(
    results: List[Dict[str, Any]],
    topic: str = "",
    max_subs: int = 5,
) -> List[str]:
    """Extract top subreddits from global search results with relevance weighting."""
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
        ups = post.get("ups") or post.get("score", 0)
        if ups and ups > 100:
            base += 0.5

        scores[sub] += base

    return [sub for sub, _ in scores.most_common(max_subs)]


# ---------------------------------------------------------------------------
# Dedup + date filtering
# ---------------------------------------------------------------------------

def _dedupe_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate posts by reddit_id, keeping first occurrence."""
    seen_ids: set = set()
    seen_urls: set = set()
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


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def search_reddit(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    pool: Optional[ProxyPool] = None,
) -> Dict[str, Any]:
    """Full Reddit search: multi-query global discovery + subreddit drill-down.

    Args:
        topic: Search topic
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: 'quick', 'default', or 'deep'
        pool: ProxyPool instance

    Returns:
        Dict with 'items' list and optional 'error'.
    """
    if not pool:
        return {"items": [], "error": "No proxy pool configured"}

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

    # Normalize all posts
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
    all_items.sort(
        key=lambda x: (x.get("engagement", {}).get("score", 0) or 0),
        reverse=True,
    )

    # Re-index IDs
    for i, item in enumerate(all_items):
        item["id"] = f"R{i+1}"

    _log(f"Final: {len(all_items)} Reddit posts")
    return {"items": all_items}


def enrich_with_comments(
    items: List[Dict[str, Any]],
    pool: ProxyPool,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """Enrich top items with comment data.

    Args:
        items: Reddit items from search_reddit()
        pool: ProxyPool instance
        depth: Depth for comment limit

    Returns:
        Items with top_comments and comment_insights added.
    """
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

            score = c.get("ups") or c.get("score", 0)
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
    pool: Optional[ProxyPool] = None,
) -> Dict[str, Any]:
    """Full Reddit pipeline: search + comment enrichment."""
    result = search_reddit(topic, from_date, to_date, depth, pool)
    items = result.get("items", [])

    if items and pool:
        items = enrich_with_comments(items, pool, depth)
        result["items"] = items

    return result


# ---------------------------------------------------------------------------
# Proxy loading helpers
# ---------------------------------------------------------------------------

def load_proxies(proxies_file: str) -> List[str]:
    """Load proxy list from file (one per line, skip comments/empty)."""
    proxies = []
    path = Path(proxies_file).expanduser()
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line)
    except FileNotFoundError:
        _log(f"Proxies file not found: {proxies_file}")
    except OSError as e:
        _log(f"Error reading proxies file: {e}")
    return proxies


def make_pool(proxies_file: str) -> Optional[ProxyPool]:
    """Create ProxyPool from file with state persistence."""
    proxies = load_proxies(proxies_file)
    if not proxies:
        _log(f"No proxies loaded from {proxies_file}")
        return None

    state_file = Path.home() / ".config" / "last30days" / "proxies.state"
    pool = ProxyPool(proxies, state_file=state_file)
    _log(f"Created proxy pool with {len(proxies)} proxies")
    return pool
