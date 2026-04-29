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
TRAP_WORDS_SET = set(TRAP_WORDS)

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
ADD_WORDS = {
    "plus", "adds", "gains", "total", "combined", "increased",
    "accelerates", "faster", "increases",
}
SUB_WORDS = {
    "minus", "slows", "subtracts", "loses", "decreased", "reduces",
    "drops", "decelerates", "slower", "decreases",
    "lose", "lost",
}
MUL_WORDS = {"times", "product", "multiplied", "multiplies", "multiply", "impulse"}
DIV_WORDS = {"divided", "over", "split", "halved"}

# Physics keyword phrases that imply specific operations
PHYSICS_PHRASES: dict[str, str] = {
    "net force": "-",     # net force = force - resistance
    "impulse": "*",       # impulse = force × time
}

# Multi-word operator phrases (checked before single words)
MULTI_WORD_OPS: dict[str, list[str]] = {
    "*": ["scaled by", "multiplied by"],
    "/": ["divided by", "split by"],
    "+": ["speeds up", "adds up", "goes up", "moves up", "powers up", "ends up by", "up by"],
    "-": ["slows down", "goes down", "drops by", "falls by", "net force", "loses by", "reduced by", "decreases by", "dropped by"],
}

# Nouns that make a preceding number a descriptor, not an operand
# "one claw" = descriptor, "twenty three" = operand
# Mid-sentence correction markers — "adds thirty? noo wait sry: sixteen"
CORRECTION_MARKERS = {
    "wait", "sry", "sorry", "actually", "oops", "scratch",
    "noo", "err", "errr", "erm", "uhh", "hmm",
}

DESCRIPTOR_NOUNS = {
    "claw", "claws", "hand", "hands", "arm", "arms",
    "side", "sides", "leg", "legs", "eye", "eyes",
    "wing", "wings", "fin", "fins", "tail", "tails",
    "antenna", "antennae", "lobster", "lobsters",
    "shell", "shells",
}


def _log(msg: str) -> None:
    print(f"[moltbook_api] {msg}", file=sys.stderr, flush=True)


# Common number-word fragments that should be merged (bug #3)
# Maps (prefix, suffix) -> merged word for fragments that _merge_fragments
# might miss when embedded in longer token sequences.
_NUMBER_FRAGMENTS = {
    ("sev", "en"): "seven",
    ("eigh", "t"): "eight",
    ("ni", "ne"): "nine",
    ("el", "even"): "eleven",
    ("twe", "lve"): "twelve",
    ("thir", "teen"): "thirteen",
    ("four", "teen"): "fourteen",
    ("fif", "teen"): "fifteen",
    ("six", "teen"): "sixteen",
    ("seven", "teen"): "seventeen",
    ("eigh", "teen"): "eighteen",
    ("nine", "teen"): "nineteen",
    ("twen", "ty"): "twenty",
    ("thir", "ty"): "thirty",
    ("for", "ty"): "forty",
    ("fif", "ty"): "fifty",
    ("six", "ty"): "sixty",
    ("seven", "ty"): "seventy",
    ("eigh", "ty"): "eighty",
    ("nine", "ty"): "ninety",
}

# Non-number prefixes/suffixes that can get glued to number words (bug #4)
_GLUE_PREFIXES = [
    "is", "are", "was", "has", "the", "its", "our", "his", "her", "and",
    "but", "for", "not", "get", "got", "can", "may", "had", "did", "does",
    "force", "be", "of", "to", "at", "by", "in", "on", "or", "an", "if",
    "so", "do", "up", "it", "my", "no", "we", "he",
]
_GLUE_SUFFIXES = [
    "is", "are", "was", "has", "the", "its", "and", "but", "for", "not",
    "can", "may", "force", "per", "by", "in", "on", "or", "of", "to",
]

# All number words for glue-splitting detection
_ALL_NUMBER_WORDS = set(ONES.keys()) | set(TENS.keys()) | {"hundred"}

