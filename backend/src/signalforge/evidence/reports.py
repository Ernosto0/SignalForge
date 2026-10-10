"""The M6 exit check (agent-modules.md §9): a stored report is valid.

Loads ``<report.out_dir>/run-<id>/report.json`` and re-validates it from the file alone (all five
validator rules against each section's stored table), then against the database: every cited claim
exists in the run with the stored statement and has not failed entailment, excerpts are clipped,
the full reports are the ones selection picks, and ``summary_ranking``, ``other_opportunities``,
``dont_build`` and ``insufficient_evidence`` equal a rebuild from the rows. ``method`` is only
checked for internal consistency (and its two pack versions): the report stage's own StageRun row
and the timestamp make it differ from a rebuild.
"""

from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select

from signalforge.db.models import Claim, ResearchRun
from signalforge.evidence.entailment import failed
from signalforge.pipeline.context import RunContext
from signalforge.reporting.deterministic import (
    dont_build,
    insufficient_evidence,
    other_opportunities,
    summary_ranking,
)
from signalforge.reporting.landscape import build_landscape
from signalforge.reporting.render import report_dir
from signalforge.reporting.schema import SECTION_KEYS, UNCITED_ALLOWED, Report
from signalforge.reporting.tables import eligible_count, load_opportunities, select_full
from signalforge.reporting.validator import validate_report


def check_report(ctx: RunContext, out_dir: Path | None = None) -> list[str]:
    """Violations of the report rules for ``ctx.run_id`` (empty = it holds)."""
    cfg = ctx.defaults.report
    directory = out_dir or report_dir(cfg, ctx.run_id)
    path = directory / "report.json"
    if not path.exists():
        return [f"{path} does not exist; run the report stage first"]
    try:
        report = Report.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        return [f"{path} is not a valid report: {exc.error_count()} schema errors"]
    errors = [f"{p.name} is missing" for p in (directory / "report.md", directory / "report.html")
              if not p.exists()]  # fmt: skip
    if report.run_id != ctx.run_id:
        errors.append(f"report is for run {report.run_id}, not {ctx.run_id}")

    errors += [str(e) for e in validate_report(report, cfg.hedges)]
    for n, opp in enumerate(report.opportunities):
        keys = [s.key for s in opp.sections]
        if keys != list(SECTION_KEYS):
            errors.append(f"opportunities[{n}]: sections {keys} are not {list(SECTION_KEYS)}")
        for s in opp.sections:
            for m, b in enumerate(s.bullets):
                if not b.claim_ids and (s.key not in UNCITED_ALLOWED or b.kind != "recommendation"):
                    errors.append(
                        f"opportunities[{n}].{s.key}.bullets[{m}]: factual bullet is uncited"
                    )

    with ctx.db() as session:
        run = session.get(ResearchRun, ctx.run_id)
        claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.run_id == ctx.run_id))}
    for view in report.claims:
        claim = claims.get(view.id)
        if claim is None:
            errors.append(f"claim {view.id} is not in run {ctx.run_id}")
            continue
        if claim.statement != view.statement or claim.kind != view.kind:
            errors.append(f"claim {view.id} differs from the stored claim")
        if failed(claim.entailment):
            errors.append(f"claim {view.id} failed entailment ({claim.entailment})")
        for e in view.excerpts:
            if (
                len(e.quote) > cfg.excerpt_max_chars
                or len(e.translation or "") > cfg.excerpt_max_chars
            ):
                errors.append(f"claim {view.id}: excerpt longer than {cfg.excerpt_max_chars} chars")
    for item in report.insufficient_evidence:
        for q in item.quotes:
            if len(q.quote) > cfg.excerpt_max_chars:
                errors.append(
                    f"cluster {item.cluster_id}: quote longer than {cfg.excerpt_max_chars}"
                )

    opps = load_opportunities(ctx)
    full = select_full(opps, cfg.full_reports)
    expected = [o.id for o in full]
    got = [o.opportunity_id for o in report.opportunities]
    if got != expected:
        errors.append(f"full reports are for opportunities {got}, selection gives {expected}")
    elif eligible_count(opps) and not got:
        errors.append("opportunities qualify for a full report but the report has none")
    rebuilt = {
        "summary_ranking": summary_ranking(full),
        "other_opportunities": other_opportunities(opps, set(got), ctx.defaults.score),
        "dont_build": dont_build(opps),
        "insufficient_evidence": insufficient_evidence(
            build_landscape(ctx.db, ctx.run_id, ctx.defaults),
            {o.problem_id for o in opps},
            cfg.excerpt_max_chars,
        ),
    }
    for name, value in rebuilt.items():
        if [x.model_dump() for x in getattr(report, name)] != [x.model_dump() for x in value]:
            errors.append(f"{name} differs from a rebuild from the rule traces")

    m = report.method
    if run is not None and m.run_pack != f"{run.pack_id}@{run.pack_version}":
        errors.append(f"method.run_pack {m.run_pack} is not the run's pack")
    if m.pack != f"{ctx.pack.id}@{ctx.pack.version}":
        errors.append(f"method.pack {m.pack} is not the loaded pack")
    if min(m.sections_generated, m.regenerations, *(v or 0 for v in m.counts.values())) < 0:
        errors.append("method has a negative count")
    errors += [f"method.dropped_bullets[{n}] lists no errors" for n, d in
               enumerate(m.dropped_bullets) if not d.errors]  # fmt: skip
    return errors
