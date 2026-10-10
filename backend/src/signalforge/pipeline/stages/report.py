"""report stage (plan §7.4; agent-modules.md §9): the final research report.

1. **Select** the top ``full_reports`` scored opportunities (strong > interesting > competitive,
   then attractiveness, then confidence); fewer qualify → fewer reports, never padded.
2. **Entailment** (``entail_pending``, stage ``report``) on every fact the chosen opportunities'
   claim pools hold; failed facts leave the tables.
3. **One call per section** (synthesis tier): the model sees that section's numbered claim table
   (``reporting/tables.py``) and returns bullets citing table numbers. Code maps them to claim
   ids and runs the pure validator (``reporting/validator.py``). A failing section is regenerated
   alone with the errors appended, up to ``max_regenerations``; then its failing bullets are
   dropped and recorded in ``method.dropped_bullets``. One more call writes the run summary.
4. **Deterministic sections** (other opportunities, don't build, insufficient evidence, method)
   are built in code from rule traces (``reporting/deterministic.py``).
5. The assembled report is validated once more as a whole; ``report.json`` is written, then the
   Markdown and HTML renderings.
"""

import json
import re
import shutil
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from signalforge.config import ReportDefaults
from signalforge.evidence.entailment import clear_entailment, entail_pending
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.pipeline.stages.score import Row
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import BudgetExceeded, LLMError, ModelTier
from signalforge.reporting.deterministic import (
    build_method,
    dont_build,
    insufficient_evidence,
    other_opportunities,
    summary_ranking,
)
from signalforge.reporting.landscape import build_landscape
from signalforge.reporting.render import render_report, report_dir
from signalforge.reporting.schema import (
    SECTION_KEYS,
    SUMMARY,
    UNCITED_ALLOWED,
    Bullet,
    ClaimView,
    DraftBullet,
    Dropped,
    OpportunityReport,
    Report,
    Section,
    SectionDraft,
    SummaryDraft,
)
from signalforge.reporting.sections import GUIDANCE, TITLES
from signalforge.reporting.tables import (
    OppData,
    _roles_of,
    claim_views,
    eligible_count,
    load_opportunities,
    row_payload,
    section_table,
    select_full,
)
from signalforge.reporting.validator import (
    BulletError,
    request_context,
    validate_bullet,
    validate_report,
)

STAGE = "report"
EXPERIMENT_EXTRA_BULLETS = 2  # LLM bullets after the code-built experiment bullet
FACTUAL = ("fact", "inference", "hypothesis", "assumption")


def reset(session: Any, cfg: ReportDefaults, run_id: int) -> None:
    """Forget the entailment verdicts this stage wrote and remove the run's report files."""
    clear_entailment(session, run_id, STAGE)
    shutil.rmtree(report_dir(cfg, run_id), ignore_errors=True)


# --- one section (or the summary): the generate / validate / regenerate loop ----------------


@dataclass
class Written:
    bullets: list[Bullet] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    calls: int = 0
    failed_calls: int = 0
    failed: bool = False  # no usable answer at all
    rules: Counter[str] = field(default_factory=Counter)


def _map(drafts: list[DraftBullet], table_ids: list[int]) -> list[Bullet]:
    """Local claim numbers → claim ids. A number outside the table becomes ``-n``, which the
    validator reports as an unknown citation."""

    def local(n: int) -> int:
        return table_ids[n - 1] if 1 <= n <= len(table_ids) else -abs(n)

    return [
        Bullet(
            text=d.text.strip(),
            kind=d.kind,
            claim_ids=[local(n) for n in d.claim_ids],
        )
        for d in drafts
    ]  # fmt: skip


def _localize(message: str, table_ids: list[int]) -> str:
    """Show the model its own claim numbers instead of ids."""

    def local(m: re.Match[str]) -> str:
        i = int(m.group(1))
        if i in table_ids:
            return f"claim {table_ids.index(i) + 1}"
        return f"claim {abs(i)}"

    return re.sub(r"claim (-?\d+)", local, message)


