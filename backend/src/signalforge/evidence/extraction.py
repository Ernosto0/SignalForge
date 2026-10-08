"""Signal extraction from one document, shared by every stage that reads pages (plan §6, §7).

:func:`extract_document` splits a document into chunks, asks the fast model for signals per chunk
(prompts/extract.md) and keeps only signals whose quote is found in the chunk
(evidence/quotes.py). Kept excerpts store the matched source span, mapped back to offsets in the
whole text. Authors are reduced to salted hashes here; raw names never leave this module.
:func:`write_signals` persists kept signals as Excerpt → Signal → fact Claim.

Used by ``extract``, and by ``verify`` on the pages its loop fetches (with a problem context and a
stance per signal, see pipeline/stages/verify.py). :func:`write_excerpt` also serves
``competitors``, whose facts have no Signal.
"""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from signalforge.config import ExtractDefaults
from signalforge.db.models import Excerpt, Signal
from signalforge.domain.evidence import ExtractedSignal, ExtractionBatch
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.claims import add_fact
from signalforge.evidence.fit import fit_facts
from signalforge.evidence.independence import author_hash
from signalforge.evidence.quotes import QuoteMatch, verify_quote
from signalforge.packs import MarketPack
from signalforge.providers.llm import BudgetExceeded, LLMError
from signalforge.text import tr_casefold


@dataclass(frozen=True)
class SourceText:
    """A document's text as the model sees it, with what the prompt says about the document."""

    document_id: int
    domain: str
    title: str | None
    source_category: str | None
    quality_tier: str | None
    triage_label: str | None
    source: str  # text | snippet
    text: str
    page_author: str | None = None


@dataclass(frozen=True)
class KeptSignal:
    document_id: int
    source: str
    match: QuoteMatch
    signal: ExtractedSignal
    author_hash: str | None
    submarket: str | None  # canonical plan submarket name


@dataclass
class DocumentSignals:
    signals: list[KeptSignal]
    notes: Counter[str] = field(default_factory=Counter)


# Proposes signals for one prompt input: -> (signals, cache_hit).
ExtractFn = Callable[[str], tuple[ExtractionBatch, bool]]


def chunk_text(text: str, cfg: ExtractDefaults) -> list[tuple[int, str]]:
    """``(offset, chunk)`` pairs covering ``text``, cut at paragraph or sentence ends if possible.

    At most ``max_chunks_per_doc`` chunks; the rest of a very long page is not read.
    """
    size, overlap = cfg.chunk_chars, cfg.chunk_overlap_chars
    chunks: list[tuple[int, str]] = []
    start = 0
    while start < len(text) and len(chunks) < cfg.max_chunks_per_doc:
        end = min(start + size, len(text))
        if end < len(text):
            floor = start + size * 3 // 5
            for sep in ("\n", ". ", " "):
                cut = text.rfind(sep, floor, end)
                if cut > 0:
                    end = cut + len(sep)
                    break
        chunks.append((start, text[start:end]))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def build_input(
    plan: ResearchPlan,
    pack: MarketPack,
    cfg: ExtractDefaults,
    doc: SourceText,
    chunk: str,
    part: tuple[int, int],
) -> str:
    """Prompt input: the shared context first (provider prefix cache), the page text last."""
    request = plan.request
    industry = pack.industries.get(request.industry)
    context = {
        "market": {"country": pack.country, "language": pack.language},
        "research": {
            "industry": request.industry,
            "industry_local": industry.name_tr if industry else None,
            "submarkets": [{"name": s.name, "name_local": s.name_tr} for s in plan.submarkets],
            "problem_area": request.problem_area,
            "target_customer": request.target_customer,
            "business_model": request.business_model,
            "avoid": plan.avoid,
        },
        "max_signals": cfg.max_signals_per_chunk,
    }
    meta = {
        "domain": doc.domain,
        "source": (
            f"{doc.source_category}, {doc.quality_tier} tier" if doc.source_category else "unlisted"
        ),
        "triage_label": doc.triage_label,
        "title": doc.title or "",
        "text_kind": "search snippet only" if doc.source == "snippet" else "page text",
        "part": f"{part[0]} of {part[1]}",
    }
    dump = json.dumps
    return (
        f"# Context\n{dump(context, ensure_ascii=False, indent=1)}\n\n"
        f"# Document\n{dump(meta, ensure_ascii=False, indent=1)}\n\n"
        f"# Text\n{chunk}"
    )


def submarket_lookup(plan: ResearchPlan) -> dict[str, str]:
    """Turkish-aware casefolded submarket name (English or local) -> the plan's ``name``."""
    lookup: dict[str, str] = {}
    for s in plan.submarkets:
        for alias in (s.name, s.name_tr):
            if alias:
                lookup.setdefault(tr_casefold(alias).strip(), s.name)
    return lookup


