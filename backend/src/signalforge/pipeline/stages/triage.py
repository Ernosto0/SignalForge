"""triage stage (plan §4, §6): search results → unique URL candidates → keep / reserve / drop.

Mechanical first: results collapse by canonical URL, unfetchable platforms and file types are
dropped by rule. The fast model then scores each remaining candidate from title + snippet + domain
(prompts/triage.md); the keep bar depends on the source tier (low tier needs a stronger signal).
Passing candidates are ranked and split into ``keep`` (≤ max_urls, ≤ max_per_domain each) and a
ranked ``reserve`` that the fetch stage draws on when kept pages fail to download.
"""

import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select

from signalforge.config import TriageDefaults
from signalforge.db.models import Query, SearchResult, UrlCandidate
from signalforge.domain.collection import TriageBatch, TriageJudgment
from signalforge.domain.plan import ResearchPlan
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import ModelTier
from signalforge.providers.urls import canonicalize_url, domain_of, in_domain

_TIER_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Candidate:
    canonical_url: str
    url: str
    domain: str
    title: str | None
    snippet: str | None
    source_category: str | None
    quality_tier: str
    access: str
    query_ids: list[int]
    queries: list[str]
    best_rank: int
    off_site: bool
    decision: str = "drop"
    decision_rule: str = "unjudged"
    judgment: TriageJudgment | None = None
    priority: int | None = None

    def row(self, run_id: int) -> UrlCandidate:
        j = self.judgment
        return UrlCandidate(
            run_id=run_id,
            canonical_url=self.canonical_url,
            url=self.url,
            domain=self.domain,
            title=self.title,
            snippet=self.snippet,
            source_category=self.source_category,
            quality_tier=self.quality_tier,
            access=self.access,
            query_ids=self.query_ids,
            best_rank=self.best_rank,
            off_site=self.off_site,
            decision=self.decision,
            decision_rule=self.decision_rule,
            triage_score=j.score if j else None,
            triage_label=j.label if j else None,
            triage_reason=j.reason if j else None,
            priority=self.priority,
        )


@dataclass(frozen=True)
class ResultRow:
    """A search result joined with the query that produced it."""

    url: str
    title: str | None
    snippet: str | None
    rank: int
    query_id: int
    query_text: str
    source_hint: str | None


# Judges one batch: prompt input -> (judgments, cache_hit).
JudgeFn = Callable[[str], tuple[TriageBatch, bool]]


@dataclass
class Triaged:
    candidates: list[Candidate]
    metrics: dict[str, Any] = field(default_factory=dict)


# --- pure steps -----------------------------------------------------------------------------


def build_candidates(rows: list[ResultRow], pack: MarketPack) -> list[Candidate]:
    """One candidate per canonical URL; title/snippet/URL from its best-ranked result.

    Ordered by the first query that found it, then rank, so prompt batches are deterministic.
    """
    groups: dict[str, list[ResultRow]] = {}
    for r in sorted(rows, key=lambda r: (r.query_id, r.rank)):
        groups.setdefault(canonicalize_url(r.url), []).append(r)

    out = []
    for canonical, group in groups.items():
        best = min(group, key=lambda r: (r.rank, r.query_id))
        domain = domain_of(best.url)
        source = pack.source_for(domain)
        hints = [r.source_hint for r in group]
        out.append(
            Candidate(
                canonical_url=canonical,
                url=best.url,
                domain=domain,
                title=best.title,
                snippet=best.snippet,
                source_category=source.category if source else None,
                quality_tier=source.tier if source else pack.default_tier,
                access=source.access if source else "fetch",
                query_ids=sorted({r.query_id for r in group}),
                queries=list(dict.fromkeys(r.query_text for r in group)),
                best_rank=best.rank,
                off_site=all(h and not in_domain(domain, h) for h in hints),
            )
        )
    return out


def prefilter(c: Candidate, cfg: TriageDefaults) -> str | None:
    """The rule that drops ``c`` without asking the model, if any."""
    if any(in_domain(c.domain, d) for d in cfg.skip_domains):
        return "skip_domain"
    path = c.canonical_url.split("?", 1)[0].lower()
    if path.endswith(tuple(cfg.skip_extensions)):
        return "file_type"
    return None


