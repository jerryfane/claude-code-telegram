#!/usr/bin/env python3
"""Standalone X/Twitter search script using twikit.

Authenticates, searches topics from config, prints JSON to stdout.
No bot imports — designed to be shelled out to by XDigestService.

Environment variables:
    X_USERNAME  — X/Twitter username
    X_EMAIL     — X/Twitter email
    X_PASSWORD  — X/Twitter password

Usage:
    python scripts/x_digest.py --config config/x_digest.json
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from twikit import Client
from twikit.errors import (
    Forbidden,
    TooManyRequests,
    TwitterException,
    Unauthorized,
)


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COOKIES_PATH = DATA_DIR / "x_cookies.json"
TRANSACTION_CACHE_PATH = DATA_DIR / "x_transaction.json"

# Rate limit defaults
BASE_DELAY = 2          # Seconds between requests when no pressure
MAX_DELAY = 90          # Cap on any single wait
MAX_RETRIES = 2         # Per-topic retry attempts
RATE_LIMIT_BUFFER = 2   # Extra seconds added to X's reset time
TRANSACTION_CACHE_TTL = 3600  # Cache transaction state for 1 hour


class RateLimitGuard:
    """Adaptive rate limiter that respects X's rate limit headers.

    - Tracks a global cooldown timestamp from 429 responses.
    - Uses exponential backoff on transient errors (403, 500, etc).
    - Applies a base delay between requests to stay under the radar.
    """

    def __init__(self, base_delay: float = BASE_DELAY) -> None:
        self.base_delay = base_delay
        self._global_reset: float = 0  # Unix timestamp when rate limit lifts
        self._consecutive_errors = 0
        self._request_count = 0

    async def wait_before_request(self) -> None:
        """Wait the appropriate amount of time before the next request."""
        now = time.time()

        # If we have a known rate limit reset time, wait for it
        if self._global_reset > now:
            wait = self._global_reset - now + RATE_LIMIT_BUFFER
            wait = min(wait, MAX_DELAY)
            _log(f"Rate limit active, waiting {wait:.0f}s until reset")
            await asyncio.sleep(wait)
            return

        # Adaptive delay: base + backoff from consecutive errors
        if self._request_count > 0:
            backoff = self.base_delay * (2 ** min(self._consecutive_errors, 4))
            delay = min(backoff, MAX_DELAY)
            await asyncio.sleep(delay)

        self._request_count += 1

    def record_success(self) -> None:
        """Reset error counter on successful request."""
        self._consecutive_errors = 0

    def record_rate_limit(self, reset_timestamp: int | None) -> None:
        """Record a 429 response with optional reset timestamp."""
        self._consecutive_errors += 1
        if reset_timestamp:
            self._global_reset = float(reset_timestamp)
        else:
            # No header — back off exponentially from now
            backoff = self.base_delay * (2 ** min(self._consecutive_errors, 4))
            self._global_reset = time.time() + min(backoff, MAX_DELAY)

    def record_error(self) -> None:
        """Record a transient error (403, 500, etc)."""
        self._consecutive_errors += 1


def _log(msg: str) -> None:
    """Log to stderr so stdout stays clean for JSON output."""
    print(f"[x_digest] {msg}", file=sys.stderr, flush=True)


def _save_transaction_cache(client: Client) -> None:
    """Cache ClientTransaction state so we don't need to refetch x.com homepage."""
    ct = client.client_transaction
    if ct and ct.home_page_response and hasattr(ct, "key"):
        cache = {
            "key": ct.key,
            "animation_key": ct.animation_key,
            "DEFAULT_ROW_INDEX": ct.DEFAULT_ROW_INDEX,
            "DEFAULT_KEY_BYTES_INDICES": ct.DEFAULT_KEY_BYTES_INDICES,
            "cached_at": time.time(),
        }
        try:
            TRANSACTION_CACHE_PATH.write_text(json.dumps(cache))
            _log("Saved transaction cache")
        except Exception:
            pass


def _restore_transaction_cache(client: Client) -> bool:
    """Restore ClientTransaction state from cache, skipping homepage fetch.

    Returns True if cache was restored successfully.
    """
    if not TRANSACTION_CACHE_PATH.exists():
        return False

    try:
        cache = json.loads(TRANSACTION_CACHE_PATH.read_text())

        # Check TTL
        age = time.time() - cache.get("cached_at", 0)
        if age > TRANSACTION_CACHE_TTL:
            _log(f"Transaction cache expired ({age:.0f}s old)")
            TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
            return False

        ct = client.client_transaction
        ct.key = cache["key"]
        ct.key_bytes = ct.get_key_bytes(cache["key"])
        ct.animation_key = cache["animation_key"]
        ct.DEFAULT_ROW_INDEX = cache["DEFAULT_ROW_INDEX"]
        ct.DEFAULT_KEY_BYTES_INDICES = cache["DEFAULT_KEY_BYTES_INDICES"]
        # Set home_page_response to a truthy sentinel so the init check passes
        ct.home_page_response = True
        _log(f"Restored transaction cache ({age:.0f}s old)")
        return True
    except Exception as e:
        _log(f"Failed to restore transaction cache: {e}")
        TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
        return False


