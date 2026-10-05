"""Documents: how a fetched page or a search snippet becomes a stored source (plan §5, §10).

Shared by ``fetch`` (collection) and the M4 loop stages (``verify``, ``competitors``), so every
document gets its domain, registry category, tier, date and text hash the same way. Full page text
stays in the page cache; a document stores only its hash.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.db.models import Document, UrlCandidate
from signalforge.evidence.dedup import text_hash
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.providers.fetch import FetchedPage, FetchStatus
from signalforge.providers.search import SearchHit
from signalforge.providers.urls import canonicalize_url, domain_of
from signalforge.text import guess_language


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value[:10]), datetime.min.time(), tzinfo=UTC)
    except ValueError:
        return None


def snippet_document(c: UrlCandidate, now: datetime) -> Document:
    return Document(
        url=c.url,
        canonical_url=c.canonical_url,
        domain=c.domain,
        title=c.title,
        source_category=c.source_category,
        quality_tier=c.quality_tier,
        snippet_only=True,
        fetched_at=now,
        text_hash=text_hash(c.snippet) if c.snippet else None,
        lang=guess_language(c.snippet or ""),
    )


def page_document(c: UrlCandidate, page: FetchedPage, pack: MarketPack) -> Document:
    final = page.final_url or c.url
    domain = domain_of(final)
    source = pack.source_for(domain) if domain != c.domain else None
    text = page.text or ""
    return Document(
        url=final,
        canonical_url=canonicalize_url(final),
        domain=domain,
        title=page.title or c.title,
        source_category=source.category if source else c.source_category,
        quality_tier=source.tier if source else c.quality_tier,
        snippet_only=False,
        published_at=parse_date(page.published_at),
        fetched_at=page.fetched_at,
        text_hash=text_hash(text),
        lang=guess_language(text) or page.html_lang,
    )


def hit_snippet_text(hit: SearchHit) -> str:
    """The evidence text of a snippet-only document: title and snippet, one per line."""
    return "\n".join(t for t in (hit.title, hit.snippet) if t)


def loop_page_document(page: FetchedPage, pack: MarketPack) -> Document:
    """Document for a page a loop fetched (status ok). Category and tier come from the registry."""
    final = page.final_url or page.url
    domain = domain_of(final)
    source = pack.source_for(domain)
    text = page.text or ""
    return Document(
        url=final,
        canonical_url=canonicalize_url(final),
        domain=domain,
        title=page.title,
        source_category=source.category if source else None,
        quality_tier=source.tier if source else pack.default_tier,
        snippet_only=False,
        published_at=parse_date(page.published_at),
        fetched_at=page.fetched_at,
        text_hash=text_hash(text),
        lang=guess_language(text) or page.html_lang,
    )


def loop_snippet_document(hit: SearchHit, pack: MarketPack, now: datetime) -> Document:
    """Document for a search hit on a ``snippet_only`` registry domain (never downloaded)."""
    domain = domain_of(hit.url)
    source = pack.source_for(domain)
    text = hit_snippet_text(hit)
    return Document(
        url=hit.url,
        canonical_url=canonicalize_url(hit.url),
        domain=domain,
        title=hit.title,
        source_category=source.category if source else None,
        quality_tier=source.tier if source else pack.default_tier,
        snippet_only=True,
        published_at=parse_date(hit.date),
        fetched_at=now,
        text_hash=text_hash(text) if text else None,
        lang=guess_language(text),
    )


def store_document(
    session: Session, run_id: int, doc: Document, *, origin: str, problem_id: int | None
) -> tuple[Document, bool]:
    """Get-or-create by ``(run_id, canonical_url)``: ``(stored document, created)``.

    An existing document (collected, or stored by another problem's loop) is reused as is, so a
    page is one source however many stages read it.
    """
    existing = session.scalar(
        select(Document).where(
            Document.run_id == run_id, Document.canonical_url == doc.canonical_url
        )
    )
    if existing is not None:
        return existing, False
    doc.run_id = run_id
    doc.origin = origin
    doc.problem_id = problem_id
    session.add(doc)
    session.flush()
    return doc, True


def collected_texts(ctx: RunContext, document_ids: list[int]) -> dict[int, str]:
    """Text of collected documents (page cache, or title + snippet for snippet-only ones).

    Documents whose page has been purged from the cache are left out.
    """
    if not document_ids:
        return {}
    with ctx.db() as session:
        docs = session.scalars(select(Document).where(Document.id.in_(document_ids))).all()
        candidates = {
            c.document_id: c
            for c in session.scalars(
                select(UrlCandidate)
                .where(UrlCandidate.document_id.in_(document_ids))
                .order_by(UrlCandidate.priority.desc())  # best priority wins below
            )
        }
    texts: dict[int, str] = {}
    for doc in docs:
        c = candidates.get(doc.id)
        if doc.snippet_only:
            text = "\n".join(t for t in (doc.title, c.snippet if c else None) if t)
        else:
            url = c.url if c and c.fetch_status == FetchStatus.OK.value else doc.url
            page = ctx.fetcher.cached(url)
            text = page.text if page and page.text else ""
        if text:
            texts[doc.id] = text
    return texts
