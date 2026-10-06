"""The bounded search/fetch loop shared by ``verify`` and ``competitors`` (agent-modules.md §0.5).

``LLMService.parse`` returns structured output only, so this is a manual loop over structured
actions, not provider-side function calling. Each step the model sees the goal, everything the
loop has seen so far and what budget is left, and returns one :class:`LoopAction`. Our own cached
search and fetch providers carry it out, so results go through the cache, the source registry and
(in the caller's ``on_page``) quote verification.

Mechanical guards:

- a fetch names a hit id from this loop's own search results, so a URL from the model's memory
  can never be fetched; a page already in the run's evidence or already fetched is rejected;
- search text is cleaned like ``query_gen`` drafts (``clean_query``): operators drop the query,
  ``site:`` survives only for an offered registry domain; Turkish-aware near-duplicate queries
  and queries the run already searched are rejected;
- the loop stops on ``finish``, when steps / searches / fetches run out, or after
  ``max_rejections_in_row`` rejected actions in a row.

Provider errors on a search are retried like the collection ``search`` stage's
(``collect.search_retries``); the retries count as one search. An LLM error (``LLMError``) ends
the loop with ``llm_error``; an unreachable provider (``LLMUnavailable``) stops the stage.

A stage can add fixed searches the model did not choose (:func:`probe`, e.g. a product's pricing
page on its own site) to a finished loop's trace; they are recorded like the loop's own, with
step 0, and never re-read a page the loop read.

The loop never writes to the database: it returns a :class:`LoopTrace`, and the stage persists
queries and results (:func:`store_searches`), documents and evidence afterwards (one writer, no
races between problems running in parallel). The model's input is a pure function of the goal and
the cached provider responses, so replay mode reproduces a loop exactly.
"""

import json
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.config import LoopDefaults
from signalforge.db.models import Query, SearchResult
from signalforge.pipeline.context import RunContext
from signalforge.prompts import Prompt
from signalforge.providers.fetch import FetchedPage, FetchStatus
from signalforge.providers.llm import BudgetExceeded, LLMError, ModelTier
from signalforge.providers.search import SearchError, SearchHit, search_with_retries
from signalforge.providers.urls import canonicalize_url, domain_of, in_domain
from signalforge.queries import Deduper, clean_query

SNIPPET_ONLY = "snippet_only"


class LoopAction(BaseModel):
    """One step of a bounded research loop (output of the ``*_loop`` prompts)."""

    action: Literal["search", "fetch", "finish"]
    query: str | None = Field(
        default=None, description="search: words only, in the market's language, ≤ 10 words"
    )
    site: str | None = Field(
        default=None, description="search: one offered domain to restrict to, or null"
    )
    hit_id: int | None = Field(default=None, description="fetch: id of a search hit shown above")
    reason: str = Field(description="English, ≤ 20 words")


@dataclass(frozen=True)
class LoopHit:
    id: int  # local to the loop; the model fetches by this id
    rank: int
    url: str
    canonical_url: str
    domain: str
    title: str | None
    snippet: str | None
    date: str | None
    category: str | None  # registry category, None if unlisted
    tier: str
    access: str  # fetch | snippet_only
    off_site: bool  # the query had a `site:` hint for another domain
    known: bool  # already in the run's evidence when the loop saw it

    def as_search_hit(self) -> SearchHit:
        return SearchHit(
            rank=self.rank, url=self.url, title=self.title, snippet=self.snippet, date=self.date
        )


@dataclass(frozen=True)
class LoopSearch:
    step: int
    query: str  # as sent to the provider (with `site:`)
    hits: list[LoopHit]
    cache_hit: bool
    error: str | None = None


@dataclass(frozen=True)
class LoopPage:
    """A page the loop read. ``page`` is None for snippet-only hits (no download)."""

    step: int
    hit: LoopHit
    page: FetchedPage | None
    text: str
    source: str  # text | snippet

    @property
    def url(self) -> str:
        if self.page is None:
            return self.hit.url
        return self.page.final_url or self.page.url


