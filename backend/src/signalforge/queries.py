"""Search-query text rules shared by ``query_gen`` and the M4 loops (agents/loop.py).

:func:`clean_query` normalises a model-written query: a ``site:`` in the text becomes the source
hint, a hint survives only for an offered registry domain, any other search operator drops the
query, quotes are balanced and limited, and the word count is bounded. :class:`Deduper` rejects
Turkish-aware near-duplicates per source hint.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from rapidfuzz import fuzz

from signalforge.config import QueryGenDefaults
from signalforge.text import collapse_whitespace, match_tokens, unify_punctuation

_SITE = re.compile(r"(?<!\S)site:(\S+)", re.IGNORECASE)
# Anything else Google treats as an operator: exclusions, other `name:value` operators, OR/AND.
_OPERATOR = re.compile(r"(?<!\S)(?:-\S|[A-Za-z]+:\S|OR(?!\S)|AND(?!\S)|\|)")
_QUOTED = re.compile(r'"([^"]*)"')


def _domain(hint: str) -> str:
    hint = re.sub(r"^[a-z]+://", "", hint.strip().lower()).removeprefix("www.")
    return hint.split("/")[0]


def _limit_quotes(text: str, max_quoted: int, notes: Counter[str]) -> str:
    kept = 0

    def unquote_extra(m: re.Match[str]) -> str:
        nonlocal kept
        phrase = m.group(1).strip()
        if phrase and kept < max_quoted:
            kept += 1
            return f'"{phrase}"'
        notes["fixed_extra_quotes"] += 1
        return f" {phrase} "

    return _QUOTED.sub(unquote_extra, text)


def _offered(domain: str, allowed: Iterable[str]) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in allowed)


def clean_query(
    text: str,
    hint: str | None,
    allowed_hints: Iterable[str],
    cfg: QueryGenDefaults,
    notes: Counter[str],
) -> tuple[str, str | None] | None:
    """Normalise one draft to ``(text, source_hint)``, or ``None`` if it must be dropped.

    A source hint survives only if it is one of the registry domains offered for the query's
    intent (or a subdomain of one); anything else is removed and the query searches the open web.
    Every fix and drop is counted in ``notes`` (reported as StageRun metrics).
    """
    text = collapse_whitespace(unify_punctuation(text))
    if sites := _SITE.findall(text):
        hint = hint or sites[0]
        text = collapse_whitespace(_SITE.sub(" ", text))
        notes["fixed_site_operator_in_text"] += 1
    if hint:
        hint = _domain(hint)
        if not _offered(hint, allowed_hints):
            notes["fixed_unoffered_source_hint"] += 1
            hint = None
    hint = hint or None
    if _OPERATOR.search(text):
        notes["dropped_operator"] += 1
        return None
    if text.count('"') % 2:
        text = text.replace('"', "")
        notes["fixed_unbalanced_quotes"] += 1
    text = collapse_whitespace(_limit_quotes(text, cfg.max_quoted_phrases, notes))
    if not cfg.min_words <= len(match_tokens(text)) <= cfg.max_words:
        notes["dropped_length"] += 1
        return None
    return text, hint


class Deduper:
    """Duplicate check per source hint: same words in any order or near-identical wording.

    Comparison is on Turkish-casefolded word tokens, so case, quotes, punctuation and I/ı/İ/i
    differences never make two queries distinct.
    """

    def __init__(self, ratio: float) -> None:
        self._ratio = ratio
        self._seen: dict[str | None, list[str]] = defaultdict(list)

    def add(self, text: str, hint: str | None) -> bool:
        norm = " ".join(match_tokens(text))
        if any(fuzz.token_sort_ratio(norm, seen) >= self._ratio for seen in self._seen[hint]):
            return False
        self._seen[hint].append(norm)
        return True
