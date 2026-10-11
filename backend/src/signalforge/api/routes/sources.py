from collections import Counter, defaultdict
from collections.abc import Iterable

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from signalforge.api.deps import DB
from signalforge.api.schemas import (
    ClusterFacet,
    Facets,
    SourceDocument,
    SourceExcerpt,
    SourceItem,
    SourcePage,
    SourceSignal,
)
from signalforge.db.models import Document, Excerpt, ProblemCluster, ResearchRun, Signal
from signalforge.evidence.strength import UNLISTED
from signalforge.text import collapse_whitespace, tr_casefold, unify_punctuation

router = APIRouter(tags=["sources"])


@router.get("/runs/{run_id}/sources", response_model=SourcePage)
def list_sources(
    run_id: int,
    db: DB,
    category: str | None = None,
    tier: str | None = None,
    domain: str | None = None,
    signal_type: str | None = None,
    cluster_id: int | None = None,
    q: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> SourcePage:
    """The run's documents that have excerpts, with their excerpts and signals.

    Document filters (``category``, ``tier``, ``domain``) select documents; excerpt filters
    (``signal_type``, ``cluster_id``, ``q``) select excerpts, and a document is listed with its
    matching excerpts only. Facets count over the whole run, not the filtered page. Documents
    from domains outside the source registry have category ``unlisted``.
    """
    with db() as session:
        if session.get(ResearchRun, run_id) is None:
            raise HTTPException(404, f"run {run_id} not found")
        rows = session.execute(
            select(Excerpt, Document)
            .join(Document, Document.id == Excerpt.document_id)
            .where(Excerpt.run_id == run_id)
            .order_by(Document.id, Excerpt.id)
        ).all()
        signals: dict[int, list[Signal]] = defaultdict(list)
        for signal in session.scalars(
            select(Signal).where(Signal.run_id == run_id).order_by(Signal.id)
        ):
            signals[signal.excerpt_id].append(signal)
        clusters = session.scalars(
            select(ProblemCluster)
            .where(ProblemCluster.run_id == run_id)
            .order_by(ProblemCluster.id)
        ).all()

    cluster_ids: dict[int, list[int]] = defaultdict(list)
    for cluster in clusters:
        for signal_id in cluster.signal_ids:
            cluster_ids[signal_id].append(cluster.id)
    needle = _normalize(q) if q else None

    def excerpt_matches(excerpt: Excerpt) -> bool:
        own = signals[excerpt.id]
        if signal_type and not any(s.type == signal_type for s in own):
            return False
        if cluster_id is not None and not any(cluster_id in cluster_ids[s.id] for s in own):
            return False
        return needle is None or needle in _normalize(excerpt.quote)

    documents: dict[int, Document] = {}
    by_document: dict[int, list[Excerpt]] = defaultdict(list)
    for excerpt, document in rows:
        documents[document.id] = document
        if (
            (category is None or (document.source_category or UNLISTED) == category)
            and (tier is None or document.quality_tier == tier)
            and (domain is None or document.domain == domain)
            and excerpt_matches(excerpt)
        ):
            by_document[document.id].append(excerpt)

    page = list(by_document.items())[offset : offset + limit]
    run_signals = [s for excerpt, _ in rows for s in signals[excerpt.id]]
    run_signal_ids = {s.id for s in run_signals}
    return SourcePage(
        total=len(by_document),
        offset=offset,
        limit=limit,
        items=[
            SourceItem(
                document=SourceDocument.model_validate(documents[doc_id], from_attributes=True),
                excerpts=[
                    SourceExcerpt(
                        id=e.id,
                        quote=e.quote,
                        translation=e.translation,
                        verified=e.verified,
                        signals=[
                            SourceSignal(
                                id=s.id,
                                type=s.type,
                                statement=s.statement,
                                first_hand=s.first_hand,
                                cluster_ids=cluster_ids[s.id],
                            )
                            for s in signals[e.id]
                        ],
                    )
                    for e in excerpts
                ],
            )
            for doc_id, excerpts in page
        ],
        facets=Facets(
            categories=_counts(d.source_category or UNLISTED for d in documents.values()),
            tiers=_counts(d.quality_tier for d in documents.values()),
            signal_types=_counts(s.type for s in run_signals),
            clusters=[
                ClusterFacet(id=c.id, name=c.name, n=len(run_signal_ids.intersection(c.signal_ids)))
                for c in clusters
            ],
        ),
    )


def _normalize(text: str) -> str:
    """Turkish-aware matching form (``I/ı/İ/i`` are one letter), as for quote verification."""
    return collapse_whitespace(tr_casefold(unify_punctuation(text)))


def _counts(values: Iterable[str | None]) -> dict[str, int]:
    """Counts of the non-null values, most common first."""
    return dict(Counter(v for v in values if v is not None).most_common())
