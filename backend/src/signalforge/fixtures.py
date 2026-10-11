"""Frozen run fixtures (plan §11): a run's stored outputs plus every cache entry it used.

A fixture is a directory with ``data.jsonl.gz`` (one ``{"table", "row"}`` object per line) and
``manifest.json`` (counts, prompt refs, sha256 of the data). Rows keep their database ids, so
labels stay attached to their targets; import them into an empty database.

What a fixture is for:

- **Frozen outputs.** Metrics over the imported runs are reproducible without any provider.
- **Replay.** A new run from the fixture's plan in ``replay`` mode re-executes every stage whose
  LLM inputs are unchanged since recording, and fails with ``CacheMiss`` at the first that is not
  (its prompt, config or upstream code changed: the recording is stale from there on).

The cache entries are the run's LLM calls (from the ledger, cache hits included), the searches
of its queries and the pages of every URL its searches returned. Fixtures hold third-party page
text: keep the data out of git (``.gitignore``); the manifest can be committed.
"""

import gzip
import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Numeric, Table, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import BACKEND_DIR
from signalforge.db.base import Base
from signalforge.db.models import (
    CacheEntry,
    LLMCall,
    Opportunity,
    ProblemCluster,
    Query,
    ResearchRun,
    SearchResult,
    Signal,
)
from signalforge.providers.cache import cache_key
from signalforge.providers.urls import canonicalize_url

FORMAT = 1
DATA, MANIFEST = "data.jsonl.gz", "manifest.json"
# Operational tables that are not research output.
SKIP_TABLES = {"jobs"}
# Label target types and the run-scoped table each points into.
LABEL_TARGETS = {"signal": Signal, "cluster": ProblemCluster, "opportunity": Opportunity}