# ── Bug 3 (LOW): Common misspelling/obfuscation patterns for teen numbers ─
# Maps misspelled variants to correct number words
_TEEN_MISSPELLINGS: dict[str, str] = {
    "fourleen": "fourteen",
    "forteen": "fourteen",
    "forteeen": "fourteen",
    "fourten": "fourteen",
    "fiften": "fifteen",
    "fiveteen": "fifteen",
    "fifteeen": "fifteen",
    "threeteen": "thirteen",
    "thirten": "thirteen",
    "sixten": "sixteen",
    "seventeeen": "seventeen",
    "eighten": "eighteen",
    "ninteen": "nineteen",
    "nineten": "nineteen",
    "elevin": "eleven",
    "twelwe": "twelve",
    "twelf": "twelve",
}


def _fuzzy_match_number_word(word: str, require_first_char: bool = True) -> Optional[str]:
    """Fuzzy-match a mangled word against known number words.

    After deobfuscation+dedup, compound number second words can get mangled:
    'fIfEe' -> 'fife' (not 'five'), 'TrEeEe' -> 'tree' or 'treee' (not 'three').

    Strategy:
    1. Check exact match in known number words
    2. Check teen misspelling table
    3. Try edit-distance-1 matches against number words
    4. Try substring containment heuristics

    If require_first_char=True, only match targets whose first char matches the word's first char.
    This prevents phantom matches like 'terr' -> 'zero'.
    """
    if word in _ALL_NUMBER_WORDS:
        return word
    if word in _TEEN_MISSPELLINGS:
        return _TEEN_MISSPELLINGS[word]

    # Build candidates from ONES and TENS keys
    all_targets = list(ONES.keys()) + list(TENS.keys())

    # Try edit distance 1 (substitution, insertion, deletion)
    best_match: Optional[str] = None
    best_dist = 999
    for target in all_targets:
        if require_first_char and word and target and word[0] != target[0]:
            continue
        d = _edit_distance(word, target)
        if d < best_dist:
            best_dist = d
            best_match = target

    # Accept edit distance <= 1 only — distance 2 causes false positives
    # ("force" -> "forty", "reef" -> number words, etc.)
    max_dist = 1
    if best_dist <= max_dist and best_match:
        return best_match

    return None


def _edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        return _edit_distance(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1] + [0] * len(b)
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr[j + 1] = min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost)
        prev = curr
    return prev[len(b)]


def _split_glued_words(text: str) -> str:
    """Split words where non-number text is glued to number words (bug #4).

    'isthirty two' -> 'is thirty two'
    'forceis' -> 'force is'
    """
    tokens = text.split()
    result: list[str] = []
    for token in tokens:
        if token in _KNOWN_WORDS or token in _ALL_NUMBER_WORDS:
            result.append(token)
            continue
        split_done = False
        # Check if token starts with a known prefix glued to a number word
        for prefix in _GLUE_PREFIXES:
            if token.startswith(prefix) and len(token) > len(prefix):
                remainder = token[len(prefix):]
                if remainder in _ALL_NUMBER_WORDS or remainder in _KNOWN_WORDS:
                    result.append(prefix)
                    result.append(remainder)
                    split_done = True
                    break
        if split_done:
            continue
        # Check if token ends with a known suffix glued to a number word
        for suffix in _GLUE_SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix):
                remainder = token[:-len(suffix)]
                if remainder in _ALL_NUMBER_WORDS or remainder in _KNOWN_WORDS:
                    result.append(remainder)
                    result.append(suffix)
                    split_done = True
                    break
        if not split_done:
            result.append(token)
    return " ".join(result)


def _merge_number_fragments(text: str) -> str:
    """Merge fragmented number words that _merge_fragments may miss (bug #3).

    'sev en' -> 'seven', 'thir ty' -> 'thirty'
    """
    tokens = text.split()
    result: list[str] = []
    i = 0
    while i < len(tokens):
        if i + 1 < len(tokens):
            pair = (tokens[i], tokens[i + 1])
            if pair in _NUMBER_FRAGMENTS:
                result.append(_NUMBER_FRAGMENTS[pair])
                i += 2
                continue
        result.append(tokens[i])
        i += 1
    return " ".join(result)


