"""Market scan: config overrides and the market comparison (reporting/compare.py)."""

from fakes import FIT, PACK, PLAN

from signalforge.config import deep_merge, get_defaults
from signalforge.db.models import Document, Excerpt, IndependenceGroup, Signal
from signalforge.pipeline.runner import create_run
from signalforge.reporting.compare import compare_markets, render_comparison

DEFAULTS = get_defaults()
LOST_PARCEL = {**FIT, "manual_task": None, "cause": "third_party"}


def test_overrides_merge_key_by_key_and_replace_lists() -> None:
    base = {"query_gen": {"max_queries": 150, "pack_seed_queries": True}, "triage": {"x": [1, 2]}}
    merged = deep_merge(base, {"query_gen": {"max_queries": 25}, "triage": {"x": [3]}})
    assert merged == {
        "query_gen": {"max_queries": 25, "pack_seed_queries": True},
        "triage": {"x": [3]},
    }
    assert base["query_gen"]["max_queries"] == 150  # not modified in place


def _market(db, industry: str, fits: list[dict | None], same_author: tuple[int, int] | None = None):
    plan = PLAN.model_copy(
        update={"request": PLAN.request.model_copy(update={"industry": industry})}
    )
    run_id = create_run(db, plan, PACK, DEFAULTS)
    with db.begin() as session:
        docs = []
        for i, fit in enumerate(fits):
            url = f"https://{industry}{i}.com/t"
            doc = Document(run_id=run_id, url=url, canonical_url=url, domain=f"{industry}{i}.com",
                           quality_tier="medium")  # fmt: skip
            session.add(doc)
            session.flush()
            docs.append(doc.id)
            excerpt = Excerpt(run_id=run_id, document_id=doc.id, quote=f"alıntı {i}",
                              translation=f"quote {i}", verified="exact",
                              source="text")  # fmt: skip
            session.add(excerpt)
            session.flush()
            session.add(Signal(run_id=run_id, excerpt_id=excerpt.id, type="workaround",
                               statement=f"s{i}", first_hand=True,
                               meta={"fit": fit} if fit else {}))  # fmt: skip
        if same_author:
            session.add(IndependenceGroup(run_id=run_id, rule="same_author",
                                          document_ids=[docs[i] for i in same_author]))  # fmt: skip
    return run_id


def test_markets_rank_by_independent_sources_with_software_fit_signals(db, tmp_path) -> None:
    # Accounting: 3 fit signals, but two share an author -> 2 sources. Logistics: 1 fit source and
    # two lost-parcel complaints (real pain, not software-fit). Old: signals without fit facts.
    accounting = _market(db, "accounting", [FIT, FIT, FIT, None], same_author=(0, 1))
    logistics = _market(db, "logistics", [FIT, LOST_PARCEL, LOST_PARCEL])
    old = _market(db, "old", [None, None])

    rows = compare_markets(db, [logistics, old, accounting], DEFAULTS, examples=5)
    assert [r.market for r in rows] == ["accounting", "logistics", "old"]
    acc, log, _ = rows
    assert (acc.fit_sources, acc.fit_signals, acc.signals, acc.documents) == (2, 3, 4, 4)
    assert (log.fit_sources, log.fit_signals, log.signals) == (1, 1, 3)
    assert len(acc.examples) == 2  # one example per independent source
    assert acc.examples[0].manual_task == FIT["manual_task"]

    report = render_comparison(rows, tmp_path / "compare.md").read_text(encoding="utf-8")
    assert report.index("| accounting |") < report.index("| logistics |")
    assert "No software-fit signals found." in report
