"""cluster stage (plan §4, §6, §8.1): signals → problem clusters with evidence strength.

The analysis model groups signals by their English statements (prompts/cluster.md). Its output is
validated mechanically so that every signal ends up in exactly one cluster or in noise: unknown and
repeated ids are discarded, clusters beyond ``max_clusters`` are dissolved, unassigned signals are
offered once more to the existing clusters (prompts/cluster_assign.md), and clusters smaller than
``min_cluster_size`` go to noise (a lone ``regulatory`` signal may stand alone). An id the model
puts in several clusters stays in the one holding most other signals from the same document. Runs
with more than ``max_signals_per_call`` signals are clustered in parts, grouped by submarket, whose
clusters are then merged (prompts/cluster_merge.md).

Each cluster gets one ``inference`` claim, derived from the ``fact`` claims of its signals.

Per cluster, independent sources and evidence strength are computed deterministically from the
stored documents and independence groups (evidence/independence.py, evidence/strength.py).
"""

import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, select

from signalforge.config import ClusterDefaults, StrengthDefaults
from signalforge.db.models import Document, Excerpt, IndependenceGroup, ProblemCluster, Signal
from signalforge.domain.evidence import AssignmentBatch, ClusterBatch, MergeBatch
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.claims import add_inference, delete_stage_claims, fact_ids_by_excerpt
from signalforge.evidence.independence import source_units
from signalforge.evidence.strength import UNLISTED, SignalRef, SourceDoc, evidence_strength
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import ModelTier

PROMPTS = ("cluster", "cluster_assign", "cluster_merge")


@dataclass(frozen=True)
class SignalItem:
    id: int
    type: str
    actor: str | None
    workflow: str | None
    statement: str
    document_id: int
    first_hand: bool
    submarket: str | None = None
    excerpt_id: int | None = None

    def prompt_item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "submarket": self.submarket,
            "actor": self.actor,
            "workflow": self.workflow,
            "statement": self.statement,
        }


@dataclass
class Draft:
    name: str
    description: str
    signal_ids: list[int]


@dataclass
class Clustering:
    clusters: list[Draft]
    noise: list[int]
    notes: Counter[str] = field(default_factory=Counter)


# Asks one prompt: (prompt id, prompt input, output schema) -> (output, cache_hit).
AskFn = Callable[[str, str, type[BaseModel]], tuple[Any, bool]]


# --- pure steps -----------------------------------------------------------------------------


def context_block(plan: ResearchPlan, pack: MarketPack, cfg: ClusterDefaults) -> dict[str, Any]:
    request = plan.request
    return {
        "market": {"country": pack.country, "language": pack.language},
        "research": {
            "industry": request.industry,
            "submarkets": [s.name for s in plan.submarkets],
            "problem_area": request.problem_area,
            "target_customer": request.target_customer,
            "business_model": request.business_model,
        },
        "max_clusters": cfg.max_clusters,
        "min_cluster_size": cfg.min_cluster_size,
    }


def _input(context: dict[str, Any], **sections: Any) -> str:
    """Prompt input: the shared context first (provider prefix cache), then the sections."""
    parts = [f"# Context\n{json.dumps(context, ensure_ascii=False, indent=1)}"]
    for title, value in sections.items():
        parts.append(f"# {title.title()}\n{json.dumps(value, ensure_ascii=False, indent=1)}")
    return "\n\n".join(parts)


def validate_batch(
    batch: ClusterBatch, items: dict[int, SignalItem], cfg: ClusterDefaults, notes: Counter[str]
) -> tuple[list[Draft], set[int], set[int]]:
    """``(clusters, noise, unassigned)`` with every id of ``items`` in exactly one of them.

    Unknown ids are discarded. An id in several clusters stays in the one with the most other
    signals from the same document (the first of those on a tie); a cluster wins over noise.
    Clusters beyond ``max_clusters`` (smallest first) are dissolved into ``unassigned``.
    """
    proposed: list[list[int]] = []
    for c in batch.clusters:
        members: list[int] = []
        for i in c.signal_ids:
            if i not in items:
                notes["unknown_ids"] += 1
            elif i in members:
                notes["repeated_ids"] += 1
            else:
                members.append(i)
        proposed.append(members)

    owners: dict[int, list[int]] = defaultdict(list)
    for n, members in enumerate(proposed):
        for i in members:
            owners[i].append(n)

    def same_document(n: int, i: int) -> int:
        doc = items[i].document_id
        return sum(j != i and items[j].document_id == doc for j in proposed[n])

    owner: dict[int, int] = {}
    for i, ns in owners.items():
        notes["repeated_ids"] += len(ns) - 1
        owner[i] = max(ns, key=lambda n: (same_document(n, i), -n))

    drafts: list[Draft] = []
    for n, (c, members) in enumerate(zip(batch.clusters, proposed, strict=True)):
        kept = [i for i in members if owner[i] == n]
        if kept:
            drafts.append(Draft(c.name, c.description, kept))
    noise = set()
    for i in batch.noise:
        if i not in items:
            notes["unknown_ids"] += 1
        elif i in owner or i in noise:
            notes["repeated_ids"] += 1
        else:
            noise.add(i)
    drafts.sort(key=lambda d: -len(d.signal_ids))  # stable: model order among equal sizes
    unassigned = set(items) - set(owner) - noise
    notes["missing_ids"] += len(unassigned)
    for dissolved in drafts[cfg.max_clusters :]:
        notes["overflow_ids"] += len(dissolved.signal_ids)
        unassigned.update(dissolved.signal_ids)
    return drafts[: cfg.max_clusters], noise, unassigned


