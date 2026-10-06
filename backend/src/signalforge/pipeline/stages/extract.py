"""extract stage (plan §4, §6, §7): documents → verified excerpts, typed signals, fact claims.

One representative per duplicate group (dedupe stage) is read; the rest add nothing new. Page text
comes from the page cache; snippet-only documents use their SERP title + snippet. Each document goes
through :func:`signalforge.evidence.extraction.extract_document` (chunking, fast-model proposals,
quote verification); unverifiable quotes are dropped and counted. Every kept signal is stored with
its excerpt and one ``fact`` claim supported by that excerpt. Authors are stored only as salted
hashes; documents sharing one form ``same_author`` independence groups, and documents quoting the
same passage (one complaint shown on several listing pages) form ``same_quote`` groups.
"""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select

from signalforge.config import ExtractDefaults
from signalforge.db.models import Document, Excerpt, IndependenceGroup, UrlCandidate
from signalforge.domain.evidence import ExtractionBatch
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.claims import delete_stage_claims
from signalforge.evidence.dedup import DuplicateGroup
from signalforge.evidence.extraction import (
    ExtractFn,
    KeptSignal,
    SourceText,
    extract_document,
    write_signals,
)
from signalforge.evidence.independence import same_author_groups, same_quote_groups
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.fetch import FetchStatus
from signalforge.providers.llm import ModelTier

DUPLICATE_RULES = ("near_dup", "syndicated")
# Groups this stage writes (and replaces): both documents are read, but count as one source.
EXTRACT_RULES = ("same_author", "same_quote")


@dataclass
class Extracted:
    signals: list[KeptSignal]
    author_groups: list[DuplicateGroup]
    quote_groups: list[DuplicateGroup]
    metrics: dict[str, Any] = field(default_factory=dict)


# --- pure steps -----------------------------------------------------------------------------


def extract(
    sources: list[SourceText],
    plan: ResearchPlan,
    pack: MarketPack,
    cfg: ExtractDefaults,
    propose: ExtractFn,
    salt: str,
    *,
    quote_min_words: int,
) -> Extracted:
    """Extract every document (in parallel), then group documents that share an author or a
    quoted passage (``quote_min_words``: ``dedupe.same_quote_min_words``)."""
    with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
        per_doc = list(
            pool.map(lambda d: extract_document(d, plan, pack, cfg, propose, salt), sources)
        )
    notes: Counter[str] = Counter()
    kept: list[KeptSignal] = []
    for result in per_doc:
        notes.update(result.notes)
        kept.extend(result.signals)

    authors: dict[int, set[str]] = defaultdict(set)
    for item in kept:
        if item.author_hash:
            authors[item.document_id].add(item.author_hash)
    groups = same_author_groups(authors)
    quotes: dict[int, set[str]] = defaultdict(set)
    for item in kept:
        quotes[item.document_id].add(item.match.quote)
    quote_groups = same_quote_groups(quotes, quote_min_words)
    metrics = extract_metrics(sources, kept, groups, notes)
    metrics["same_quote_groups"] = len(quote_groups)
    return Extracted(kept, groups, quote_groups, metrics)


def extract_metrics(
    sources: list[SourceText],
    kept: list[KeptSignal],
    groups: list[DuplicateGroup],
    notes: Counter[str],
) -> dict[str, Any]:
    proposed = notes["quotes_proposed"]
    matched = notes["quotes_exact"] + notes["quotes_fuzzy"]
    docs_with = Counter(k.document_id for k in kept)

    def yield_by(attr: str) -> dict[str, dict[str, int]]:
        """Documents read, documents with ≥1 signal, and signals, per value of ``attr``."""
        out: dict[str, dict[str, int]] = {}
        for s in sources:
            row = out.setdefault(
                str(getattr(s, attr) or "unlisted"),
                {"documents": 0, "with_signals": 0, "signals": 0},
            )
            row["documents"] += 1
            row["with_signals"] += docs_with[s.document_id] > 0
            row["signals"] += docs_with[s.document_id]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["documents"]))

    return {
        "documents_read": len(sources),
        "documents_snippet": sum(s.source == "snippet" for s in sources),
        "documents_with_signals": len(docs_with),
        "llm_calls": notes["llm_calls"],
        "llm_cache_hits": notes["llm_cache_hits"],
        "llm_errors": notes["llm_errors"],
        "quotes_proposed": proposed,
        "quotes_exact": notes["quotes_exact"],
        "quotes_fuzzy": notes["quotes_fuzzy"],
        "quotes_unverified": notes["quotes_unverified"],
        # Share of proposed quotes found in the source text (M3 exit: ≥ 0.95).
        "quote_pass_rate": round(matched / proposed, 3) if proposed else None,
        "dropped_length": notes["dropped_length"],
        "dropped_overlap": notes["dropped_overlap"],
        "signals": len(kept),
        "signals_by_type": dict(Counter(k.signal.type for k in kept).most_common()),
        "first_hand_share": (
            round(sum(k.signal.first_hand for k in kept) / len(kept), 3) if kept else None
        ),
        "signals_per_document": round(len(kept) / len(sources), 2) if sources else 0.0,
        "signals_by_submarket": dict(Counter(k.submarket or "(none)" for k in kept).most_common()),
        "submarket_unmatched": notes["submarket_unmatched"],
        "signals_with_author": sum(k.author_hash is not None for k in kept),
        "same_author_groups": len(groups),
        "yield_by_category": yield_by("source_category"),
        "yield_by_triage_label": yield_by("triage_label"),
    }


