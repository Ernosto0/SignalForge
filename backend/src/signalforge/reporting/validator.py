"""Citation validator for report bullets (plan §7.4; agent-modules.md §9). Pure: it works on a
section's table of claim views, so it checks a stored ``report.json`` as well as a draft.

Rules, each reported with the bullet's path:

1. ``uncited_fact``: no claim ids outside ``UNCITED_ALLOWED`` (inside it, only a recommendation).
2. ``unknown_citation``: an id outside the section's table, or a fact that failed entailment.
3. ``unsupported_specific``: a number or proper noun the cited claims (statements, quotes,
   translations), the section's extra sources and the request context do not contain.
4. ``kind_mismatch``: the bullet's kind does not match what it cites, or it cites a hypothesis or an
   unsourced assumption without a hedge.
5. ``recommendation_outside_allowed``.

Numbers match in Turkish and English formats (``1.234,56`` / ``1,234.56``), ignoring currency
symbols and ``%``; a rounded bullet number (``$153`` for 152.93) matches within 1% of the source.
Number words and unnamed entities are not checked.
"""

import re
from dataclasses import dataclass

from signalforge.domain.plan import ResearchRequest
from signalforge.evidence.entailment import failed
from signalforge.reporting.schema import SUMMARY, UNCITED_ALLOWED, Bullet, ClaimView, Report
from signalforge.text import match_tokens, tr_casefold

NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
RANGE_PERCENT = re.compile(r"[–—-]\s*\d[\d.,]*\s*%")  # the first number of "15–40%"
WORD = re.compile(r"[^\W\d_][^\W_]*")
TOLERANCE = 0.01  # relative difference allowed for a rounded bullet number
SENTENCE_END = (".", "!", "?", ":")
# Capitalised words that are not names: pronouns, calendar words, common acronyms.
_STOP_WORDS = """
I We You They It He She The This That These Those A An In On At For To Of And Or But If
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May
June July August September October November December Turkish Turkey English
ERP API SaaS SMB SME SMEs KPI MVP USD TRY TL EUR OCR EDI GPS VAT KDV B2B B2C CRM WMS TMS
SMS PDF CSV UI AI ID
"""
STOPLIST = frozenset(tr_casefold(w) for w in _STOP_WORDS.split())


@dataclass(frozen=True)
class BulletError:
    path: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule}: {self.message}"


# --- numbers --------------------------------------------------------------------------------


def number_values(token: str) -> list[tuple[float, int]]:
    """Possible (value, decimals) readings of a number token in Turkish or English format."""
    seps = [c for c in token if c in ".,"]
    if not seps:
        return [(float(token), 0)]
    if "." in seps and "," in seps:
        decimal = token[max(token.rfind("."), token.rfind(",")) :][0]
        other = "." if decimal == "," else ","
        whole, _, frac = token.replace(other, "").partition(decimal)
        return [(float(f"{whole}.{frac}"), len(frac))]
    sep = seps[0]
    if len(seps) > 1:  # 1.234.567: grouping
        return [(float(token.replace(sep, "")), 0)]
    whole, _, frac = token.partition(sep)
    readings = [(float(f"{whole}.{frac}"), len(frac))]
    if len(frac) == 3 and whole != "0" and len(whole) <= 3:  # 1.234 / 1,234: grouping too
        readings.append((float(whole + frac), 0))
    return readings


def source_numbers(text: str) -> list[float]:
    return [v for m in NUMBER.findall(text) for v, _ in number_values(m)]


def _close(bullet: float, decimals: int, source: float) -> bool:
    if abs(bullet - source) < 1e-9:
        return True
    if source == 0:
        return False
    return round(source, decimals) == bullet and abs(bullet - source) / abs(source) <= TOLERANCE


def number_supported(text: str, sources: list[float]) -> list[str]:
    """Numbers in ``text`` that no source number matches."""
    missing = []
    for m in NUMBER.finditer(text):
        before, after = text[: m.start()].rstrip(), text[m.end() :].lstrip()
        percent = after.startswith("%") or before.endswith("%") or bool(RANGE_PERCENT.match(after))
        readings = number_values(m.group())
        ok = any(
            _close(v, d, s) or (percent and _close(v, d, s * 100))
            for v, d in readings
            for s in sources
        )
        if not ok:
            missing.append(m.group())
    return missing


# --- proper nouns ---------------------------------------------------------------------------


def proper_nouns(text: str) -> list[str]:
    """Capitalised words that are not at the start of a sentence."""
    out = []
    for m in WORD.finditer(text):
        word = m.group()
        if not word[0].isupper():
            continue
        before = text[: m.start()].rstrip(" \t\n\"'([“‘•-*#")
        if not before or before.endswith(SENTENCE_END):
            continue
        out.append(word)
    return out


