"""Research-quality benchmark (docs/eval/metrics.md): quality, cost and latency of frozen runs.

Reads the runs of a benchmark manifest (``evals/benchmark/v1.yaml``) from a database their
fixtures were imported into; no provider is called. Every metric carries its tier and its ``n``:

- ``deterministic``: computed by code from stored rows and cached pages;
- ``llm_judged``: verdicts a model gave inside the pipeline (entailment), never audited;
- ``proxy_label``: model labels standing in for the founder (labeler ``proxy:*``);
- ``human_label``: labels by people.

A metric the data cannot support is reported ``unavailable`` with the reason, never estimated.
``replay_scenario`` re-runs a scenario's plan in replay mode and reports how far the recording
still reproduces it (the first stage whose LLM inputs changed stops it) and whether the stages it
re-ran give the recorded metrics.
"""

import csv
import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import Defaults
from signalforge.db.models import (
    CacheEntry,
    Claim,
    Competitor,
    Document,
    Excerpt,
    Label,
    Opportunity,
    ResearchRun,
    ScoreCard,
)
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.entailment import failed
from signalforge.evidence.gaps import check_gap_matrices
from signalforge.evidence.opportunities import (
    check_buyer_roles,
    check_economic_models,
    check_score_cards,
)
from signalforge.fixtures import LABEL_TARGETS
from signalforge.packs import load_pack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import Stage, create_run, latest_stage_runs, run_pipeline
from signalforge.providers.cache import cache_key
from signalforge.providers.urls import canonicalize_url
from signalforge.reporting.evaluation import RunEval, evaluate_runs
from signalforge.scoring.scorer import MODEL_FACTORS, attractiveness

TIERS = ("deterministic", "llm_judged", "proxy_label", "human_label")
PROXY = "proxy:"
# Stage-metric keys that describe the cache, not the research: a replay differs from the
# recording on them by design.
CACHE_KEYS = frozenset({"cache_hits", "searched_live", "searches_live", "retried",
                        "page_cache_hits", "llm_cache_hits"})  # fmt: skip
# Human labels per target type below which a label-based metric is not reported.
MIN_HUMAN_LABELS = 20


@dataclass
class Metric:
    """``num / den`` for a share (``den`` is its n), else ``num`` is the value. ``pool`` says how
    scenarios combine: ``share`` sums both parts, ``sum`` adds values, ``median`` takes it."""

    name: str
    tier: str
    num: float | None = None
    den: int | None = None
    pool: str = "share"
    unavailable: str | None = None

    @property
    def value(self) -> float | None:
        if self.unavailable is not None or self.num is None:
            return None
        if self.pool != "share":
            return round(self.num, 4)
        return round(self.num / self.den, 4) if self.den else None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tier": self.tier, "value": self.value}
        if self.pool == "share":
            out["n"] = self.den
        if self.unavailable is not None:
            out["unavailable"] = self.unavailable
        return out


def share(name: str, tier: str, num: float, den: int, empty: str | None = None) -> Metric:
    """A share; ``empty`` is the reason it is unavailable when ``den`` is 0."""
    if den == 0 and empty is not None:
        return Metric(name, tier, unavailable=empty)
    return Metric(name, tier, num, den)


def value(name: str, tier: str, v: float | None, pool: str = "sum") -> Metric:
    return Metric(name, tier, v, None, pool)


def unavailable(name: str, tier: str, reason: str) -> Metric:
    return Metric(name, tier, unavailable=reason)


@dataclass
class Scenario:
    id: str
    industry: str
    run: int
    split: str
    fixture: str


@dataclass
class Benchmark:
    version: int
    scenarios: list[Scenario]
    proxy: dict[tuple[int, int], dict[str, str]]  # (run, opportunity id) -> TSV row


def load_benchmark(path: Path) -> Benchmark:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    proxy: dict[tuple[int, int], dict[str, str]] = {}
    for source in raw.get("proxy_labels", []):
        if not source["labeler"].startswith(PROXY) or source["target_type"] != "opportunity":
            raise ValueError(f"{path}: proxy labels must be opportunity labels by '{PROXY}*'")
        with (path.parent / source["file"]).open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                proxy[(int(row["run"]), int(row["opportunity"]))] = row
    scenarios = [Scenario(**s) for s in raw["scenarios"]]
    return Benchmark(raw["version"], scenarios, proxy)


