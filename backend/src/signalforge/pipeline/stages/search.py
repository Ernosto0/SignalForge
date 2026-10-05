"""search stage (plan §4, §6): every query of the run → SERP results, via the record/replay cache.

Provider errors are retried; a query that still fails is counted and skipped, so one flaky call
doesn't sink the run. Rerunning the stage re-searches only what isn't cached.
"""

import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from statistics import mean

from sqlalchemy import delete, select

from signalforge.db.models import Query, SearchResult
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult
from signalforge.providers.cache import cache_key
from signalforge.providers.search import SearchError, SearchResponse
from signalforge.providers.urls import canonicalize_url, domain_of, in_domain


@dataclass(frozen=True)
class _Outcome:
    query: Query
    response: SearchResponse | None
    attempts: int
    error: str | None = None


class Search:
    name = "search"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.collect
        n = ctx.defaults.search.results_per_query
        locale = ctx.pack.search
        with ctx.db() as session:
            queries = session.scalars(
                select(Query).where(Query.run_id == ctx.run_id).order_by(Query.id)
            ).all()
        if not queries:
            raise ValueError(f"run {ctx.run_id} has no queries; run query_gen first")

        def search_one(query: Query) -> _Outcome:
            for attempt in range(1, cfg.search_retries + 2):
                try:
                    return _Outcome(query, ctx.search.search(query.text, locale, n), attempt)
                except SearchError as exc:
                    error = str(exc)
                    if attempt <= cfg.search_retries:
                        time.sleep(2**attempt)
            return _Outcome(query, None, attempt, error)

        with ThreadPoolExecutor(max_workers=max(cfg.search_concurrency, 1)) as pool:
            outcomes = list(pool.map(search_one, queries))
        if all(o.response is None for o in outcomes):
            raise SearchError(f"every query failed; last error: {outcomes[-1].error}")

        provider = ctx.search.provider.name
        with ctx.db.begin() as session:
            session.execute(
                delete(SearchResult).where(SearchResult.query_id.in_([q.id for q in queries]))
            )
            session.add_all(
                SearchResult(
                    query_id=o.query.id,
                    url=hit.url,
                    title=hit.title,
                    snippet=hit.snippet,
                    rank=hit.rank,
                    provider=provider,
                )
                for o in outcomes
                if o.response is not None
                for hit in o.response.hits
            )

        input_hash = cache_key(
            {
                "queries": [q.text for q in queries],
                "provider": provider,
                "locale": locale.model_dump(),
                "n": n,
            }
        )
        return StageResult(metrics=search_metrics(outcomes), input_hash=input_hash)


def search_metrics(outcomes: list[_Outcome]) -> dict[str, object]:
    ok = [o for o in outcomes if o.response is not None]
    all_urls: list[str] = []
    seen: set[str] = set()
    hits_by_intent: dict[str, list[int]] = defaultdict(list)
    new_by_intent: Counter[str] = Counter()
    hinted_hits = hinted_on_site = 0
    for o in ok:
        assert o.response is not None
        hint = o.query.meta.get("source_hint")
        hits_by_intent[o.query.intent].append(len(o.response.hits))
        for hit in o.response.hits:
            canonical = canonicalize_url(hit.url)
            all_urls.append(canonical)
            if canonical not in seen:
                seen.add(canonical)
                new_by_intent[o.query.intent] += 1
            if hint:
                hinted_hits += 1
                hinted_on_site += in_domain(domain_of(hit.url), hint)

    return {
        "queries": len(outcomes),
        "searched_live": sum(not o.response.cache_hit for o in ok if o.response),
        "cache_hits": sum(o.response.cache_hit for o in ok if o.response),
        "retried": sum(o.attempts > 1 for o in outcomes),
        "failed": len(outcomes) - len(ok),
        "failed_queries": [o.query.text for o in outcomes if o.response is None],
        "zero_result_queries": sum(not o.response.hits for o in ok if o.response),
        "results": len(all_urls),
        "unique_urls": len(seen),
        # Same page returned by several queries (after URL canonicalisation).
        "url_collapse_rate": round(1 - len(seen) / len(all_urls), 3) if all_urls else 0.0,
        "results_per_query_by_intent": {k: round(mean(v), 1) for k, v in hits_by_intent.items()},
        # Query yield: URLs first contributed by each intent (queries run in id order).
        "new_urls_by_intent": dict(new_by_intent),
        # Google silently drops `site:` when nothing on the site matches every word.
        "site_hint_on_site_rate": round(hinted_on_site / hinted_hits, 3) if hinted_hits else None,
    }
