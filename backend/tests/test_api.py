"""The M8 API (api/routes): every endpoint on seeded rows, including 404 / 409 / 422."""

from dataclasses import dataclass

import pytest
from fakes import PACK, PLAN
from fastapi.testclient import TestClient

from signalforge.agents import AGENTS
from signalforge.api.deps import get_config, get_db
from signalforge.api.main import app
from signalforge.config import get_defaults
from signalforge.db.models import (
    Claim,
    Competitor,
    Document,
    Excerpt,
    GapMatrix,
    Opportunity,
    ProblemCluster,
    ResearchRun,
    ScoreCard,
    Signal,
    StageRun,
)
from signalforge.pipeline.runner import create_run

DEFAULTS = get_defaults()


@pytest.fixture
def client(db, tmp_path):
    config = DEFAULTS.model_copy(
        update={"report": DEFAULTS.report.model_copy(update={"out_dir": tmp_path})}
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    yield TestClient(app)
    app.dependency_overrides.clear()


@dataclass
class Seeded:
    run_id: int
    top: int  # scored opportunity
    knocked_out: int
    middle: int
    excerpts: list[int]
    claims: dict[str, int]
    clusters: list[int]


def _seed(db) -> Seeded:
    """One run with two problems, three opportunities, documents, excerpts, signals and claims."""
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    with db.begin() as s:
        docs = [
            Document(run_id=run_id, url="https://a.com/1", canonical_url="a.com/1",
                     domain="a.com", title="A", source_category="forum", quality_tier="medium",
                     text_hash="secret-hash"),
            Document(run_id=run_id, url="https://b.com/1", canonical_url="b.com/1",
                     domain="b.com", source_category="jobs", quality_tier="high",
                     snippet_only=True),
            Document(run_id=run_id, url="https://c.com/1", canonical_url="c.com/1",
                     domain="c.com"),  # unlisted, no excerpts: not a source
            Document(run_id=run_id, url="https://d.com/1", canonical_url="d.com/1",
                     domain="d.com", quality_tier="low"),  # unlisted
        ]  # fmt: skip
        s.add_all(docs)
        s.flush()
        excerpts = [
            Excerpt(run_id=run_id, document_id=docs[0].id,
                    quote="İRSALİYE bilgisini elle giriyoruz",
                    translation="We type delivery notes by hand", verified="exact",
                    author_hash="author-secret"),
            Excerpt(run_id=run_id, document_id=docs[0].id, quote="Excel tablosu her gün bozuluyor",
                    verified="fuzzy"),
            Excerpt(run_id=run_id, document_id=docs[1].id, quote="Operasyon uzmanı aranıyor",
                    verified="exact"),
            Excerpt(run_id=run_id, document_id=docs[3].id, quote="Kimse bunu çözmedi",
                    verified="exact"),
        ]  # fmt: skip
        s.add_all(excerpts)
        s.flush()
        signals = [
            Signal(run_id=run_id, excerpt_id=excerpts[0].id, type="workaround",
                   statement="types notes", first_hand=True),
            Signal(run_id=run_id, excerpt_id=excerpts[1].id, type="tool_complaint",
                   statement="excel breaks", first_hand=True),
            Signal(run_id=run_id, excerpt_id=excerpts[2].id, type="labor_spend",
                   statement="hires ops", first_hand=False),
        ]  # fmt: skip
        s.add_all(signals)
        s.flush()
        fact = Claim(
            run_id=run_id,
            kind="fact",
            statement="Teams re-type notes",
            supports=[excerpts[0].id],
            stage="extract",
            entailment="supported",
        )
        fact2 = Claim(
            run_id=run_id,
            kind="fact",
            statement="Ops staff are hired",
            supports=[excerpts[2].id],
            stage="extract",
        )
        root = Claim(
            run_id=run_id,
            kind="fact",
            statement="Excel breaks",
            supports=[excerpts[1].id],
            stage="extract",
        )
        s.add_all([fact, fact2, root])
        s.flush()
        # inference ← mid (derived from root): only one level of derived_from is included.
        mid = Claim(
            run_id=run_id,
            kind="inference",
            statement="Spreadsheets fail",
            derived_from=[root.id],
            stage="cluster",
        )
        s.add(mid)
        s.flush()
        inference = Claim(
            run_id=run_id,
            kind="inference",
            statement="Carriers have a problem",
            derived_from=[fact.id, mid.id],
            stage="cluster",
        )
        hypothesis = Claim(run_id=run_id, kind="hypothesis", statement="Owner pays", stage="buyers")
        price = Claim(run_id=run_id, kind="fact", statement="Rival costs $20", stage="competitors")
        problem_claim = Claim(
            run_id=run_id, kind="inference", statement="Carriers re-type notes", stage="cluster"
        )
        s.add_all([inference, hypothesis, price, problem_claim])
        s.flush()  # fmt: skip
        problems = [
            ProblemCluster(run_id=run_id, name="Delivery notes", description="d",
                           signal_ids=[signals[0].id, signals[1].id], independent_source_count=3,
                           evidence_strength=6.5, source_category_mix={"forum": 2},
                           claim_id=problem_claim.id),
            ProblemCluster(run_id=run_id, name="Hiring", description="h",
                           signal_ids=[signals[2].id], evidence_strength=4.0),
        ]  # fmt: skip
        s.add_all(problems)
        s.flush()
        rival = Competitor(run_id=run_id, problem_id=problems[0].id, name="Rival",
                           url="https://rival.com")  # fmt: skip
        s.add(rival)
        s.flush()
        s.add(
            GapMatrix(
                run_id=run_id,
                problem_id=problems[0].id,
                matrix={
                    "dimensions": [{"key": "price", "label": "Price"}],
                    "competitor_ids": [rival.id],
                    "cells": {"price": {str(rival.id): {"value": "yes", "claim_id": price.id}}},
                    "gaps": [],
                },
            )
        )
        opps = [
            Opportunity(run_id=run_id, problem_id=problems[0].id, segment="SMB carriers",
                        solution_angle="app", status="passed",
                        economic_model={"value_usd_month": [100.5, 300]},
                        buyer_roles={"buyer": {"role": "owner", "claim_ids": [fact.id],
                                               "hypothesis_claim_id": hypothesis.id}},
                        wtp_signals=[{"kind": "labor_spend", "claim_id": fact2.id}]),
            Opportunity(run_id=run_id, problem_id=problems[1].id, segment="x", solution_angle="y",
                        status="knocked_out", knockouts=[{"rule": "no_budget_owner"}]),
            Opportunity(run_id=run_id, problem_id=problems[1].id, segment="m", solution_angle="n",
                        status="passed", economic_model={"value_usd_month": "bad"}),
        ]  # fmt: skip
        s.add_all(opps)
        s.flush()
        s.add_all(
            [
                ScoreCard(opportunity_id=opps[0].id, attractiveness=70, confidence="medium",
                          founder_fit="fit", category="interesting",
                          factors={"severity": {"level": 3, "claim_ids": [inference.id]}},
                          rule_trace=[{"rule": "x"}], experiment={"claim_id": fact.id}),
                ScoreCard(opportunity_id=opps[1].id, category="false_positive", factors={}),
                ScoreCard(opportunity_id=opps[2].id, attractiveness=40, confidence="low",
                          founder_fit="fit", category="weak", factors={}),
            ]
        )  # fmt: skip
        return Seeded(
            run_id=run_id,
            top=opps[0].id,
            knocked_out=opps[1].id,
            middle=opps[2].id,
            excerpts=[e.id for e in excerpts],
            claims={
                "fact": fact.id,
                "fact2": fact2.id,
                "root": root.id,
                "mid": mid.id,
                "inference": inference.id,
                "hypothesis": hypothesis.id,
                "price": price.id,
                "problem": problem_claim.id,
            },  # fmt: skip
            clusters=[p.id for p in problems],
        )


# --- agents, plans --------------------------------------------------------------------------


def test_agents_in_run_order(client) -> None:
    body = client.get("/api/agents").json()
    assert [a["name"] for a in body] == [a.name for a in AGENTS]
    assert body[0]["stages"][0] == "query_gen" and body[-1]["stages"] == ["report"]


def test_plan_examples(client) -> None:
    body = {e["id"]: e for e in client.get("/api/plans/examples").json()}
    assert "m7/customs" in body and "tr-logistics" in body
    assert body["m7/customs"]["title"].startswith("M7 full-run plan")
    assert body["m7/customs"]["plan"]["request"]["industry"] == "customs"


# --- runs -----------------------------------------------------------------------------------


def test_create_run_queues_a_full_pipeline_job(client) -> None:
    response = client.post("/api/runs", json=PLAN.model_dump(mode="json"))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "planned" and body["pack_id"] == "tr"
    assert body["industry"] == PLAN.request.industry and body["budget_usd"] == 10.0
    assert body["latest_job"]["status"] == "queued"
    assert [j["id"] for j in body["jobs"]] == [body["latest_job"]["id"]]
    assert body["stages_done"] == 0 and body["current_stage"] is None
    assert {a["status"] for a in body["agents"]} == {"pending"}
    assert body["created_at"].endswith("Z")


def test_create_run_without_start(client) -> None:
    body = client.post("/api/runs?start=false", json=PLAN.model_dump(mode="json")).json()
    assert body["jobs"] == [] and body["latest_job"] is None


def test_create_run_validation(client) -> None:
    plan = PLAN.model_dump(mode="json")
    plan["request"]["country"] = "XX"
    response = client.post("/api/runs", json=plan)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "request", "country"]

    del plan["submarkets"]
    assert client.post("/api/runs", json=plan).status_code == 422


