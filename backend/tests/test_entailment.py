"""Entailment checks (evidence/entailment.py): batching, local numbering, stored verdicts."""

import json

from fakes import PACK, PLAN, FakeLLM, FakeSearch, make_context
from sqlalchemy import select

from signalforge.config import get_defaults
from signalforge.db.models import Claim, Document, Excerpt
from signalforge.evidence.claims import add_fact, add_hypothesis
from signalforge.evidence.entailment import (
    ClaimEvidence,
    EntailmentBatch,
    EntailmentVerdict,
    build_input,
    check,
    clear_entailment,
    entail_pending,
    failed,
)
from signalforge.pipeline.runner import create_run

DEFAULTS = get_defaults()


def _items(n: int) -> list[ClaimEvidence]:
    return [ClaimEvidence(100 + i, f"claim {i}", [(f"alıntı {i}", f"quote {i}")]) for i in range(n)]


def test_batches_are_numbered_locally_and_mapped_back() -> None:
    seen: list[list[int]] = []

    def ask(prompt_input: str) -> tuple[EntailmentBatch, bool]:
        items = json.loads(prompt_input.split("# Claims\n", 1)[1])
        seen.append([i["item"] for i in items])
        verdicts = [EntailmentVerdict(item=i["item"], verdict="partial", note="n") for i in items]
        # A bogus number and a repeat are ignored; the last item is skipped by the "model".
        verdicts[-1] = EntailmentVerdict(item=99, verdict="supported", note="bogus")
        verdicts.append(EntailmentVerdict(item=1, verdict="contradicted", note="repeat"))
        return EntailmentBatch(verdicts=verdicts), False

    out, notes = check(_items(5), ask, batch_size=3)
    assert seen == [[1, 2, 3], [1, 2]]
    assert {i: j.verdict for i, j in out.items()} == {
        100: "partial",
        101: "partial",
        103: "partial",
    }
    assert (notes["missing"], notes["unknown_items"], notes["repeated_items"]) == (2, 2, 2)
    assert notes["llm_calls"] == 2


def test_input_carries_quotes_and_translations_not_ids() -> None:
    text = build_input(_items(1))
    payload = json.loads(text.split("# Claims\n", 1)[1])
    assert payload == [
        {
            "item": 1,
            "claim": "claim 0",
            "evidence": [{"quote": "alıntı 0", "translation": "quote 0"}],
        }
    ]
    assert "100" not in text


def test_failed_verdicts() -> None:
    assert failed("not_supported") and failed("contradicted")
    assert not failed("supported") and not failed("partial") and not failed(None)


def test_entail_pending_checks_each_fact_once(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    with db.begin() as session:
        doc = Document(run_id=run_id, url="https://a.com", canonical_url="https://a.com",
                       domain="a.com")  # fmt: skip
        session.add(doc)
        session.flush()
        excerpt = Excerpt(run_id=run_id, document_id=doc.id, quote="İrsaliye elle giriliyor.",
                          translation="Notes are typed by hand.", verified="exact")  # fmt: skip
        session.add(excerpt)
        session.flush()
        fact = add_fact(session, run_id, "Notes are typed by hand.", [excerpt.id], stage="extract")
        hypothesis = add_hypothesis(session, run_id, "Owners would pay.", stage="plan")
    llm = FakeLLM()
    ctx = make_context(db, run_id, FakeSearch(), llm)

    counts = entail_pending(ctx, [fact.id, hypothesis.id], stage="verify")
    assert counts["checked"] == 1 and counts["supported"] == 1
    assert llm.calls == [EntailmentBatch]
    with db() as session:
        stored = session.get(Claim, fact.id)
        assert (stored.entailment, stored.entailment_checked) == ("supported", True)
        assert stored.meta == {"entailment_note": "ok", "entailment_by": "verify"}
        assert session.get(Claim, hypothesis.id).entailment is None  # only facts are checked

    assert entail_pending(ctx, [fact.id], stage="report") == {"checked": 0, "already_checked": 1}
    assert len(llm.calls) == 1

    # A stage forgets only its own verdicts.
    with db.begin() as session:
        clear_entailment(session, run_id, "report")
    with db() as session:
        assert session.get(Claim, fact.id).entailment == "supported"
    with db.begin() as session:
        clear_entailment(session, run_id, "verify")
    with db() as session:
        stored = session.scalar(select(Claim).where(Claim.id == fact.id))
        assert (stored.entailment, stored.entailment_checked, stored.meta) == (None, False, {})