def _tables() -> list[Table]:
    """Every table to export, parents before children."""
    return [t for t in Base.metadata.sorted_tables if t.name not in SKIP_TABLES]


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _decode(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for col in table.columns:
        value = out.get(col.name)
        if value is None:
            continue
        if isinstance(col.type, DateTime):
            out[col.name] = datetime.fromisoformat(value)
        elif isinstance(col.type, Numeric) and col.type.asdecimal:  # Float is Numeric too
            out[col.name] = Decimal(value)
    return out


def _cache_keys(session: Session, run_ids: list[int]) -> dict[str, set[str]]:
    """Cache keys per namespace that the runs used."""
    llm = {
        cache_key({"model": m, "prompt": f"{p}@{v}", "input_hash": h})
        for m, p, v, h in session.execute(
            select(LLMCall.model, LLMCall.prompt_id, LLMCall.prompt_version, LLMCall.input_hash)
            .where(LLMCall.run_id.in_(run_ids))
            .distinct()
        )
    }
    queries = set(session.scalars(select(Query.text).where(Query.run_id.in_(run_ids))))
    search = {
        key
        for key, request in session.execute(
            select(CacheEntry.key, CacheEntry.request).where(CacheEntry.namespace == "search")
        )
        if request.get("query") in queries
    }
    urls = session.scalars(
        select(SearchResult.url)
        .join(Query, Query.id == SearchResult.query_id)
        .where(Query.run_id.in_(run_ids))
    )
    page = {cache_key({"canonical_url": canonicalize_url(u)}) for u in urls}
    return {"llm": llm, "search": search, "page": page}


def _rows(session: Session, table: Table, run_ids: list[int]) -> Iterator[dict[str, Any]]:
    c = table.c
    if table.name == "research_runs":
        stmt = select(table).where(c.id.in_(run_ids))
    elif table.name == "cache_entries":
        yield from _cache_rows(session, table, run_ids)
        return
    elif table.name == "search_results":
        queries = select(Query.id).where(Query.run_id.in_(run_ids))
        stmt = select(table).where(c.query_id.in_(queries))
    elif table.name == "score_cards":
        opportunities = select(Opportunity.id).where(Opportunity.run_id.in_(run_ids))
        stmt = select(table).where(c.opportunity_id.in_(opportunities))
    elif table.name == "labels":
        for target_type, model in LABEL_TARGETS.items():
            ids = select(model.id).where(model.run_id.in_(run_ids))
            stmt = select(table).where(c.target_type == target_type, c.target_id.in_(ids))
            yield from (dict(r._mapping) for r in session.execute(stmt.order_by(c.id)))
        return
    elif "run_id" in c:
        stmt = select(table).where(c.run_id.in_(run_ids))
    else:
        raise ValueError(f"no export rule for table {table.name!r}")
    order = [c.id] if "id" in c else list(table.primary_key.columns)
    yield from (dict(r._mapping) for r in session.execute(stmt.order_by(*order)))


def _cache_rows(session: Session, table: Table, run_ids: list[int]) -> Iterator[dict[str, Any]]:
    for namespace, keys in _cache_keys(session, run_ids).items():
        wanted = sorted(keys)
        for start in range(0, len(wanted), 1000):
            stmt = (
                select(table)
                .where(
                    table.c.namespace == namespace, table.c.key.in_(wanted[start : start + 1000])
                )
                .order_by(table.c.key)
            )
            yield from (dict(r._mapping) for r in session.execute(stmt))


def _commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_runs(db: sessionmaker[Session], run_ids: list[int], out: Path) -> dict[str, Any]:
    """Write the runs' fixture to ``out`` and return its manifest."""
    run_ids = sorted(set(run_ids))
    out.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    cache_counts: Counter[str] = Counter()
    with db() as session:
        found = session.scalars(
            select(ResearchRun).where(ResearchRun.id.in_(run_ids)).order_by(ResearchRun.id)
        ).all()
        if missing := sorted(set(run_ids) - {r.id for r in found}):
            raise ValueError(f"runs not found: {missing}")
        prompts = sorted(
            f"{p}@{v}"
            for p, v in session.execute(
                select(LLMCall.prompt_id, LLMCall.prompt_version)
                .where(LLMCall.run_id.in_(run_ids))
                .distinct()
            )
        )
        # mtime=0: the same rows give the same bytes, so the checksum identifies the content.
        with gzip.GzipFile(out / DATA, "wb", mtime=0) as raw:
            for table in _tables():
                for row in _rows(session, table, run_ids):
                    line = {"table": table.name, "row": row}
                    raw.write(json.dumps(line, ensure_ascii=False, default=_json_default).encode())
                    raw.write(b"\n")
                    counts[table.name] += 1
                    if table.name == "cache_entries":
                        cache_counts[row["namespace"]] += 1
    manifest = {
        "format": FORMAT,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _commit(),
        "runs": [
            {
                "id": r.id,
                "industry": r.request.get("industry"),
                "status": r.status,
                "pack": f"{r.pack_id}@{r.pack_version}",
            }
            for r in found
        ],  # fmt: skip
        "prompts": prompts,
        "rows": dict(sorted(counts.items())),
        "cache": dict(sorted(cache_counts.items())),
        "data": {"file": DATA, "sha256": _sha256(out / DATA)},
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise ValueError(f"{path}: fixture format {manifest.get('format')}, expected {FORMAT}")
    return manifest


def import_fixture(db: sessionmaker[Session], path: Path, batch: int = 500) -> dict[str, int]:
    """Load a fixture into ``db``: rows with their ids (a clash raises: use an empty database),
    cache entries skipped when the key exists. Checks the data's sha256 first."""
    manifest = load_manifest(path)
    if _sha256(path / DATA) != manifest["data"]["sha256"]:
        raise ValueError(f"{path / DATA}: checksum differs from the manifest")
    tables = {t.name: t for t in _tables()}
    counts: Counter[str] = Counter()
    pending: dict[str, list[dict[str, Any]]] = {}

    def flush(session: Session, name: str) -> None:
        rows = pending.pop(name, [])
        if not rows:
            return
        table = tables[name]
        if name == "cache_entries":
            stmt = pg_insert(table).on_conflict_do_nothing(index_elements=["namespace", "key"])
        else:
            stmt = insert(table)
        session.execute(stmt, rows)
        counts[name] += len(rows)

    with db.begin() as session, gzip.open(path / DATA, "rt", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            name = item["table"]
            # Rows are written parents first: flush earlier tables before a new one starts.
            for other in [n for n in pending if n != name]:
                flush(session, other)
            pending.setdefault(name, []).append(_decode(tables[name], item["row"]))
            if len(pending[name]) >= batch:
                flush(session, name)
        for name in list(pending):
            flush(session, name)
        for table in tables.values():
            if "id" in table.c and table.c.id.autoincrement is not False:
                session.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                        f"GREATEST((SELECT MAX(id) FROM {table.name}), 1))"
                    )
                )
    return dict(counts)


def table_counts(db: sessionmaker[Session]) -> dict[str, int]:
    with db() as session:
        return {t.name: session.scalar(select(func.count()).select_from(t)) for t in _tables()}
