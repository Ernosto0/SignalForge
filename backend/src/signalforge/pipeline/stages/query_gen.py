"""query_gen stage (plan §4, §6): plan submarkets × pack pain phrases × source hints → queries.

The fast model drafts queries per submarket, because it composes natural search phrasing far better
than string templates. Everything after the draft is mechanical: operator cleanup, source hints
checked against the pack registry, Turkish-aware dedupe, per-submarket/intent quotas and the cap.
The pack's regulatory seeds are added verbatim, so mandate coverage never depends on the model.
"""

import json
import math
from collections import Counter, deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete

from signalforge.config import QueryGenDefaults
from signalforge.db.models import Query
from signalforge.domain.plan import PlanSubmarket, ResearchPlan
from signalforge.domain.queries import GeneratedIntent, QueryDrafts
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import ModelTier
from signalforge.queries import Deduper, clean_query

INTENTS: tuple[GeneratedIntent, ...] = ("pain", "jobs", "regulatory")
# jobs and regulatory queries can only target one signal type.
_FIXED_SIGNAL_TYPE = {"jobs": "labor_spend", "regulatory": "regulatory"}

Bucket = tuple[str | None, GeneratedIntent]  # (submarket name or None for market-wide, intent)


@dataclass(frozen=True)
class Candidate:
    text: str  # without operators
    intent: GeneratedIntent
    signal_type: str
    source_hint: str | None  # registry domain
    submarket: str | None  # None for market-wide pack seeds
    origin: str  # llm | pack_seed

    @property
    def query(self) -> str:
        """The string sent to the search provider."""
        return f"{self.text} site:{self.source_hint}" if self.source_hint else self.text


@dataclass
class Generated:
    queries: list[Candidate]
    metrics: dict[str, Any]


# Drafts one submarket: (submarket, prompt input) -> (drafts, cache_hit).
DraftFn = Callable[[PlanSubmarket, str], tuple[QueryDrafts, bool]]


# --- pure steps -----------------------------------------------------------------------------


def allocate[K](total: int, weights: dict[K, float]) -> dict[K, int]:
    """Split ``total`` into integers proportional to ``weights`` (largest remainder, stable)."""
    weight_sum = sum(weights.values())
    if total <= 0 or weight_sum <= 0:
        return dict.fromkeys(weights, 0)
    raw = {k: total * w / weight_sum for k, w in weights.items()}
    out = {k: math.floor(v) for k, v in raw.items()}
    by_remainder = sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)
    for k in by_remainder[: total - sum(out.values())]:
        out[k] += 1
    return out


def _interleave(items: Iterable[Candidate], weights: dict[str, float]) -> deque[Candidate]:
    """Order a bucket so every prefix follows ``weights`` over signal types as closely as the
    candidates allow (smooth weighted round-robin), keeping the model's order within each type.
    Without weights the types alternate evenly."""
    groups: dict[str, deque[Candidate]] = {}
    for c in items:
        groups.setdefault(c.signal_type, deque()).append(c)
    total = sum(weights.get(k, 0.0) for k in groups) or 1.0
    taken: Counter[str] = Counter()
    out: deque[Candidate] = deque()
    while groups:
        n = len(out) + 1
        share = {k: weights.get(k, 0.0) / total if weights else 1 / len(groups) for k in groups}
        key = max(groups, key=lambda k: share[k] * n - taken[k])  # ties: first-seen type
        out.append(groups[key].popleft())
        taken[key] += 1
        if not groups[key]:
            del groups[key]
    return out


def select(
    candidates: list[Candidate],
    quotas: dict[Bucket, int],
    cap: int,
    type_weights: dict[str, dict[str, float]] | None = None,
) -> list[Candidate]:
    """Fill every bucket up to its quota (following ``type_weights[intent]`` over signal types),
    then hand spare capacity out round-robin."""
    pools: dict[Bucket, list[Candidate]] = {}
    for c in candidates:
        pools.setdefault((c.submarket, c.intent), []).append(c)
    weights = type_weights or {}
    buckets = {key: _interleave(items, weights.get(key[1], {})) for key, items in pools.items()}

    chosen: list[Candidate] = []
    for key, quota in quotas.items():
        bucket = buckets.get(key, deque())
        while quota > 0 and bucket and len(chosen) < cap:
            chosen.append(bucket.popleft())
            quota -= 1
    active = [b for b in buckets.values() if b]
    while active and len(chosen) < cap:
        for bucket in active:
            if len(chosen) >= cap:
                break
            chosen.append(bucket.popleft())
        active = [b for b in active if b]
    return chosen


