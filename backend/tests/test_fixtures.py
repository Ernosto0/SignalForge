"""Frozen run fixtures: export → empty database → import → replay without providers."""

import gzip
import json

import pytest
from fakes import PACK, PLAN, FakeSearch, make_context
from sqlalchemy import select, text

from signalforge.config import CacheMode, get_defaults
from signalforge.db.base import Base
from signalforge.db.models import Document, Label, Query, ResearchRun
from signalforge.fixtures import DATA, MANIFEST, export_runs, import_fixture, table_counts
from signalforge.pipeline.runner import create_run, latest_stage_runs, run_pipeline
from signalforge.pipeline.stages import STAGES

COLLECTION = STAGES[1:5]  # search → dedupe; query_gen needs the real model
QUERIES = ["irsaliye excel", "bad gümrük evrak", "depo sayım farkı"]
# Metrics that describe the cache, not the research: they differ between a live run and replay.
CACHE_KEYS = {"cache_hits", "searched_live", "retried", "page_cache_hits", "llm_cache_hits"}


class NoProvider(FakeSearch):
    """A search provider that fails the test if replay ever reaches it."""

    def search(self, *_: object) -> list:
        raise AssertionError("replay called the search provider")


def _collect(db, search: FakeSearch, mode: CacheMode = CacheMode.LIVE) -> int:
    run_id = create_run(db, PLAN, PACK, get_defaults())
    with db.begin() as session:
        session.add_all(
            Query(run_id=run_id, text=t, lang="tr", intent="pain", meta={}) for t in QUERIES
        )
    pages = {"page_text": _page} if mode is CacheMode.REPLAY else {}
    ctx = make_context(db, run_id, search, **pages)
    ctx.cache.mode = mode
    run_pipeline(ctx, COLLECTION)
    return run_id


def _page(path: str) -> str:
    raise AssertionError(f"replay fetched {path}")


def _research_metrics(db, run_id: int) -> dict[str, dict]:
    return {
        name: {k: v for k, v in row.metrics.items() if k not in CACHE_KEYS}
        for name, row in latest_stage_runs(db, run_id).items()
    }


def _wipe(db) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with db.begin() as session:
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def test_export_import_keeps_rows_ids_and_labels(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("signalforge.providers.search.base.time.sleep", lambda _: None)
    run_id = _collect(db, FakeSearch())
    other = _collect(db, FakeSearch())  # not exported
    with db.begin() as session:
        doc_id = session.scalars(select(Document.id).where(Document.run_id == run_id)).first()
        session.add(Label(target_type="signal", target_id=1, labeler="x", value="y"))
    before = table_counts(db)

    manifest = export_runs(db, [run_id], tmp_path)
    assert manifest["runs"][0]["id"] == run_id
    assert manifest["rows"]["research_runs"] == 1
    assert set(manifest["cache"]) == {"llm", "search", "page"}  # triage judges with the LLM
    assert json.loads((tmp_path / MANIFEST).read_text())["data"]["sha256"]
    # The same rows give the same bytes.
    first = (tmp_path / DATA).read_bytes()
    export_runs(db, [run_id], tmp_path)
    assert (tmp_path / DATA).read_bytes() == first

    _wipe(db)
    counts = import_fixture(db, tmp_path)

    after = table_counts(db)
    assert after["research_runs"] == 1 and counts["research_runs"] == 1
    assert 0 < after["documents"] < before["documents"]  # the other run is not in the fixture
    assert after["labels"] == 0  # the label targets a signal of no exported run
    with db() as session:
        assert session.get(ResearchRun, run_id) is not None
        assert session.get(ResearchRun, other) is None
        assert session.get(Document, doc_id).run_id == run_id
    # Sequences continue after the imported ids.
    assert create_run(db, PLAN, PACK, get_defaults()) > run_id


def test_import_checks_the_checksum_and_refuses_clashing_rows(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("signalforge.providers.search.base.time.sleep", lambda _: None)
    run_id = _collect(db, FakeSearch())
    export_runs(db, [run_id], tmp_path)

    with pytest.raises(Exception, match="duplicate key"):
        import_fixture(db, tmp_path)  # the rows are still there

    with gzip.open(tmp_path / DATA, "ab") as f:
        f.write(b"\n")
    with pytest.raises(ValueError, match="checksum"):
        import_fixture(db, tmp_path)

    with pytest.raises(ValueError, match="runs not found"):
        export_runs(db, [run_id + 99], tmp_path / "missing")


def test_a_fixture_replays_the_pipeline_without_providers(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("signalforge.providers.search.base.time.sleep", lambda _: None)
    recorded = _collect(db, FakeSearch())
    expected = _research_metrics(db, recorded)
    export_runs(db, [recorded], tmp_path)
    _wipe(db)
    import_fixture(db, tmp_path)

    replayed = _collect(db, NoProvider(), CacheMode.REPLAY)

    assert replayed != recorded
    assert _research_metrics(db, replayed) == expected
