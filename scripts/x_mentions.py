#!/usr/bin/env python3
"""X/Twitter mention checker. Zero Claude tokens.

Searches for @PhobosIntern mentions, filters new ones, sends summary
to Telegram via raw API. State tracked in data/x_mentions_state.json.

Usage:
    poetry run python scripts/x_mentions.py
    poetry run python scripts/x_mentions.py --dry-run
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from twikit import Client
from twikit.errors import Forbidden, TooManyRequests, Unauthorized

# Load .env if present
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COOKIES_PATH = DATA_DIR / "x_cookies.json"
TRANSACTION_CACHE_PATH = DATA_DIR / "x_transaction.json"
TRANSACTION_CACHE_TTL = 3600
STATE_PATH = DATA_DIR / "x_mentions_state.json"


def _log(msg: str) -> None:
    print(f"[x_mentions] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Auth (same as x_post.py / x_digest.py)
# ---------------------------------------------------------------------------

def _restore_transaction_cache(client: Client) -> bool:
    if not TRANSACTION_CACHE_PATH.exists():
        return False
    try:
        cache = json.loads(TRANSACTION_CACHE_PATH.read_text())
        age = time.time() - cache.get("cached_at", 0)
        if age > TRANSACTION_CACHE_TTL:
            TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
            return False
        ct = client.client_transaction
        ct.key = cache["key"]
        ct.key_bytes = ct.get_key_bytes(cache["key"])
        ct.animation_key = cache["animation_key"]
        ct.DEFAULT_ROW_INDEX = cache["DEFAULT_ROW_INDEX"]
        ct.DEFAULT_KEY_BYTES_INDICES = cache["DEFAULT_KEY_BYTES_INDICES"]
        ct.home_page_response = True
        return True
    except Exception:
        TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
        return False


async def _seed_transaction_cache(client: Client) -> bool:
    import re
    import bs4
    import httpx
    from twikit.x_client_transaction.transaction import (
        INDICES_REGEX, ON_DEMAND_FILE_REGEX, ON_DEMAND_HASH_PATTERN,
    )
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
            cache = {
                "key": ct.key, "animation_key": ct.animation_key,
                "DEFAULT_ROW_INDEX": ct.DEFAULT_ROW_INDEX,
                "DEFAULT_KEY_BYTES_INDICES": ct.DEFAULT_KEY_BYTES_INDICES,
                "cached_at": time.time(),
            }
            TRANSACTION_CACHE_PATH.write_text(json.dumps(cache))
            return True
    except Exception:
        return False


async def authenticate(client: Client) -> None:
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
        _log("X_USERNAME, X_EMAIL, X_PASSWORD required")
        sys.exit(1)
    try:
        await client.login(auth_info_1=username, auth_info_2=email, password=password)
    except Exception:
        COOKIES_PATH.unlink(missing_ok=True)
        await client.login(auth_info_1=username, auth_info_2=email, password=password)
    client.save_cookies(str(COOKIES_PATH))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"last_seen_id": None, "last_checked": None}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ.get("NOTIFICATION_CHAT_IDS", "13218410").split(",")[0]
    resp = httpx.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )
    if resp.status_code != 200:
        _log(f"Telegram send failed: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dry_run: bool = False) -> None:
    screen_name = os.environ.get("X_USERNAME", "")
    if not screen_name:
        _log("X_USERNAME required")
        sys.exit(1)

    client = Client("en-US")
    await authenticate(client)

    try:
        tweets = await client.search_tweet(
            f"@{screen_name}", product="Latest", count=20
        )
    except TooManyRequests as e:
        _log(f"Rate limited: {e}")
        sys.exit(1)
    except (Unauthorized, Forbidden):
        _log("Auth failed, attempting re-login...")
        COOKIES_PATH.unlink(missing_ok=True)
        TRANSACTION_CACHE_PATH.unlink(missing_ok=True)
        await authenticate(client)
        try:
            tweets = await client.search_tweet(
                f"@{screen_name}", product="Latest", count=20
            )
        except Exception as e2:
            _log(f"Re-login failed: {e2}")
            sys.exit(1)

    client.save_cookies(str(COOKIES_PATH))

    # Filter out our own tweets
    mentions = [
        t for t in tweets
        if getattr(t, "user", None)
        and t.user.screen_name.lower() != screen_name.lower()
    ]

    # Filter to only new mentions
    state = _load_state()
    last_seen_id = state.get("last_seen_id")
    if last_seen_id:
        mentions = [t for t in mentions if int(t.id) > int(last_seen_id)]

    if not mentions:
        _log("No new mentions")
        state["last_checked"] = datetime.now(UTC).isoformat()
        _save_state(state)
        return

    # Sort oldest first
    mentions.sort(key=lambda t: int(t.id))

    # Format message
    lines = [f"\U0001f426 <b>X Mentions ({len(mentions)} new)</b>\n"]
    for t in mentions:
        author = t.user.screen_name
        likes = t.favorite_count or 0
        rts = t.retweet_count or 0
        text = (t.text[:200] + "...") if len(t.text) > 200 else t.text
        url = f"https://x.com/{author}/status/{t.id}"
        lines.append(
            f"@{author} [{likes}\u2665 {rts}\U0001f501]: \"{text}\"\n"
            f"  \u2192 {url}\n"
        )
    message = "\n".join(lines)

    if dry_run:
        print(message)
    else:
        _send_telegram(message)
        _log(f"Sent {len(mentions)} mention(s) to Telegram")

    # Update state
    state["last_seen_id"] = mentions[-1].id
    state["last_checked"] = datetime.now(UTC).isoformat()
    _save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check X mentions for @PhobosIntern")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending to Telegram")
    args = parser.parse_args()

    try:
        asyncio.run(run(dry_run=args.dry_run))
    except Exception as e:
        _log(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
