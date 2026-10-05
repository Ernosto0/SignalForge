"""Entailment: does a fact claim's evidence really state it? (plan §2 #9, §7.2)

Quote verification proves an excerpt exists on the page; it does not prove the excerpt supports
the claim written about it. A cheap model reads each fact with the original quotes and
translations of its excerpts and returns ``supported`` / ``partial`` / ``not_supported`` /
``contradicted``. Facts that fail (``not_supported``, ``contradicted``) are kept, but evidence
counts and citations skip them; ``partial`` facts may be cited with a flag.

Called by ``verify`` (a problem's key claims), ``competitors`` (the claims a gap matrix cites) and,
in M6, ``report`` on every fact it cites. Each fact is checked once: :func:`entail_pending` skips
facts that already have a verdict.
"""

import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.db.models import Claim, Excerpt
from signalforge.pipeline.context import RunContext
from signalforge.prompts import load_prompt
from signalforge.providers.llm import BudgetExceeded, LLMError, ModelTier

Verdict = Literal["supported", "partial", "not_supported", "contradicted"]
FAILED: frozenset[str] = frozenset({"not_supported", "contradicted"})


class EntailmentVerdict(BaseModel):
    item: int = Field(description="number of the claim in the input")
    verdict: Verdict
    note: str = Field(description="English, ≤ 20 words")


class EntailmentBatch(BaseModel):
    """Output of the ``entailment`` prompt: one verdict per numbered claim."""

    verdicts: list[EntailmentVerdict]


@dataclass(frozen=True)
class ClaimEvidence:
    claim_id: int
    statement: str
    quotes: list[tuple[str, str | None]]  # (original quote, English translation)


@dataclass(frozen=True)
class Judged:
    verdict: str
    note: str


# Asks the model for one batch: prompt input -> (verdicts, cache_hit).
AskFn = Callable[[str], tuple[EntailmentBatch, bool]]


def failed(entailment: str | None) -> bool:
    """A checked fact whose excerpts do not support it (it no longer counts as evidence)."""
    return entailment in FAILED


def build_input(items: list[ClaimEvidence]) -> str:
    """Claims numbered 1..n within the batch (not database ids), so cached answers survive a
    re-extract that renumbers the claims."""
    payload = [
        {
            "item": n,
            "claim": item.statement,
            "evidence": [
                {"quote": quote, "translation": translation} for quote, translation in item.quotes
            ],
        }
        for n, item in enumerate(items, 1)
    ]
    return f"# Claims\n{json.dumps(payload, ensure_ascii=False, indent=1)}"


def check(
    items: list[ClaimEvidence], ask: AskFn, batch_size: int
) -> tuple[dict[int, Judged], Counter[str]]:
    """Verdict per claim id. A claim the model skipped stays unchecked (counted as ``missing``)."""
    notes: Counter[str] = Counter()
    out: dict[int, Judged] = {}
    ordered = sorted(items, key=lambda i: i.claim_id)
    for start in range(0, len(ordered), max(batch_size, 1)):
        batch = ordered[start : start + batch_size]
        notes["llm_calls"] += 1
        try:
            answer, hit = ask(build_input(batch))
        except BudgetExceeded:
            raise
        except LLMError:
            notes["llm_errors"] += 1
            notes["missing"] += len(batch)
            continue
        notes["llm_cache_hits"] += hit
        for v in answer.verdicts:
            if not 1 <= v.item <= len(batch):
                notes["unknown_items"] += 1
                continue
            claim_id = batch[v.item - 1].claim_id
            if claim_id in out:
                notes["repeated_items"] += 1
                continue
            out[claim_id] = Judged(v.verdict, v.note)
        notes["missing"] += sum(item.claim_id not in out for item in batch)
    return out, notes


def load_claim_evidence(
    session: Session, run_id: int, claim_ids: Iterable[int], max_quotes: int
) -> list[ClaimEvidence]:
    """The fact claims among ``claim_ids`` with their excerpts' quotes and translations."""
    claims = session.scalars(
        select(Claim).where(
            Claim.run_id == run_id, Claim.id.in_(sorted(set(claim_ids))), Claim.kind == "fact"
        )
    ).all()
    excerpt_ids = {e for c in claims for e in c.supports}
    excerpts = {
        e.id: e for e in session.scalars(select(Excerpt).where(Excerpt.id.in_(excerpt_ids)))
    }
    return [
        ClaimEvidence(
            c.id,
            c.statement,
            [
                (excerpts[e].quote, excerpts[e].translation)
                for e in c.supports[:max_quotes]
                if e in excerpts
            ],
        )
        for c in claims
    ]


def entail_pending(ctx: RunContext, claim_ids: Iterable[int], *, stage: str) -> dict[str, int]:
    """Check the fact claims among ``claim_ids`` that have no verdict yet; store the verdicts.

    Returns counts for stage metrics: ``checked`` (new verdicts), ``already_checked``, verdicts by
    kind, and call notes.
    """
    cfg = ctx.defaults.entailment
    prompt = load_prompt("entailment")
    ids = sorted(set(claim_ids))
    with ctx.db() as session:
        pending_ids = session.scalars(
            select(Claim.id).where(
                Claim.run_id == ctx.run_id,
                Claim.id.in_(ids),
                Claim.kind == "fact",
                Claim.entailment.is_(None),
            )
        ).all()
        items = load_claim_evidence(session, ctx.run_id, pending_ids, cfg.max_quotes_per_claim)
    counts: Counter[str] = Counter()
    if not items:
        return {"checked": 0, "already_checked": len(ids)}

    def ask(prompt_input: str) -> tuple[EntailmentBatch, bool]:
        result = ctx.llm.parse(
            ModelTier.FAST,
            prompt,
            prompt_input,
            EntailmentBatch,
            stage=stage,
            max_output_tokens=cfg.max_output_tokens,
        )
        return result.output, result.cache_hit

    verdicts, notes = check(items, ask, cfg.batch_size)
    with ctx.db.begin() as session:
        for claim in session.scalars(select(Claim).where(Claim.id.in_(list(verdicts)))):
            judged = verdicts[claim.id]
            claim.entailment = judged.verdict
            claim.entailment_checked = True
            claim.meta = {**claim.meta, "entailment_note": judged.note, "entailment_by": stage}
            counts[judged.verdict] += 1
    return {
        "checked": len(verdicts),
        "already_checked": len(ids) - len(items),
        **counts,
        **notes,
    }


def clear_entailment(session: Session, run_id: int, stage: str) -> None:
    """Forget the verdicts a stage wrote (it re-checks them on a re-run; answers are cached)."""
    for claim in session.scalars(
        select(Claim).where(
            Claim.run_id == run_id,
            Claim.entailment.is_not(None),
            Claim.meta["entailment_by"].astext == stage,
        )
    ):
        claim.entailment = None
        claim.entailment_checked = False
        claim.meta = {
            k: v for k, v in claim.meta.items() if k not in ("entailment_note", "entailment_by")
        }