def test_list_runs_newest_first(client, db) -> None:
    ids = [create_run(db, PLAN, PACK, DEFAULTS) for _ in range(3)]
    body = client.get("/api/runs?limit=2").json()
    assert [r["id"] for r in body] == ids[::-1][:2]
    assert body[0]["stages_total"] == sum(len(a.stages) for a in AGENTS)


def test_run_detail_progress(client, db, tmp_path) -> None:
    seeded = _seed(db)
    run_id = seeded.run_id
    with db.begin() as s:
        s.add_all(
            [
                StageRun(run_id=run_id, stage="query_gen", status="failed", error="old"),
                StageRun(run_id=run_id, stage="query_gen", status="completed", cost_usd=0.5),
                StageRun(run_id=run_id, stage="search", status="completed", cost_usd=0.25),
                StageRun(run_id=run_id, stage="triage", status="running"),
                StageRun(run_id=run_id, stage="extract", status="completed"),
            ]
        )
        s.get_one(ResearchRun, run_id).spent_usd = 0.75
    (tmp_path / f"run-{run_id}").mkdir()
    (tmp_path / f"run-{run_id}" / "report.md").write_text("# r", encoding="utf-8")

    body = client.get(f"/api/runs/{run_id}").json()

    assert body["stages_done"] == 3 and body["current_stage"] == "triage"
    assert body["spent_usd"] == 0.75 and body["opportunity_count"] == 3
    assert body["report_formats"] == ["md"]
    agents = {a["name"]: a for a in body["agents"]}
    discovery = agents["source_discovery"]
    assert discovery["status"] == "running" and discovery["cost_usd"] == 0.75
    assert discovery["finished_at"] is None
    assert [s["status"] for s in discovery["stages"][:4]] == [
        "completed", "completed", "running", "pending",
    ]  # fmt: skip
    assert discovery["stages"][0]["error"] is None  # the latest StageRun wins
    assert agents["problem_discovery"]["status"] == "partial"
    assert agents["report_writer"]["status"] == "pending"