def write_bullets(
    ask: Callable[[str], BaseModel | None],
    base: dict[str, Any],
    *,
    section: str,
    table_ids: list[int],
    extra: list[str],
    claims: dict[int, ClaimView],
    context: list[str],
    cfg: ReportDefaults,
    opportunity_id: int | None,
    max_bullets: int,
    lead: list[Bullet] | None = None,
) -> Written:
    """Ask for bullets, validate, regenerate with the errors, then drop what still fails.

    ``lead`` bullets are code-built and validated like the rest; they come first.
    """
    out = Written()
    path = "p"

    def check(bullets: list[Bullet]) -> dict[int, list[BulletError]]:
        by_bullet: dict[int, list[BulletError]] = {}
        for n, b in enumerate(bullets):
            errs = validate_bullet(
                path, section, b, set(table_ids), extra, claims, context, cfg.hedges
            )
            if errs:
                by_bullet[n] = errs
        return by_bullet

    best: tuple[list[Bullet], dict[int, list[BulletError]]] | None = None
    retry: dict[str, Any] = {}
    for attempt in range(cfg.max_regenerations + 1):
        out.calls += 1
        draft = ask(json.dumps({**base, **retry}, ensure_ascii=False, indent=1))
        if draft is None:
            out.failed_calls += 1
            continue
        bullets = _map(draft.bullets[:max_bullets], table_ids)  # type: ignore[attr-defined]
        errors = check(bullets)
        if best is None or len(errors) < len(best[1]):
            best = (bullets, errors)
        if not errors:
            break
        retry = {
            "attempt": attempt + 2,  # a retry with identical input would be served from the cache
            "previous": [
                {"text": b.text, "kind": b.kind,
                 "claim_ids": [table_ids.index(i) + 1 if i in table_ids else abs(i)
                               for i in b.claim_ids]}
                for b in bullets
            ],
            "errors": [
                f"bullet {n + 1}: {e.rule}: {_localize(e.message, table_ids)}"
                for n, errs in errors.items()
                for e in errs
            ],
        }  # fmt: skip
    chosen, errors = best if best else ([], {})
    if best is None:
        out.failed = True
    lead = lead or []
    lead_errors = check(lead)
    for n, b in enumerate([*lead, *chosen]):
        errs = lead_errors.get(n) if n < len(lead) else errors.get(n - len(lead))
        if errs:
            out.dropped.append(
                Dropped(opportunity_id=opportunity_id, section=section, text=b.text,
                        claim_ids=b.claim_ids, errors=[e.message for e in errs],
                        rules=sorted({e.rule for e in errs}))
            )  # fmt: skip
            out.rules.update(e.rule for e in errs)
        else:
            out.bullets.append(b)
    return out


def experiment_bullet(o: OppData) -> Bullet | None:
    """The stored experiment as a recommendation, built in code so it cannot contradict the card."""
    e = (o.card or {}).get("experiment")
    if not e:
        return None
    pool = {r.id for r in o.pool}
    cost = f"{e['cost_usd']:g}" if e.get("cost_usd") is not None else None
    parts = [f"Run {e['name']}: {e['pass_fail']}."]
    if cost is not None or e.get("duration_days"):
        parts.append(
            f"Estimated cost ${cost or 0}, about {e.get('duration_days')} days."
            if e.get("duration_days")
            else f"Estimated cost ${cost}."
        )
    claim_id = e.get("claim_id")
    return Bullet(
        text=" ".join(parts),
        claim_ids=[claim_id] if claim_id in pool else [],
        kind="recommendation",
    )


# --- stage ----------------------------------------------------------------------------------


@dataclass
class Task:
    o: OppData
    key: str
    rows: list[Row]
    extra: list[str]


