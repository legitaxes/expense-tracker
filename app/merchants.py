"""Merchant-string normalisation for learned categories.

Bank and receipt merchant strings repeat with high fidelity but carry noise:
a country suffix on every row, per-transaction reference codes, and
outlet-specific detail. `merchant_stem` reduces them to a stable brand key so
one category correction covers every future charge from the same brand.
"""
from __future__ import annotations
import re

# How many leading parts make up the stem. Two keeps "ntuc fp" distinct from
# "ntuc fairprice" while still collapsing every NTUC FP outlet into one key.
STEM_TOKENS = 2

# Trailing locale noise. Deliberately not anchored on a word boundary at the
# start: Trust truncates long merchant names and glues the country straight on
# ("NTUC FairPrice App PaymenSINGAPORE SG").
_LOCALE_RE = re.compile(r"(singapore|s'pore)?\s*\b(sgp|sg)\s*$")

# A support phone number embedded in an overseas charge, e.g. the
# "866-579-7172" in "NETFLIX 866-579-7172 US". It has no letters, so it
# never alternates and the check below scores it 0 -- this rule is what
# catches it. A purely numeric token this long is never a brand name.
_NUMERIC_RE = re.compile(r"^[\d-]{6,}$")

# Threshold for the "generated code" check in _is_reference_code, counted
# by _count_alternations. Measured against real and realistic data: real
# merchant names top out at 3 alternations between letter-runs and
# digit-runs ("7-eleven24hr" -> 7 | eleven | 24 | hr, 3 switches), while
# generated per-transaction reference codes start at 4
# ("a-1b2345cdefghi" -> a | 1 | b | 2345 | cdefghi, 4 switches). A token
# needs 4 or more alternations to be a code, not a name.
_REF_MIN_ALTERNATIONS = 4

# Stripped from the stem's edges; "grab*" -> "grab".
_EDGE_PUNCT = "*.,-/&' "


def _count_alternations(token: str) -> int:
    """Count how many times the token switches between a run of letters
    and a run of digits, scanning left to right. Hyphens and any other
    non-alphanumeric characters are ignored entirely -- they neither start
    a run nor count as a switch. "a-1b2345cdefghi" collapses to the run
    sequence a / 1 / b / 2345 / cdefghi (letter, digit, letter, digit,
    letter), which is 4 switches."""
    run_kinds: list[str] = []
    for ch in token:
        if ch.isdigit():
            kind = "d"
        elif ch.isalpha():
            kind = "l"
        else:
            continue
        if not run_kinds or run_kinds[-1] != kind:
            run_kinds.append(kind)
    return max(len(run_kinds) - 1, 0)


def _is_reference_code(token: str) -> bool:
    """A token is noise -- not part of the brand name -- when it's a bare
    phone number (digits and hyphens only, no letters; see _NUMERIC_RE), or
    when it alternates between letter-runs and digit-runs
    _REF_MIN_ALTERNATIONS or more times (see _count_alternations). The
    reasoning: a generated per-transaction code interleaves letters and
    digits repeatedly, while a real brand name mixes them in at most a
    couple of runs (the brand itself, then a trailing store/outlet number
    or suffix). This checks only the number of letter/digit-run switches --
    nothing about token length, digit position, or any other property."""
    if _NUMERIC_RE.match(token):
        return True
    return _count_alternations(token) >= _REF_MIN_ALTERNATIONS


def merchant_stem(note: str) -> str | None:
    """Reduce a bank/receipt merchant string to a stable brand key.

    Returns None when there is nothing worth remembering (empty, all digits,
    or too short to be a distinctive merchant)."""
    text = " ".join(str(note or "").lower().split())
    text = _LOCALE_RE.sub("", text).strip()

    parts: list[str] = []
    for token in text.split():
        if _is_reference_code(token):
            continue
        # Split outlet detail off the brand ("fp-orchard" -> "fp", "orchard"), but
        # never when a piece would be a stub — that would reduce "7-eleven"
        # to "7", which collides with every other name starting in a digit.
        pieces = token.split("-")
        parts.extend(pieces if all(len(p) >= 2 for p in pieces) else [token])
        if len(parts) >= STEM_TOKENS:
            break

    stem = " ".join(parts[:STEM_TOKENS]).strip(_EDGE_PUNCT)
    if len(stem) < 3 or stem.isdigit():
        return None
    return stem