@dataclass
class LoopTrace:
    stop_reason: str = ""
    steps: int = 0
    searches: list[LoopSearch] = field(default_factory=list)
    pages: list[LoopPage] = field(default_factory=list)
    failed_fetches: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)  # every action, with its outcome
    rejections: Counter[str] = field(default_factory=Counter)
    llm_cache_hits: int = 0
    read_urls: set[str] = field(default_factory=set)  # canonical URLs fetched (or tried)

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-safe record for StageRun metrics and ProblemCluster.verification."""
        return {
            "stop_reason": self.stop_reason,
            "steps": self.steps,
            "searches": len(self.searches),
            "fetches": len(self.pages) + len(self.failed_fetches),
            "pages": len(self.pages),
            "failed_fetches": len(self.failed_fetches),
            "rejections": dict(self.rejections),
            "llm_cache_hits": self.llm_cache_hits,
            "actions": self.actions,
        }


@dataclass(frozen=True)
class _Observation:
    full: str
    short: str


@dataclass(frozen=True)
class _Unread:
    """A hit that could not be read: ``outcome`` for the action record, ``note`` for the model."""

    outcome: str
    note: str


# Agent-specific processing of a page the loop read; returns a one-line note the model sees
# (e.g. "3 signals kept"), so it learns which sources pay off.
OnPage = Callable[[LoopPage], str]


def _hit_line(hit: LoopHit, with_snippet: bool) -> str:
    source = f"{hit.category}, {hit.tier}" if hit.category else f"unlisted, {hit.tier}"
    flags = "".join(
        f" [{flag}]"
        for flag, on in (
            ("already in evidence", hit.known),
            ("off-site", hit.off_site),
            ("snippet only", hit.access == SNIPPET_ONLY),
        )
        if on
    )
    line = f"  [h{hit.id}] {hit.domain} ({source}){flags} — {hit.title or '(no title)'}"
    if with_snippet and hit.snippet:
        line += f"\n        {hit.snippet}"
    return line


def render_input(
    goal: str,
    allowed_domains: Sequence[str],
    observations: Sequence[_Observation],
    left: dict[str, int],
    max_chars: int,
) -> str:
    """Prompt input: goal and offered domains first (stable prefix), history, then the budget.

    When the history exceeds ``max_chars``, the oldest observations are shortened first.
    """
    parts = [o.full for o in observations]
    total = sum(map(len, parts))
    for i, o in enumerate(observations):
        if total <= max_chars:
            break
        total -= len(parts[i]) - len(o.short)
        parts[i] = o.short
    history = "\n\n".join(parts) if parts else "(nothing yet)"
    domains = ", ".join(allowed_domains) if allowed_domains else "(none)"
    budget = json.dumps(left)
    return (
        f"# Goal\n{goal}\n\n"
        f"# Domains you may use with `site`\n{domains}\n\n"
        f"# History\n{history}\n\n"
        f"# Budget left\n{budget}"
    )


def _make_hit(
    ctx: RunContext, h: SearchHit, hit_id: int, hint: str | None, known_urls: Collection[str]
) -> LoopHit:
    canonical = canonicalize_url(h.url)
    domain = domain_of(h.url)
    source = ctx.pack.source_for(domain)
    return LoopHit(
        id=hit_id,
        rank=h.rank,
        url=h.url,
        canonical_url=canonical,
        domain=domain,
        title=h.title,
        snippet=h.snippet,
        date=h.date,
        category=source.category if source else None,
        tier=source.tier if source else ctx.pack.default_tier,
        access=source.access if source else "fetch",
        off_site=bool(hint) and not in_domain(domain, hint),
        known=canonical in known_urls,
    )


def _read(
    ctx: RunContext, hit: LoopHit, step: int, trace: LoopTrace, known_urls: Collection[str]
) -> LoopPage | _Unread:
    """Read one hit: download it, or use its snippet on a ``snippet_only`` domain."""
    trace.read_urls.add(hit.canonical_url)
    if hit.access == SNIPPET_ONLY:
        text = "\n".join(t for t in (hit.title, hit.snippet) if t)
        return LoopPage(step, hit, None, text, "snippet")
    page = ctx.fetcher.fetch(hit.url)
    if page.status is not FetchStatus.OK or not page.text:
        trace.failed_fetches.append({"step": step, "url": hit.url, "status": page.status.value})
        status = page.status.value
        return _Unread(f"fetch failed: {status}", f"failed ({status})")
    final = canonicalize_url(page.final_url or hit.url)
    if final != hit.canonical_url and (final in known_urls or final in trace.read_urls):
        trace.rejections["duplicate_url"] += 1
        return _Unread("redirected to a known page", "redirected to a page already read")
    trace.read_urls.add(final)
    return LoopPage(step, hit, page, page.text, "text")


def run_loop(
    ctx: RunContext,
    *,
    stage: str,
    prompt: Prompt,
    goal: str,
    budget: LoopDefaults,
    allowed_domains: Sequence[str],
    known_urls: Collection[str],
    known_queries: Collection[str],
    on_page: OnPage,
    tier: ModelTier = ModelTier.ANALYSIS,
) -> LoopTrace:
    """Run one bounded loop; see the module docstring for the guards."""
    trace = LoopTrace()
    observations: list[_Observation] = []
    hits: dict[int, LoopHit] = {}
    deduper = Deduper(ctx.defaults.query_gen.near_dup_ratio)
    query_cfg = ctx.defaults.query_gen
    locale = ctx.pack.search
    n = ctx.defaults.search.results_per_query
    searches_left, fetches_left = budget.max_searches, budget.max_fetches
    rejected_in_row = 0

    def observe(full: str, short: str | None = None) -> None:
        observations.append(_Observation(full, short or full))

    def reject(step: int, action: LoopAction, reason: str) -> None:
        nonlocal rejected_in_row
        rejected_in_row += 1
        trace.rejections[reason] += 1
        trace.actions[-1]["outcome"] = f"rejected: {reason}"
        observe(f"step {step}: {_describe(action)} → REJECTED ({reason})")

    while True:
        if trace.steps >= budget.max_steps:
            trace.stop_reason = "max_steps"
            break
        if searches_left <= 0 and fetches_left <= 0:
            trace.stop_reason = "budget"
            break
        if rejected_in_row >= budget.max_rejections_in_row:
            trace.stop_reason = "rejections"
            break
        step = trace.steps + 1
        left = {
            "steps": budget.max_steps - trace.steps,
            "searches": searches_left,
            "fetches": fetches_left,
        }
        prompt_input = render_input(
            goal, allowed_domains, observations, left, budget.max_observation_chars
        )
        try:
            result = ctx.llm.parse(
                tier,
                prompt,
                prompt_input,
                LoopAction,
                stage=stage,
                max_output_tokens=budget.max_output_tokens,
            )
        except BudgetExceeded:
            raise
        except LLMError:
            trace.stop_reason = "llm_error"
            break
        trace.steps = step
        trace.llm_cache_hits += result.cache_hit
        action = result.output
        trace.actions.append({"step": step, **action.model_dump(exclude_none=True)})

        if action.action == "finish":
            trace.actions[-1]["outcome"] = "finish"
            trace.stop_reason = "finish"
            break

        if action.action == "search":
            if searches_left <= 0:
                reject(step, action, "no_searches_left")
                continue
            if not action.query:
                reject(step, action, "missing_query")
                continue
            notes: Counter[str] = Counter()
            cleaned = clean_query(action.query, action.site, allowed_domains, query_cfg, notes)
            if cleaned is None:
                reject(step, action, "invalid_query")
                continue
            text, hint = cleaned
            query = f"{text} site:{hint}" if hint else text
            if query in known_queries or not deduper.add(text, hint):
                reject(step, action, "duplicate_query")
                continue
            searches_left -= 1
            rejected_in_row = 0
            try:
                # Timeouts are retried here, so a slow answer doesn't cost the loop a search.
                response, _ = search_with_retries(
                    ctx.search, query, locale, n, ctx.defaults.collect.search_retries
                )
            except SearchError as exc:
                trace.searches.append(LoopSearch(step, query, [], False, str(exc)))
                trace.actions[-1]["outcome"] = "search_error"
                observe(f'step {step}: search "{query}" → error: {exc}')
                continue
            found = []
            for h in response.hits:
                hit = _make_hit(ctx, h, len(hits) + 1, hint, known_urls)
                hits[hit.id] = hit
                found.append(hit)
            trace.searches.append(LoopSearch(step, query, found, response.cache_hit))
            trace.actions[-1]["outcome"] = f"{len(found)} hits"
            head = f'step {step}: search "{query}" → {len(found)} hits'
            observe(
                "\n".join([head, *(_hit_line(h, True) for h in found)]),
                "\n".join([head, *(_hit_line(h, False) for h in found)]),
            )
            continue

        # fetch
        if fetches_left <= 0:
            reject(step, action, "no_fetches_left")
            continue
        hit = hits.get(action.hit_id) if action.hit_id is not None else None
        if hit is None:
            reject(step, action, "unknown_hit")
            continue
        if hit.known:
            reject(step, action, "known_url")
            continue
        if hit.canonical_url in trace.read_urls:
            reject(step, action, "duplicate_url")
            continue
        fetches_left -= 1
        rejected_in_row = 0
        read = _read(ctx, hit, step, trace, known_urls)
        if isinstance(read, _Unread):
            trace.actions[-1]["outcome"] = read.outcome
            observe(f"step {step}: fetch [h{hit.id}] → {read.note}")
            continue
        trace.pages.append(read)
        note = on_page(read)
        trace.actions[-1]["outcome"] = note
        preview = read.text[: budget.page_preview_chars]
        head = f"step {step}: read [h{hit.id}] {read.url} — {note}"
        observe(f"{head}\n  title: {hit.title or ''}\n  text: {preview}", head)

    return trace


def probe(
    ctx: RunContext,
    trace: LoopTrace,
    *,
    query: str,
    domain: str,
    reads: int,
    known_urls: Collection[str],
    on_page: OnPage,
) -> str:
    """A fixed search after a loop: search ``query`` as written (a ``site:`` included), then read
    up to ``reads`` of its hits on ``domain`` that the loop has not read. Recorded in ``trace``
    with step 0; returns the outcome."""
    action: dict[str, Any] = {"step": 0, "action": "probe", "query": query}
    trace.actions.append(action)
    try:
        response, _ = search_with_retries(
            ctx.search,
            query,
            ctx.pack.search,
            ctx.defaults.search.results_per_query,
            ctx.defaults.collect.search_retries,
        )
    except SearchError as exc:
        trace.searches.append(LoopSearch(0, query, [], False, str(exc)))
        action["outcome"] = "search_error"
        return action["outcome"]
    first_id = sum(len(s.hits) for s in trace.searches) + 1
    found = [
        _make_hit(ctx, h, first_id + i, domain, known_urls) for i, h in enumerate(response.hits)
    ]
    trace.searches.append(LoopSearch(0, query, found, response.cache_hit))
    notes = []
    for hit in found:
        if len(notes) >= reads:
            break
        if hit.off_site or hit.known or hit.canonical_url in trace.read_urls:
            continue
        read = _read(ctx, hit, 0, trace, known_urls)
        if isinstance(read, _Unread):
            notes.append(read.outcome)
            continue
        trace.pages.append(read)
        notes.append(on_page(read))
    action["outcome"] = "; ".join(notes) or f"{len(found)} hits, none new on {domain}"
    return action["outcome"]


def _describe(action: LoopAction) -> str:
    if action.action == "search":
        site = f" site:{action.site}" if action.site else ""
        return f'search "{action.query or ""}{site}"'
    if action.action == "fetch":
        return f"fetch [h{action.hit_id}]"
    return action.action


def store_searches(
    session: Session,
    run_id: int,
    traces: Sequence[tuple[int, LoopTrace]],
    *,
    intent: str,
    lang: str,
    provider: str,
) -> None:
    """Write the loops' searches as Query rows (``meta.origin = "loop"``) with their results.

    ``traces`` pairs each loop with its problem id. A query several problems ran is stored once,
    with every problem in ``meta.problem_ids``; text the run already has under another intent
    (e.g. a collection query) is not stored again.
    """
    existing = {q.text: q for q in session.scalars(select(Query).where(Query.run_id == run_id))}
    for problem_id, trace in traces:
        for s in trace.searches:
            query = existing.get(s.query)
            if query is not None:
                ids = query.meta.get("problem_ids", [])
                if query.intent == intent and problem_id not in ids:
                    query.meta = {**query.meta, "problem_ids": [*ids, problem_id]}
                continue
            hint = s.query.split(" site:", 1)[1] if " site:" in s.query else None
            query = Query(
                run_id=run_id,
                text=s.query,
                lang=lang,
                intent=intent,
                meta={
                    "origin": "loop",
                    "problem_ids": [problem_id],
                    "step": s.step,
                    "source_hint": hint,
                    "error": s.error,
                },
            )
            session.add(query)
            session.flush()
            existing[s.query] = query
            session.add_all(
                SearchResult(
                    query_id=query.id,
                    url=h.url,
                    title=h.title,
                    snippet=h.snippet,
                    rank=h.rank,
                    provider=provider,
                )
                for h in s.hits
            )