def _normalize(text: str) -> str:
    """Normalize mangled challenge text before any parsing.

    Strips all non-alphabetic, non-digit, non-space characters, collapses
    spaces, lowercases, then merges fragmented tokens back into known words.

    "tW/eN tY" -> "twenty", "lOo.bS tTeRr" -> "lobster", "T hR Ee" -> "three"
    """
    # Preserve literal math operators when used as operators (surrounded by
    # spaces/digits), not when embedded in words (tW/eN = slash inside word)
    cleaned = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", " plus ", text)
    cleaned = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " minus ", cleaned)
    cleaned = re.sub(r"(?<=\d)\s*\*\s*(?=\d)", " times ", cleaned)
    cleaned = re.sub(r"(?<=\d)\s*/\s*(?=\d)", " divided ", cleaned)
    # Also preserve standalone operators between words (e.g. "twenty * fifteen")
    cleaned = re.sub(r"\s\+\s", " plus ", cleaned)
    cleaned = re.sub(r"\s-\s", " minus ", cleaned)
    cleaned = re.sub(r"\s\*\s", " times ", cleaned)
    cleaned = re.sub(r"\s/\s", " divided ", cleaned)
    # Keep only letters, digits, and spaces
    cleaned = re.sub(r"[^a-zA-Z0-9 ]", "", cleaned)
    # Collapse multiple spaces and lowercase
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    # Merge fragmented tokens into known words
    cleaned = _merge_fragments(cleaned)
    # Merge number-specific fragments (bug #3)
    cleaned = _merge_number_fragments(cleaned)
    # Split glued words (bug #4): 'isthirty' -> 'is thirty'
    cleaned = _split_glued_words(cleaned)
    return cleaned


# All words the solver needs to recognize (for fragment merging)
_KNOWN_WORDS = (
    set(ONES.keys())
    | set(TENS.keys())
    | TRAP_WORDS_SET
    | ADD_WORDS
    | SUB_WORDS
    | MUL_WORDS
    | DIV_WORDS
    | {"hundred"}
)


def _dedup_chars(word: str) -> Optional[str]:
    """Progressively collapse repeated consecutive characters until a known word emerges.

    "tweentyy" -> "twenty", "fiveee" -> "five", "acceelerates" -> "accelerates"
    """
    if word in _KNOWN_WORDS:
        return word

    # Find all positions with repeated consecutive chars
    repeat_positions: list[int] = []
    for j in range(len(word) - 1):
        if word[j] == word[j + 1]:
            repeat_positions.append(j + 1)  # Index of the duplicate

    # Only attempt dedup on words longer than the shortest known word they
    # could match — skip short words like "plus", "five" to avoid over-reduction.
    # Strategy: try removing chars that appear consecutively OR that create a
    # known word when removed from the end (suffix trimming).

    # 1. Consecutive-char dedup (safe, handles "fiveee" -> "five", "foouurrtteeen" -> "fourteen")
    candidates = {word}
    seen: set[str] = {word}
    for _ in range(8):
        next_candidates: set[str] = set()
        for candidate in candidates:
            for j in range(len(candidate) - 1):
                if candidate[j] == candidate[j + 1]:
                    reduced = candidate[:j] + candidate[j + 1 :]
                    if reduced in _KNOWN_WORDS:
                        return reduced
                    if reduced not in seen and len(reduced) >= 3:
                        seen.add(reduced)
                        next_candidates.add(reduced)
        candidates = next_candidates
        if not candidates:
            break

    # 1.5. Single-char deletion — try removing ANY one character (handles
    # non-consecutive insertions like "losoes" -> "loses", "fifve" -> "five")
    if len(word) >= 4:
        for j in range(len(word)):
            reduced = word[:j] + word[j + 1:]
            if reduced in _KNOWN_WORDS:
                return reduced

    # 2. Suffix/prefix trimming — remove chars from ends (handles "newtonss")
    # Don't strip meaningful short words as prefixes ("to"+"seven" must stay
    # separate so "from X to Y" pattern is preserved)
    _PRESERVE_PREFIXES = {
        "to", "at", "by", "in", "on", "up", "is", "it", "an", "or",
        "no", "if", "so", "do", "we", "he", "me", "my", "be", "of",
        "as", "am", "go",
    }
    for trim in range(1, min(4, len(word) - 2)):
        # Trim from end
        trimmed = word[:-trim]
        if trimmed in _KNOWN_WORDS:
            return trimmed
        # Trim from start (only if prefix is junk, not a real word)
        prefix_part = word[:trim]
        if prefix_part not in _PRESERVE_PREFIXES and prefix_part not in _KNOWN_WORDS:
            trimmed = word[trim:]
            if trimmed in _KNOWN_WORDS:
                return trimmed

    # 3. Fuzzy match against number words (Bug 1: handles mangled second words)
    # Only for words >= 3 chars — 2-char words produce false positives ("to"->"two")
    if len(word) >= 3:
        fuzzy = _fuzzy_match_number_word(word)
        if fuzzy:
            return fuzzy

    # 4. Check teen misspelling table directly
    if word in _TEEN_MISSPELLINGS:
        return _TEEN_MISSPELLINGS[word]

    return None