async def _seed_transaction_cache(client: Client) -> bool:
    """Fetch x.com homepage with a clean httpx client and seed transaction state.

    twikit's built-in httpx client sometimes gets fingerprinted by Cloudflare.
    This uses a fresh client with a standard browser User-Agent as fallback.
    """
    import re

    import bs4
    import httpx
    from twikit.x_client_transaction.transaction import (
        INDICES_REGEX,
        ON_DEMAND_FILE_REGEX,
        ON_DEMAND_HASH_PATTERN,
    )

    _log("Seeding transaction cache from homepage...")
    headers = {
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as session:
            r = await session.get("https://x.com", headers=headers)
            soup = bs4.BeautifulSoup(r.text, "lxml")

            meta = soup.select_one("[name='twitter-site-verification']")
            if not meta:
                _log("No twitter-site-verification meta tag found")
                return False
            key = meta.get("content")

            # Extract indices from ondemand.s.js
            response_str = str(soup)
            row_index, key_bytes_indices = 2, [12, 42, 45]  # defaults
            on_demand_file = ON_DEMAND_FILE_REGEX.search(response_str)
            if on_demand_file:
                idx = on_demand_file.group(1)
                hash_regex = re.compile(ON_DEMAND_HASH_PATTERN.format(idx))
                hash_match = hash_regex.search(response_str)
                if hash_match:
                    filename = hash_match.group(1)
                    url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
                    jr = await session.get(url, headers=headers)
                    matches = list(INDICES_REGEX.finditer(jr.text))
                    if matches:
                        indices = [int(m.group(1)) for m in matches]
                        row_index, key_bytes_indices = indices[0], indices[1:]

            # Build ClientTransaction state
            ct = client.client_transaction
            ct.DEFAULT_ROW_INDEX = row_index
            ct.DEFAULT_KEY_BYTES_INDICES = key_bytes_indices
            ct.home_page_response = soup
            ct.key = key
            ct.key_bytes = ct.get_key_bytes(key)
            ct.animation_key = ct.get_animation_key(ct.key_bytes, soup)

            _save_transaction_cache(client)
            _log("Transaction cache seeded successfully")
            return True
    except Exception as e:
        _log(f"Failed to seed transaction cache: {e}")
        return False


async def authenticate(client: Client) -> None:
    """Authenticate with cookie caching and retry on failure."""
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Try cached cookies first (skip verification — avoids Cloudflare blocks)
    if COOKIES_PATH.exists():
        try:
            client.load_cookies(str(COOKIES_PATH))
            # Restore transaction cache to avoid homepage fetch.
            # If cache is missing/expired, seed it from a fresh homepage fetch.
            if not _restore_transaction_cache(client):
                await _seed_transaction_cache(client)
            return
        except Exception:
            # Cookies file corrupt — delete and fall through to login
            COOKIES_PATH.unlink(missing_ok=True)

    # Fresh login
    username = os.environ.get("X_USERNAME", "")
    email = os.environ.get("X_EMAIL", "")
    password = os.environ.get("X_PASSWORD", "")

    if not all([username, email, password]):
        raise RuntimeError(
            "X_USERNAME, X_EMAIL, and X_PASSWORD environment variables are required"
        )

    try:
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )
    except Exception:
        # Delete cookies on auth failure and retry once
        COOKIES_PATH.unlink(missing_ok=True)
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )

    client.save_cookies(str(COOKIES_PATH))


def tweet_to_dict(tweet) -> dict:
    """Extract relevant fields from a twikit Tweet object."""
    return {
        "id": tweet.id,
        "text": tweet.text,
        "user": {
            "name": tweet.user.name if tweet.user else "?",
            "screen_name": tweet.user.screen_name if tweet.user else "?",
        },
        "created_at": tweet.created_at if tweet.created_at else None,
        "favorite_count": tweet.favorite_count,
        "retweet_count": tweet.retweet_count,
        "reply_count": tweet.reply_count,
    }


