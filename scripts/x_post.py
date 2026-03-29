#!/usr/bin/env python3
"""X/Twitter posting CLI using twikit.

Standalone script — no bot imports. Handles auth + error recovery.
Posts from the account configured via X_USERNAME env var.

Environment variables:
    X_USERNAME  — X/Twitter username
    X_EMAIL     — X/Twitter email
    X_PASSWORD  — X/Twitter password

Usage:
    poetry run python scripts/x_post.py tweet "text here"
    poetry run python scripts/x_post.py thread "tweet 1" "tweet 2" "tweet 3"
    poetry run python scripts/x_post.py reply <tweet_id> "reply text"
    poetry run python scripts/x_post.py quote <tweet_id> "quote text"
    poetry run python scripts/x_post.py delete <tweet_id>
    poetry run python scripts/x_post.py timeline --limit 10
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from twikit import Client
from twikit.errors import (
    Forbidden,
    TooManyRequests,
    TwitterException,
    Unauthorized,
)


# Load .env if present (for manual CLI use; bot sets env vars when shelling out)
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COOKIES_PATH = DATA_DIR / "x_cookies.json"
TRANSACTION_CACHE_PATH = DATA_DIR / "x_transaction.json"
TRANSACTION_CACHE_TTL = 3600
MAX_TWEET_LENGTH = 280


def _log(msg: str) -> None:
    """Log to stderr so stdout stays clean for JSON output."""
    print(f"[x_post] {msg}", file=sys.stderr, flush=True)


def _output(data: dict) -> None:
    """Print JSON result to stdout."""
    print(json.dumps(data, indent=2), flush=True)


def _error_exit(msg: str) -> None:
    """Print JSON error to stdout and exit 1."""
    _output({"success": False, "error": msg})
    sys.exit(1)


def _validate_text(text: str) -> str:
    """Strip whitespace and fail if over 280 chars."""
    text = text.strip()
    if len(text) > MAX_TWEET_LENGTH:
        _error_exit(
            f"Tweet too long: {len(text)}/{MAX_TWEET_LENGTH} chars. "
            f"Trim {len(text) - MAX_TWEET_LENGTH} characters."
        )
    return text


# ---------------------------------------------------------------------------
# Auth — copied from x_digest.py (standalone, no cross-imports)
# ---------------------------------------------------------------------------

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
    """Restore ClientTransaction state from cache. Returns True if restored."""
    if not TRANSACTION_CACHE_PATH.exists():
        return False
    try:
        cache = json.loads(TRANSACTION_CACHE_PATH.read_text())
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
        ct.home_page_response = True
        _log(f"Restored transaction cache ({age:.0f}s old)")
        return True
    except Exception as e:
        _log(f"Failed to restore transaction cache: {e}")
        TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
        return False


async def _seed_transaction_cache(client: Client) -> bool:
    """Fetch x.com homepage with a clean httpx client and seed transaction state."""
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

            response_str = str(soup)
            row_index, key_bytes_indices = 2, [12, 42, 45]
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

    if COOKIES_PATH.exists():
        try:
            client.load_cookies(str(COOKIES_PATH))
            if not _restore_transaction_cache(client):
                await _seed_transaction_cache(client)
            return
        except Exception:
            COOKIES_PATH.unlink(missing_ok=True)

    username = os.environ.get("X_USERNAME", "")
    email = os.environ.get("X_EMAIL", "")
    password = os.environ.get("X_PASSWORD", "")

    if not all([username, email, password]):
        _error_exit(
            "X_USERNAME, X_EMAIL, and X_PASSWORD environment variables are required"
        )

    try:
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )
    except Exception:
        COOKIES_PATH.unlink(missing_ok=True)
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )

    client.save_cookies(str(COOKIES_PATH))


# ---------------------------------------------------------------------------
# Error wrapper
# ---------------------------------------------------------------------------

async def _run_with_retry(coro_fn, retries: int = 1):
    """Run an async callable with retry and auto-reauth on expired cookies."""
    for attempt in range(retries + 1):
        try:
            return await coro_fn()
        except TooManyRequests as e:
            wait = getattr(e, "retry_after", None) or 60
            _error_exit(f"Rate limited. Retry after {wait}s.")
        except (Unauthorized, Forbidden):
            if attempt < retries and _active_client is not None:
                _log("Auth failed, attempting re-login...")
                COOKIES_PATH.unlink(missing_ok=True)
                TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
                await authenticate(_active_client)
                continue
            _error_exit("Cookies expired and re-login failed.")
        except TwitterException:
            raise
        except Exception as e:
            if attempt < retries:
                _log(f"Network error, retrying in 5s: {e}")
                await asyncio.sleep(5)
            else:
                raise


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _tweet_url(screen_name: str, tweet_id: str) -> str:
    return f"https://x.com/{screen_name}/status/{tweet_id}"


_active_client: Client | None = None


async def _get_client() -> Client:
    global _active_client
    client = Client("en-US")
    await authenticate(client)
    _active_client = client
    return client


async def _get_screen_name() -> str:
    return os.environ.get("X_USERNAME", "unknown")


def _format_tweet(t) -> dict:
    """Format a twikit Tweet object into a dict for JSON output."""
    author = getattr(t, "user", None)
    screen_name = author.screen_name if author else None
    return {
        "id": t.id,
        "author": screen_name,
        "author_name": author.name if author else None,
        "text": t.text,
        "created_at": t.created_at,
        "favorite_count": t.favorite_count,
        "retweet_count": t.retweet_count,
        "reply_count": t.reply_count,
        "url": _tweet_url(screen_name, t.id) if screen_name else None,
    }


async def cmd_tweet(args: argparse.Namespace) -> None:
    text = _validate_text(args.text)
    client = await _get_client()
    screen_name = await _get_screen_name()

    async def _post():
        media_ids = None
        if args.media:
            media_id = await client.upload_media(args.media)
            media_ids = [media_id]
        return await client.create_tweet(text=text, media_ids=media_ids)

    result = await _run_with_retry(_post)
    client.save_cookies(str(COOKIES_PATH))

    _output({
        "success": True,
        "tweet_id": result.id,
        "text": text,
        "url": _tweet_url(screen_name, result.id),
        "char_count": len(text),
    })


async def cmd_thread(args: argparse.Namespace) -> None:
    texts = [_validate_text(t) for t in args.tweets]
    if len(texts) < 2:
        _error_exit("Thread requires at least 2 tweets.")

    client = await _get_client()
    screen_name = await _get_screen_name()
    thread = []

    first = await _run_with_retry(
        lambda: client.create_tweet(text=texts[0])
    )
    thread.append({
        "tweet_id": first.id,
        "text": texts[0],
        "url": _tweet_url(screen_name, first.id),
        "char_count": len(texts[0]),
    })

    prev = first
    for t in texts[1:]:
        reply = await _run_with_retry(
            lambda t=t, prev=prev: client.create_tweet(text=t, reply_to=prev.id)
        )
        thread.append({
            "tweet_id": reply.id,
            "text": t,
            "url": _tweet_url(screen_name, reply.id),
            "char_count": len(t),
        })
        prev = reply

    client.save_cookies(str(COOKIES_PATH))
    _output({"success": True, "thread": thread})


async def cmd_reply(args: argparse.Namespace) -> None:
    text = _validate_text(args.text)
    client = await _get_client()
    screen_name = await _get_screen_name()

    result = await _run_with_retry(
        lambda: client.create_tweet(text=text, reply_to=args.tweet_id)
    )
    client.save_cookies(str(COOKIES_PATH))

    _output({
        "success": True,
        "tweet_id": result.id,
        "text": text,
        "url": _tweet_url(screen_name, result.id),
        "char_count": len(text),
        "reply_to": args.tweet_id,
    })


async def cmd_quote(args: argparse.Namespace) -> None:
    text = _validate_text(args.text)
    client = await _get_client()
    screen_name = await _get_screen_name()

    result = await _run_with_retry(
        lambda: client.create_tweet(text=text, quote_tweet_id=args.tweet_id)
    )
    client.save_cookies(str(COOKIES_PATH))

    _output({
        "success": True,
        "tweet_id": result.id,
        "text": text,
        "url": _tweet_url(screen_name, result.id),
        "char_count": len(text),
        "quoted_tweet": args.tweet_id,
    })


async def cmd_follow(args: argparse.Namespace) -> None:
    client = await _get_client()
    user = await _run_with_retry(
        lambda: client.get_user_by_screen_name(args.screen_name)
    )
    await _run_with_retry(lambda: user.follow())
    client.save_cookies(str(COOKIES_PATH))
    _output({
        "success": True,
        "action": "follow",
        "screen_name": args.screen_name,
        "user_id": user.id,
    })


async def cmd_unfollow(args: argparse.Namespace) -> None:
    client = await _get_client()
    user = await _run_with_retry(
        lambda: client.get_user_by_screen_name(args.screen_name)
    )
    await _run_with_retry(lambda: user.unfollow())
    client.save_cookies(str(COOKIES_PATH))
    _output({
        "success": True,
        "action": "unfollow",
        "screen_name": args.screen_name,
        "user_id": user.id,
    })


async def cmd_like(args: argparse.Namespace) -> None:
    client = await _get_client()
    await _run_with_retry(lambda: client.favorite_tweet(args.tweet_id))
    client.save_cookies(str(COOKIES_PATH))
    _output({"success": True, "liked": args.tweet_id})


async def cmd_delete(args: argparse.Namespace) -> None:
    client = await _get_client()

    await _run_with_retry(lambda: client.delete_tweet(args.tweet_id))
    client.save_cookies(str(COOKIES_PATH))

    _output({"success": True, "deleted_tweet_id": args.tweet_id})


async def cmd_timeline(args: argparse.Namespace) -> None:
    client = await _get_client()
    screen_name = os.environ.get("X_USERNAME", "")

    if not screen_name:
        _error_exit("X_USERNAME environment variable is required for timeline.")

    user = await _run_with_retry(
        lambda: client.get_user_by_screen_name(screen_name)
    )
    tweets = await _run_with_retry(
        lambda: user.get_tweets("Tweets", count=args.limit)
    )
    client.save_cookies(str(COOKIES_PATH))

    tweet_list = [_format_tweet(t) for t in tweets]
    _output({
        "success": True,
        "screen_name": screen_name,
        "count": len(tweet_list),
        "tweets": tweet_list,
    })


async def cmd_feed(args: argparse.Namespace) -> None:
    client = await _get_client()
    tweets = await _run_with_retry(
        lambda: client.get_timeline(count=args.limit)
    )
    client.save_cookies(str(COOKIES_PATH))

    tweet_list = [_format_tweet(t) for t in tweets]
    _output({"success": True, "count": len(tweet_list), "tweets": tweet_list})


async def cmd_mentions(args: argparse.Namespace) -> None:
    client = await _get_client()
    screen_name = os.environ.get("X_USERNAME", "")

    if not screen_name:
        _error_exit("X_USERNAME environment variable is required for mentions.")

    tweets = await _run_with_retry(
        lambda: client.search_tweet(f"@{screen_name}", product="Latest", count=args.limit)
    )
    client.save_cookies(str(COOKIES_PATH))

    tweet_list = [_format_tweet(t) for t in tweets]
    _output({"success": True, "count": len(tweet_list), "tweets": tweet_list})


async def cmd_search(args: argparse.Namespace) -> None:
    client = await _get_client()
    tweets = await _run_with_retry(
        lambda: client.search_tweet(args.query, product="Latest", count=args.limit)
    )
    client.save_cookies(str(COOKIES_PATH))

    tweet_list = [_format_tweet(t) for t in tweets]
    _output({
        "success": True,
        "query": args.query,
        "count": len(tweet_list),
        "tweets": tweet_list,
    })


async def cmd_user(args: argparse.Namespace) -> None:
    client = await _get_client()
    user = await _run_with_retry(
        lambda: client.get_user_by_screen_name(args.screen_name)
    )
    tweets = await _run_with_retry(
        lambda: user.get_tweets("Tweets", count=args.limit)
    )
    client.save_cookies(str(COOKIES_PATH))

    tweet_list = [_format_tweet(t) for t in tweets]
    _output({
        "success": True,
        "screen_name": args.screen_name,
        "count": len(tweet_list),
        "tweets": tweet_list,
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="X/Twitter posting CLI using twikit"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # tweet
    p_tweet = sub.add_parser("tweet", help="Post a single tweet")
    p_tweet.add_argument("text", help="Tweet text (max 280 chars)")
    p_tweet.add_argument("--media", default=None, help="Path to image file")

    # thread
    p_thread = sub.add_parser("thread", help="Post a thread (auto-chained)")
    p_thread.add_argument("tweets", nargs="+", help="Tweet texts in order")

    # reply
    p_reply = sub.add_parser("reply", help="Reply to a tweet")
    p_reply.add_argument("tweet_id", help="Tweet ID to reply to")
    p_reply.add_argument("text", help="Reply text")

    # quote
    p_quote = sub.add_parser("quote", help="Quote tweet")
    p_quote.add_argument("tweet_id", help="Tweet ID to quote")
    p_quote.add_argument("text", help="Quote text")

    # follow
    p_follow = sub.add_parser("follow", help="Follow a user")
    p_follow.add_argument("screen_name", help="Username to follow")

    # unfollow
    p_unfollow = sub.add_parser("unfollow", help="Unfollow a user")
    p_unfollow.add_argument("screen_name", help="Username to unfollow")

    # like
    p_like = sub.add_parser("like", help="Like a tweet")
    p_like.add_argument("tweet_id", help="Tweet ID to like")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a tweet")
    p_delete.add_argument("tweet_id", help="Tweet ID to delete")

    # timeline
    p_timeline = sub.add_parser("timeline", help="Get recent tweets from our account")
    p_timeline.add_argument("--limit", type=int, default=10, help="Number of tweets")

    # feed
    p_feed = sub.add_parser("feed", help="Home timeline from followed accounts")
    p_feed.add_argument("--limit", type=int, default=20, help="Number of tweets")

    # mentions
    p_mentions = sub.add_parser("mentions", help="Check our mentions")
    p_mentions.add_argument("--limit", type=int, default=10, help="Number of tweets")

    # search
    p_search = sub.add_parser("search", help="Search tweets")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10, help="Number of tweets")

    # user
    p_user = sub.add_parser("user", help="View a user's tweets")
    p_user.add_argument("screen_name", help="Username")
    p_user.add_argument("--limit", type=int, default=10, help="Number of tweets")

    cmd_map = {
        "tweet": cmd_tweet,
        "thread": cmd_thread,
        "reply": cmd_reply,
        "quote": cmd_quote,
        "follow": cmd_follow,
        "unfollow": cmd_unfollow,
        "like": cmd_like,
        "delete": cmd_delete,
        "timeline": cmd_timeline,
        "feed": cmd_feed,
        "mentions": cmd_mentions,
        "search": cmd_search,
        "user": cmd_user,
    }

    args = parser.parse_args()
    try:
        asyncio.run(cmd_map[args.command](args))
    except SystemExit:
        raise
    except Exception as e:
        _error_exit(str(e))


if __name__ == "__main__":
    main()