def _merge_fragments(text: str) -> str:
    """Greedily merge adjacent tokens into known words.

    Moltbook splits words with spaces: "twen ty" -> "twenty",
    "lob ster" -> "lobster", "th ree" -> "three".
    Also handles character duplication: "tweentyy" -> "twenty".
    """
    tokens = text.split()
    result: list[str] = []
    i = 0
    while i < len(tokens):
        merged = False
        # Try merging up to 4 adjacent tokens (handles extreme splits like "t h r e e")
        for span in range(min(5, len(tokens) - i), 1, -1):
            # Skip multi-token merges if any individual token is already known
            # ("five" + "cms" should not merge — "five" stands alone)
            if span > 1 and any(t in _KNOWN_WORDS for t in tokens[i : i + span]):
                continue
            candidate = "".join(tokens[i : i + span])
            if candidate in _KNOWN_WORDS:
                result.append(candidate)
                i += span
                merged = True
                break
            # Try dedup on the merged candidate
            deduped = _dedup_chars(candidate)
            if deduped:
                result.append(deduped)
                i += span
                merged = True
                break
        if not merged:
            # Try dedup on a single token
            deduped = _dedup_chars(tokens[i])
            if deduped:
                result.append(deduped)
            else:
                result.append(tokens[i])
            i += 1
    return " ".join(result)


def _remove_trap_words(text: str) -> str:
    """Remove trap words from already-normalized text."""
    cleaned = text
    for word in TRAP_WORDS:
        # Simple word boundary match on normalized (lowercase, clean) text
        cleaned = re.sub(rf"\b{word}\b", " ", cleaned)
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
        # Bug 3: Try fuzzy match for obfuscated single numbers
        fuzzy = _fuzzy_match_number_word(word)
        if fuzzy:
            if fuzzy in ONES:
                return float(ONES[fuzzy])
            if fuzzy in TENS:
                return float(TENS[fuzzy])
        return None

    if len(parts) == 2:
        w1, w2 = parts
        # Bug 1: fuzzy-match second word of compound numbers
        if w1 not in TENS:
            fw1 = _fuzzy_match_number_word(w1)
            if fw1:
                w1 = fw1
        if w2 not in ONES:
            fw2 = _fuzzy_match_number_word(w2)
            if fw2:
                w2 = fw2
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


