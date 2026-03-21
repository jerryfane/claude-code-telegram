#!/usr/bin/env python3
"""Standalone Moltbook post performance tracker.

Polls the Moltbook API for post/comment stats, auto-discovers new posts
from the agent's profile, computes deltas, and updates the tracker JSON.
Prints summary JSON to stdout for the bot to display.

Usage:
    python scripts/moltbook_stats.py \
        --credentials data/moltbook_credentials.json \
        --tracker .memory/users/13218410/memory/projects/moltbook_posts.json
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

# Rate limit defaults
BASE_DELAY = 1.5  # Seconds between API calls
MAX_RETRIES = 2


def _log(msg: str) -> None:
    print(f"[moltbook_stats] {msg}", file=sys.stderr, flush=True)


def _empty_tracker() -> dict:
    return {
        "posts": [],
        "comments": [],
        "account": {},
        "last_full_poll": None,
    }


def _load_tracker(path: Path) -> dict:
    if not path.exists():
        return _empty_tracker()
    try:
        data = json.loads(path.read_text())
        # Ensure required keys
        data.setdefault("posts", [])
        data.setdefault("comments", [])
        data.setdefault("account", {})
        data.setdefault("last_full_poll", None)
        return data
    except Exception as e:
        _log(f"Failed to read tracker: {e}")
        return _empty_tracker()


def _save_tracker(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


async def _api_get(
    client: httpx.AsyncClient, base_url: str, path: str, api_key: str
) -> dict | None:
    """GET request with retry logic."""
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            await asyncio.sleep(BASE_DELAY)
            r = await client.get(url, headers=headers, timeout=15)
            if r.status_code == 429:
                wait = min(30, BASE_DELAY * (2 ** attempt))
                _log(f"429 rate limited, waiting {wait:.0f}s")
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                _log(f"GET {path} returned {r.status_code}")
                return None
            return r.json()
        except Exception as e:
            _log(f"GET {path} failed: {e}")
            if attempt <= MAX_RETRIES:
                await asyncio.sleep(BASE_DELAY * attempt)
                continue
            return None
    return None


async def run(credentials_path: str, tracker_path: str) -> dict:
    """Main polling routine."""
    # Load credentials
    try:
        creds = json.loads(Path(credentials_path).read_text())
    except Exception as e:
        return {"error": f"Failed to read credentials: {e}", "posts": [], "account": {}}

    api_key = creds.get("api_key", "")
    agent_name = creds.get("agent_name", "")
    base_url = creds.get("base_url", "https://www.moltbook.com/api/v1")

    if not api_key or not agent_name:
        return {"error": "Missing api_key or agent_name in credentials", "posts": [], "account": {}}

    tracker = _load_tracker(Path(tracker_path))
    tracked_post_ids = {p["id"] for p in tracker["posts"]}
    tracked_comment_ids = {c["id"] for c in tracker["comments"]}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # 1. Fetch agent profile — account stats + auto-discover posts/comments
        _log("Fetching agent profile...")
        profile_data = await _api_get(
            client, base_url, f"/agents/profile?name={agent_name}", api_key
        )

        if profile_data and profile_data.get("success"):
            agent = profile_data.get("agent", {})
            tracker["account"] = {
                "karma": agent.get("karma", 0),
                "followers": agent.get("follower_count", 0),
                "following": agent.get("following_count", 0),
                "posts_count": agent.get("posts_count", 0),
                "comments_count": agent.get("comments_count", 0),
                "last_polled": datetime.now(UTC).isoformat(),
            }

            # Auto-discover posts from profile
            for post in profile_data.get("recentPosts", []):
                pid = post.get("id", "")
                if pid and pid not in tracked_post_ids:
                    tracker["posts"].append({
                        "id": pid,
                        "type": "post",
                        "title": post.get("title", ""),
                        "submolt": post.get("submolt", {}).get("name", "")
                            if isinstance(post.get("submolt"), dict)
                            else post.get("submolt", ""),
                        "created_at": post.get("created_at", ""),
                        "upvotes": post.get("upvotes", 0),
                        "downvotes": post.get("downvotes", 0),
                        "comment_count": post.get("comment_count", 0),
                        "score": post.get("score", 0),
                        "hot_score": post.get("hot_score", 0),
                        "last_polled": datetime.now(UTC).isoformat(),
                        "prev_upvotes": 0,
                        "prev_comment_count": 0,
                    })
                    tracked_post_ids.add(pid)
                    _log(f"Auto-discovered post: {post.get('title', pid)[:60]}")

            # Auto-discover comments from profile
            for comment in profile_data.get("recentComments", []):
                cid = comment.get("id", "")
                if cid and cid not in tracked_comment_ids:
                    post_info = comment.get("post", {})
                    tracker["comments"].append({
                        "id": cid,
                        "post_id": post_info.get("id", ""),
                        "post_title": post_info.get("title", ""),
                        "content_preview": comment.get("content", "")[:80],
                        "created_at": comment.get("created_at", ""),
                        "upvotes": comment.get("upvotes", 0),
                        "downvotes": comment.get("downvotes", 0),
                        "last_polled": datetime.now(UTC).isoformat(),
                    })
                    tracked_comment_ids.add(cid)
            _log(f"Profile: karma={agent.get('karma')}, "
                 f"posts={agent.get('posts_count')}, "
                 f"comments={agent.get('comments_count')}")
        else:
            _log("Profile fetch failed, continuing with existing tracker data")

        # 2. Poll each tracked post for current stats
        _log(f"Polling {len(tracker['posts'])} tracked posts...")
        for post in tracker["posts"]:
            post_data = await _api_get(
                client, base_url, f"/posts/{post['id']}", api_key
            )
            if post_data and post_data.get("success"):
                p = post_data["post"]
                # Store previous values for delta calculation
                post["prev_upvotes"] = post.get("upvotes", 0)
                post["prev_comment_count"] = post.get("comment_count", 0)
                # Update current values
                post["upvotes"] = p.get("upvotes", 0)
                post["downvotes"] = p.get("downvotes", 0)
                post["comment_count"] = p.get("comment_count", 0)
                post["score"] = p.get("score", 0)
                post["hot_score"] = p.get("hot_score", 0)
                post["last_polled"] = datetime.now(UTC).isoformat()
                # Backfill title/submolt if missing
                if not post.get("title"):
                    post["title"] = p.get("title", "")
                if not post.get("submolt"):
                    submolt = p.get("submolt", {})
                    post["submolt"] = submolt.get("name", "") if isinstance(submolt, dict) else ""
                if not post.get("created_at"):
                    post["created_at"] = p.get("created_at", "")

    # Sort posts by created_at descending
    tracker["posts"].sort(
        key=lambda p: p.get("created_at", ""),
        reverse=True,
    )
    tracker["last_full_poll"] = datetime.now(UTC).isoformat()

    # Save updated tracker
    _save_tracker(Path(tracker_path), tracker)
    _log(f"Tracker saved with {len(tracker['posts'])} posts, {len(tracker['comments'])} comments")

    # Build summary output
    summary_posts = []
    for p in tracker["posts"]:
        delta_up = p.get("upvotes", 0) - p.get("prev_upvotes", 0)
        delta_comments = p.get("comment_count", 0) - p.get("prev_comment_count", 0)
        summary_posts.append({
            "id": p["id"],
            "title": p.get("title", "")[:60],
            "created_at": p.get("created_at", ""),
            "upvotes": p.get("upvotes", 0),
            "delta_upvotes": delta_up,
            "comment_count": p.get("comment_count", 0),
            "delta_comments": delta_comments,
            "score": p.get("score", 0),
        })

    return {
        "error": None,
        "account": tracker["account"],
        "posts": summary_posts,
        "comments_count": len(tracker["comments"]),
        "polled_at": tracker["last_full_poll"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Moltbook post stats poller")
    parser.add_argument("--credentials", required=True, help="Path to credentials JSON")
    parser.add_argument("--tracker", required=True, help="Path to tracker JSON")
    args = parser.parse_args()

    if not Path(args.credentials).exists():
        print(json.dumps({"error": f"Credentials not found: {args.credentials}"}), flush=True)
        sys.exit(1)

    try:
        output = asyncio.run(run(args.credentials, args.tracker))
    except Exception as e:
        output = {"error": f"Fatal: {e}", "posts": [], "account": {}}
        print(json.dumps(output), flush=True)
        sys.exit(1)

    print(json.dumps(output), flush=True)

    if output.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
