"""Multi-market evaluation (plan §13 M7): funnel metrics, outcomes and cost per run, side by side.

Reads finished runs from the database (no LLM calls). Cost has two parts: LLM spend from the
ledger and live searches from the stages' metrics, priced with ``search.usd_per_query``. A "clean
pass" sums each stage's latest StageRun, which is what one run from scratch costs; "spent" adds
every re-run of a stage during tuning. Founder verdicts come from ``label opportunities``.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from signalforge.agents import AGENTS
from signalforge.config import Defaults, get_settings
from signalforge.db.models import Label, LLMCall, Opportunity, ResearchRun, ScoreCard
from signalforge.pipeline.runner import latest_stage_runs

# Stages whose metrics count searches sent to the provider (cache misses).
SEARCH_STAGES = {
    "search": "searched_live",
    "verify": "searches_live",
    "competitors": "searches_live",
}
CATEGORIES = ("strong", "interesting", "competitive", "weak", "false_positive")
VERDICTS = ("investigate", "maybe", "no")


@dataclass
class RunEval:
    run_id: int
    market: str
    status: str
    pack: str
    stages: dict[str, dict[str, Any]]  # stage -> latest metrics
    llm_spent_usd: float  # every LLM call of the run, re-runs included
    llm_clean_usd: float  # latest StageRun per stage
    llm_by_agent: dict[str, float]  # clean pass
    live_searches: int  # clean pass
    search_usd: float | None
    wall_s: float  # clean pass, sum of stage durations
    categories: dict[str, int] = field(default_factory=dict)
    top_score: float | None = None
    verdicts: dict[str, int] = field(default_factory=dict)  # labeler-independent latest labels

    @property
    def clean_usd(self) -> float | None:
        if self.search_usd is None:
            return None
        return round(self.llm_clean_usd + self.search_usd, 4)

    def metric(self, stage: str, key: str) -> Any:
        return self.stages.get(stage, {}).get(key)


def _eval_run(db: sessionmaker[Session], run_id: int, cfg: Defaults) -> RunEval:
    latest = latest_stage_runs(db, run_id)
    agent_of = {name: a.name for a in AGENTS for name in a.stage_names}
    with db() as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        spent = session.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.run_id == run_id)
        )
        cards = session.execute(
            select(Opportunity.id, ScoreCard.category, ScoreCard.attractiveness)
            .join(ScoreCard, ScoreCard.opportunity_id == Opportunity.id)
            .where(Opportunity.run_id == run_id)
        ).all()
        labels = session.scalars(
            select(Label)
            .where(Label.target_type == "opportunity", Label.target_id.in_([c[0] for c in cards]))
            .order_by(Label.id)
        ).all()
    by_agent: dict[str, Decimal] = defaultdict(Decimal)
    for name, row in latest.items():
        by_agent[agent_of.get(name, "-")] += row.cost_usd
    searches = sum(
        (latest[s].metrics or {}).get(key) or 0 for s, key in SEARCH_STAGES.items() if s in latest
    )
    price = cfg.search.usd_per_query.get(get_settings().search_provider)
    wall = sum(
        (r.finished_at - r.started_at).total_seconds() for r in latest.values() if r.finished_at
    )
    final = {(lab.target_id, lab.labeler): lab.value for lab in labels}  # latest per labeler
    scores = [c[2] for c in cards if c[2] is not None]
    return RunEval(
        run_id=run_id,
        market=str(run.request.get("industry", "?")),
        status=run.status,
        pack=f"{run.pack_id}@{run.pack_version}",
        stages={name: row.metrics or {} for name, row in latest.items()},
        llm_spent_usd=float(spent),
        llm_clean_usd=float(sum(by_agent.values(), Decimal(0))),
        llm_by_agent={k: float(v) for k, v in by_agent.items()},
        live_searches=searches,
        search_usd=round(searches * price, 4) if price is not None else None,
        wall_s=wall,
        categories=dict(Counter(c[1] for c in cards)),
        top_score=max(scores) if scores else None,
        verdicts=dict(Counter(final.values())),
    )


def evaluate_runs(db: sessionmaker[Session], run_ids: list[int], cfg: Defaults) -> list[RunEval]:
    return [_eval_run(db, run_id, cfg) for run_id in run_ids]


def _fmt(value: Any, spec: str = "") -> str:
    if value is None:
        return "-"
    return format(value, spec) if spec else str(value)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def render_evaluation(rows: list[RunEval], out: Path) -> Path:
    lines = [
        "# Multi-market evaluation",
        "",
        "One full pipeline run per market (`signalforge evaluate`). Cost is one clean pass: each "
        "stage's latest run. *Spent* also counts re-runs during tuning.",
        "",
        "## Funnel",
        "",
        "| Market | Run | Docs | Signals | Quote pass | First-hand | Clusters | Gate 1 | Verified "
        "| Competitors | Opps | Gate 2 pass |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        cells = [
            r.metric("fetch", "documents"), r.metric("extract", "signals"),
            _pct(r.metric("extract", "quote_pass_rate")),
            _pct(r.metric("extract", "first_hand_share")), r.metric("cluster", "clusters"),
            r.metric("shortlist", "shortlisted"), r.metric("verify", "passed"),
            r.metric("competitors", "competitors"), r.metric("monetization", "opportunities"),
            r.metric("monetization", "passed"),
        ]  # fmt: skip
        lines.append(f"| {r.market} | {r.run_id} | " + " | ".join(map(_fmt, cells)) + " |")
    lines += [
        "",
        "## Outcomes",
        "",
        "| Market | Run | " + " | ".join(CATEGORIES) + " | Top score | Founder verdicts |",
        "|---|---:|" + "---:|" * len(CATEGORIES) + "---:|---|",
    ]
    for r in rows:
        verdicts = ", ".join(f"{v} {r.verdicts[v]}" for v in VERDICTS if v in r.verdicts)
        lines.append(
            f"| {r.market} | {r.run_id} | "
            + " | ".join(str(r.categories.get(c, 0)) for c in CATEGORIES)
            + f" | {_fmt(r.top_score, '.1f')} | {verdicts or 'not labelled'} |"
        )
    agents = [a.name for a in AGENTS]
    lines += [
        "",
        "## Cost per run",
        "",
        "| Market | Run | LLM | Live searches | Search | **Total** | Spent (LLM, all passes) "
        "| Wall time |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r.market} | {r.run_id} | ${r.llm_clean_usd:.3f} | {r.live_searches} "
            f"| {'-' if r.search_usd is None else f'${r.search_usd:.3f}'} "
            f"| **{'-' if r.clean_usd is None else f'${r.clean_usd:.3f}'}** "
            f"| ${r.llm_spent_usd:.3f} | {r.wall_s / 60:.0f} min |"
        )
    totals = [r.clean_usd for r in rows if r.clean_usd is not None]
    if totals:
        lines.append(
            f"| **median** | | ${median(r.llm_clean_usd for r in rows):.3f} "
            f"| {median(r.live_searches for r in rows):.0f} | | **${median(totals):.3f}** | "
            f"| {median(r.wall_s for r in rows) / 60:.0f} min |"
        )
    lines += [
        "",
        "LLM cost by agent (clean pass):",
        "",
        "| Market | " + " | ".join(agents) + " |",
        "|---|" + "---:|" * len(agents),
    ]
    for r in rows:
        lines.append(
            f"| {r.market} | "
            + " | ".join(f"${r.llm_by_agent.get(a, 0):.4f}" for a in agents)
            + " |"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def summary(rows: list[RunEval]) -> list[dict[str, Any]]:
    return [
        {
            "run_id": r.run_id, "market": r.market, "status": r.status, "pack": r.pack,
            "llm_clean_usd": r.llm_clean_usd, "llm_spent_usd": r.llm_spent_usd,
            "live_searches": r.live_searches, "search_usd": r.search_usd,
            "clean_usd": r.clean_usd, "wall_s": r.wall_s, "llm_by_agent": r.llm_by_agent,
            "categories": r.categories, "top_score": r.top_score, "verdicts": r.verdicts,
        }
        for r in rows
    ]  # fmt: skip