def assign_leftovers(
    drafts: list[Draft],
    leftovers: set[int],
    items: dict[int, SignalItem],
    context: dict[str, Any],
    ask: AskFn,
    notes: Counter[str],
) -> set[int]:
    """Offer unassigned signals to the existing clusters once; returns the ids left as noise."""
    if not leftovers:
        return set()
    if not drafts:
        return set(leftovers)
    prompt_input = _input(
        context,
        clusters=[
            {"index": n, "name": d.name, "description": d.description} for n, d in enumerate(drafts)
        ],
        signals=[items[i].prompt_item() for i in sorted(leftovers)],
    )
    out, hit = ask("cluster_assign", prompt_input, AssignmentBatch)
    notes["llm_calls"] += 1
    notes["llm_cache_hits"] += hit
    answered: set[int] = set()
    placed: set[int] = set()
    for a in out.assignments:
        if a.signal_id not in leftovers or a.signal_id in answered:
            notes["unknown_ids"] += 1
            continue
        answered.add(a.signal_id)
        if a.cluster is not None and 0 <= a.cluster < len(drafts):
            drafts[a.cluster].signal_ids.append(a.signal_id)
            placed.add(a.signal_id)
    notes["reassigned"] += len(placed)
    notes["leftover_noise"] += len(leftovers - placed)  # answered null, bad index, or unanswered
    return leftovers - placed


def cluster_part(
    part: list[SignalItem],
    items: dict[int, SignalItem],
    context: dict[str, Any],
    cfg: ClusterDefaults,
    ask: AskFn,
    notes: Counter[str],
) -> Clustering:
    out, hit = ask(
        "cluster", _input(context, signals=[s.prompt_item() for s in part]), ClusterBatch
    )
    notes["llm_calls"] += 1
    notes["llm_cache_hits"] += hit
    drafts, noise, unassigned = validate_batch(out, {s.id: s for s in part}, cfg, notes)
    noise |= assign_leftovers(drafts, unassigned, items, context, ask, notes)
    return Clustering(drafts, sorted(noise), notes)


def merge_parts(
    parts: list[Clustering],
    items: dict[int, SignalItem],
    context: dict[str, Any],
    cfg: ClusterDefaults,
    ask: AskFn,
    notes: Counter[str],
) -> Clustering:
    """Combine the clusters of separately clustered parts into run-level clusters."""
    part_clusters = [d for p in parts for d in p.clusters]
    noise = {i for p in parts for i in p.noise}
    listing = [
        {
            "id": n,
            "name": d.name,
            "description": d.description,
            "size": len(d.signal_ids),
            "sample_statements": [items[i].statement for i in d.signal_ids[:3]],
        }
        for n, d in enumerate(part_clusters)
    ]
    out, hit = ask("cluster_merge", _input(context, clusters=listing), MergeBatch)
    notes["llm_calls"] += 1
    notes["llm_cache_hits"] += hit

    used: set[int] = set()
    merged: list[Draft] = []
    for m in out.clusters:
        members = [n for n in m.members if 0 <= n < len(part_clusters) and n not in used]
        notes["merge_bad_members"] += len(m.members) - len(members)
        used.update(members)
        if members:
            ids = [i for n in members for i in part_clusters[n].signal_ids]
            merged.append(Draft(m.name, m.description, ids))
    for n, d in enumerate(part_clusters):  # clusters the model forgot are kept as they were
        if n not in used:
            notes["merge_unlisted"] += 1
            merged.append(Draft(d.name, d.description, list(d.signal_ids)))

    merged.sort(key=lambda d: -len(d.signal_ids))
    unassigned = {i for d in merged[cfg.max_clusters :] for i in d.signal_ids}
    notes["overflow_ids"] += len(unassigned)
    merged = merged[: cfg.max_clusters]
    noise |= assign_leftovers(merged, unassigned, items, context, ask, notes)
    return Clustering(merged, sorted(noise), notes)