async def _execute_with_guard(
    guard: RateLimitGuard,
    label: str,
    coro_factory,
    client: Client | None = None,
) -> list:
    """Execute an async call with rate limit guard and retries.

    coro_factory is a zero-arg callable that returns a fresh coroutine
    (needed because coroutines can't be re-awaited after an exception).

    Returns a list of tweet dicts, or raises on non-retryable errors.
    """
    for attempt in range(1, MAX_RETRIES + 2):  # +2 because range is exclusive
        await guard.wait_before_request()
        try:
            results = await coro_factory()
            guard.record_success()
            return [tweet_to_dict(t) for t in results]

        except TooManyRequests as e:
            reset = getattr(e, "rate_limit_reset", None)
            guard.record_rate_limit(reset)
            if attempt <= MAX_RETRIES:
                _log(f"{label}: 429 rate limited (attempt {attempt}/{MAX_RETRIES}), "
                     f"reset={reset}, retrying...")
                continue
            raise

        except Forbidden:
            guard.record_error()
            if attempt <= MAX_RETRIES:
                _log(f"{label}: 403 forbidden (attempt {attempt}/{MAX_RETRIES}), "
                     f"retrying after backoff...")
                continue
            raise

        except Unauthorized:
            if attempt <= MAX_RETRIES:
                _log(f"{label}: Unauthorized, attempting re-login...")
                COOKIES_PATH.unlink(missing_ok=True)
                TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
                await authenticate(client)
                continue
            raise

        except TwitterException:
            raise

    return []  # Shouldn't reach here, but safety net


async def fetch_user_tweets(
    client: Client, screen_name: str, count: int, guard: RateLimitGuard
) -> dict:
    """Fetch latest tweets from a specific user's timeline."""
    topic_label = f"@{screen_name}"
    try:
        # User lookup is a separate API call — guard it too
        await guard.wait_before_request()
        user = await client.get_user_by_screen_name(screen_name)
        guard.record_success()

        tweets = await _execute_with_guard(
            guard,
            topic_label,
            lambda uid=user.id: client.get_user_tweets(uid, "Tweets", count=count),
            client=client,
        )
        return {
            "topic": topic_label,
            "query": f"user:{screen_name}",
            "tweets": tweets,
            "error": None,
        }
    except Exception as e:
        _log(f"{topic_label}: failed — {e}")
        return {
            "topic": topic_label,
            "query": f"user:{screen_name}",
            "tweets": [],
            "error": str(e),
        }


async def search_topic(
    client: Client, topic_name: str, query: str, count: int, guard: RateLimitGuard
) -> dict:
    """Search a single topic, returning a result dict."""
    try:
        tweets = await _execute_with_guard(
            guard,
            topic_name,
            lambda: client.search_tweet(query, product="Latest", count=count),
            client=client,
        )
        return {
            "topic": topic_name,
            "query": query,
            "tweets": tweets,
            "error": None,
        }
    except Exception as e:
        _log(f"{topic_name}: failed — {e}")
        return {
            "topic": topic_name,
            "query": query,
            "tweets": [],
            "error": str(e),
        }


async def run(config_path: str) -> dict:
    """Main search routine."""
    # Load config
    with open(config_path) as f:
        config = json.load(f)

    topics = config.get("topics", [])
    default_count = config.get("default_count", 20)

    if not topics:
        return {
            "topics": [],
            "searched_at": datetime.now(UTC).isoformat(),
            "error": "No topics configured",
        }

    # Authenticate
    client = Client("en-US")
    try:
        await authenticate(client)
    except Exception as e:
        return {
            "topics": [],
            "searched_at": datetime.now(UTC).isoformat(),
            "error": f"Authentication failed: {e}",
        }

    # Rate limit guard — shared across all topics
    guard = RateLimitGuard(base_delay=BASE_DELAY)

    # Search each topic
    topic_results = []
    for i, topic in enumerate(topics):
        name = topic.get("name", f"Topic {i + 1}")
        query = topic.get("query", "")
        count = topic.get("count", default_count)
        topic_type = topic.get("type", "search")

        if topic_type == "user":
            screen_name = topic.get("screen_name", query)
            result = await fetch_user_tweets(client, screen_name, count, guard)
        else:
            result = await search_topic(client, name, query, count, guard)

        topic_results.append(result)
        _log(f"{result['topic']}: {len(result['tweets'])} tweets")

    # Save cookies and transaction state after successful run
    try:
        client.save_cookies(str(COOKIES_PATH))
        _save_transaction_cache(client)
    except Exception:
        pass

    return {
        "topics": topic_results,
        "searched_at": datetime.now(UTC).isoformat(),
        "error": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="X/Twitter digest search")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to x_digest.json config file",
    )
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(
            json.dumps({"topics": [], "searched_at": None, "error": f"Config not found: {args.config}"}),
            flush=True,
        )
        sys.exit(1)

    try:
        output = asyncio.run(run(args.config))
    except Exception as e:
        output = {
            "topics": [],
            "searched_at": datetime.now(UTC).isoformat(),
            "error": f"Fatal: {e}",
        }
        print(json.dumps(output), flush=True)
        sys.exit(1)

    print(json.dumps(output), flush=True)

    # Exit 1 only for fatal errors (top-level error), not per-topic errors
    if output.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
