"""Market scan comparison: which market shows the most work a small team's software could take over.

Reads runs that went at least through ``extract`` (a scan stops at ``shortlist``) and ranks them by
independent sources with a software-fit signal (evidence/fit.py), the same measure Gate 1 uses.
Raw signal counts would reward markets with one talkative page; independent sources don't. For each
market it also lists the manual tasks behind its fit signals, with the quotes as evidence. No LLM
calls: everything is read from the database.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import Defaults
from signalforge.db.models import (
    Document,
    Excerpt,
    IndependenceGroup,
    ProblemCluster,
    ResearchRun,
    Signal,
)
from signalforge.evidence.fit import software_fit
from signalforge.evidence.independence import source_units


@dataclass
class FitExample:
    manual_task: str | None
    statement: str
    quote: str
    translation: str | None
    url: str
    first_hand: bool


@dataclass
class MarketRow:
    run_id: int
    market: str
    status: str
    spent_usd: float
    documents: int
    signals: int
    fit_signals: int
    fit_first_hand: int
    fit_sources: int  # independent sources with ≥1 software-fit signal: the ranking measure
    clusters: int
    shortlisted: int
    fit_by_type: dict[str, int] = field(default_factory=dict)
    examples: list[FitExample] = field(default_factory=list)

    @property
    def fit_share(self) -> float | None:
        return round(self.fit_signals / self.signals, 2) if self.signals else None


def market_row(session: Session, run_id: int, defaults: Defaults, examples: int) -> MarketRow:
    run = session.get(ResearchRun, run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    rows = session.execute(
        select(Signal, Excerpt, Document)
        .join(Excerpt, Signal.excerpt_id == Excerpt.id)
        .join(Document, Excerpt.document_id == Document.id)
        .where(Signal.run_id == run_id, Excerpt.stage == "extract")
        .order_by(Signal.id)
    ).all()
    docs = session.execute(
        select(Document.id, Document.origin).where(Document.run_id == run_id)
    ).all()
    doc_ids = [i for i, _ in docs]
    groups = session.scalars(
        select(IndependenceGroup.document_ids).where(IndependenceGroup.run_id == run_id)
    ).all()
    units = source_units(doc_ids, groups)
    clusters = session.scalars(select(ProblemCluster).where(ProblemCluster.run_id == run_id)).all()

    fit_rows = [
        (s, e, d)
        for s, e, d in rows
        if software_fit((s.meta or {}).get("fit"), defaults.software_fit)
    ]
    # Best evidence first: first-hand, then one example per source.
    fit_rows.sort(key=lambda r: (not r[0].first_hand, r[0].id))
    seen_units: set[int] = set()
    picked: list[FitExample] = []
    for s, e, d in fit_rows:
        unit = units.get(d.id, d.id)
        if unit in seen_units:
            continue
        seen_units.add(unit)
        picked.append(
            FitExample(
                (s.meta or {}).get("fit", {}).get("manual_task"),
                s.statement,
                e.quote,
                e.translation,
                d.url,
                s.first_hand,
            )
        )
    plan = run.plan or {}
    return MarketRow(
        run_id=run_id,
        market=(plan.get("request") or {}).get("industry") or (run.request or {}).get("industry"),
        status=run.status,
        spent_usd=float(run.spent_usd or 0),
        documents=sum(origin == "collect" for _, origin in docs),
        signals=len(rows),
        fit_signals=len(fit_rows),
        fit_first_hand=sum(s.first_hand for s, _, _ in fit_rows),
        fit_sources=len({units.get(d.id, d.id) for _, _, d in fit_rows}),
        clusters=len(clusters),
        shortlisted=sum(bool(c.shortlisted) for c in clusters),
        fit_by_type=dict(Counter(s.type for s, _, _ in fit_rows).most_common()),
        examples=picked[:examples],
    )


def compare_markets(
    db: sessionmaker[Session], run_ids: list[int], defaults: Defaults, examples: int = 5
) -> list[MarketRow]:
    """One row per run, best market first: fit sources, then first-hand fit signals, then run id."""
    with db() as session:
        rows = [market_row(session, r, defaults, examples) for r in run_ids]
    return sorted(rows, key=lambda m: (-m.fit_sources, -m.fit_first_hand, m.run_id))


def render_comparison(rows: list[MarketRow], path: Path) -> Path:
    """Markdown report: the ranking table, then each market's manual tasks with their quotes."""
    lines = [
        "# Market scan: work a small team's software could take over",
        "",
        "Ranked by **independent sources with a software-fit signal** (recurring manual work on",
        "documents, messages or data, caused by the business's own process, a weak tool or",
        "paperwork rules). Raw counts are shown for context.",
        "",
        "| Rank | Market | Run | Fit sources | Fit signals (first-hand) | Signals | Fit share "
        "| Documents | Shortlisted | Spent |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, m in enumerate(rows, 1):
        share = f"{m.fit_share:.0%}" if m.fit_share is not None else "–"
        lines.append(
            f"| {i} | {m.market} | {m.run_id} | **{m.fit_sources}** | {m.fit_signals} "
            f"({m.fit_first_hand}) | {m.signals} | {share} | {m.documents} | "
            f"{m.shortlisted}/{m.clusters} | ${m.spent_usd:.3f} |"
        )
    for m in rows:
        lines += ["", f"## {m.market} (run {m.run_id})", ""]
        if m.fit_by_type:
            lines.append(
                "Fit signals by type: " + ", ".join(f"{k} {v}" for k, v in m.fit_by_type.items())
            )
            lines.append("")
        if not m.examples:
            lines.append("No software-fit signals found.")
            continue
        for ex in m.examples:
            hand = "first-hand" if ex.first_hand else "second-hand"
            lines.append(f"- **{ex.manual_task or ex.statement}** ({hand})")
            lines.append(f'  > „{ex.quote}"')
            if ex.translation:
                lines.append(f"  > — *{ex.translation}*")
            lines.append(f"  > {ex.url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def summary(rows: list[MarketRow]) -> list[dict[str, Any]]:
    """JSON-safe ranking (for --json)."""
    return [
        {
            "rank": i,
            "market": m.market,
            "run_id": m.run_id,
            "fit_sources": m.fit_sources,
            "fit_signals": m.fit_signals,
            "fit_first_hand": m.fit_first_hand,
            "signals": m.signals,
            "documents": m.documents,
            "shortlisted": m.shortlisted,
            "clusters": m.clusters,
            "spent_usd": m.spent_usd,
            "fit_by_type": m.fit_by_type,
        }
        for i, m in enumerate(rows, 1)
    ]