def test_run_not_found(client) -> None:
    assert client.get("/api/runs/999").status_code == 404


# --- jobs -----------------------------------------------------------------------------------


def test_create_and_cancel_jobs(client, db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)

    response = client.post(f"/api/runs/{run_id}/jobs", json={"agent": "report_writer"})
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "queued" and job["args"]["agent"] == "report_writer"

    assert client.post(f"/api/runs/{run_id}/jobs", json={}).status_code == 409
    cancelled = client.post(f"/api/jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert client.post("/api/jobs/999/cancel").status_code == 404

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["latest_job"]["status"] == "cancelled"


@pytest.mark.parametrize(
    "args",
    [{"agent": "report_writer", "resume": True}, {"from_stage": "nope"}, {"extra": 1}],
)
def test_create_job_validation(client, db, args) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    assert client.post(f"/api/runs/{run_id}/jobs", json=args).status_code == 422


def test_create_job_unknown_run(client) -> None:
    assert client.post("/api/runs/999/jobs", json={}).status_code == 404


# --- opportunities --------------------------------------------------------------------------


def test_opportunity_cards_by_attractiveness(client, db) -> None:
    seeded = _seed(db)
    body = client.get(f"/api/runs/{seeded.run_id}/opportunities").json()

    assert [o["id"] for o in body] == [seeded.top, seeded.middle, seeded.knocked_out]
    top, middle, knocked = body
    assert top["value_usd_month"] == {"low": 100.5, "high": 300.0}
    assert top["problem_name"] == "Delivery notes" and top["evidence_strength"] == 6.5
    assert middle["value_usd_month"] is None
    assert knocked["gate2_status"] == "knocked_out" and knocked["attractiveness"] is None
    assert knocked["knockouts"] == [{"rule": "no_budget_owner"}]
    assert client.get("/api/runs/999/opportunities").status_code == 404


def test_opportunity_detail_resolves_every_claim(client, db) -> None:
    seeded = _seed(db)
    response = client.get(f"/api/opportunities/{seeded.top}")
    body = response.json()
    c = seeded.claims

    # Referenced: fact, fact2, inference, hypothesis, price, the problem's claim; one derived_from
    # level adds mid (not root).
    assert set(body["claims"]) == {
        str(c[k]) for k in ("fact", "fact2", "inference", "hypothesis", "price", "problem", "mid")
    }
    assert body["problem"]["claim_id"] == c["problem"]
    assert body["claims"][str(c["problem"])]["statement"] == "Carriers re-type notes"
    inference = body["claims"][str(c["inference"])]
    assert inference["kind"] == "inference" and inference["excerpts"] == []
    assert inference["derived_from"] == [c["fact"], c["mid"]]
    fact = body["claims"][str(c["fact"])]
    assert fact["entailment"] == "supported"
    assert fact["excerpts"] == [
        {
            "id": seeded.excerpts[0],
            "quote": "İRSALİYE bilgisini elle giriyoruz",
            "translation": "We type delivery notes by hand",
            "verified": "exact",
            "url": "https://a.com/1",
            "domain": "a.com",
            "source_category": "forum",
            "quality_tier": "medium",
        }
    ]
    assert body["problem"]["independent_source_count"] == 3
    assert body["score"]["experiment"] == {"claim_id": c["fact"]}
    matrix = body["gap_matrix"]
    assert [x["name"] for x in matrix["competitors"]] == ["Rival"]
    assert list(matrix["cells"]["price"]) == [str(matrix["competitors"][0]["id"])]
    assert "author-secret" not in response.text and "secret-hash" not in response.text


def test_opportunity_detail_without_score_or_matrix(client, db) -> None:
    seeded = _seed(db)
    body = client.get(f"/api/opportunities/{seeded.knocked_out}").json()
    assert body["gap_matrix"] is None and body["claims"] == {}
    assert client.get("/api/opportunities/999").status_code == 404


# --- reports --------------------------------------------------------------------------------


def test_report_files(client, db, tmp_path) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    directory = tmp_path / f"run-{run_id}"
    directory.mkdir()
    (directory / "report.html").write_text("<p>ok</p>", encoding="utf-8")

    response = client.get(f"/api/runs/{run_id}/report/html")
    assert response.status_code == 200 and response.text == "<p>ok</p>"
    assert response.headers["content-type"].startswith("text/html")
    assert client.get(f"/api/runs/{run_id}/report/md").status_code == 404
    assert client.get(f"/api/runs/{run_id}/report/pdf").status_code == 422


# --- sources --------------------------------------------------------------------------------


def test_sources_lists_documents_with_excerpts(client, db) -> None:
    seeded = _seed(db)
    response = client.get(f"/api/runs/{seeded.run_id}/sources")
    body = response.json()

    assert body["total"] == 3 and body["offset"] == 0 and body["limit"] == 50
    first = body["items"][0]
    assert first["document"]["domain"] == "a.com" and first["document"]["origin"] == "collect"
    assert [e["id"] for e in first["excerpts"]] == seeded.excerpts[:2]
    assert first["excerpts"][0]["signals"][0]["cluster_ids"] == [seeded.clusters[0]]
    assert body["facets"] == {
        "categories": {"forum": 1, "jobs": 1, "unlisted": 1},
        "tiers": {"medium": 1, "high": 1, "low": 1},
        "signal_types": {"workaround": 1, "tool_complaint": 1, "labor_spend": 1},
        "clusters": [
            {"id": seeded.clusters[0], "name": "Delivery notes", "n": 2},
            {"id": seeded.clusters[1], "name": "Hiring", "n": 1},
        ],
    }
    assert "author-secret" not in response.text and "secret-hash" not in response.text


@pytest.mark.parametrize(
    ("query", "documents", "excerpts"),
    [
        ("category=jobs", ["b.com"], [2]),
        ("category=unlisted", ["d.com"], [3]),
        ("tier=medium", ["a.com"], [0, 1]),
        ("domain=b.com", ["b.com"], [2]),
        ("signal_type=tool_complaint", ["a.com"], [1]),
        ("q=irsaliye", ["a.com"], [0]),  # Turkish-aware: İRSALİYE
        ("q=EXCEL%20TABLOSU", ["a.com"], [1]),
        ("q=nothing", [], []),
    ],
)
def test_source_filters(client, db, query, documents, excerpts) -> None:
    seeded = _seed(db)
    body = client.get(f"/api/runs/{seeded.run_id}/sources?{query}").json()

    assert [i["document"]["domain"] for i in body["items"]] == documents
    assert [e["id"] for i in body["items"] for e in i["excerpts"]] == [
        seeded.excerpts[k] for k in excerpts
    ]
    assert body["total"] == len(documents)
    # Over the run, not the page.
    assert body["facets"]["categories"] == {"forum": 1, "jobs": 1, "unlisted": 1}


def test_source_cluster_filter_and_paging(client, db) -> None:
    seeded = _seed(db)
    url = f"/api/runs/{seeded.run_id}/sources"
    body = client.get(f"{url}?cluster_id={seeded.clusters[1]}").json()
    assert [i["document"]["domain"] for i in body["items"]] == ["b.com"]

    page = client.get(f"{url}?offset=1&limit=1").json()
    assert page["total"] == 3 and [i["document"]["domain"] for i in page["items"]] == ["b.com"]
    assert client.get(f"{url}?limit=201").status_code == 422
    assert client.get("/api/runs/999/sources").status_code == 404
