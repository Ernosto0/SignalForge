"""dedupe stage (plan §4, §6, §7.3): documents → independence groups.

URL-level duplicates are already collapsed upstream (canonical URLs in triage, redirects in fetch).
Here fetched texts are compared: identical text or MinHash near-duplicates form one group, i.e. one
independent source. Snippet-only documents are too short to compare and stay singletons. Author
grouping (``same_author``) runs on excerpts in M3.
"""

from collections import Counter
from typing import Any

from sqlalchemy import delete, select

from signalforge.db.models import Document, IndependenceGroup, UrlCandidate
from signalforge.evidence.dedup import DocText, DuplicateGroup, find_duplicates
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult
from signalforge.providers.cache import cache_key
from signalforge.providers.fetch import FetchStatus


def dedupe_metrics(
    documents: int, compared: int, groups: list[DuplicateGroup], domains: dict[int, str]
) -> dict[str, Any]:
    collapsed = sum(len(g.document_ids) - 1 for g in groups)
    return {
        "documents": documents,
        "compared": compared,
        "groups": len(groups),
        "groups_by_rule": dict(Counter(g.rule for g in groups)),
        "documents_in_groups": sum(len(g.document_ids) for g in groups),
        # Documents that are not an independent source of their own (plan §11).
        "duplicate_collapse_rate": round(collapsed / documents, 3) if documents else 0.0,
        "independent_sources": documents - collapsed,
        "largest_group": max((len(g.document_ids) for g in groups), default=0),
        "group_domains": dict(
            Counter(domains[g.document_ids[0]] for g in groups if g.rule == "near_dup")
        ),
    }


class Dedupe:
    name = "dedupe"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.dedupe
        with ctx.db() as session:
            documents = session.scalars(
                select(Document)
                .where(Document.run_id == ctx.run_id, Document.origin == "collect")
                .order_by(Document.id)
            ).all()
            # The page cache is keyed by the URL that was fetched, which is the candidate's URL.
            fetched_url = dict(
                session.execute(
                    select(UrlCandidate.document_id, UrlCandidate.url).where(
                        UrlCandidate.run_id == ctx.run_id,
                        UrlCandidate.fetch_status == FetchStatus.OK.value,
                    )
                ).all()
            )
        if not documents:
            raise ValueError(f"run {ctx.run_id} has no documents; run fetch first")

        texts = []
        missing = 0
        for doc in documents:
            if doc.snippet_only or doc.id not in fetched_url:
                continue
            page = ctx.fetcher.cached(fetched_url[doc.id])
            if page is None or not page.text:
                missing += 1  # purged from the page cache since fetch
                continue
            texts.append(DocText(doc.id, doc.domain, page.text))
        groups = find_duplicates(texts, cfg)

        with ctx.db.begin() as session:
            session.execute(delete(IndependenceGroup).where(IndependenceGroup.run_id == ctx.run_id))
            session.add_all(
                IndependenceGroup(run_id=ctx.run_id, rule=g.rule, document_ids=g.document_ids)
                for g in groups
            )

        domains = {d.id: d.domain for d in documents}
        input_hash = cache_key(
            {
                "documents": [f"{d.canonical_url}|{d.text_hash}" for d in documents],
                "config": cfg.model_dump(mode="json"),
            }
        )
        metrics = dedupe_metrics(len(documents), len(texts), groups, domains)
        metrics["text_missing"] = missing
        return StageResult(metrics=metrics, input_hash=input_hash)
