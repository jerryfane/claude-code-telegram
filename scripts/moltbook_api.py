#!/usr/bin/env python3
"""Moltbook API wrapper with automatic verification challenge solver.

Wraps all Moltbook endpoints into single CLI commands. Handles
verification challenges (trap word removal + math parsing) automatically.

Usage as CLI:
    python scripts/moltbook_api.py home
    python scripts/moltbook_api.py feed [--limit 15]
    python scripts/moltbook_api.py notifications
    python scripts/moltbook_api.py comments <post_id> [--sort new] [--limit 20]
    python scripts/moltbook_api.py comment <post_id> "Your comment here"
    python scripts/moltbook_api.py post "Title" "Content body" [--submolt general]
    python scripts/moltbook_api.py upvote <post_id>
    python scripts/moltbook_api.py mark-read [--post <post_id>]
    python scripts/moltbook_api.py stats
    python scripts/moltbook_api.py verify "challenge text"  # debug solver

Usage as module:
    from scripts.moltbook_api import MoltbookAPI
    api = MoltbookAPI.from_credentials("data/moltbook_credentials.json")
    result = await api.get_home()
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

# ── Trap words to remove before parsing verification challenges ──────────
TRAP_WORDS = [
    "lobster",
    "newton",
    "antenna",
    "centimeter",
    "fortune",
    "often",
    "listen",
    "mitten",
    "kitten",
    "button",
    "castle",
    "whistle",
    "wrestle",
    "gristle",
    "thistle",
    "apostle",
    "bustle",
    "hustle",
    "jostle",
    "nestle",
    "pestle",
    "rustle",
    "trestle",
    "bristle",
    "epistle",
    "fasten",
    "glisten",
    "hasten",
    "moisten",
    "christen",
    "soften",
    "mortgage",
]

# ── Number word mappings ─────────────────────────────────────────────────
ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}

TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# ── Operator mappings ────────────────────────────────────────────────────
ADD_WORDS = {"plus", "adds", "gains", "total", "combined", "increased", "accelerates"}
SUB_WORDS = {"minus", "slows", "subtracts", "loses", "decreased", "reduces", "drops"}
MUL_WORDS = {"times", "product", "multiplied", "multiplies"}
DIV_WORDS = {"divided", "over", "split", "halved"}


def _log(msg: str) -> None:
    print(f"[moltbook_api] {msg}", file=sys.stderr, flush=True)


def _remove_trap_words(text: str) -> str:
    """Remove trap words from challenge text before parsing."""
    cleaned = text
    for word in TRAP_WORDS:
        # Remove trap words (case-insensitive, whole word)
        cleaned = re.sub(rf"\b{word}\b", " ", cleaned, flags=re.IGNORECASE)
    # Collapse multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _parse_number_word(text: str) -> Optional[float]:
    """Parse a number word or compound (e.g. 'twenty three' -> 23)."""
    text = text.strip().lower()

    # Try direct numeric
    try:
        return float(text)
    except ValueError:
        pass

    # Try compound: "twenty three", "thirty-seven", etc.
    text = text.replace("-", " ")
    parts = text.split()

    if len(parts) == 1:
        word = parts[0]
        if word in ONES:
            return float(ONES[word])
        if word in TENS:
            return float(TENS[word])
        # "hundred"
        if word == "hundred":
            return 100.0
        return None

    if len(parts) == 2:
        w1, w2 = parts
        if w1 in TENS and w2 in ONES:
            return float(TENS[w1] + ONES[w2])
        if w1 in ONES and w2 == "hundred":
            return float(ONES[w1] * 100)
        return None

    if len(parts) == 3:
        # "one hundred twenty" or "two hundred five"
        if parts[1] == "hundred":
            hundreds = ONES.get(parts[0], 0) * 100
            rest = TENS.get(parts[2], ONES.get(parts[2], 0))
            return float(hundreds + rest)
        # "twenty three hundred" (unlikely but handle)
        return None

    return None


def _detect_operator(text: str) -> Optional[str]:
    """Find the math operator in the challenge text."""
    words = set(text.lower().split())
    if words & ADD_WORDS:
        return "+"
    if words & SUB_WORDS:
        return "-"
    if words & MUL_WORDS:
        return "*"
    if words & DIV_WORDS:
        return "/"
    return None


def solve_verification(challenge_text: str) -> Optional[str]:
    """Solve a Moltbook verification challenge.

    Algorithm:
    1. Remove trap words
    2. Extract number words/digits
    3. Detect operator
    4. Calculate result to 2 decimal places

    Returns the answer as string (e.g. "42.00") or None if unsolvable.
    """
    cleaned = _remove_trap_words(challenge_text)
    _log(f"After trap removal: {cleaned}")

    operator = _detect_operator(cleaned)
    if not operator:
        _log(f"Could not detect operator in: {cleaned}")
        return None

    # Find numbers - try numeric digits first
    digit_matches = re.findall(r"\b\d+(?:\.\d+)?\b", cleaned)

    # Also find number words
    number_word_pattern = r"\b(?:"
    all_number_words = list(ONES.keys()) + list(TENS.keys())
    number_word_pattern += "|".join(all_number_words)
    number_word_pattern += r")(?:\s+(?:" + "|".join(all_number_words) + r"))?\b"

    word_matches = re.findall(number_word_pattern, cleaned.lower())

    numbers: list[float] = []

    # Prefer digit matches if we have 2+
    if len(digit_matches) >= 2:
        numbers = [float(d) for d in digit_matches[:2]]
    elif digit_matches and word_matches:
        # Mix of digits and words
        numbers.append(float(digit_matches[0]))
        parsed = _parse_number_word(word_matches[0])
        if parsed is not None:
            numbers.append(parsed)
    elif len(word_matches) >= 2:
        for wm in word_matches[:2]:
            parsed = _parse_number_word(wm)
            if parsed is not None:
                numbers.append(parsed)
    elif len(digit_matches) == 1 and len(word_matches) == 0:
        # Only one number found - look harder for word numbers
        numbers.append(float(digit_matches[0]))
        # Scan all words for a number
        for word in cleaned.lower().split():
            parsed = _parse_number_word(word)
            if parsed is not None and parsed != numbers[0]:
                numbers.append(parsed)
                break
    elif len(word_matches) == 1:
        parsed = _parse_number_word(word_matches[0])
        if parsed is not None:
            numbers.append(parsed)
        for d in digit_matches:
            numbers.append(float(d))
            break

    if len(numbers) < 2:
        _log(f"Found only {len(numbers)} numbers in: {cleaned}")
        return None

    a, b = numbers[0], numbers[1]

    if operator == "+":
        result = a + b
    elif operator == "-":
        result = a - b
    elif operator == "*":
        result = a * b
    elif operator == "/":
        if b == 0:
            _log("Division by zero")
            return None
        result = a / b
    else:
        return None

    answer = f"{result:.2f}"
    _log(f"Solved: {a} {operator} {b} = {answer}")
    return answer


class MoltbookAPI:
    """Async Moltbook API client with auto-verification."""

    def __init__(self, api_key: str, base_url: str = "https://www.moltbook.com/api/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_credentials(cls, path: str) -> "MoltbookAPI":
        """Create from credentials JSON file."""
        creds = json.loads(Path(path).read_text())
        return cls(
            api_key=creds["api_key"],
            base_url=creds.get("base_url", "https://www.moltbook.com/api/v1"),
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """GET request."""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(
                f"{self.base_url}{path}",
                headers=self._headers,
                params=params,
                timeout=15,
            )
            return r.json()

    async def _post(
        self, path: str, data: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """POST request."""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.post(
                f"{self.base_url}{path}",
                headers=self._headers,
                json=data or {},
                timeout=15,
            )
            return r.json()

    async def _auto_verify(self, response: dict[str, Any]) -> dict[str, Any]:
        """If response contains a verification challenge, solve and verify it.

        Verification data can be at top level or nested under:
        - response["comment"]["verification"]
        - response["post"]["verification"]
        """
        vc = response.get("verification_code") or response.get("verificationCode")
        challenge = response.get("verification_challenge") or response.get("verificationChallenge")

        # Check nested locations if not found at top level
        if not vc or not challenge:
            for key in ("comment", "post"):
                obj = response.get(key, {})
                if isinstance(obj, dict):
                    verification = obj.get("verification", {})
                    if isinstance(verification, dict):
                        vc = vc or verification.get("verification_code")
                        challenge = challenge or verification.get("challenge_text")

        if not vc or not challenge:
            return response

        _log(f"Verification challenge: {challenge}")
        answer = solve_verification(challenge)
        if not answer:
            _log("FAILED to solve verification")
            return {**response, "verification_error": "Could not solve challenge"}

        _log(f"Submitting verification: code={vc[:30]}... answer={answer}")
        verify_result = await self._post("/verify", {
            "verification_code": vc,
            "answer": answer,
        })
        return {
            **response,
            "verification_result": verify_result,
            "verification_answer": answer,
        }

    # ── Read endpoints ───────────────────────────────────────────────

    async def get_home(self) -> dict[str, Any]:
        """GET /home - Full dashboard (notifications, followed posts, DMs)."""
        return await self._get("/home")

    async def get_feed(self, limit: int = 15) -> dict[str, Any]:
        """GET /feed - Latest posts."""
        return await self._get("/feed", {"limit": limit})

    async def get_notifications(self) -> dict[str, Any]:
        """GET /notifications - All notifications."""
        return await self._get("/notifications")

    async def get_comments(
        self, post_id: str, sort: str = "new", limit: int = 20
    ) -> dict[str, Any]:
        """GET /posts/{id}/comments - Post comments."""
        return await self._get(
            f"/posts/{post_id}/comments",
            {"sort": sort, "limit": limit},
        )

    async def get_post(self, post_id: str) -> dict[str, Any]:
        """GET /posts/{id} - Single post details."""
        return await self._get(f"/posts/{post_id}")

    async def get_stats(self) -> dict[str, Any]:
        """GET /agents/me - Agent profile stats."""
        return await self._get("/agents/me")

    async def get_profile(self, name: str) -> dict[str, Any]:
        """GET /agents/profile?name=... - Agent profile with recent posts/comments."""
        return await self._get("/agents/profile", {"name": name})

    async def check_dms(self) -> dict[str, Any]:
        """GET /agents/dm/check - Check for DMs."""
        return await self._get("/agents/dm/check")

    # ── Write endpoints (with auto-verification) ─────────────────────

    async def post_comment(self, post_id: str, content: str) -> dict[str, Any]:
        """POST /posts/{id}/comments - Post a comment, auto-verify."""
        response = await self._post(f"/posts/{post_id}/comments", {"content": content})
        return await self._auto_verify(response)

    async def create_post(
        self,
        title: str,
        content: str,
        submolt: str = "general",
    ) -> dict[str, Any]:
        """POST /posts - Create a post, auto-verify."""
        response = await self._post("/posts", {
            "title": title,
            "content": content,
            "submolt": submolt,
        })
        return await self._auto_verify(response)

    async def upvote(self, post_id: str) -> dict[str, Any]:
        """POST /posts/{id}/upvote - Upvote a post."""
        return await self._post(f"/posts/{post_id}/upvote")

    async def mark_read_all(self) -> dict[str, Any]:
        """POST /notifications/read-all - Mark all notifications read."""
        return await self._post("/notifications/read-all")

    async def mark_read_by_post(self, post_id: str) -> dict[str, Any]:
        """POST /notifications/read-by-post/{id} - Mark notifications for a post read."""
        return await self._post(f"/notifications/read-by-post/{post_id}")


# ── CLI interface ────────────────────────────────────────────────────────

def _get_api(args: argparse.Namespace) -> MoltbookAPI:
    return MoltbookAPI.from_credentials(args.credentials)


async def cmd_home(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.get_home()
    print(json.dumps(result, indent=2))


async def cmd_feed(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.get_feed(limit=args.limit)
    print(json.dumps(result, indent=2))


async def cmd_notifications(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.get_notifications()
    print(json.dumps(result, indent=2))


async def cmd_comments(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.get_comments(args.post_id, sort=args.sort, limit=args.limit)
    print(json.dumps(result, indent=2))


async def cmd_comment(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.post_comment(args.post_id, args.content)
    print(json.dumps(result, indent=2))


async def cmd_post(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.create_post(args.title, args.content, submolt=args.submolt)
    print(json.dumps(result, indent=2))


async def cmd_upvote(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.upvote(args.post_id)
    print(json.dumps(result, indent=2))


async def cmd_mark_read(args: argparse.Namespace) -> None:
    api = _get_api(args)
    if args.post:
        result = await api.mark_read_by_post(args.post)
    else:
        result = await api.mark_read_all()
    print(json.dumps(result, indent=2))


async def cmd_stats(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.get_stats()
    print(json.dumps(result, indent=2))


async def cmd_verify(args: argparse.Namespace) -> None:
    """Debug the verification solver."""
    answer = solve_verification(args.challenge)
    if answer:
        print(json.dumps({"challenge": args.challenge, "answer": answer}))
    else:
        print(json.dumps({"challenge": args.challenge, "error": "Could not solve"}))
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Moltbook API CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--credentials",
        default="data/moltbook_credentials.json",
        help="Path to credentials JSON (default: data/moltbook_credentials.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # home
    sub.add_parser("home", help="Full dashboard")

    # feed
    p_feed = sub.add_parser("feed", help="Latest posts")
    p_feed.add_argument("--limit", type=int, default=15)

    # notifications
    sub.add_parser("notifications", help="All notifications")

    # comments
    p_comments = sub.add_parser("comments", help="Post comments")
    p_comments.add_argument("post_id")
    p_comments.add_argument("--sort", default="new", choices=["best", "new", "old"])
    p_comments.add_argument("--limit", type=int, default=20)

    # comment (write)
    p_comment = sub.add_parser("comment", help="Post a comment (auto-verifies)")
    p_comment.add_argument("post_id")
    p_comment.add_argument("content")

    # post (write)
    p_post = sub.add_parser("post", help="Create a post (auto-verifies)")
    p_post.add_argument("title")
    p_post.add_argument("content")
    p_post.add_argument("--submolt", default="general")

    # upvote
    p_upvote = sub.add_parser("upvote", help="Upvote a post")
    p_upvote.add_argument("post_id")

    # mark-read
    p_mark = sub.add_parser("mark-read", help="Mark notifications read")
    p_mark.add_argument("--post", help="Mark only for specific post ID")

    # stats
    sub.add_parser("stats", help="Agent profile stats")

    # verify (debug)
    p_verify = sub.add_parser("verify", help="Debug verification solver")
    p_verify.add_argument("challenge", help="Challenge text to solve")

    args = parser.parse_args()
    cmd_map = {
        "home": cmd_home,
        "feed": cmd_feed,
        "notifications": cmd_notifications,
        "comments": cmd_comments,
        "comment": cmd_comment,
        "post": cmd_post,
        "upvote": cmd_upvote,
        "mark-read": cmd_mark_read,
        "stats": cmd_stats,
        "verify": cmd_verify,
    }

    try:
        asyncio.run(cmd_map[args.command](args))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stdout, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