class ReportStage:
    name = STAGE

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.report
        plan = load_run_plan(ctx)
        if plan.request.report_language != "en":
            raise ValueError(
                f"report_language {plan.request.report_language!r} is not supported yet: "
                "hedge words are English, so reports are written in English"
            )
        prompt, summary_prompt = load_prompt("report"), load_prompt("report_summary")
        founder = plan.request.founder.model_dump()
        context = request_context(plan.request)

        with ctx.db.begin() as session:
            reset(session, cfg, ctx.run_id)
        opps = load_opportunities(ctx)
        chosen = select_full(opps, cfg.full_reports)
        ids = {r.id for o in chosen for r in o.pool if r.kind == "fact"}
        entailment = entail_pending(ctx, ids, stage=STAGE) if ids else {}
        opps = load_opportunities(ctx)  # failed facts leave the pools
        full = [o for o in opps if o.id in {c.id for c in chosen}]
        full.sort(key=lambda o: [c.id for c in chosen].index(o.id))

        tasks = [
            Task(o, key, *section_table(key, o, cfg, founder)) for o in full for key in SECTION_KEYS
        ]
        want = {r.id for t in tasks for r in t.rows}
        want |= {r.id for o in full for r in o.pool}
        with ctx.db() as session:
            views = claim_views(session, ctx.run_id, want, cfg.excerpt_max_chars)

        def ask_with(schema: type[BaseModel], p: Any) -> Callable[[str], BaseModel | None]:
            def ask(text: str) -> BaseModel | None:
                try:
                    return ctx.llm.parse(
                        ModelTier.SYNTHESIS, p, text, schema, stage=STAGE,
                        max_output_tokens=cfg.max_output_tokens,
                    ).output  # fmt: skip
                except BudgetExceeded:
                    raise
                except LLMError:
                    return None

            return ask

        ask_section = ask_with(SectionDraft, prompt)

        def write(t: Task) -> tuple[Task, Written | None]:
            lead = [b] if t.key == "validation_experiment" and (b := experiment_bullet(t.o)) else []
            if not t.rows and t.key not in UNCITED_ALLOWED:
                return t, None
            role_of = _roles_of(t.o)
            limit = EXPERIMENT_EXTRA_BULLETS if lead else cfg.max_bullets_per_section
            base = {
                "section": {"key": t.key, "title": TITLES[t.key], "guidance": GUIDANCE[t.key]},
                "opportunity": {
                    "segment": t.o.segment, "solution_angle": t.o.solution_angle,
                    "category": (t.o.card or {})["category"],
                    "attractiveness": (t.o.card or {})["attractiveness"],
                },
                "claims": [row_payload(n, r, views, role_of) for n, r in enumerate(t.rows, 1)],
                "extra": t.extra,
                "max_bullets": limit,
            }  # fmt: skip
            w = write_bullets(
                ask_section, base, section=t.key, table_ids=[r.id for r in t.rows], extra=t.extra,
                claims=views, context=context, cfg=cfg, opportunity_id=t.o.id,
                max_bullets=limit, lead=lead,
            )  # fmt: skip
            return t, w

        with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
            written = list(pool.map(write, tasks))

        # The run summary: over the final bullets of the full reports.
        by_opp: dict[int, dict[str, Written | None]] = {}
        for t, w in written:
            by_opp.setdefault(t.o.id, {})[t.key] = w
        cited = Counter(i for _, w in written if w for b in w.bullets for i in b.claim_ids)
        summary_ids = [i for i, _ in sorted(cited.items(), key=lambda kv: (-kv[1], kv[0]))][
            : cfg.max_claims_per_section
        ]
        summary_ids.sort()
        # Segments only: the scores are in the code-built ranking, not for the model to restate.
        summary_extra = [f"opportunity: {o.segment}" for o in full]
        summary: Written | None = None
        if full and summary_ids:
            payload = {
                "opportunities": summary_extra,
                "bullets": [
                    {"opportunity": t.o.segment, "section": t.key, "text": b.text, "kind": b.kind}
                    for t, w in written
                    if w
                    for b in w.bullets
                ],
                "claims": [
                    {"n": n, "kind": views[i].kind, "entailment": views[i].entailment,
                     "sourced": views[i].meta.get("sourced"),
                     "statement": views[i].statement.partition(": ")[0]
                     if views[i].kind == "assumption" else views[i].statement}
                    for n, i in enumerate(summary_ids, 1)
                ],
                "extra": summary_extra,
                "max_bullets": 5,
            }  # fmt: skip
            summary = write_bullets(
                ask_with(SummaryDraft, summary_prompt), payload, section=SUMMARY,
                table_ids=summary_ids, extra=summary_extra, claims=views, context=context,
                cfg=cfg, opportunity_id=None, max_bullets=5,
            )  # fmt: skip

        report, stats = self._assemble(ctx, plan, cfg, opps, full, written, summary, summary_ids,
                                       summary_extra, views, entailment)  # fmt: skip
        errors = validate_report(report, cfg.hedges)
        if errors:
            raise RuntimeError(
                f"report fails its own validator ({len(errors)} errors): "
                + "; ".join(str(e) for e in errors[:3])
            )
        files = render_report(report, report_dir(cfg, ctx.run_id))
        stats["files"] = [str(p) for p in files]
        input_hash = cache_key(
            {
                "sections": [
                    (t.o.id, t.key, [(r.id, r.entailment) for r in t.rows], t.extra)
                    for t in tasks
                ],
                "founder": founder,
                "config": cfg.model_dump(mode="json"),
                "prompts": [prompt.ref, summary_prompt.ref, load_prompt("entailment").ref],
            }
        )  # fmt: skip
        return StageResult(metrics=stats, input_hash=input_hash)

    def _assemble(
        self,
        ctx: RunContext,
        plan: Any,
        cfg: ReportDefaults,
        opps: list[OppData],
        full: list[OppData],
        written: list[tuple[Task, Written | None]],
        summary: Written | None,
        summary_ids: list[int],
        summary_extra: list[str],
        views: dict[int, ClaimView],
        entailment: dict[str, int],
    ) -> tuple[Report, dict[str, Any]]:
        sections: dict[int, list[Section]] = {}
        for t, w in written:
            sections.setdefault(t.o.id, []).append(
                Section(key=t.key, bullets=w.bullets if w else [], table=[r.id for r in t.rows],
                        extra=t.extra)
            )  # fmt: skip
        reports = [
            OpportunityReport(
                opportunity_id=o.id, title=o.segment, one_liner=o.solution_angle,
                category=(o.card or {})["category"],
                attractiveness=(o.card or {})["attractiveness"],
                confidence=(o.card or {})["confidence"],
                founder_fit=(o.card or {})["founder_fit"], sections=sections[o.id],
            )
            for o in full
        ]  # fmt: skip
        all_bullets = [b for r in reports for s in r.sections for b in s.bullets]
        summary_bullets = summary.bullets if summary else []
        # Claims the report cites or lets a writer cite (the validator needs the whole tables).
        ids = {i for r in reports for s in r.sections for i in s.table} | set(summary_ids)
        ids |= {i for b in [*all_bullets, *summary_bullets] for i in b.claim_ids}
        claims = [views[i] for i in sorted(ids) if i in views]

        with ctx.db() as session:
            method = build_method(session, ctx.run_id, ctx.pack)
        landscape = build_landscape(ctx.db, ctx.run_id, ctx.defaults)
        dropped = [d for _, w in written if w for d in w.dropped] + (
            summary.dropped if summary else []
        )
        all_written = [w for _, w in written if w] + ([summary] if summary else [])
        failed = [f"{t.o.id}:{t.key}" for t, w in written if w and w.failed]
        failed += [SUMMARY] if summary and summary.failed else []
        regenerations = sum(max(w.calls - 1, 0) for w in all_written)
        method.sections_generated = sum(1 for _, w in written if w)
        method.regenerations = regenerations
        method.failed_sections = failed
        method.dropped_bullets = dropped
        eligible = eligible_count(opps)
        method.notes = [
            "The report stage's own cost is not in the stage costs above; see the run ledger.",
            "Excerpts are clipped to "
            f"{cfg.excerpt_max_chars} characters; full page text is never exported.",
        ]
        below_min = len(full) < cfg.min_full_reports
        if below_min:
            method.notes.append(
                f"{len(full)} full reports, fewer than the {cfg.min_full_reports} asked for: only "
                f"{eligible} opportunities are strong, interesting or competitive."
            )
        if method.run_pack != method.pack:
            method.notes.append(
                f"run created with {method.run_pack}; economics and this report use {method.pack}."
            )
        no_card = sum(1 for o in opps if o.card is None)
        if no_card:
            method.notes.append(f"{no_card} opportunities have no score card and are not reported.")
        report = Report(
            run_id=ctx.run_id,
            generated_at=datetime.now(UTC),
            request=plan.request,
            summary_ranking=summary_ranking(full),
            summary=summary_bullets,
            summary_table=summary_ids,
            summary_extra=summary_extra,
            opportunities=reports,
            other_opportunities=other_opportunities(opps, {o.id for o in full}, ctx.defaults.score),
            dont_build=dont_build(opps),
            insufficient_evidence=insufficient_evidence(
                landscape, {o.problem_id for o in opps}, cfg.excerpt_max_chars
            ),
            claims=claims,
            method=method,
        )  # fmt: skip
        every = [*all_bullets, *summary_bullets]
        factual = [b for b in every if b.kind in FACTUAL]
        rules: Counter[str] = Counter()
        for w in all_written:
            rules.update(w.rules)
        stats = {
            "full_reports": len(full),
            "eligible": eligible,
            "below_min_full_reports": below_min,
            "sections_generated": method.sections_generated,
            "sections_empty": sum(1 for r in reports for s in r.sections if not s.bullets),
            "llm_calls": sum(w.calls for w in all_written),
            "regenerations": regenerations,
            "failed_calls": sum(w.failed_calls for w in all_written),
            "failed_sections": len(failed),
            "bullets": len(every),
            "bullets_dropped": len(dropped),
            "dropped_by_rule": dict(rules),
            "factual_bullets": len(factual),
            "factual_cited_pct": (
                sum(bool(b.claim_ids) for b in factual) / len(factual) if factual else 1.0
            ),
            "claims_cited": len({i for b in every for i in b.claim_ids}),
            "entailment": entailment,
            "deterministic": {
                "other_opportunities": len(report.other_opportunities),
                "dont_build": len(report.dont_build),
                "insufficient_evidence": len(report.insufficient_evidence),
            },
        }
        return report, stats