# --- one scenario -----------------------------------------------------------------------------


def _quote_metrics(session: Session, run_id: int, stages: dict[str, dict]) -> list[Metric]:
    """Quote verification (extract) and integrity: every stored text excerpt must equal the
    cached page text at its offsets."""
    ext = stages.get("extract", {})
    proposed = ext.get("quotes_proposed") or 0
    exact, fuzzy = ext.get("quotes_exact") or 0, ext.get("quotes_fuzzy") or 0
    rows = session.execute(
        select(Excerpt.quote, Excerpt.char_start, Excerpt.char_end, Excerpt.source, Document.url,
               Document.canonical_url)
        .join(Document, Document.id == Excerpt.document_id)
        .where(Excerpt.run_id == run_id)
    ).all()  # fmt: skip
    keys = {
        k
        for r in rows
        if r.source == "text"
        for k in (cache_key({"canonical_url": canonicalize_url(r.url)}),
                  cache_key({"canonical_url": r.canonical_url}))
    }  # fmt: skip
    texts = {
        key: response.get("text")
        for key, response in session.execute(
            select(CacheEntry.key, CacheEntry.response).where(
                CacheEntry.namespace == "page", CacheEntry.key.in_(sorted(keys))
            )
        )
    }
    match = checked = 0
    for r in rows:
        if r.source != "text":
            continue
        text = texts.get(cache_key({"canonical_url": canonicalize_url(r.url)})) or texts.get(
            cache_key({"canonical_url": r.canonical_url})
        )
        if not text or r.char_start is None:
            continue
        checked += 1
        match += text[r.char_start : r.char_end] == r.quote
    return [
        share("quote_pass_rate", "deterministic", exact + fuzzy, proposed),
        share("quote_fuzzy_share", "deterministic", fuzzy, exact + fuzzy),
        share("quote_integrity", "deterministic", match, checked),
        share("quote_integrity_unchecked_share", "deterministic", len(rows) - checked, len(rows)),
    ]


def _independence_metrics(stages: dict[str, dict]) -> list[Metric]:
    d = stages.get("dedupe", {})
    docs = d.get("documents") or 0
    return [
        share("duplicate_collapse_rate", "deterministic",
              docs - (d.get("independent_sources") or docs), docs),
        # Documents dedupe never compared (snippet-only, or the page left the cache): their
        # syndicated copies count as independent sources (audit R1, R2).
        share("dedupe_unchecked_share", "deterministic", docs - (d.get("compared") or 0), docs),
    ]  # fmt: skip


def _claim_metrics(session: Session, run_id: int, cards: list[ScoreCard]) -> list[Metric]:
    facts = session.scalars(select(Claim).where(Claim.run_id == run_id, Claim.kind == "fact")).all()
    checked = [c for c in facts if c.entailment is not None]
    verdicts = Counter(c.entailment for c in checked)
    by_id = {c.id: c for c in facts}
    cited = {
        i for card in cards for f in card.factors.values() for i in f.get("claim_ids", [])
    } & set(by_id)
    cited_verdicts = Counter(by_id[i].entailment for i in cited)
    n_failed = sum(v for k, v in verdicts.items() if failed(k))
    return [
        share("fact_checked_share", "llm_judged", len(checked), len(facts)),
        share("fact_supported_share", "llm_judged", verdicts["supported"], len(checked)),
        share("fact_partial_share", "llm_judged", verdicts["partial"], len(checked)),
        share("fact_failed_share", "llm_judged", n_failed, len(checked)),
        share("cited_fact_supported_share", "llm_judged", cited_verdicts["supported"], len(cited)),
        share("cited_fact_partial_share", "llm_judged", cited_verdicts["partial"], len(cited)),
        share("cited_fact_unchecked_share", "llm_judged", cited_verdicts[None], len(cited)),
    ]


