import csv
import getpass
import json
import random
import sys
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import groupby
from pathlib import Path
from typing import NoReturn

import click
import typer
import uvicorn
import yaml
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from signalforge import __version__
from signalforge.agents import AGENTS, STAGES, get_agent
from signalforge.config import CacheMode, get_defaults
from signalforge.db.models import (
    Competitor,
    Document,
    Excerpt,
    GapMatrix,
    IndependenceGroup,
    Label,
    LLMCall,
    ProblemCluster,
    Query,
    ResearchRun,
    Signal,
    StageRun,
    UrlCandidate,
)
from signalforge.db.session import get_session_factory
from signalforge.domain.diagnostics import PainCheck
from signalforge.domain.plan import load_plan
from signalforge.evidence.gaps import check_gap_matrices
from signalforge.packs import load_pack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import create_run, latest_stage_runs, run_pipeline, run_stage
from signalforge.pipeline.stages.query_gen import QueryGen
from signalforge.prompts import load_prompt
from signalforge.providers.cache import CacheMiss
from signalforge.providers.llm import LLMError, LLMUnavailable, ModelTier
from signalforge.providers.search import SearchError
from signalforge.providers.urls import domain_of
from signalforge.reporting.landscape import build_landscape, render_landscape

# Turkish text must print on Windows consoles and through pipes (default there is cp1252).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

app = typer.Typer(help="SignalForge: evidence-backed market research.", no_args_is_help=True)

DEFAULT_CHECK_TEXT = (
    "Sevkiyatları hâlâ Excel'de takip ediyoruz, şoförler teslimatı WhatsApp'tan bildiriyor. "
    "Her gün iki kişi yarım günü bununla geçiriyor."
)


@app.callback()
def main(
    ctx: typer.Context,
    cache_mode: CacheMode | None = typer.Option(
        None, help="live | record | replay. Defaults to CACHE_MODE from .env (live)."
    ),
) -> None:
    ctx.obj = {"cache_mode": cache_mode}


@contextmanager
def _run_context(
    ctx: typer.Context, pack: str | None = None, run_id: int | None = None
) -> Iterator[RunContext]:
    try:
        yield RunContext.create(cache_mode=ctx.obj["cache_mode"], pack_id=pack, run_id=run_id)
    except (CacheMiss, SearchError, LLMError, LLMUnavailable) as exc:
        _fail(str(exc))
    except OperationalError as exc:
        _fail(
            f"database unavailable ({exc.orig}).\n"
            "Start it with `docker compose up -d db`, then `uv run alembic upgrade head`."
        )


def _fail(message: str) -> NoReturn:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _resolve_run(plan_file: Path | None, run: int | None) -> tuple[int, str]:
    """``(run_id, pack_id)``: a new run from a reviewed plan.yaml, or an existing run."""
    if (plan_file is None) == (run is None):
        _fail("pass exactly one of --plan or --run")
    db = get_session_factory()
    try:
        if plan_file is not None:
            plan = load_plan(plan_file)
            pack_id = plan.request.pack_id
            return create_run(db, plan, load_pack(pack_id), get_defaults()), pack_id
        with db() as session:
            pack_id = session.scalar(select(ResearchRun.pack_id).where(ResearchRun.id == run))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        _fail(f"invalid plan {plan_file}: {exc}")
    except OperationalError as exc:
        _fail(f"database unavailable ({exc.orig}).")
    if pack_id is None:
        _fail(f"run {run} not found")
    assert run is not None
    return run, pack_id


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = typer.Option(False, help="Auto-reload on code changes (dev)."),
) -> None:
    """Run the API server."""
    uvicorn.run("signalforge.api.main:app", host=host, port=port, reload=reload)


