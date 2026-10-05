"""Problem landscape report (plan §13 M3): the intermediate report after Gate 1.

Built from stored rows only, with no LLM call: every quote is a verified excerpt with its source,
every number is computed from the evidence graph. The only model-written text is each cluster's
name and description, which the report labels as a summary. ``landscape.json`` is the source of
truth; the Markdown and HTML files are renderings of it. Excerpts only, never full page text.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import Defaults
from signalforge.db.models import (
    Document,
    Excerpt,
    IndependenceGroup,
    LLMCall,
    ProblemCluster,
    ResearchRun,
    Signal,
)
from signalforge.evidence.independence import source_units
from signalforge.pipeline.runner import latest_stage_runs
from signalforge.pipeline.stages.shortlist import failed_rules

TEMPLATES = Path(__file__).parent / "templates"
QUOTES_PER_PROBLEM = 5

# Funnel rows: (label, stage, metric).
FUNNEL = [
    ("Queries", "query_gen", "kept"),
    ("Search results", "search", "results"),
    ("Unique URLs", "search", "unique_urls"),
    ("URLs kept by triage", "triage", "keep"),
    ("Documents", "fetch", "documents"),
    ("Independent documents (after duplicate collapse)", "dedupe", "independent_sources"),
    ("Documents read for signals", "extract", "documents_read"),
    ("Quotes proposed by the model", "extract", "quotes_proposed"),
    ("Verified signals", "extract", "signals"),
    ("Problem clusters", "cluster", "clusters"),
    ("Shortlisted (Gate 1)", "shortlist", "shortlisted"),
]

RULE_TEXT = {
    "min_strength": "evidence strength below the bar",
    "min_independent_sources": "too few independent sources",
    "max_shortlisted": "passed, but outside the shortlist cap",
}


def _quote_rank(q: dict[str, Any], tiers: dict[str, float]) -> tuple[Any, ...]:
    """Best evidence first: first-hand, source quality, exact match, full text, newest."""
    return (
        not q["first_hand"],
        -tiers.get(q["tier"] or "", 0.0),
        q["verified"] != "exact",
        q["snippet_only"],
        -(datetime.fromisoformat(q["published"]).timestamp() if q["published"] else 0.0),
        q["signal_id"],
    )


def pick_quotes(
    quotes: list[dict[str, Any]], units: dict[int, int], tiers: dict[str, float], n: int
) -> list[dict[str, Any]]:
    """Up to ``n`` strongest quotes, one per independent source while sources last."""
    ranked = sorted(quotes, key=lambda q: _quote_rank(q, tiers))
    chosen: list[dict[str, Any]] = []
    seen_units: set[int] = set()
    for q in ranked:
        unit = units.get(q["document_id"], q["document_id"])
        if unit not in seen_units:
            seen_units.add(unit)
            chosen.append(q)
    for q in ranked:  # fewer sources than slots: fill with further quotes
        if len(chosen) >= n:
            break
        if q not in chosen:
            chosen.append(q)
    return chosen[:n]


def build_landscape(db: sessionmaker[Session], run_id: int, defaults: Defaults) -> dict[str, Any]:
    with db() as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        clusters = session.scalars(
            select(ProblemCluster)
            .where(ProblemCluster.run_id == run_id)
            .order_by(ProblemCluster.rank, ProblemCluster.id)
        ).all()
        rows = session.execute(
            select(Signal, Excerpt, Document)
            .join(Excerpt, Signal.excerpt_id == Excerpt.id)
            .join(Document, Excerpt.document_id == Document.id)
            .where(Signal.run_id == run_id)
        ).all()
        groups = session.scalars(
            select(IndependenceGroup.document_ids).where(IndependenceGroup.run_id == run_id)
        ).all()
        doc_ids = session.scalars(select(Document.id).where(Document.run_id == run_id)).all()
        models = session.execute(
            select(LLMCall.stage, LLMCall.model).where(LLMCall.run_id == run_id).distinct()
        ).all()
    stages = latest_stage_runs(db, run_id)
    units = source_units(doc_ids, groups)

    quotes: dict[int, dict[str, Any]] = {}
    for sig, ex, doc in rows:
        quotes[sig.id] = {
            "signal_id": sig.id,
            "type": sig.type,
            "first_hand": sig.first_hand,
            "actor": sig.actor,
            "statement": sig.statement,
            "quote": ex.quote,
            "translation": ex.translation,
            "verified": ex.verified,
            "document_id": doc.id,
            "domain": doc.domain,
            "url": doc.url,
            "title": doc.title,
            "tier": doc.quality_tier,
            "category": doc.source_category,
            "snippet_only": doc.snippet_only,
            "published": doc.published_at.date().isoformat() if doc.published_at else None,
        }

    def problem(c: ProblemCluster) -> dict[str, Any]:
        members = [quotes[i] for i in c.signal_ids if i in quotes]
        first_hand = sum(q["first_hand"] for q in members)
        return {
            "id": c.id,
            "rank": c.rank,
            "name": c.name,
            "description": c.description,
            "signals": len(c.signal_ids),
            "independent_sources": c.independent_source_count,
            "evidence_strength": c.evidence_strength,
            "strength_components": (c.strength or {}).get("components", {}),
            "source_category_mix": c.source_category_mix,
            "signal_type_mix": c.signal_type_mix,
            "first_hand_share": round(first_hand / len(members), 2) if members else None,
            "failed_rules": [RULE_TEXT.get(r, r) for r in failed_rules(c.gate_trace or [])],
            "quotes": pick_quotes(
                members, units, defaults.strength.tier_weights, QUOTES_PER_PROBLEM
            ),
        }

    def metric(stage: str, key: str) -> Any:
        row = stages.get(stage)
        return (row.metrics or {}).get(key) if row else None

    by_stage: dict[str, set[str]] = defaultdict(set)
    for stage, model in models:
        by_stage[stage or "-"].add(model)
    plan = run.plan or {}
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run": {
            "id": run.id,
            "status": run.status,
            "created_at": run.created_at.isoformat(timespec="seconds"),
            "request": run.request,
            "submarkets": [s["name"] for s in plan.get("submarkets", [])],
            "pack": f"{run.pack_id}@{run.pack_version}",
            "spent_usd": float(run.spent_usd),
            "budget_usd": float(run.budget_usd),
            "models": {stage: sorted(m) for stage, m in sorted(by_stage.items())},
        },
        "funnel": [{"step": label, "count": metric(stage, key)} for label, stage, key in FUNNEL],
        "quality": {
            "quote_pass_rate": metric("extract", "quote_pass_rate"),
            "quotes_fuzzy": metric("extract", "quotes_fuzzy"),
            "first_hand_share": metric("extract", "first_hand_share"),
            "signals_by_type": metric("extract", "signals_by_type") or {},
            "duplicate_collapse_rate": metric("dedupe", "duplicate_collapse_rate"),
            "noise_signals": metric("cluster", "noise"),
        },
        "gate": defaults.shortlist.model_dump(mode="json"),
        "shortlist": [problem(c) for c in clusters if c.shortlisted],
        "insufficient_evidence": [problem(c) for c in clusters if not c.shortlisted],
    }


def render_landscape(report: dict[str, Any], out_dir: Path) -> list[Path]:
    """Write landscape.json / .md / .html into ``out_dir``; returns the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(enabled_extensions=("html.j2",), default=False),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v: "–" if v is None else f"{v:.0%}"
    written = []
    path = out_dir / "landscape.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(path)
    for ext in ("md", "html"):
        path = out_dir / f"landscape.{ext}"
        path.write_text(env.get_template(f"landscape.{ext}.j2").render(r=report), encoding="utf-8")
        written.append(path)
    return written