def cluster_signals(
    signals: list[SignalItem],
    plan: ResearchPlan,
    pack: MarketPack,
    cfg: ClusterDefaults,
    ask: AskFn,
) -> Clustering:
    notes: Counter[str] = Counter()
    if not signals:
        return Clustering([], [], notes)
    items = {s.id: s for s in signals}
    context = context_block(plan, pack, cfg)
    n_parts = math.ceil(len(signals) / cfg.max_signals_per_call)
    size = math.ceil(len(signals) / n_parts)
    if n_parts == 1:
        ordered = sorted(signals, key=lambda s: s.id)
    else:  # related signals share a part: grouped by submarket, unassigned ones last
        ordered = sorted(signals, key=lambda s: (s.submarket is None, s.submarket or "", s.id))
    parts = [
        cluster_part(ordered[i : i + size], items, context, cfg, ask, notes)
        for i in range(0, len(ordered), size)
    ]
    notes["parts"] = len(parts)
    result = parts[0] if len(parts) == 1 else merge_parts(parts, items, context, cfg, ask, notes)

    def big_enough(d: Draft) -> bool:
        if len(d.signal_ids) >= cfg.min_cluster_size:
            return True
        lone_rule = cfg.keep_regulatory_singletons and all(
            items[i].type == "regulatory" for i in d.signal_ids
        )
        notes["regulatory_singletons"] += lone_rule
        return lone_rule

    kept, small = [], []
    for d in result.clusters:
        if big_enough(d):
            kept.append(d)
        else:
            small.extend(d.signal_ids)
    notes["too_small_ids"] += len(small)
    noise = sorted(set(result.noise) | set(small))
    assigned = [i for d in kept for i in d.signal_ids]
    if sorted(assigned + noise) != sorted(items):  # every signal exactly once
        raise AssertionError("cluster validation left signals unassigned or assigned twice")
    for d in kept:
        d.signal_ids.sort()
    return Clustering(kept, noise, notes)


@dataclass(frozen=True)
class ClusterStats:
    independent_sources: int
    source_category_mix: dict[str, int]
    signal_type_mix: dict[str, int]
    strength: dict[str, Any]
    score: float


def cluster_stats(
    draft: Draft,
    items: dict[int, SignalItem],
    docs: dict[int, SourceDoc],
    units: dict[int, int],
    cfg: StrengthDefaults,
    now: datetime,
) -> ClusterStats:
    signals = [items[i] for i in draft.signal_ids]
    strength = evidence_strength(
        [SignalRef(s.document_id, s.first_hand) for s in signals], docs, units, cfg, now
    )
    doc_ids = {s.document_id for s in signals}
    categories = Counter(docs[d].source_category or UNLISTED for d in doc_ids)
    return ClusterStats(
        independent_sources=strength.independent_sources,
        source_category_mix=dict(categories.most_common()),
        signal_type_mix=dict(Counter(s.type for s in signals).most_common()),
        strength=strength.as_dict(),
        score=strength.score,
    )


def dominant_submarket(draft: Draft, items: dict[int, SignalItem]) -> str:
    """The submarket most of a cluster's signals name (``(none)`` if none do)."""
    named = Counter(items[i].submarket for i in draft.signal_ids if items[i].submarket)
    return named.most_common(1)[0][0] if named else "(none)"


def cluster_metrics(
    result: Clustering,
    items: dict[int, SignalItem],
    stats: list[ClusterStats],
    without_facts: int = 0,
) -> dict:
    notes = result.notes
    n_signals = len(items)
    sizes = sorted((len(d.signal_ids) for d in result.clusters), reverse=True)
    return {
        "signals": n_signals,
        "parts": notes["parts"],
        "clusters": len(result.clusters),
        "noise": len(result.noise),
        "noise_share": round(len(result.noise) / n_signals, 3) if n_signals else None,
        "cluster_sizes": sizes,
        "size_min_median_max": [sizes[-1], statistics.median(sizes), sizes[0]] if sizes else None,
        "clusters_per_submarket": dict(
            Counter(dominant_submarket(d, items) for d in result.clusters).most_common()
        ),
        "llm_calls": notes["llm_calls"],
        "llm_cache_hits": notes["llm_cache_hits"],
        # Mechanical fixes to the model's assignment (plan §6 guard).
        "unknown_ids": notes["unknown_ids"],
        "repeated_ids": notes["repeated_ids"],
        "missing_ids": notes["missing_ids"],
        "overflow_ids": notes["overflow_ids"],
        "reassigned": notes["reassigned"],
        "leftover_noise": notes["leftover_noise"],
        "too_small_ids": notes["too_small_ids"],
        "regulatory_singletons": notes["regulatory_singletons"],
        "merge_unlisted": notes["merge_unlisted"],
        "independent_sources": sorted((s.independent_sources for s in stats), reverse=True),
        "strength": sorted((s.score for s in stats), reverse=True),
        # Clusters whose signals have no fact claims (extracted before claims existed) get no
        # inference claim; re-run from extract to fix.
        "clusters_without_facts": without_facts,
    }