def _overlaps(a: QuoteMatch, b: QuoteMatch) -> bool:
    """True when two spans share more than half of the shorter one."""
    shared = min(a.char_end, b.char_end) - max(a.char_start, b.char_start)
    shorter = min(a.char_end - a.char_start, b.char_end - b.char_start)
    return shared > shorter / 2


def extract_document(
    doc: SourceText,
    plan: ResearchPlan,
    pack: MarketPack,
    cfg: ExtractDefaults,
    propose: ExtractFn,
    salt: str,
) -> DocumentSignals:
    """The verified signals of one document, with counts for the stage metrics.

    A failed call (e.g. output cut off) loses only its chunk; an exceeded budget stops the stage.
    """
    notes: Counter[str] = Counter()
    submarkets = submarket_lookup(plan)
    kept: list[KeptSignal] = []
    chunks = chunk_text(doc.text, cfg)
    for i, (offset, chunk) in enumerate(chunks, 1):
        notes["llm_calls"] += 1
        try:
            batch, hit = propose(build_input(plan, pack, cfg, doc, chunk, (i, len(chunks))))
        except BudgetExceeded:
            raise
        except LLMError:
            notes["llm_errors"] += 1
            continue
        notes["llm_cache_hits"] += hit
        for sig in batch.signals[: cfg.max_signals_per_chunk]:
            notes["quotes_proposed"] += 1
            match = verify_quote(sig.quote, chunk, cfg.fuzzy_min_ratio)
            if match is None:
                notes["quotes_unverified"] += 1
                continue
            notes[f"quotes_{match.verified}"] += 1
            if not cfg.min_quote_chars <= len(match.quote) <= cfg.max_quote_chars:
                notes["dropped_length"] += 1
                continue
            match = QuoteMatch(
                match.verified,
                match.char_start + offset,
                match.char_end + offset,
                match.quote,
                match.score,
            )
            if any(_overlaps(match, other.match) for other in kept):
                notes["dropped_overlap"] += 1
                continue
            submarket = None
            if sig.submarket:
                submarket = submarkets.get(tr_casefold(sig.submarket).strip())
                notes["submarket_unmatched"] += submarket is None
            kept.append(
                KeptSignal(
                    document_id=doc.document_id,
                    source=doc.source,
                    match=match,
                    signal=sig,
                    author_hash=author_hash(sig.author or doc.page_author, salt),
                    submarket=submarket,
                )
            )
    return DocumentSignals(kept, notes)


def write_excerpt(
    session: Session,
    run_id: int,
    document_id: int,
    source: str,
    match: QuoteMatch,
    translation: str | None,
    *,
    stage: str,
    author_hash: str | None = None,
) -> Excerpt:
    """Store one verified quote (the matched source span) and flush, so it has its id."""
    excerpt = Excerpt(
        run_id=run_id,
        document_id=document_id,
        quote=match.quote,
        translation=translation,
        char_start=match.char_start,
        char_end=match.char_end,
        source=source,
        verified=match.verified,
        author_hash=author_hash,
        stage=stage,
    )
    session.add(excerpt)
    session.flush()
    return excerpt


def write_signals(
    session: Session,
    run_id: int,
    kept: list[KeptSignal],
    *,
    stage: str,
    meta: Callable[[KeptSignal], dict[str, Any]] | None = None,
) -> list[tuple[int, int]]:
    """Store each kept signal as Excerpt → Signal → fact Claim; returns ``(signal id, claim id)``.

    The fact's statement is the signal's statement, supported by exactly its one excerpt.
    ``Signal.meta`` keeps the submarket and the software-fit facts (``fit``, evidence/fit.py);
    ``meta`` adds stage-specific keys (e.g. verify's ``counter``).
    """
    written = []
    for item in kept:
        sig = item.signal
        excerpt = write_excerpt(
            session,
            run_id,
            item.document_id,
            item.source,
            item.match,
            sig.translation,
            stage=stage,
            author_hash=item.author_hash,
        )
        signal = Signal(
            run_id=run_id,
            excerpt_id=excerpt.id,
            type=sig.type,
            actor=sig.actor,
            workflow=sig.workflow,
            statement=sig.statement,
            first_hand=sig.first_hand,
            meta=({"submarket": item.submarket} if item.submarket else {})
            | {"fit": fit_facts(sig)}
            | (meta(item) if meta else {}),
        )
        session.add(signal)
        claim = add_fact(session, run_id, sig.statement, [excerpt.id], stage=stage)
        written.append((signal.id, claim.id))
    return written