def source_hints(pack: MarketPack, cfg: QueryGenDefaults) -> dict[str, list[dict[str, str]]]:
    hints: dict[str, list[dict[str, str]]] = {}
    for intent in INTENTS:
        categories = cfg.source_categories.get(intent, [])
        hints[intent] = [
            {"domain": s.domain, "category": s.category} | ({"note": s.notes} if s.notes else {})
            for s in pack.sources
            if s.category in categories
        ]
    return hints


def build_input(
    plan: ResearchPlan,
    pack: MarketPack,
    submarket: PlanSubmarket,
    asks: dict[str, dict[str, int]],
    hints: dict[str, list[dict[str, str]]],
) -> str:
    """Prompt input. The context block is identical for every submarket and comes first, so the
    provider's prefix cache can reuse it; the per-submarket part comes last."""
    request = plan.request
    industry = pack.industries.get(request.industry)
    context = {
        "market": {"country": pack.country, "language": pack.language},
        "research": {
            "industry": request.industry,
            "industry_local": industry.name_tr if industry else None,
            "problem_area": request.problem_area,
            "target_customer": request.target_customer,
            "business_model": request.business_model,
            "avoid": plan.avoid,
        },
        "industry_terms": industry.terms if industry else [],
        "pain_phrases": pack.pain_phrases,
        "source_hints": hints,
        "regulatory_seeds": [m.model_dump() for m in pack.regulatory.mandates],
    }
    task = {"submarket": submarket.model_dump(exclude_none=True), "counts": asks}
    dump = json.dumps
    return (
        f"# Context\n{dump(context, ensure_ascii=False, indent=1)}\n\n"
        f"# Submarket\n{dump(task, ensure_ascii=False, indent=1)}"
    )