def _economics_metrics(session: Session, run_id: int) -> list[Metric]:
    assumptions = session.scalars(
        select(Claim.meta).where(Claim.run_id == run_id, Claim.kind == "assumption")
    ).all()
    sourced = [m for m in assumptions if m.get("sourced")]
    traced = [m for m in sourced if m.get("source_url") and m.get("as_of")]
    return [
        share("assumption_sourced_share", "deterministic", len(sourced), len(assumptions)),
        share("sourced_assumption_url_and_date_share", "deterministic", len(traced), len(sourced)),
    ]


def _competitor_metrics(session: Session, run_id: int) -> list[Metric]:
    prices = [
        p
        for pricing in session.scalars(
            select(Competitor.pricing).where(Competitor.run_id == run_id)
        )
        for p in pricing
    ]
    claims = dict(
        session.execute(
            select(Claim.id, Claim.entailment).where(
                Claim.id.in_([p["claim_id"] for p in prices if p.get("claim_id")])
            )
        ).all()
    )
    verdicts = Counter(claims.get(p.get("claim_id")) for p in prices)
    return [
        share("price_dated_share", "deterministic",
              sum(bool(p.get("observed_at") and p.get("claim_id")) for p in prices), len(prices)),
        share("price_fact_supported_share", "llm_judged",
              verdicts["supported"] + verdicts["partial"], len(prices)),
        share("price_fact_failed_share", "llm_judged",
              sum(v for k, v in verdicts.items() if failed(k)), len(prices)),
        share("price_fact_unchecked_share", "llm_judged", verdicts[None], len(prices)),
    ]  # fmt: skip


def _stability_metrics(cards: list[ScoreCard], weights: dict[str, int]) -> list[Metric]:
    """Agreement of the k judges on one card (score.k_judges for the top cards): the range of
    the attractiveness each judge alone gives, and cards with a factor spread ≥ 2 levels."""
    multi = [c for c in cards if c.factors and len(c.factors["severity"].get("judges", [])) > 1]
    ranges, wide = [], 0
    for card in multi:
        k = len(card.factors["severity"]["judges"])
        per_judge = [
            attractiveness({f: card.factors[f]["judges"][i] for f in weights}, weights)
            for i in range(k)
        ]
        ranges.append(max(per_judge) - min(per_judge))
        wide += max(card.factors[f]["spread"] for f in MODEL_FACTORS) >= 2
    if not multi:
        reason = "no card was judged more than once"
        return [unavailable("attractiveness_judge_range_median", "llm_judged", reason),
                unavailable("factor_spread_ge2_share", "llm_judged", reason)]  # fmt: skip
    return [
        value("attractiveness_judge_range_median", "llm_judged", median(ranges), pool="median"),
        share("factor_spread_ge2_share", "llm_judged", wide, len(multi)),
    ]


def _proxy_metrics(
    run_id: int, cards: list[tuple[Opportunity, ScoreCard]], proxy: dict, ev: RunEval
) -> list[Metric]:
    tier = "proxy_label"
    labelled = {o.id: proxy[(run_id, o.id)]["verdict"] for o, _ in cards if (run_id, o.id) in proxy}
    scored = sorted(
        ((o, c) for o, c in cards if c.attractiveness is not None),
        key=lambda oc: (-oc[1].attractiveness, oc[0].id),
    )
    top = [labelled[o.id] for o, _ in scored[:3] if o.id in labelled]
    out = [
        share("precision_at_3_proxy", tier, top.count("investigate"), len(top), "no proxy labels"),
        share("precision_at_3_lenient_proxy", tier,
              sum(v in ("investigate", "maybe") for v in top), len(top), "no proxy labels"),
    ]  # fmt: skip
    for category in ("strong", "interesting"):
        verdicts = [labelled[o.id] for o, c in cards if c.category == category and o.id in labelled]
        out.append(share(f"false_positive_rate_{category}_proxy", tier, verdicts.count("no"),
                         len(verdicts), f"no {category} card with a proxy label"))  # fmt: skip
    for level in ("high", "medium", "low"):
        verdicts = [labelled[o.id] for o, c in cards if c.confidence == level and o.id in labelled]
        out.append(share(f"investigate_share_confidence_{level}_proxy", tier,
                         verdicts.count("investigate"), len(verdicts),
                         f"no {level}-confidence card with a proxy label"))  # fmt: skip
    investigate = list(labelled.values()).count("investigate")
    out.append(
        value("cost_per_investigate_proxy_usd", tier,
              ev.clean_usd / investigate if investigate and ev.clean_usd is not None else None,
              pool="median")
        if investigate
        else unavailable("cost_per_investigate_proxy_usd", tier, "no proxy `investigate` verdict")
    )  # fmt: skip
    return out