# --- stage ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _CandidateInfo:
    url: str
    snippet: str | None
    triage_label: str | None
    fetched: bool


def load_sources(ctx: RunContext) -> tuple[list[SourceText], dict[str, int]]:
    """The documents to read (one per duplicate group) with their text, plus skip counts."""
    with ctx.db() as session:
        documents = session.scalars(
            select(Document)
            .where(Document.run_id == ctx.run_id, Document.origin == "collect")
            .order_by(Document.id)
        ).all()
        candidates: dict[int, _CandidateInfo] = {}
        for c in session.scalars(
            select(UrlCandidate)
            .where(UrlCandidate.run_id == ctx.run_id, UrlCandidate.document_id.is_not(None))
            .order_by(UrlCandidate.priority.desc())  # best priority wins below
        ):
            candidates[c.document_id] = _CandidateInfo(
                c.url, c.snippet, c.triage_label, c.fetch_status == FetchStatus.OK.value
            )
        duplicate_groups = session.scalars(
            select(IndependenceGroup.document_ids).where(
                IndependenceGroup.run_id == ctx.run_id,
                IndependenceGroup.rule.in_(DUPLICATE_RULES),
            )
        ).all()
    if not documents:
        raise ValueError(f"run {ctx.run_id} has no documents; run fetch first")

    # Documents are stored in fetch priority order, so the lowest id is the best-ranked copy.
    skip = {i for group in duplicate_groups for i in sorted(group)[1:]}
    counts: Counter[str] = Counter()
    sources = []
    for doc in documents:
        if doc.id in skip:
            counts["skipped_duplicate"] += 1
            continue
        info = candidates.get(doc.id)
        common = {
            "document_id": doc.id,
            "domain": doc.domain,
            "title": doc.title,
            "source_category": doc.source_category,
            "quality_tier": doc.quality_tier,
            "triage_label": info.triage_label if info else None,
        }
        if doc.snippet_only:
            text = "\n".join(t for t in (doc.title, info.snippet if info else None) if t)
            if text:
                sources.append(SourceText(**common, source="snippet", text=text))
            else:
                counts["no_text"] += 1
            continue
        page = ctx.fetcher.cached(info.url) if info and info.fetched else None
        if page is None or not page.text:
            counts["text_missing"] += 1  # purged from the page cache since fetch
            continue
        sources.append(SourceText(**common, source="text", text=page.text, page_author=page.author))
    return sources, dict(counts)


class Extract:
    name = "extract"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.extract
        if ctx.settings.author_hash_salt is None:
            raise ValueError("AUTHOR_HASH_SALT is not set in .env (see .env.example)")
        salt = ctx.settings.author_hash_salt.get_secret_value()
        plan = load_run_plan(ctx)
        prompt = load_prompt("extract")
        sources, skipped = load_sources(ctx)

        def propose(prompt_input: str) -> tuple[ExtractionBatch, bool]:
            result = ctx.llm.parse(
                ModelTier.FAST,
                prompt,
                prompt_input,
                ExtractionBatch,
                stage=self.name,
                max_output_tokens=cfg.max_output_tokens,
            )
            return result.output, result.cache_hit

        extracted = extract(
            sources, plan, ctx.pack, cfg, propose, salt,
            quote_min_words=ctx.defaults.dedupe.same_quote_min_words,
        )  # fmt: skip

        with ctx.db.begin() as session:
            # Idempotent: replaces this run's excerpts (signals cascade), their fact claims and the
            # author groups. Cluster claims derived from the old facts are replaced by cluster.
            session.execute(delete(Excerpt).where(Excerpt.run_id == ctx.run_id))
            delete_stage_claims(session, ctx.run_id, self.name)
            session.execute(
                delete(IndependenceGroup).where(
                    IndependenceGroup.run_id == ctx.run_id,
                    IndependenceGroup.rule.in_(EXTRACT_RULES),
                )
            )
            write_signals(session, ctx.run_id, extracted.signals, stage=self.name)
            session.add_all(
                IndependenceGroup(run_id=ctx.run_id, rule=g.rule, document_ids=g.document_ids)
                for g in [*extracted.author_groups, *extracted.quote_groups]
            )

        input_hash = cache_key(
            {
                "sources": [f"{s.document_id}|{cache_key({'t': s.text})}" for s in sources],
                "plan": plan.model_dump(mode="json"),
                "pack": f"{ctx.pack.id}@{ctx.pack.version}",
                "config": cfg.model_dump(mode="json"),
                "prompt": prompt.ref,
            }
        )
        metrics = {**extracted.metrics, "skipped_duplicate": 0, "text_missing": 0, **skipped}
        return StageResult(metrics=metrics, input_hash=input_hash)