def names_unsupported(text: str, allowed: set[str]) -> list[str]:
    return sorted({w for w in proper_nouns(text) if tr_casefold(w) not in allowed | STOPLIST})


# --- rules ----------------------------------------------------------------------------------


def claim_text(c: ClaimView) -> str:
    """What a claim states, for matching. An assumption keeps ``name = range unit`` and drops its
    rationale, which cites another prompt's local claim numbers."""
    parts = [c.statement.partition(": ")[0] if c.kind == "assumption" else c.statement]
    for e in c.excerpts:
        parts += [e.quote, e.translation or ""]
    return "\n".join(parts)


def _hedged(text: str, hedges: list[str]) -> bool:
    folded = tr_casefold(text)
    return any(re.search(rf"(?<!\w){re.escape(tr_casefold(h))}(?!\w)", folded) for h in hedges)


def _needs_hedge(c: ClaimView) -> bool:
    return c.kind == "hypothesis" or (c.kind == "assumption" and c.meta.get("sourced") is False)


def validate_bullet(
    path: str,
    section: str,
    bullet: Bullet,
    table: set[int],
    extra: list[str],
    claims: dict[int, ClaimView],
    context: list[str],
    hedges: list[str],
) -> list[BulletError]:
    errors: list[BulletError] = []

    def err(rule: str, message: str) -> None:
        errors.append(BulletError(path, rule, message))

    cited = bullet.claim_ids
    if not cited and (section not in UNCITED_ALLOWED or bullet.kind != "recommendation"):
        err("uncited_fact", "no claim ids" if section not in UNCITED_ALLOWED else
            "an uncited bullet must be a recommendation")  # fmt: skip
    if bullet.kind == "recommendation" and section not in UNCITED_ALLOWED:
        err("recommendation_outside_allowed", f"recommendations are not allowed in {section}")
    known: list[ClaimView] = []
    for i in dict.fromkeys(cited):
        c = claims.get(i)
        if i not in table or c is None:
            err("unknown_citation", f"claim {i} is not in this section's claim table")
        elif failed(c.entailment):
            err("unknown_citation", f"claim {i} failed entailment ({c.entailment})")
        else:
            known.append(c)

    # Rule 3: an uncited recommendation is checked against the whole table.
    sources = known if cited else [claims[i] for i in table if i in claims]
    allowed_text = "\n".join([*(claim_text(c) for c in sources), *extra, *context])
    for n in number_supported(bullet.text, source_numbers(allowed_text)):
        err("unsupported_specific", f"number {n!r} is not in the cited claims")
    allowed_words = set(match_tokens(allowed_text))
    for w in names_unsupported(bullet.text, allowed_words):
        err("unsupported_specific", f"name {w!r} is not in the cited claims")

    kinds = {c.kind for c in known}
    if bullet.kind == "fact" and cited and "fact" not in kinds:
        err("kind_mismatch", "a fact bullet must cite at least one fact claim")
    elif bullet.kind == "inference" and cited and not kinds & {"fact", "inference"}:
        err("kind_mismatch", "an inference bullet must cite a fact or inference claim")
    elif bullet.kind == "assumption" and cited and "assumption" not in kinds:
        err("kind_mismatch", "an assumption bullet must cite an assumption claim")
    soft = [c for c in known if _needs_hedge(c)]
    if (bullet.kind == "hypothesis" or (soft and bullet.kind != "recommendation")) and not _hedged(
        bullet.text, hedges
    ):
        what = "cites a hypothesis or unsourced assumption" if soft else "is a hypothesis"
        err("kind_mismatch", f"bullet {what} without a hedge ({', '.join(hedges[:4])}, …)")
    return errors


def validate_section(
    prefix: str,
    section: str,
    bullets: list[Bullet],
    table: list[int],
    extra: list[str],
    claims: dict[int, ClaimView],
    context: list[str],
    hedges: list[str],
) -> list[BulletError]:
    ids = set(table)
    return [
        e
        for n, b in enumerate(bullets)
        for e in validate_bullet(
            f"{prefix}.bullets[{n}]", section, b, ids, extra, claims, context, hedges
        )
    ]


def request_context(request: ResearchRequest) -> list[str]:
    return [request.country, request.industry]


def validate_report(report: Report, hedges: list[str]) -> list[BulletError]:
    """Every bullet of a stored report against its own section's table."""
    claims = {c.id: c for c in report.claims}
    context = request_context(report.request)
    errors = validate_section(
        "summary", SUMMARY, report.summary, report.summary_table, report.summary_extra, claims,
        context, hedges,
    )  # fmt: skip
    for o, opp in enumerate(report.opportunities):
        for s in opp.sections:
            errors += validate_section(
                f"opportunities[{o}].sections[{s.key}]", s.key, s.bullets, s.table, s.extra,
                claims, context, hedges,
            )  # fmt: skip
    return errors