@app.command()
def search(
    ctx: typer.Context,
    query: str,
    n: int | None = typer.Option(None, "--n", help="Results to return (default from config)."),
    pack: str | None = typer.Option(None, help="Market pack (locale + source registry)."),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Search the web with the pack's locale. Results are cached (see --cache-mode)."""
    with _run_context(ctx, pack) as rc:
        n = n or rc.defaults.search.results_per_query
        result = rc.search.search(query, rc.pack.search, n)
        if as_json:
            typer.echo(result.model_dump_json(indent=2))
            return
        locale = rc.pack.search
        typer.echo(
            f'"{query}"  ·  {rc.search.provider.name} gl={locale.gl} hl={locale.hl}  ·  '
            f"{len(result.hits)} results  ·  {'cache hit' if result.cache_hit else 'live'}"
        )
        for hit in result.hits:
            domain = domain_of(hit.url)
            source = rc.pack.source_for(domain)
            tag = f"{source.category}, {source.tier}" if source else f"{rc.pack.default_tier}*"
            typer.echo(f"\n{hit.rank:>2}. {hit.title or '(no title)'}")
            typer.echo(f"    {hit.url}  [{tag}]")
            if hit.snippet:
                typer.echo(f"    {hit.snippet}")


@app.command()
def fetch(
    ctx: typer.Context,
    url: str,
    chars: int = typer.Option(600, help="Characters of extracted text to show."),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON (includes full text)."),
) -> None:
    """Fetch one page (robots.txt, rate limit, text extraction). Cached by canonical URL."""
    with _run_context(ctx) as rc:
        page = rc.fetcher.fetch(url)
        if as_json:
            typer.echo(page.model_dump_json(indent=2))
            return
        typer.echo(
            f"{page.status}  ·  HTTP {page.http_status}  ·  "
            f"{'cache hit' if page.cache_hit else 'live'}  ·  {page.canonical_url}"
        )
        for label, value in [
            ("title", page.title),
            ("published", page.published_at),
            ("lang", page.html_lang),
            ("error", page.error),
        ]:
            if value:
                typer.echo(f"{label}: {value}")
        if page.text:
            more = f" … (+{len(page.text) - chars} chars)" if len(page.text) > chars else ""
            typer.echo(f"\n{page.text[:chars]}{more}")


@app.command("llm-check")
def llm_check(
    ctx: typer.Context,
    text: str = typer.Argument(DEFAULT_CHECK_TEXT, help="Text to classify."),
    tier: ModelTier = typer.Option(ModelTier.FAST, help="Model tier to call."),
) -> None:
    """Make one structured LLM call and show its ledger entry (tokens, cost, cache)."""
    with _run_context(ctx) as rc:
        result = rc.llm.parse(tier, load_prompt("llm_check"), text, PainCheck, stage="llm_check")
        typer.echo(result.output.model_dump_json(indent=2))
        with rc.db() as session:
            call = session.get(LLMCall, result.call_id)
        assert call is not None
        typer.echo(
            f"\nllm_calls #{call.id}: {call.model} ({call.tier}) {call.prompt_id}@"
            f"{call.prompt_version}  in={call.input_tokens} (cached {call.cached_tokens}) "
            f"out={call.output_tokens}  cost=${call.cost_usd}  "
            f"{'cache hit' if call.cache_hit else f'{call.latency_ms} ms'}"
        )


@app.command("query-gen")
def query_gen(
    ctx: typer.Context,
    plan_file: Path | None = typer.Option(
        None, "--plan", help="Reviewed plan.yaml; creates a new run from it."
    ),
    run: int | None = typer.Option(None, "--run", help="Regenerate an existing run's queries."),
    out: Path | None = typer.Option(None, "--out", help="Also write the queries to a review CSV."),
) -> None:
    """Generate a run's search queries: submarkets × pack pain phrases × source hints."""
    run, pack_id = _resolve_run(plan_file, run)

    with _run_context(ctx, pack_id, run) as rc:
        stage = run_stage(rc, QueryGen())
        with rc.db() as session:
            queries = session.scalars(
                select(Query).where(Query.run_id == run).order_by(Query.id)
            ).all()

    m = stage.metrics
    typer.echo(
        f"run #{run}  ·  {stage.stage} {stage.status}  ·  {m['kept']} queries  ·  "
        f"${stage.cost_usd}  ·  {m['llm_calls']} LLM calls ({m['llm_cache_hits']} cached)"
    )
    dropped = [f"{k.removeprefix('dropped_')} {v}" for k, v in m.items() if k[:8] == "dropped_"]
    typer.echo(
        f"{m['drafts']} drafts + {m['pack_seeds']} pack seeds  ·  "
        f"dropped: {', '.join(dropped) or '-'}"
    )
    mix = "  ".join(f"{k} {v}" for k, v in m["kept_by_intent"].items())
    typer.echo(f"{mix}  ·  site-hinted {m['site_hinted']}")
    if m["quota_shortfall"]:
        typer.echo(f"quota shortfall (filled elsewhere): {m['quota_shortfall']}")

    for submarket, group in groupby(queries, key=lambda q: q.submarket):
        typer.echo(f"\n{submarket or '(market-wide)'}")
        for q in group:
            typer.echo(f"  {q.intent:<10} {q.meta.get('signal_type', ''):<14} {q.text}")

    if out is not None:
        columns = ["id", "submarket", "intent", "signal_type", "source_hint", "origin", "query"]
        with out.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: opens cleanly in Excel
            writer = csv.writer(f)
            writer.writerow([*columns, "verdict", "note"])
            for q in queries:
                meta = q.meta
                writer.writerow(
                    [q.id, q.submarket or "", q.intent, meta.get("signal_type"),
                     meta.get("source_hint") or "", meta.get("origin"), q.text, "", ""]
                )  # fmt: skip
        typer.echo(f"\nwrote {len(queries)} queries to {out} (fill verdict: ok / bad)")


@app.command()
def ledger(
    limit: int = typer.Option(20, help="Most recent calls to show."),
    run: int | None = typer.Option(None, help="Only calls for this research run."),
) -> None:
    """Show recent LLM calls and total spend."""
    try:
        with get_session_factory()() as session:
            where = [LLMCall.run_id == run] if run is not None else []
            calls = session.scalars(
                select(LLMCall).where(*where).order_by(LLMCall.id.desc()).limit(limit)
            ).all()
            total, count = session.execute(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0), func.count()).where(*where)
            ).one()
    except OperationalError as exc:
        _fail(f"database unavailable ({exc.orig}).")
    for c in reversed(calls):
        status = "ERROR " + (c.error or "")[:60] if c.error else ("hit" if c.cache_hit else "live")
        when = c.created_at.astimezone()
        typer.echo(
            f"#{c.id:<5} {when:%Y-%m-%d %H:%M}  {c.stage or '-':<12} {c.model:<14} "
            f"in={c.input_tokens:<6} out={c.output_tokens:<6} ${c.cost_usd:<10} {status}"
        )
    typer.echo(f"\n{count} calls, total ${total}")


