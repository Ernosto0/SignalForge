"""Verbatim quote verification (plan §7.1).

The model's quote is located in the source text after Turkish-aware normalisation
(:func:`signalforge.text.normalize_with_map`). An exact match is ``exact``; otherwise a rapidfuzz
partial-ratio alignment at or above the configured ratio is ``fuzzy`` (flagged); anything else is
dropped. The stored quote is always the matched span of the *source*, never the model's copy, so
every stored excerpt is verbatim by construction.
"""

from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz

from signalforge.text import normalize_with_map

Verified = Literal["exact", "fuzzy"]


@dataclass(frozen=True)
class QuoteMatch:
    verified: Verified
    char_start: int  # offsets into the source text
    char_end: int
    quote: str  # source[char_start:char_end]
    score: float  # 100 for exact matches


def _expand_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a fuzzy span so it does not start or end in the middle of a word."""
    while start > 0 and text[start - 1].isalnum() and text[start].isalnum():
        start -= 1
    while end < len(text) and text[end - 1].isalnum() and text[end].isalnum():
        end += 1
    return start, end


def verify_quote(quote: str, source: str, fuzzy_min_ratio: float) -> QuoteMatch | None:
    """Where ``quote`` occurs in ``source``, or ``None`` when it does not (closely enough)."""
    needle, _, _ = normalize_with_map(quote)
    if not needle:
        return None
    haystack, starts, ends = normalize_with_map(source)
    if len(needle) > len(haystack):
        return None

    if (at := haystack.find(needle)) >= 0:
        verified: Verified = "exact"
        score = 100.0
        a, b = at, at + len(needle)
    else:
        alignment = fuzz.partial_ratio_alignment(needle, haystack, score_cutoff=fuzzy_min_ratio)
        if alignment is None or alignment.dest_end <= alignment.dest_start:
            return None
        verified, score = "fuzzy", alignment.score
        a, b = alignment.dest_start, alignment.dest_end

    start, end = starts[a], ends[b - 1]
    if verified == "fuzzy":
        start, end = _expand_to_words(source, start, end)
    return QuoteMatch(verified, start, end, source[start:end], round(score, 1))
