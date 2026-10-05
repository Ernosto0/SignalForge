"""fetch stage (plan §4, §6, §10): triaged URLs → documents.

Candidates are processed in triage priority order, in waves: each wave asks for exactly the
documents still missing, kept candidates first, then the ranked reserve, so failed downloads are
replaced instead of lowering the document count. ``snippet_only`` sources (login-walled or JS-only,
per the pack registry) become documents without a download; their evidence is the SERP snippet.
Full page text stays in the page cache (never exported); documents store its hash.
"""

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import Any

from sqlalchemy import delete, select, update

from signalforge.config import FetchStageDefaults
from signalforge.db.models import Document, UrlCandidate
from signalforge.evidence.documents import page_document, snippet_document
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult
from signalforge.providers.cache import cache_key
from signalforge.providers.fetch import FetchedPage, FetchStatus

SNIPPET_ONLY = "snippet_only"
DUPLICATE_URL = "duplicate_url"  # redirected to a page another candidate already produced


@dataclass
class Outcome:
    candidate: UrlCandidate
    page: FetchedPage | None  # None for snippet-only candidates
    document: Document | None = None
    duplicate_of: str | None = None  # canonical URL of the existing document


@dataclass
class Fetched:
    outcomes: list[Outcome]
    metrics: dict[str, Any] = field(default_factory=dict)


def collect(
    candidates: list[UrlCandidate],
    fetch: Callable[[str], FetchedPage],
    pack: MarketPack,
    cfg: FetchStageDefaults,
) -> Fetched:
    """Fetch in waves until ``max_documents`` documents exist or candidates/attempts run out."""
    queue = list(candidates)  # already in priority order: keep, then reserve
    outcomes: list[Outcome] = []
    by_canonical: dict[str, Document] = {}
    attempts = 0
    now = datetime.now(UTC)

    with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
        while queue and len(by_canonical) < cfg.max_documents:
            need = cfg.max_documents - len(by_canonical)
            wave: list[UrlCandidate] = []
            while queue and len(wave) < need:
                c = queue[0]
                if c.access != SNIPPET_ONLY and attempts >= cfg.max_attempts:
                    break
                wave.append(queue.pop(0))
                attempts += c.access != SNIPPET_ONLY
            if not wave:
                break
            to_fetch = [c for c in wave if c.access != SNIPPET_ONLY]
            fetched = pool.map(lambda c: fetch(c.url), to_fetch)
            pages = dict(zip(map(id, to_fetch), fetched, strict=True))
            for c in wave:  # priority order decides which duplicate keeps the document
                page = pages.get(id(c))
                outcome = Outcome(c, page)
                if page is None:
                    doc = snippet_document(c, now)
                elif page.status is FetchStatus.OK:
                    doc = page_document(c, page, pack)
                else:
                    doc = None
                if doc is not None:
                    if existing := by_canonical.get(doc.canonical_url):
                        outcome.duplicate_of = existing.canonical_url
                    else:
                        by_canonical[doc.canonical_url] = doc
                        outcome.document = doc
                outcomes.append(outcome)
    return Fetched(outcomes, fetch_metrics(outcomes, len(candidates)))


def status_of(o: Outcome) -> str:
    if o.page is None:
        return SNIPPET_ONLY
    if o.duplicate_of:
        return DUPLICATE_URL
    return o.page.status.value


def fetch_metrics(outcomes: list[Outcome], available: int) -> dict[str, Any]:
    fetched = [o for o in outcomes if o.page is not None]
    ok = [o for o in fetched if o.page and o.page.status is FetchStatus.OK]
    docs = [o.document for o in outcomes if o.document is not None]
    failed_domains = Counter(o.candidate.domain for o in fetched if o not in ok)
    text_chars = [len(o.page.text or "") for o in ok if o.page]
    return {
        "candidates_available": available,
        "attempted": len(fetched),
        "fetched_ok": len(ok),
        # Share of download attempts that produced extractable text (plan §11).
        "fetch_success_rate": round(len(ok) / len(fetched), 3) if fetched else None,
        "page_cache_hits": sum(bool(o.page and o.page.cache_hit) for o in fetched),
        "by_status": dict(Counter(status_of(o) for o in outcomes).most_common()),
        "from_reserve": sum(o.candidate.decision == "reserve" for o in outcomes),
        "documents": len(docs),
        "documents_fetched": sum(not d.snippet_only for d in docs),
        "documents_snippet_only": sum(d.snippet_only for d in docs),
        "duplicate_url": sum(o.duplicate_of is not None for o in outcomes),
        "documents_by_lang": dict(Counter(str(d.lang) for d in docs).most_common()),
        "documents_by_tier": dict(Counter(str(d.quality_tier) for d in docs).most_common()),
        "documents_by_category": dict(
            Counter(str(d.source_category or "unlisted") for d in docs).most_common()
        ),
        "documents_with_date": sum(d.published_at is not None for d in docs),
        "unique_domains": len({d.domain for d in docs}),
        "median_text_chars": int(median(text_chars)) if text_chars else 0,
        "top_failed_domains": dict(failed_domains.most_common(8)),
    }


class Fetch:
    name = "fetch"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.fetch_stage
        with ctx.db() as session:
            candidates = session.scalars(
                select(UrlCandidate)
                .where(
                    UrlCandidate.run_id == ctx.run_id,
                    UrlCandidate.decision.in_(("keep", "reserve")),
                )
                .order_by(UrlCandidate.priority)
            ).all()
        if not candidates:
            raise ValueError(f"run {ctx.run_id} has no kept URLs; run triage first")

        fetched = collect(list(candidates), ctx.fetcher.fetch, ctx.pack, cfg)

        with ctx.db.begin() as session:
            # Idempotent: a rerun replaces this run's documents (and, by cascade, their evidence).
            session.execute(
                update(UrlCandidate)
                .where(UrlCandidate.run_id == ctx.run_id)
                .values(fetch_status=None, fetch_error=None, document_id=None)
            )
            session.execute(delete(Document).where(Document.run_id == ctx.run_id))
            docs: dict[str, Document] = {}
            for o in fetched.outcomes:
                if o.document is not None:
                    o.document.run_id = ctx.run_id
                    session.add(o.document)
                    docs[o.document.canonical_url] = o.document
            session.flush()
            for o in fetched.outcomes:
                doc = o.document or (docs.get(o.duplicate_of) if o.duplicate_of else None)
                session.execute(
                    update(UrlCandidate)
                    .where(UrlCandidate.id == o.candidate.id)
                    .values(
                        fetch_status=status_of(o),
                        fetch_error=(o.page.error if o.page else None),
                        document_id=doc.id if doc else None,
                    )
                )

        input_hash = cache_key(
            {
                "candidates": [c.canonical_url for c in candidates],
                "config": cfg.model_dump(mode="json"),
            }
        )
        return StageResult(metrics=fetched.metrics, input_hash=input_hash)