def _human_metrics(session: Session, run_id: int) -> list[Metric]:
    """Counts of human labels on the run; label-based metrics stay unavailable below
    ``MIN_HUMAN_LABELS`` per target type."""
    counts: Counter[str] = Counter()
    for target_type, model in LABEL_TARGETS.items():
        ids = select(model.id).where(model.run_id == run_id)
        counts[target_type] = len(
            session.scalars(
                select(Label.id).where(
                    Label.target_type == target_type,
                    Label.target_id.in_(ids),
                    Label.labeler.not_like(f"{PROXY}%"),
                )
            ).all()
        )
    tier = "human_label"
    out = [value(f"human_labels_{t}", tier, n) for t, n in sorted(counts.items())]

    def need(target: str) -> str:
        return f"{counts[target]} human {target} labels < {MIN_HUMAN_LABELS}"

    # Not yet computed from human labels: each needs a label type the CLI does not collect yet
    # (evals/label_schema.yaml) or more labels than exist.
    out += [
        unavailable("claim_support_precision", tier, "no human claim labels (label_schema: claim)"),
        unavailable("independent_source_precision", tier,
                    "no human source-pair labels (label_schema: source_pair)"),
        unavailable("problem_discovery_precision", tier, need("cluster")),
        unavailable("problem_discovery_recall", tier,
                    "no reference problem list (label_schema: reference_problem)"),
        unavailable("precision_at_3", tier, need("opportunity")),
        unavailable("false_positive_rate_strong", tier, need("opportunity")),
        unavailable("confidence_calibration", tier, need("opportunity")),
        unavailable("competitor_fact_accuracy", tier, "no human competitor-fact labels"),
    ]  # fmt: skip
    return out


def scenario_metrics(
    db: sessionmaker[Session], scenario: Scenario, cfg: Defaults, proxy: dict
) -> list[Metric]:
    run_id = scenario.run
    with db() as session:
        if session.get(ResearchRun, run_id) is None:
            raise ValueError(f"run {run_id} not found: import {scenario.fixture} first")
    (ev,) = evaluate_runs(db, [run_id], cfg)
    stages = ev.stages
    latest = latest_stage_runs(db, run_id)
    with db() as session:
        pairs = [
            (o, c)
            for o, c in session.execute(
                select(Opportunity, ScoreCard)
                .join(ScoreCard, ScoreCard.opportunity_id == Opportunity.id)
                .where(Opportunity.run_id == run_id)
                .order_by(Opportunity.id)
            )
        ]
        cards = [c for _, c in pairs]
        violations = sum(
            len(check(session, run_id))
            for check in (check_gap_matrices, check_buyer_roles, check_economic_models)
        ) + len(check_score_cards(session, run_id, cfg.score))
        out = [
            *_quote_metrics(session, run_id, stages),
            *_independence_metrics(stages),
            *_economics_metrics(session, run_id),
            *_competitor_metrics(session, run_id),
            *_claim_metrics(session, run_id, cards),
            *_human_metrics(session, run_id),
        ]
    categories = Counter(c.category for c in cards)
    passed = ev.metric("monetization", "passed") or 0
    det = "deterministic"
    out += [
        value("stages_completed", det, sum(r.status == "completed" for r in latest.values())),
        value("stage_failures", det, sum(r.status != "completed" for r in latest.values())),
        value("exit_check_violations", det, violations),
        value("documents", det, ev.metric("fetch", "documents")),
        value("signals", det, ev.metric("extract", "signals")),
        value("clusters", det, ev.metric("cluster", "clusters")),
        value("shortlisted", det, ev.metric("shortlist", "shortlisted")),
        value("opportunities", det, ev.metric("monetization", "opportunities")),
        value("gate2_passed", det, passed),
        *(value(f"cards_{c}", det, categories.get(c, 0)) for c in ("strong", "interesting",
                                                                   "competitive", "weak")),
        value("cost_usd", det, ev.clean_usd),
        value("llm_usd", det, ev.llm_clean_usd),
        value("live_searches", det, ev.live_searches),
        value("wall_min", det, round(ev.wall_s / 60, 1)),
        value("cost_per_gate2_survivor_usd", det,
              ev.clean_usd / passed if passed and ev.clean_usd is not None else None, "median"),
        *_stability_metrics(cards, cfg.score.weights),
        *_proxy_metrics(run_id, pairs, proxy, ev),
    ]  # fmt: skip
    return out