def generate(
    plan: ResearchPlan, pack: MarketPack, cfg: QueryGenDefaults, draft: DraftFn
) -> Generated:
    notes: Counter[str] = Counter()
    dedupe = Deduper(cfg.near_dup_ratio)
    hints = source_hints(pack, cfg)
    hint_domains = {intent: [h["domain"] for h in offered] for intent, offered in hints.items()}
    intent_weights = {i: cfg.intent_mix.get(i, 0.0) for i in INTENTS}

    # Market-wide pack seeds first: they always survive dedupe against model drafts.
    seeds: list[Candidate] = []
    if cfg.pack_seed_queries:
        for mandate in pack.regulatory.mandates:
            for seed in mandate.seeds:
                cleaned = clean_query(seed, None, (), cfg, notes)
                if cleaned and dedupe.add(*cleaned):
                    text, _ = cleaned
                    seeds.append(
                        Candidate(text, "regulatory", "regulatory", None, None, origin="pack_seed")
                    )
    seeds = seeds[: cfg.max_queries]

    quotas: dict[Bucket, int] = {(None, "regulatory"): len(seeds)} if seeds else {}
    per_submarket = allocate(cfg.max_queries - len(seeds), {s.name: 1.0 for s in plan.submarkets})
    inputs: list[str] = []
    for sub in plan.submarkets:
        split = allocate(per_submarket[sub.name], intent_weights)
        asks = {}
        for intent, n in split.items():
            quotas[(sub.name, intent)] = n
            total = math.ceil(n * cfg.overgenerate)
            share = cfg.site_hint_share.get(intent, 0.0) if hints[intent] else 0.0
            asks[intent] = {"total": total, "with_source_hint": round(total * share)}
            if intent == "pain":
                asks[intent]["by_signal_type"] = allocate(total, cfg.pain_signal_mix)
        inputs.append(build_input(plan, pack, sub, asks, hints))

    with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
        drafted = list(pool.map(draft, plan.submarkets, inputs))

    candidates = list(seeds)
    drafts_by_intent: Counter[str] = Counter()
    for sub, (drafts, _) in zip(plan.submarkets, drafted, strict=True):
        for d in drafts.queries:
            drafts_by_intent[d.intent] += 1
            cleaned = clean_query(d.text, d.source_hint, hint_domains[d.intent], cfg, notes)
            if cleaned is None:
                continue
            if not dedupe.add(*cleaned):
                notes["dropped_duplicate"] += 1
                continue
            text, hint = cleaned
            signal_type = _FIXED_SIGNAL_TYPE.get(d.intent, d.signal_type)
            candidates.append(Candidate(text, d.intent, signal_type, hint, sub.name, origin="llm"))

    chosen = select(candidates, quotas, cfg.max_queries, {"pain": cfg.pain_signal_mix})
    notes["dropped_over_cap"] = len(candidates) - len(chosen)
    order = {name: i for i, name in enumerate(s.name for s in plan.submarkets)}
    chosen.sort(key=lambda c: (order.get(c.submarket, len(order)), INTENTS.index(c.intent)))

    kept_per_bucket = Counter((c.submarket, c.intent) for c in chosen)
    metrics = {
        "submarkets": len(plan.submarkets),
        "llm_calls": len(drafted),
        "llm_cache_hits": sum(hit for _, hit in drafted),
        "pack_seeds": len(seeds),
        "drafts": sum(drafts_by_intent.values()),
        "drafts_by_intent": dict(drafts_by_intent),
        **dict(sorted(notes.items())),
        "kept": len(chosen),
        "kept_by_intent": dict(Counter(c.intent for c in chosen)),
        "kept_by_signal_type": dict(Counter(c.signal_type for c in chosen)),
        "kept_by_submarket": dict(Counter(c.submarket or "(market-wide)" for c in chosen)),
        "site_hinted": sum(c.source_hint is not None for c in chosen),
        # Buckets the model under-delivered on after cleanup/dedupe; spare capacity went elsewhere.
        "quota_shortfall": {
            f"{sub or '(market-wide)'}/{intent}": quota - kept_per_bucket[(sub, intent)]
            for (sub, intent), quota in quotas.items()
            if kept_per_bucket[(sub, intent)] < quota
        },
    }
    return Generated(chosen, metrics)


# --- stage ----------------------------------------------------------------------------------


class QueryGen:
    name = "query_gen"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.query_gen
        plan = load_run_plan(ctx)
        prompt = load_prompt("query_gen")

        def draft(submarket: PlanSubmarket, prompt_input: str) -> tuple[QueryDrafts, bool]:
            result = ctx.llm.parse(
                ModelTier.FAST,
                prompt,
                prompt_input,
                QueryDrafts,
                stage=self.name,
                max_output_tokens=cfg.max_output_tokens,
            )
            return result.output, result.cache_hit

        generated = generate(plan, ctx.pack, cfg, draft)

        with ctx.db.begin() as session:
            # Idempotent: regenerate replaces this stage's queries (and, by cascade, their results).
            session.execute(
                delete(Query).where(Query.run_id == ctx.run_id, Query.intent.in_(INTENTS))
            )
            session.add_all(
                Query(
                    run_id=ctx.run_id,
                    text=c.query,
                    lang=ctx.pack.language,
                    intent=c.intent,
                    submarket=c.submarket,
                    meta={
                        "signal_type": c.signal_type,
                        "source_hint": c.source_hint,
                        "origin": c.origin,
                    },
                )
                for c in generated.queries
            )

        input_hash = cache_key(
            {
                "plan": plan.model_dump(mode="json"),
                "pack": f"{ctx.pack.id}@{ctx.pack.version}",
                "config": cfg.model_dump(mode="json"),
                "prompt": prompt.ref,
            }
        )
        return StageResult(metrics=generated.metrics, input_hash=input_hash)