@app.command("purge-cache")
def purge_cache(
    ctx: typer.Context,
    namespace: str | None = typer.Option(None, help="search | page | llm (default: all)."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Delete cached search results, pages and/or LLM responses (privacy, plan §2 #17)."""
    if not yes:
        typer.confirm(f"Delete {namespace or 'all'} cache entries?", abort=True)
    with _run_context(ctx) as rc:
        typer.echo(f"deleted {rc.cache.purge(namespace)} entries")


def _counts_text(counts: dict[str, int], keys: tuple[str, ...]) -> str:
    return ", ".join(f"{k} {counts.get(k, 0)}" for k in keys)


def _stage_line(row: StageRun) -> str:
    """One-line summary of a stage's headline metrics."""
    m = row.metrics or {}
    pct = lambda v: "-" if v is None else f"{v:.0%}"  # noqa: E731
    detail = {
        "query_gen": lambda: f"{m.get('kept')} queries",
        "search": lambda: (
            f"{m['queries']} queries ({m['searched_live']} live, {m['cache_hits']} cached, "
            f"{m['failed']} failed)  ·  {m['results']} results → {m['unique_urls']} unique URLs "
            f"(URL collapse {pct(m['url_collapse_rate'])})"
        ),
        "triage": lambda: (
            f"{m['candidates']} candidates, {m['judged']} judged  ·  keep {m['keep']}, "
            f"reserve {m['reserve']}, drop {m['drop']}  ·  {m['kept_unique_domains']} domains"
        ),
        "fetch": lambda: (
            f"{m['attempted']} fetched, {m['fetched_ok']} ok "
            f"(success {pct(m['fetch_success_rate'])})  ·  {m['documents']} documents "
            f"({m['documents_snippet_only']} snippet-only, "
            f"{m['from_reserve']} candidates from reserve)"
        ),
        "dedupe": lambda: (
            f"{m['documents']} documents, {m['groups']} duplicate groups  ·  duplicate collapse "
            f"{pct(m['duplicate_collapse_rate'])}  ·  {m['independent_sources']} independent"
        ),
        "extract": lambda: (
            f"{m['documents_read']} documents read, {m['documents_with_signals']} with signals  ·  "
            f"{m['quotes_proposed']} quotes, pass {pct(m['quote_pass_rate'])} "
            f"({m['quotes_fuzzy']} fuzzy)  ·  {m['signals']} signals, first-hand "
            f"{pct(m['first_hand_share'])}"
        ),
        "cluster": lambda: (
            f"{m['signals']} signals → {m['clusters']} clusters, {m['noise']} noise  ·  "
            f"{m['reassigned']} reassigned, {m['unknown_ids'] + m['repeated_ids']} bad ids"
        ),
        "shortlist": lambda: (
            f"{m['shortlisted']} of {m['clusters']} clusters shortlisted  ·  failed: "
            + (", ".join(f"{k} {v}" for k, v in m["failed_by_rule"].items()) or "-")
        ),
        "verify": lambda: (
            f"{m['passed']} of {m['problems']} problems still shortlisted  ·  "
            f"{m['searches']} searches, {m['fetches']} fetches  ·  {m['new_signals']} new "
            f"signals, {m['counter_signals']} counter  ·  entailment "
            + _counts_text(m["entailment"], ("supported", "partial", "not_supported"))
        ),
        "competitors": lambda: (
            f"{m['competitors']} competitors for {m['problems']} problems  ·  "
            f"{m['facts_verified']} facts ({m['prices']} prices)  ·  cells "
            + _counts_text(m["cells"], ("yes", "partial", "no", "unknown"))
            + f"  ·  {m['demoted_cells']} demoted, {m['gaps']} gaps"
        ),
    }.get(row.stage)
    took = (row.finished_at - row.started_at).total_seconds() if row.finished_at else 0
    head = f"{row.stage:<10} {row.status:<9} {took:>5.0f}s  ${row.cost_usd:<9}"
    if row.status != "completed":
        return f"{head} {row.error or ''}"
    return f"{head} {detail() if detail else ''}"


@app.command("run")
def run_pipeline_cmd(
    ctx: typer.Context,
    plan_file: Path | None = typer.Option(
        None, "--plan", help="Reviewed plan.yaml; creates a new run from it."
    ),
    run: int | None = typer.Option(None, "--run", help="Continue or re-run an existing run."),
    resume: bool = typer.Option(False, "--resume", help="Skip stages that already completed."),
    from_stage: str | None = typer.Option(
        None, "--from", help="Re-run from this stage on (replaces its and later outputs)."
    ),
    until: str | None = typer.Option(None, "--until", help="Stop after this stage."),
    agent: str | None = typer.Option(
        None, "--agent", help="Re-run one agent's stages as a unit (see `signalforge agents`)."
    ),
) -> None:
    """Run the research pipeline: query_gen → search → triage → fetch → dedupe → extract →
    cluster → shortlist → verify → competitors."""
    if agent is not None:
        if from_stage or until or resume:
            _fail("--agent cannot be combined with --from, --until or --resume")
        try:
            chosen = get_agent(agent)
        except ValueError as exc:
            _fail(str(exc))
        from_stage, until = chosen.first, chosen.last
    run, pack_id = _resolve_run(plan_file, run)
    typer.echo(f"run #{run}")
    with _run_context(ctx, pack_id, run) as rc:
        try:
            run_pipeline(rc, STAGES, resume=resume, from_stage=from_stage, until=until)
        except ValueError as exc:
            _fail(str(exc))
        finally:
            for row in latest_stage_runs(rc.db, run).values():
                typer.echo(_stage_line(row))


@app.command()
def agents(
    run: int | None = typer.Option(None, "--run", help="Show each stage's status for this run."),
) -> None:
    """List the research agents and their stages, in run order."""
    latest: dict[str, StageRun] = {}
    if run is not None:
        try:
            latest = latest_stage_runs(get_session_factory(), run)
        except OperationalError as exc:
            _fail(f"database unavailable ({exc.orig}).")
    for agent in AGENTS:
        typer.secho(f"{agent.name}", bold=True, nl=False)
        typer.echo(f"  {agent.description}")
        for name in agent.stage_names:
            state = f"  {latest[name].status}" if name in latest else ""
            typer.echo(f"    {name}{state}")


@app.command()
def status(
    run: int = typer.Argument(..., help="Research run id."),
    as_json: bool = typer.Option(False, "--json", help="Print every stage's full metrics."),
) -> None:
    """Show a run's latest stage results and metrics."""
    try:
        with get_session_factory()() as session:
            row = session.get(ResearchRun, run)
        latest = latest_stage_runs(get_session_factory(), run)
    except OperationalError as exc:
        _fail(f"database unavailable ({exc.orig}).")
    if row is None:
        _fail(f"run {run} not found")
    if as_json:
        payload = {s: {"status": r.status, "metrics": r.metrics} for s, r in latest.items()}
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    typer.echo(f"run #{run}  ·  {row.status}  ·  spent ${row.spent_usd} of ${row.budget_usd}")
    for stage_run in latest.values():
        typer.echo(_stage_line(stage_run))


@app.command()
def documents(
    run: int = typer.Argument(..., help="Research run id."),
    out: Path = typer.Option(..., "--out", help="CSV file to write."),
) -> None:
    """Export a run's documents with triage and duplicate-group info, for review."""
    try:
        with get_session_factory()() as session:
            docs = session.scalars(
                select(Document).where(Document.run_id == run).order_by(Document.id)
            ).all()
            candidates = {
                c.document_id: c
                for c in session.scalars(
                    select(UrlCandidate)
                    .where(UrlCandidate.run_id == run, UrlCandidate.document_id.is_not(None))
                    .order_by(UrlCandidate.priority.desc())  # best priority wins below
                )
            }
            groups = session.scalars(
                select(IndependenceGroup).where(IndependenceGroup.run_id == run)
            ).all()
    except OperationalError as exc:
        _fail(f"database unavailable ({exc.orig}).")
    group_of = {doc_id: (g.id, g.rule) for g in groups for doc_id in g.document_ids}
    columns = [
        "id", "domain", "tier", "category", "snippet_only", "lang", "published", "triage_label",
        "triage_score", "triage_reason", "dup_group", "dup_rule", "title", "url",
    ]  # fmt: skip
    with out.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: opens cleanly in Excel
        writer = csv.writer(f)
        writer.writerow(columns)
        for d in docs:
            c = candidates.get(d.id)
            group_id, rule = group_of.get(d.id, ("", ""))
            writer.writerow(
                [d.id, d.domain, d.quality_tier, d.source_category or "", d.snippet_only, d.lang,
                 d.published_at.date() if d.published_at else "", c.triage_label if c else "",
                 c.triage_score if c else "", c.triage_reason if c else "", group_id, rule,
                 d.title or "", d.url]
            )  # fmt: skip
    typer.echo(f"wrote {len(docs)} documents to {out}")


# --- M3: signals, landscape report, labels ----------------------------------------------------


def _signal_rows(run: int) -> list[tuple[Signal, Excerpt, Document]]:
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(Signal, Excerpt, Document)
                .join(Excerpt, Signal.excerpt_id == Excerpt.id)
                .join(Document, Excerpt.document_id == Document.id)
                .where(Signal.run_id == run)
                .order_by(Signal.id)
            ).all()
    except OperationalError as exc:
        _fail(f"database unavailable ({exc.orig}).")
    return [(sig, ex, doc) for sig, ex, doc in rows]


def _run_clusters(run: int) -> list[ProblemCluster]:
    with get_session_factory()() as session:
        return list(
            session.scalars(
                select(ProblemCluster)
                .where(ProblemCluster.run_id == run)
                .order_by(ProblemCluster.rank, ProblemCluster.id)
            )
        )


def _latest_labels(target_type: str, ids: list[int]) -> dict[tuple[int, str], str]:
    """``(target id, labeler) -> value``: each labeler's most recent label per target."""
    with get_session_factory()() as session:
        rows = session.scalars(
            select(Label)
            .where(Label.target_type == target_type, Label.target_id.in_(ids))
            .order_by(Label.id)
        ).all()
    return {(row.target_id, row.labeler): row.value for row in rows}


@app.command()
def landscape(
    run: int = typer.Argument(..., help="Research run id."),
    out: Path = typer.Option(Path("reports"), "--out", help="Directory for the report files."),
) -> None:
    """Write the problem landscape report (json, md, html) of a run after shortlist."""
    try:
        report = build_landscape(get_session_factory(), run, get_defaults())
    except ValueError as exc:
        _fail(str(exc))
    except OperationalError as exc:
        _fail(f"database unavailable ({exc.orig}).")
    for path in render_landscape(report, out / f"run-{run}"):
        typer.echo(f"wrote {path}")
    typer.echo(
        f"{len(report['shortlist'])} problems shortlisted, "
        f"{len(report['insufficient_evidence'])} with insufficient evidence"
    )


_CELL_MARK = {"yes": "yes", "partial": "part", "no": "no", "unknown": "?"}


@app.command()
def gaps(
    run: int = typer.Argument(..., help="Research run id."),
    as_json: bool = typer.Option(False, "--json", help="Print the stored matrices as JSON."),
) -> None:
    """Show a run's gap matrices and check the M4 exit rule: every cell is a cited fact or
    unknown. Exits with status 1 if any cell breaks it."""
    try:
        with get_session_factory()() as session:
            matrices = session.scalars(
                select(GapMatrix).where(GapMatrix.run_id == run).order_by(GapMatrix.problem_id)
            ).all()
            problems = {
                c.id: c.name
                for c in session.scalars(select(ProblemCluster).where(ProblemCluster.run_id == run))
            }
            names = {
                c.id: c.name
                for c in session.scalars(select(Competitor).where(Competitor.run_id == run))
            }
            errors = check_gap_matrices(session, run)
    except OperationalError as exc:
        _fail(f"database unavailable ({exc.orig}).")
    if as_json:
        payload = [
            {"problem_id": m.problem_id, "problem": problems.get(m.problem_id), **m.matrix}
            for m in matrices
        ]
        typer.echo(json.dumps({"matrices": payload, "errors": errors}, ensure_ascii=False,
                              indent=2))  # fmt: skip
    else:
        for m in matrices:
            matrix = m.matrix
            ids = matrix.get("competitor_ids", [])
            typer.secho(f"\n{problems.get(m.problem_id, m.problem_id)}", bold=True)
            if not ids:
                typer.echo("  no competitors confirmed on their own site")
                continue
            columns = (f"[{i}] {names.get(c, c)}" for i, c in enumerate(ids, 1))
            typer.echo("  " + "  |  ".join(columns))
            gap_dims = {g["dimension"] for g in matrix.get("gaps", [])}
            for d in matrix.get("dimensions", []):
                row = matrix["cells"].get(d["key"], {})
                marks = " ".join(
                    f"{_CELL_MARK.get(row.get(str(c), {}).get('value', '?'), '?'):>4}" for c in ids
                )
                flag = "  ← gap" if d["key"] in gap_dims else ""
                typer.echo(f"  {d['label'][:40]:<40} {marks}{flag}")
    if errors:
        for e in errors:
            typer.secho(f"violation: {e}", fg="red", err=True)
        raise typer.Exit(1)
    if matrices and not as_json:
        typer.echo(f"\n{len(matrices)} matrices; every cell is a cited fact or unknown")


@app.command()
def signals(
    run: int = typer.Argument(..., help="Research run id."),
    out: Path = typer.Option(..., "--out", help="CSV file to write."),
) -> None:
    """Export a run's signals with quotes, sources, clusters and labels, for review."""
    rows = _signal_rows(run)
    cluster_of = {i: c for c in _run_clusters(run) for i in c.signal_ids}
    labels_of: dict[int, list[str]] = defaultdict(list)
    for (signal_id, labeler), value in _latest_labels("signal", [r[0].id for r in rows]).items():
        labels_of[signal_id].append(f"{labeler}:{value}")
    columns = [
        "id", "cluster_id", "cluster", "shortlisted", "type", "first_hand", "actor", "workflow",
        "statement", "quote", "translation", "verified", "domain", "tier", "category",
        "snippet_only", "published", "labels", "url",
    ]  # fmt: skip
    with out.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: opens cleanly in Excel
        writer = csv.writer(f)
        writer.writerow(columns)
        for sig, ex, doc in rows:
            c = cluster_of.get(sig.id)
            writer.writerow(
                [sig.id, c.id if c else "", c.name if c else "(noise)", c.shortlisted if c else "",
                 sig.type, sig.first_hand, sig.actor or "", sig.workflow or "", sig.statement,
                 ex.quote, ex.translation or "", ex.verified, doc.domain, doc.quality_tier,
                 doc.source_category or "", doc.snippet_only,
                 doc.published_at.date() if doc.published_at else "",
                 " ".join(labels_of[sig.id]), doc.url]
            )  # fmt: skip
    typer.echo(f"wrote {len(rows)} signals to {out}")


label_app = typer.Typer(
    help="Record human judgments (plan §11) for evaluation.", no_args_is_help=True
)
app.add_typer(label_app, name="label")

_VERDICT = click.Choice(["y", "n", "s", "q"], case_sensitive=False)


def _ask_verdict(question: str) -> str:
    return typer.prompt(f"{question} [y/n/s/q]", type=_VERDICT, show_choices=False).lower()


@label_app.command("signals")
def label_signals(
    run: int = typer.Argument(..., help="Research run id."),
    n: int = typer.Option(50, "--n", help="Signals to label."),
    labeler: str = typer.Option(getpass.getuser(), help="Your name, stored with each label."),
    seed: int = typer.Option(0, help="Sample seed: the same seed gives the same sample."),
) -> None:
    """Is each sampled signal real, first-hand B2B pain? y / n / s (skip) / q (quit)."""
    rows = _signal_rows(run)
    done = {sid for sid, who in _latest_labels("signal", [r[0].id for r in rows]) if who == labeler}
    todo = [r for r in rows if r[0].id not in done]
    sample = random.Random(seed).sample(todo, min(n, len(todo)))
    typer.echo(
        f"{len(rows)} signals, {len(done)} already labelled by {labeler}; labelling "
        f"{len(sample)}.\ny = real, first-hand pain of a business in this market   "
        "n = not (consumer, vendor claim, generic, misread)   s = skip   q = quit"
    )
    db = get_session_factory()
    for i, (sig, ex, doc) in enumerate(sample, 1):
        first_hand = "  first-hand" if sig.first_hand else ""
        snippet = ", snippet only" if doc.snippet_only else ""
        typer.secho(f"\n[{i}/{len(sample)}] signal #{sig.id}  {sig.type}{first_hand}", bold=True)
        typer.echo(
            f"source:    {doc.domain} ({doc.source_category or 'unlisted'}, {doc.quality_tier}"
            f"{snippet})  {doc.url}"
        )
        typer.echo(f"actor:     {sig.actor or '-'}   workflow: {sig.workflow or '-'}")
        typer.echo(f"statement: {sig.statement}")
        typer.echo(f"quote:     {ex.quote}")
        typer.echo(f"           {ex.translation or ''}")
        verdict = _ask_verdict("real first-hand B2B pain?")
        if verdict == "q":
            break
        if verdict != "s":
            with db.begin() as session:
                session.add(
                    Label(target_type="signal", target_id=sig.id, labeler=labeler, value=verdict)
                )


@label_app.command("clusters")
def label_clusters(
    run: int = typer.Argument(..., help="Research run id."),
    labeler: str = typer.Option(getpass.getuser(), help="Your name, stored with each label."),
) -> None:
    """Is each cluster one coherent, distinct problem? y / n / s (skip) / q (quit)."""
    statements = {sig.id: sig for sig, _, _ in _signal_rows(run)}
    clusters = _run_clusters(run)
    labelled = _latest_labels("cluster", [c.id for c in clusters])
    db = get_session_factory()
    for c in clusters:
        if (c.id, labeler) in labelled:
            continue
        mark = "shortlisted" if c.shortlisted else "not shortlisted"
        typer.secho(f"\n#{c.rank} {c.name}  ({len(c.signal_ids)} signals, {mark})", bold=True)
        typer.echo(c.description)
        for signal_id in c.signal_ids[:8]:
            sig = statements[signal_id]
            typer.echo(f"  - [{sig.type}] {sig.statement}")
        verdict = _ask_verdict("one coherent, distinct problem?")
        if verdict == "q":
            break
        if verdict != "s":
            with db.begin() as session:
                session.add(
                    Label(target_type="cluster", target_id=c.id, labeler=labeler, value=verdict)
                )


def _precision(title: str, values: list[str]) -> str:
    yes = values.count("y")
    return f"{title:<30} {yes / len(values):>4.0%}  ({yes}/{len(values)})"


@app.command()
def labels(run: int = typer.Argument(..., help="Research run id.")) -> None:
    """Precision of a run's labels: share of `y`, overall and by signal type and source."""
    by_id = {sig.id: (sig, doc) for sig, _, doc in _signal_rows(run)}
    signal_labels = _latest_labels("signal", list(by_id))
    if not signal_labels:
        typer.echo(f"no signal labels yet; run `signalforge label signals {run}`")
    splits: list[tuple[str, Callable[[Signal, Document], object]]] = [
        ("type", lambda s, d: s.type),
        ("source", lambda s, d: d.source_category or "unlisted"),
        ("first_hand", lambda s, d: s.first_hand),
    ]
    for labeler in sorted({who for _, who in signal_labels}):
        mine = {sid: v for (sid, who), v in signal_labels.items() if who == labeler}
        typer.secho(f"\nsignals labelled by {labeler} (M3 exit: ≥ 70% y on 50)", bold=True)
        typer.echo(_precision("all", list(mine.values())))
        for name, key in splits:
            groups: dict[str, list[str]] = defaultdict(list)
            for sid, value in mine.items():
                groups[f"{name}={key(*by_id[sid])}"].append(value)
            for title, values in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                typer.echo(_precision(f"  {title}", values))

    cluster_labels = _latest_labels("cluster", [c.id for c in _run_clusters(run)])
    for labeler in sorted({who for _, who in cluster_labels}):
        values = [v for (_, who), v in cluster_labels.items() if who == labeler]
        typer.secho(f"\nclusters labelled by {labeler}", bold=True)
        typer.echo(_precision("coherent, distinct problem", values))