# --- replay -----------------------------------------------------------------------------------


def _research_part(metrics: Any) -> Any:
    """Stage metrics without cache counters and without lists (they carry run-specific ids)."""
    if isinstance(metrics, dict):
        return {k: _research_part(v) for k, v in metrics.items()
                if k not in CACHE_KEYS and not isinstance(v, list)}  # fmt: skip
    return metrics


@dataclass
class Replay:
    run_id: int
    stages: list[str] = field(default_factory=list)  # completed in replay
    stopped_at: str | None = None  # first stage that failed
    error: str | None = None
    mismatched: dict[str, list[str]] = field(default_factory=dict)  # stage -> differing keys

    @property
    def reproduced(self) -> int:
        return sum(s not in self.mismatched for s in self.stages)


def replay_scenario(
    db: sessionmaker[Session],
    scenario: Scenario,
    stages: list[Stage],
    make_context: Callable[[int, str], RunContext],
    cfg: Defaults,
) -> Replay:
    """A new run from the scenario's plan, in replay mode, as far as the recording reaches."""
    with db() as session:
        run = session.get_one(ResearchRun, scenario.run)
        plan, pack_id = ResearchPlan.model_validate(run.plan), run.pack_id
    new_id = create_run(db, plan, load_pack(pack_id), cfg)
    result = Replay(new_id)
    try:
        run_pipeline(make_context(new_id, pack_id), stages)
    except Exception as exc:  # the first stage the recording does not cover
        result.error = f"{type(exc).__name__}: {exc}"[:300]
    recorded = latest_stage_runs(db, scenario.run)
    for name, row in latest_stage_runs(db, new_id).items():
        if row.status != "completed":
            result.stopped_at = name
            continue
        result.stages.append(name)
        want = _research_part(recorded[name].metrics) if name in recorded else None
        got = _research_part(row.metrics)
        if want != got:
            keys = sorted(
                k for k in set(want or {}) | set(got) if (want or {}).get(k) != got.get(k)
            )
            result.mismatched[name] = keys
    return result


# --- report -----------------------------------------------------------------------------------


def pool(metrics: Iterable[Metric]) -> Metric | None:
    items = [m for m in metrics if m.unavailable is None and m.num is not None]
    if not items:
        return None
    first = items[0]
    if first.pool == "share":
        return Metric(first.name, first.tier, sum(m.num for m in items),
                      sum(m.den or 0 for m in items))  # fmt: skip
    values = [m.num for m in items]
    total = sum(values) if first.pool == "sum" else median(values)
    return Metric(first.name, first.tier, total, None, first.pool)


def run_benchmark(
    db: sessionmaker[Session],
    bench: Benchmark,
    cfg: Defaults,
    replay: Callable[[Scenario], Replay] | None = None,
) -> dict[str, Any]:
    scenarios = []
    by_split: dict[str, list[list[Metric]]] = {}
    for s in bench.scenarios:
        metrics = scenario_metrics(db, s, cfg, bench.proxy)
        by_split.setdefault(s.split, []).append(metrics)
        entry: dict[str, Any] = {
            **asdict(s),
            "metrics": {m.name: m.as_dict() for m in metrics},
        }
        if replay is not None:
            r = replay(s)
            entry["replay"] = asdict(r) | {"reproduced": r.reproduced}
        scenarios.append(entry)
    pooled = {}
    for split, runs in by_split.items():
        names = [m.name for m in runs[0]]
        pooled[split] = {}
        for i, name in enumerate(names):
            column = [r[i] for r in runs]
            merged = pool(column)
            if merged is None:
                reasons = sorted({m.unavailable for m in column if m.unavailable})
                merged = unavailable(name, column[0].tier, "; ".join(reasons) or "no data")
            pooled[split][name] = merged.as_dict()
    return {
        "benchmark": bench.version,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scenarios": scenarios,
        "pooled": pooled,
    }