def build_input(plan: ResearchPlan, pack: MarketPack, items: list[dict[str, Any]]) -> str:
    """Prompt input: the shared context first (provider prefix cache), the batch last."""
    request = plan.request
    industry = pack.industries.get(request.industry)
    context = {
        "market": {"country": pack.country, "language": pack.language},
        "research": {
            "industry": request.industry,
            "industry_local": industry.name_tr if industry else None,
            "submarkets": [s.name for s in plan.submarkets],
            "problem_area": request.problem_area,
            "target_customer": request.target_customer,
            "business_model": request.business_model,
            "avoid": plan.avoid,
        },
    }
    dump = json.dumps
    return (
        f"# Context\n{dump(context, ensure_ascii=False, indent=1)}\n\n"
        f"# Results\n{dump(items, ensure_ascii=False, indent=1)}"
    )


def _item(i: int, c: Candidate) -> dict[str, Any]:
    source = f"{c.source_category}, {c.quality_tier} tier" if c.source_category else "unlisted"
    return {
        "id": i,
        "domain": c.domain,
        "source": source,
        "title": c.title or "",
        "snippet": c.snippet or "",
        "queries": c.queries[:3],
    }


def judge_all(
    plan: ResearchPlan,
    pack: MarketPack,
    todo: list[Candidate],
    cfg: TriageDefaults,
    judge: JudgeFn,
    notes: Counter[str],
) -> None:
    """Attach a judgment to every candidate in ``todo``. Ids the model skips get one more round."""
    pending = list(range(len(todo)))
    for round_no in range(2):
        if not pending:
            break
        batches = [pending[i : i + cfg.batch_size] for i in range(0, len(pending), cfg.batch_size)]
        inputs = [build_input(plan, pack, [_item(i, todo[i]) for i in b]) for b in batches]
        with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
            results = list(pool.map(judge, inputs))
        notes["llm_calls"] += len(results)
        notes["llm_cache_hits"] += sum(hit for _, hit in results)
        for batch, (out, _) in zip(batches, results, strict=True):
            asked = set(batch)
            for j in out.judgments:
                if j.id not in asked:
                    notes["unknown_ids"] += 1
                elif todo[j.id].judgment is None:
                    todo[j.id].judgment = j
        pending = [i for i in pending if todo[i].judgment is None]
        if round_no == 0:
            notes["rejudged"] = len(pending)


def decide(candidates: list[Candidate], cfg: TriageDefaults) -> int:
    """Split judged candidates into keep / reserve / drop and set fetch priority.

    Returns how many passing candidates were pushed to reserve by the per-domain cap.
    """
    passing: list[Candidate] = []
    for c in candidates:
        if c.decision_rule != "llm":
            continue
        assert c.judgment is not None
        if c.judgment.score >= cfg.min_score.get(c.quality_tier, 3):
            passing.append(c)
        else:
            c.decision, c.decision_rule = "drop", "below_min_score"

    passing.sort(
        key=lambda c: (
            -c.judgment.score if c.judgment else 0,
            _TIER_ORDER.get(c.quality_tier, 3),
            -len(c.query_ids),
            c.best_rank,
            c.canonical_url,
        )
    )
    per_domain: Counter[str] = Counter()
    kept: list[Candidate] = []
    reserve: list[Candidate] = []
    domain_capped = 0
    for c in passing:
        if len(kept) < cfg.max_urls and per_domain[c.domain] < cfg.max_per_domain:
            per_domain[c.domain] += 1
            c.decision = "keep"
            kept.append(c)
        else:
            domain_capped += len(kept) < cfg.max_urls
            c.decision = "reserve"
            reserve.append(c)
    for priority, c in enumerate(kept + reserve):
        c.priority = priority
    return domain_capped