# --- stage ----------------------------------------------------------------------------------


def to_database_ids(result: Clustering, signals: list[SignalItem]) -> Clustering:
    """Map the 1..n numbering the model saw back to the signals' ids (``signals`` in that order)."""
    real = {n: s.id for n, s in enumerate(signals, 1)}
    for d in result.clusters:
        d.signal_ids = [real[i] for i in d.signal_ids]
    result.noise = [real[i] for i in result.noise]
    return result


def load_evidence(
    ctx: RunContext,
) -> tuple[list[SignalItem], dict[int, SourceDoc], dict[int, int]]:
    """The run's signals, its documents and the document -> independent-source mapping."""
    with ctx.db() as session:
        signals = [
            SignalItem(
                s.id,
                s.type,
                s.actor,
                s.workflow,
                s.statement,
                doc_id,
                s.first_hand,
                submarket=(s.meta or {}).get("submarket"),
                excerpt_id=s.excerpt_id,
            )
            for s, doc_id in session.execute(
                select(Signal, Excerpt.document_id)
                .join(Excerpt, Signal.excerpt_id == Excerpt.id)
                .where(Signal.run_id == ctx.run_id)
                .order_by(Signal.id)
            )
        ]
        docs = {
            d.id: SourceDoc(d.id, d.source_category, d.quality_tier, d.snippet_only, d.published_at)
            for d in session.scalars(select(Document).where(Document.run_id == ctx.run_id))
        }
        groups = session.scalars(
            select(IndependenceGroup.document_ids).where(IndependenceGroup.run_id == ctx.run_id)
        ).all()
    return signals, docs, source_units(docs, groups)


class Cluster:
    name = "cluster"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.cluster
        plan = load_run_plan(ctx)
        prompts = {p: load_prompt(p) for p in PROMPTS}
        signals, docs, units = load_evidence(ctx)
        if not signals:
            raise ValueError(f"run {ctx.run_id} has no signals; run extract first")

        def ask(prompt_id: str, prompt_input: str, schema: type[BaseModel]) -> tuple[Any, bool]:
            result = ctx.llm.parse(
                ModelTier.ANALYSIS,
                prompts[prompt_id],
                prompt_input,
                schema,
                stage=self.name,
                max_output_tokens=cfg.max_output_tokens,
            )
            return result.output, result.cache_hit

        # The model sees signals numbered 1..n in id order, not database ids, which change on every
        # re-extract: unchanged signals then hit the LLM cache (and replay mode keeps working).
        local = [replace(s, id=n) for n, s in enumerate(signals, 1)]
        result = to_database_ids(cluster_signals(local, plan, ctx.pack, cfg, ask), signals)
        items = {s.id: s for s in signals}
        now = datetime.now(UTC)
        stats = [
            cluster_stats(d, items, docs, units, ctx.defaults.strength, now)
            for d in result.clusters
        ]

        without_facts = 0
        with ctx.db.begin() as session:
            session.execute(delete(ProblemCluster).where(ProblemCluster.run_id == ctx.run_id))
            delete_stage_claims(session, ctx.run_id, self.name)
            facts = fact_ids_by_excerpt(session, ctx.run_id, "extract")
            ranked = sorted(zip(result.clusters, stats, strict=True), key=lambda p: -p[1].score)
            for rank, (d, s) in enumerate(ranked, 1):
                fact_ids = [
                    facts[items[i].excerpt_id] for i in d.signal_ids if items[i].excerpt_id in facts
                ]
                claim = (
                    add_inference(session, ctx.run_id, d.description, fact_ids, stage=self.name)
                    if fact_ids
                    else None
                )
                without_facts += claim is None
                session.add(
                    ProblemCluster(
                        run_id=ctx.run_id,
                        name=d.name,
                        description=d.description,
                        signal_ids=d.signal_ids,
                        independent_source_count=s.independent_sources,
                        source_category_mix=s.source_category_mix,
                        signal_type_mix=s.signal_type_mix,
                        evidence_strength=s.score,
                        strength=s.strength,
                        rank=rank,
                        claim_id=claim.id if claim else None,
                    )
                )

        input_hash = cache_key(
            {
                "signals": [s.prompt_item() for s in local],
                "plan": plan.model_dump(mode="json"),
                "config": cfg.model_dump(mode="json"),
                "strength": ctx.defaults.strength.model_dump(mode="json"),
                "prompts": [p.ref for p in prompts.values()],
            }
        )
        return StageResult(
            metrics=cluster_metrics(result, items, stats, without_facts), input_hash=input_hash
        )