def _cell(m: dict[str, Any]) -> str:
    if m.get("unavailable"):
        return "n/a"
    v = m["value"]
    if v is None:
        return "-"
    text = f"{v:.1%}" if "n" in m else (f"{v:g}" if isinstance(v, float) else str(v))
    return f"{text} ({m['n']})" if "n" in m else text


def compare(result: dict[str, Any], baseline: dict[str, Any]) -> list[tuple[str, str, Any, Any]]:
    """(split, metric, baseline value, current value) for pooled metrics that changed."""
    out = []
    for split, metrics in result["pooled"].items():
        before = baseline.get("pooled", {}).get(split, {})
        for name, m in metrics.items():
            old = before.get(name, {}).get("value")
            if old != m["value"]:
                out.append((split, name, old, m["value"]))
    return out


def render_benchmark(
    result: dict[str, Any], out: Path, baseline: dict[str, Any] | None = None
) -> list[Path]:
    scenarios = result["scenarios"]
    splits = list(result["pooled"])
    head = [s["id"].removeprefix("tr-") for s in scenarios] + [f"**{x}**" for x in splits]
    lines = [
        f"# Research-quality benchmark v{result['benchmark']}",
        "",
        f"Generated {result['created_at']} by `signalforge benchmark run` from frozen fixtures "
        "(no provider calls). Shares show `value (n)`; `n/a` is unavailable (reasons below). "
        "Definitions: docs/eval/metrics.md.",
        "",
    ]
    names = list(scenarios[0]["metrics"])
    for tier in TIERS:
        rows = [n for n in names if scenarios[0]["metrics"][n]["tier"] == tier]
        lines += [f"## {tier.replace('_', ' ')}", "", "| metric | " + " | ".join(head) + " |",
                  "|---|" + "---:|" * len(head)]  # fmt: skip
        for n in rows:
            cells = [_cell(s["metrics"][n]) for s in scenarios]
            cells += [_cell(result["pooled"][x][n]) for x in splits]
            lines.append(f"| {n} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["## Unavailable", ""]
    seen = set()
    for s in scenarios:
        for n, m in s["metrics"].items():
            if m.get("unavailable") and (n, m["unavailable"]) not in seen:
                seen.add((n, m["unavailable"]))
                lines.append(f"- `{n}` ({s['id']}): {m['unavailable']}")
    if any("replay" in s for s in scenarios):
        lines += ["", "## Replay", "",
                  "A new run from each plan in replay mode. *Reproduced*: stages whose research "
                  "metrics equal the recording's. *Stopped at*: the first stage the recording no "
                  "longer covers (its LLM inputs changed since recording).", "",
                  "| scenario | reproduced | mismatched | stopped at | error |",
                  "|---|---:|---|---|---|"]  # fmt: skip
        for s in scenarios:
            r = s["replay"]
            mism = ", ".join(f"{k}: {', '.join(v)}" for k, v in r["mismatched"].items()) or "-"
            err = (r["error"] or "-").replace("|", "/")[:120]
            lines.append(f"| {s['id']} | {r['reproduced']}/{len(r['stages'])} | {mism} "
                         f"| {r['stopped_at'] or '-'} | {err} |")  # fmt: skip
    if baseline is not None:
        lines += ["", f"## Changes against the baseline ({baseline.get('created_at')})", "",
                  "| split | metric | baseline | now |", "|---|---|---:|---:|"]  # fmt: skip
        changes = compare(result, baseline)
        lines += [f"| {sp} | {n} | {old} | {new} |" for sp, n, old, new in changes]
        if not changes:
            lines.append("| | no pooled metric changed | | |")
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "benchmark.md", out / "benchmark.json"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [md, js]