def triage(
    rows: list[ResultRow],
    plan: ResearchPlan,
    pack: MarketPack,
    cfg: TriageDefaults,
    judge: JudgeFn,
) -> Triaged:
    notes: Counter[str] = Counter()
    candidates = build_candidates(rows, pack)
    todo: list[Candidate] = []
    for c in candidates:
        if rule := prefilter(c, cfg):
            c.decision, c.decision_rule = "drop", rule
            notes[f"prefilter_{rule}"] += 1
        else:
            todo.append(c)

    judge_all(plan, pack, todo, cfg, judge, notes)
    for c in todo:
        c.decision_rule = "llm" if c.judgment else "unjudged"
    domain_capped = decide(candidates, cfg)

    def by(attr: Callable[[Candidate], Any], items: list[Candidate]) -> dict[str, int]:
        return dict(Counter(str(attr(c)) for c in items).most_common())

    kept = [c for c in candidates if c.decision == "keep"]
    judged = [c for c in todo if c.judgment]
    metrics = {
        "search_results": len(rows),
        "candidates": len(candidates),
        **{k: v for k, v in sorted(notes.items()) if k.startswith("prefilter_")},
        "judged": len(judged),
        "unjudged": len(todo) - len(judged),
        "llm_calls": notes["llm_calls"],
        "llm_cache_hits": notes["llm_cache_hits"],
        "rejudged": notes["rejudged"],
        "unknown_ids": notes["unknown_ids"],
        "off_site": sum(c.off_site for c in candidates),
        "score_histogram": by(lambda c: c.judgment.score, judged),
        "labels": by(lambda c: c.judgment.label, judged),
        "keep": len(kept),
        "reserve": sum(c.decision == "reserve" for c in candidates),
        "drop": sum(c.decision == "drop" for c in candidates),
        "below_min_score": sum(c.decision_rule == "below_min_score" for c in candidates),
        "domain_capped": domain_capped,
        "kept_by_label": by(lambda c: c.judgment.label, kept),
        "kept_by_tier": by(lambda c: c.quality_tier, kept),
        "kept_snippet_only": sum(c.access == "snippet_only" for c in kept),
        "kept_unique_domains": len({c.domain for c in kept}),
        "kept_top_domains": dict(Counter(c.domain for c in kept).most_common(10)),
    }
    return Triaged(candidates, metrics)


# --- stage ----------------------------------------------------------------------------------


class Triage:
    name = "triage"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.triage
        plan = load_run_plan(ctx)
        prompt = load_prompt("triage")
        with ctx.db() as session:
            rows = [
                ResultRow(
                    url=r.url,
                    title=r.title,
                    snippet=r.snippet,
                    rank=r.rank,
                    query_id=q.id,
                    query_text=q.text,
                    source_hint=q.meta.get("source_hint"),
                )
                for r, q in session.execute(
                    select(SearchResult, Query)
                    .join(Query, SearchResult.query_id == Query.id)
                    .where(Query.run_id == ctx.run_id)
                    .order_by(SearchResult.id)
                )
            ]
        if not rows:
            raise ValueError(f"run {ctx.run_id} has no search results; run search first")

        def judge(prompt_input: str) -> tuple[TriageBatch, bool]:
            result = ctx.llm.parse(
                ModelTier.FAST,
                prompt,
                prompt_input,
                TriageBatch,
                stage=self.name,
                max_output_tokens=cfg.max_output_tokens,
            )
            return result.output, result.cache_hit

        triaged = triage(rows, plan, ctx.pack, cfg, judge)
        with ctx.db.begin() as session:
            session.execute(delete(UrlCandidate).where(UrlCandidate.run_id == ctx.run_id))
            session.add_all(c.row(ctx.run_id) for c in triaged.candidates)

        input_hash = cache_key(
            {
                "results": sorted(f"{r.query_id}|{r.rank}|{r.url}" for r in rows),
                "plan": plan.model_dump(mode="json"),
                "pack": f"{ctx.pack.id}@{ctx.pack.version}",
                "config": cfg.model_dump(mode="json"),
                "prompt": prompt.ref,
            }
        )
        return StageResult(metrics=triaged.metrics, input_hash=input_hash)