def _detect_pressure_area(text: str) -> bool:
    """Detect pressure x area pattern (bug #5).

    'X per square centimeter over/across Y square centimeters' -> multiplication.
    Pattern: 'per' ... 'over|across|on' -> pressure * area = force.
    """
    lower = text.lower()
    if "per" in lower and re.search(r"\b(?:over|across|on)\b", lower):
        # Check for unit pattern: "per [square] <unit>" ... "over/across" ... "<unit>"
        if re.search(r"per\s+(?:square\s+)?\w+.*?\b(?:over|across|on)\b", lower):
            return True
    return False


def _is_separator_divided(text: str) -> bool:
    """Check if 'divided' is used as a separator, not an operator (bug #1).

    'divided and another lobster adds twelve' — 'divided' before 'and' without
    a number immediately after is a separator. The real operator is later.
    """
    lower = text.lower()
    words_list = lower.split()
    if "divided" not in words_list:
        return False
    # 'divided by' is always a real operator
    div_idx = words_list.index("divided")
    if div_idx + 1 < len(words_list) and words_list[div_idx + 1] == "by":
        return False
    # Check if there's an add/sub operator word AFTER 'divided'
    after_divided = " ".join(words_list[div_idx + 1:])
    after_words = set(after_divided.split())
    if after_words & ADD_WORDS or after_words & SUB_WORDS:
        # Check that 'divided' is NOT immediately followed by a number
        # If there's 'and' or non-number words between divided and next number,
        # it's a separator
        remaining = words_list[div_idx + 1:]
        for w in remaining[:3]:  # Check next few words
            if _parse_number_word(w) is not None:
                return False  # Number right after -> real division
            if w in ("and", "but", "then", "another", "also"):
                return True  # Separator word -> 'divided' is separator
        return True
    return False


def _detect_operator(text: str) -> Optional[str]:
    """Find the math operator in the challenge text.

    Checks multi-word phrases first, then single words.
    Checks mul/div before add/sub — "times" and "divided" are unambiguous
    operator words, while add/sub words (e.g. "total", "drops") can appear
    in non-math context.
    """
    lower = text.lower()

    # "from NUMBER to NUMBER" pattern = subtraction (e.g. "from twenty five to seven")
    if re.search(r"\bfrom\b\s+\w+(?:\s+\w+){0,3}\s+to\b", lower):
        _log("Detected 'from X to Y' pattern -> subtraction")
        return "-"

    # Bug #2: Physics keyword detection before normal operator extraction
    for phrase, op in PHYSICS_PHRASES.items():
        if phrase in lower:
            _log(f"Detected physics keyword '{phrase}' -> operator '{op}'")
            return op

    # Bug #5: Pressure x area pattern overrides normal operator detection
    if _detect_pressure_area(lower):
        _log("Detected pressure x area pattern -> multiplication")
        return "*"

    # Multi-word phrases first (mul/div before add/sub)
    for op in ["*", "/", "+", "-"]:
        for phrase in MULTI_WORD_OPS.get(op, []):
            if phrase in lower:
                return op

    # Bug #1: Check if 'divided' is a separator, not an operator
    separator_divided = _is_separator_divided(lower)

    # Single words (mul/div before add/sub)
    words = set(lower.split())
    if words & MUL_WORDS:
        return "*"
    if not separator_divided and (words & DIV_WORDS):
        return "/"
    if words & ADD_WORDS:
        return "+"
    if words & SUB_WORDS:
        return "-"
    # If divided was a separator and we found no other operator, fall through
    if separator_divided and (words & DIV_WORDS):
        # Last resort: maybe it really is division after all
        return "/"
    return None


