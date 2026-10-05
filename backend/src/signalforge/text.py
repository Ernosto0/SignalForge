"""Turkish-aware text normalisation for matching (plan §7).

Used by query dedupe now and by quote verification in M3. Python's ``str.lower()`` is wrong for
Turkish (``"İ".lower()`` adds a combining dot, ``"I".lower()`` gives ``"i"`` instead of ``"ı"``),
so matching treats ``I/ı/İ/i`` as one class instead of trying to case-map them correctly.
"""

import re
import unicodedata

_PUNCTUATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "–": "-",
        "—": "-",
    }
)
_DOTTED_I = str.maketrans({"İ": "i", "I": "i", "ı": "i"})
_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"\w+")


def unify_punctuation(text: str) -> str:
    """NFKC plus typographic quotes/dashes mapped to their ASCII forms."""
    return unicodedata.normalize("NFKC", text).translate(_PUNCTUATION)


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def tr_casefold(text: str) -> str:
    """Casefold for matching, with ``I/ı/İ/i`` collapsed to ``i``."""
    return unicodedata.normalize("NFKC", text).translate(_DOTTED_I).casefold()


_COMBINING_DOT = "̇"  # left over from "İ".lower() / decomposed İ; dropped like the dot itself


def normalize_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """Matching form of ``text`` plus, per output character, the original span it came from.

    Same rules as :func:`tr_casefold` + :func:`unify_punctuation` + :func:`collapse_whitespace`,
    applied per character cluster (a base character with its combining marks, so composed and
    decomposed forms agree). Format characters (soft hyphen, zero-width space) are dropped.
    Returns ``(normalized, starts, ends)``: normalized character ``k`` came from
    ``text[starts[k]:ends[k]]``, so a match ``normalized[a:b]`` is ``text[starts[a]:ends[b - 1]]``.
    """
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and unicodedata.combining(text[j]):
            j += 1
        cluster = text[i:j]
        if cluster.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                starts.append(i)
                ends.append(j)
        elif unicodedata.category(cluster[0]) != "Cf":
            norm = tr_casefold(unify_punctuation(cluster)).replace(_COMBINING_DOT, "")
            for ch in collapse_whitespace(norm) or norm.strip():
                out.append(ch)
                starts.append(i)
                ends.append(j)
        i = j
    if out and out[-1] == " ":
        out.pop()
        starts.pop()
        ends.pop()
    return "".join(out), starts, ends


def match_tokens(text: str) -> list[str]:
    """Casefolded word tokens, punctuation and quotes dropped."""
    return _WORD.findall(tr_casefold(text))


# Very frequent function words; enough to tell Turkish from English pages when <html lang> is
# missing or wrong (many Turkish sites declare lang="en" from their theme).
_STOPWORDS = {
    "tr": {"ve", "bir", "bu", "için", "ile", "da", "de", "çok", "ama", "gibi", "olarak", "daha"},
    "en": {"the", "and", "of", "to", "in", "is", "for", "that", "with", "on", "are", "this"},
}


def guess_language(text: str) -> str | None:
    """``tr`` / ``en`` by stopword share, or ``None`` when neither is clearly present."""
    words = _WORD.findall(tr_casefold(text[:20000]))
    if len(words) < 20:
        return None
    counts = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORDS.items()}
    lang, hits = max(counts.items(), key=lambda kv: kv[1])
    return lang if hits / len(words) >= 0.03 else None
