import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NoReturn

import typer
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from signalforge import __version__
from signalforge.config import CacheMode
from signalforge.db.models import LLMCall
from signalforge.db.session import get_session_factory
from signalforge.domain.diagnostics import PainCheck
from signalforge.pipeline.context import RunContext
from signalforge.prompts import load_prompt
from signalforge.providers.cache import CacheMiss
from signalforge.providers.llm import LLMError, ModelTier
from signalforge.providers.search import SearchError
from signalforge.providers.urls import domain_of

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
def _run_context(ctx: typer.Context, pack: str | None = None) -> Iterator[RunContext]:
    try:
        yield RunContext.create(cache_mode=ctx.obj["cache_mode"], pack_id=pack)
    except (CacheMiss, SearchError, LLMError) as exc:
        _fail(str(exc))
    except OperationalError as exc:
        _fail(
            f"database unavailable ({exc.orig}).\n"
            "Start it with `docker compose up -d db`, then `uv run alembic upgrade head`."
        )


def _fail(message: str) -> NoReturn:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


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