def _filter_descriptor_numbers(numbers: list[float], text: str) -> list[float]:
    """Remove numbers that are descriptors rather than operands.

    "twenty three newtons with one claw plus seventeen"
    -> "one" is followed by "claw" (descriptor noun), so exclude 1.0
    -> returns [23.0, 17.0]

    Also handles "one claw exerts thirty five" with exactly 2 numbers.
    """
    if len(numbers) < 2:
        return numbers

    words = text.lower().split()
    # Build a set of number values that appear before descriptor nouns
    descriptor_values: set[float] = set()
    for i, word in enumerate(words):
        if i + 1 < len(words) and words[i + 1] in DESCRIPTOR_NOUNS:
            parsed = _parse_number_word(word)
            if parsed is not None:
                descriptor_values.add(parsed)

    if not descriptor_values:
        return numbers

    # Remove descriptor numbers, but keep at least 2
    filtered = [n for n in numbers if n not in descriptor_values]
    if len(filtered) >= 2:
        return filtered
    return numbers


def solve_verification(challenge_text: str) -> Optional[str]:
    """Solve a Moltbook verification challenge.

    Algorithm:
    1. Remove trap words
    2. Extract number words/digits
    3. Detect operator
    4. Calculate result to 2 decimal places

    Returns the answer as string (e.g. "42.00") or None if unsolvable.
    """
    normalized = _normalize(challenge_text)
    _log(f"After normalization: {normalized}")
    cleaned = _remove_trap_words(normalized)
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

    # Collect ALL number candidates (digits + words), then filter
    if digit_matches and not word_matches:
        numbers = [float(d) for d in digit_matches]
    elif word_matches and not digit_matches:
        for wm in word_matches:
            parsed = _parse_number_word(wm)
            if parsed is not None:
                numbers.append(parsed)
    elif digit_matches and word_matches:
        # Mix: collect all, deduplicate
        for d in digit_matches:
            numbers.append(float(d))
        for wm in word_matches:
            parsed = _parse_number_word(wm)
            if parsed is not None and parsed not in numbers:
                numbers.append(parsed)
    else:
        # Last resort: scan all words
        for word in cleaned.lower().split():
            parsed = _parse_number_word(word)
            if parsed is not None and parsed not in numbers:
                numbers.append(parsed)

    # Filter out descriptor numbers ("one claw", "two hands")
    if len(numbers) >= 2:
        numbers = _filter_descriptor_numbers(numbers, cleaned)
        _log(f"After descriptor filter: {numbers}")

    # Handle mid-sentence corrections ("adds thirty? wait sry: sixteen")
    # If correction markers present and 3+ numbers, the middle number(s)
    # were "corrected" — use first and last.
    if len(numbers) >= 3:
        words_set = set(cleaned.lower().split())
        if words_set & CORRECTION_MARKERS:
            _log(
                f"Correction markers detected in text, "
                f"using first ({numbers[0]}) and last ({numbers[-1]})"
            )
            numbers = [numbers[0], numbers[-1]]

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
        # Bug #6: Log full details when verification is rejected for debugging
        is_success = verify_result.get("success") or verify_result.get("verified")
        if not is_success:
            _log(
                f"VERIFICATION REJECTED — challenge: {challenge!r} | "
                f"computed answer: {answer} | "
                f"server response: {json.dumps(verify_result)}"
            )
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

    async def post_comment(self, post_id: str, content: str, parent_id: str | None = None) -> dict[str, Any]:
        """POST /posts/{id}/comments - Post a comment, auto-verify."""
        payload: dict[str, str] = {"content": content}
        if parent_id:
            payload["parent_id"] = parent_id
        response = await self._post(f"/posts/{post_id}/comments", payload)
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

    async def follow_agent(self, agent_name: str) -> dict[str, Any]:
        """POST /agents/{name}/follow - Follow an agent by name."""
        return await self._post(f"/agents/{agent_name}/follow", {})


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
    result = await api.post_comment(args.post_id, args.content, parent_id=getattr(args, "parent", None))
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


async def cmd_follow(args: argparse.Namespace) -> None:
    api = _get_api(args)
    result = await api.follow_agent(args.agent_name)
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
    p_comment.add_argument("--parent", help="Parent comment ID for threaded replies")

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

    # follow
    p_follow = sub.add_parser("follow", help="Follow an agent")
    p_follow.add_argument("agent_name", help="Agent name to follow")

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
        "follow": cmd_follow,
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
