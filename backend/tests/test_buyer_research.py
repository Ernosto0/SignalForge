"""Buyer research (M5): opportunities whose buyer roles cite the run's claims or are labelled
hypotheses, a null budget owner kept for Gate 2, and the buyer-roles exit check."""

import json
from datetime import UTC, datetime

from fakes import PACK, PLAN, FakeLLM, make_context
from sqlalchemy import func, select

from signalforge.config import get_defaults
from signalforge.db.models import (
    Claim,
    Competitor,
    Document,
    Excerpt,
    Opportunity,
    ProblemCluster,
    Signal,
)
from signalforge.domain.commercial import (
    BuyerAnalysis,
    BuyerRoles,
    Channel,
    OpportunityDraft,
    RoleClaim,
)
from signalforge.evidence.claims import add_fact, add_inference
from signalforge.evidence.entailment import EntailmentBatch, EntailmentVerdict
from signalforge.evidence.opportunities import check_buyer_roles
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.buyers import (
    GAP,
    PLAN_HYPOTHESIS,
    SIGNAL,
    Buyers,
    RoleCheck,
    TableRow,
    apply_role_verdict,
    validate_analysis,
)
from signalforge.providers.llm import LLMError

DEFAULTS = get_defaults()
CFG = DEFAULTS.buyers


class NoSearch:
    name = "none"

    def search(self, *_: object) -> list:
        raise AssertionError("buyers never searches")


def _role(role: str, ids: list[int] | None = None, hypothesis: str | None = None) -> RoleClaim:
    return RoleClaim(role=role, claim_ids=ids or [], hypothesis=hypothesis)


def _roles(budget_owner: RoleClaim | None = None, **kw: RoleClaim) -> BuyerRoles:
    default = _role("firma sahibi", hypothesis="Owners buy tools in small firms.")
    return BuyerRoles(
        user=kw.get("user", default),
        buyer=kw.get("buyer", default),
        decision_maker=kw.get("decision_maker", default),
        economic_beneficiary=kw.get("economic_beneficiary", default),
        budget_owner=budget_owner,
    )


def _opp(segment: str, roles: BuyerRoles | None = None, **kw) -> OpportunityDraft:
    fields = {"solution_angle": "An app that does it.", "gap_claim_ids": [], "channels": [],
              "breadth_hint": None, **kw}  # fmt: skip
    return OpportunityDraft(segment=segment, buyer_roles=roles or _roles(), **fields)


TABLE = [
    TableRow(10, "fact", SIGNAL, "Job ad: clerk reports to the operations manager."),
    TableRow(11, "inference", GAP, "No product covers driver apps."),
    TableRow(12, "hypothesis", PLAN_HYPOTHESIS, "Owners pay for tools."),
]


# --- validation -----------------------------------------------------------------------------


def test_a_role_citing_a_missing_claim_is_rejected() -> None:
    analysis = BuyerAnalysis(
        opportunities=[_opp("Carriers", _roles(user=_role("operasyon müdürü", [99])))]
    )
    v = validate_analysis(analysis, TABLE, CFG)
    assert v.drafts == []
    assert v.notes["invalid_citations"] == 1 and v.notes["dropped_unsupported_role"] == 1


def test_a_role_without_citation_or_hypothesis_is_rejected() -> None:
    analysis = BuyerAnalysis(
        opportunities=[_opp("Carriers", _roles(buyer=_role("satın alma", [], "  ")))]
    )
    v = validate_analysis(analysis, TABLE, CFG)
    assert v.drafts == [] and v.notes["dropped_unsupported_role"] == 1


def test_a_null_budget_owner_is_preserved() -> None:
    cited = _role("operasyon müdürü", [1, 99])
    analysis = BuyerAnalysis(opportunities=[_opp("Carriers", _roles(None, user=cited))])
    (d,) = validate_analysis(analysis, TABLE, CFG).drafts
    assert d.roles["budget_owner"] is None
    assert d.roles["user"] == {"role": "operasyon müdürü", "claim_ids": [10], "hypothesis": None}
    assert d.basis["user"] == "fact" and d.basis["buyer"] == "hypothesis"


def test_duplicate_segments_collapse_and_the_list_is_capped() -> None:
    analysis = BuyerAnalysis(
        opportunities=[
            _opp("Road freight firms with 5–50 trucks"),
            _opp("road freight firms, 5-50 trucks"),
            _opp("Customs brokers"),
            _opp("Warehouse operators"),
        ]
    )
    v = validate_analysis(
        analysis, TABLE, CFG.model_copy(update={"max_opportunities_per_problem": 2})
    )
    assert [d.segment for d in v.drafts] == [
        "Road freight firms with 5–50 trucks",
        "Customs brokers",
    ]
    assert (v.notes["dropped_duplicate_segment"], v.notes["dropped_over_cap"]) == (1, 1)


def test_gap_citations_must_be_gaps_and_channels_keep_uncited() -> None:
    channels = [Channel(kind="association", name="UND", claim_ids=[]),
                Channel(kind="community", name="Forum", claim_ids=[1])]  # fmt: skip
    analysis = BuyerAnalysis(opportunities=[_opp("Carriers", gap_claim_ids=[2, 1, 7],
                                                 channels=channels)])  # fmt: skip
    v = validate_analysis(analysis, TABLE, CFG)
    (d,) = v.drafts
    assert d.gap_claim_ids == [11] and v.notes["gap_citations_dropped"] == 2
    assert [(c["name"], c["claim_ids"], c["cited"]) for c in d.channels] == [
        ("UND", [], False),
        ("Forum", [10], True),
    ]


# --- stage ----------------------------------------------------------------------------------


def _seed(db, run_id: int) -> dict[str, int]:
    with db.begin() as session:
        doc = Document(run_id=run_id, url="https://f.com/t", canonical_url="https://f.com/t",
                       domain="f.com", source_category="forum", quality_tier="medium",
                       published_at=datetime.now(UTC))  # fmt: skip
        session.add(doc)
        session.flush()
        ids: dict[str, int] = {}

        def excerpt(quote: str) -> Excerpt:
            e = Excerpt(run_id=run_id, document_id=doc.id, quote=quote, translation="t",
                        verified="exact", char_start=0, char_end=1)  # fmt: skip
            session.add(e)
            session.flush()
            return e

        def signal(key: str, kind: str, statement: str, first_hand: bool = True) -> Claim:
            e = excerpt(f"Alıntı {key}.")
            s = Signal(run_id=run_id, excerpt_id=e.id, type=kind, actor="nakliyeci",
                       workflow="delivery", statement=statement,
                       first_hand=first_hand)  # fmt: skip
            session.add(s)
            session.flush()
            ids[key] = s.id
            fact = add_fact(session, run_id, statement, [e.id], stage="extract")
            ids[f"{key}_fact"] = fact.id
            return fact

        pain = signal("pain", "complaint", "Carriers collect delivery photos over WhatsApp.")
        job = signal("job", "labor_spend", "A carrier hires a clerk reporting to the ops manager.",
                     first_hand=False)  # fmt: skip
        signal("bad", "complaint", "Failed fact.").entailment = "not_supported"
        signal("excluded", "complaint", "Excluded by verify.")
        signal("other", "complaint", "Other problem's signal.")
        inference = add_inference(session, run_id, "Carriers struggle with proof of delivery.",
                                  [pain.id, job.id], stage="cluster")  # fmt: skip
        clusters = []
        for name, signals, shortlisted in (
            ("Proof of delivery", ["pain", "job", "bad", "excluded"], True),
            ("Customs paperwork", ["other"], False),
        ):
            c = ProblemCluster(run_id=run_id, name=name, description="d",
                               signal_ids=[ids[k] for k in signals], shortlisted=shortlisted,
                               claim_id=inference.id if shortlisted else None,
                               verification={"excluded_signal_ids": [ids["excluded"]]})  # fmt: skip
            session.add(c)
            session.flush()
            clusters.append(c)
        ids["problem"] = clusters[0].id
        competitor = Competitor(run_id=run_id, problem_id=clusters[0].id, name="Rota Pro",
                                geo="turkey", pricing=[])  # fmt: skip
        session.add(competitor)
        session.flush()
        segment = add_fact(session, run_id, "Rota Pro is sold to small carriers.",
                           [excerpt("Küçük nakliyeciler için.").id], stage="competitors",
                           meta={"competitor_id": competitor.id, "kind": "segment"})  # fmt: skip
        ids["segment"] = segment.id
        ids["gap"] = add_inference(
            session, run_id, "None of the 1 existing products reviewed fully covers: Driver app.",
            [segment.id], stage="competitors",
            meta={"problem_id": clusters[0].id, "dimension": "driver_app", "gap": True},
        ).id  # fmt: skip
        return ids


def _analysis(seen: list[dict]):
    def handle(prompt_input: str) -> BuyerAnalysis:
        payload = json.loads(prompt_input)
        seen.append(payload)
        n = {}
        for c in payload["claims"]:
            n.setdefault(c["about"], c["n"])
            if c.get("signal_type") == "labor_spend":
                n["job"] = c["n"]
        roles = _roles(
            None,
            user=_role("sevkiyat sorumlusu", [n["job"]]),
            buyer=_role("operasyon müdürü", [n["job"], 999]),
            decision_maker=_role("firma sahibi", [], "Owners approve purchases in small firms."),
            economic_beneficiary=_role("firma sahibi", [n["plan_hypothesis"]]),
        )
        channels = [
            Channel(kind="association", name="UND", claim_ids=[]),
            Channel(kind="community", name="Forum", claim_ids=[n["competitor_segment"]]),
        ]
        return BuyerAnalysis(opportunities=[
            _opp("Road freight firms with 5–50 trucks", roles, channels=channels,
                 gap_claim_ids=[n["gap"], n["job"]], breadth_hint="TÜİK road freight firms"),
            _opp("Road freight firms with 5-50 trucks", roles),
            _opp("Customs brokers", _roles(user=_role("gümrük müşaviri", [999]))),
            _opp("Freight forwarders", _roles(user=_role("operasyon uzmanı"))),
            _opp("Warehouse operators", _roles(_role("genel müdür", [], "The GM owns budget."))),
        ])  # fmt: skip

    return handle


def _counts(db) -> dict[str, int]:
    with db() as session:
        return {
            t.__name__: session.scalar(select(func.count()).select_from(t))
            for t in (Opportunity, Claim)
        }


def test_buyers_stage_writes_cited_opportunities(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed(db, run_id)
    seen: list[dict] = []
    llm = FakeLLM({BuyerAnalysis: _analysis(seen)})
    ctx = make_context(db, run_id, NoSearch(), llm)

    row = run_stage(ctx, Buyers())
    assert row.status == "completed", row.error

    # Claim table: one shortlisted problem; failed, excluded and other problems' facts left out.
    (payload,) = seen
    assert payload["target_customer"] == PLAN.request.target_customer
    statements = [c["statement"] for c in payload["claims"]]
    assert not {"Failed fact.", "Excluded by verify.", "Other problem's signal."} & set(statements)
    abouts = [c["about"] for c in payload["claims"]]
    assert abouts == ["problem", "signal", "signal", "gap", "competitor_segment",
                      "plan_hypothesis", "plan_hypothesis"]  # fmt: skip
    assert payload["claims"][1]["signal_type"] == "labor_spend"  # job ads first
    assert statements[-2:] == PLAN.buyer_hypotheses
    assert {c["kind"] for c in payload["claims"] if c["about"] == "plan_hypothesis"} == {
        "hypothesis"
    }

    m = row.metrics
    assert (m["problems"], m["opportunities"], m["budget_owners"]) == (1, 2, 1)
    assert m["dropped"] == {"duplicate_segment": 1, "unsupported_role": 2}
    # 999 is cited by the first opportunity, its duplicate and the customs one.
    assert (m["invalid_citations"], m["gap_citations_dropped"], m["llm_failures"]) == (3, 1, 0)
    assert m["per_problem"][0]["reason"] == "ok"

    with db() as session:
        opps = session.scalars(select(Opportunity).order_by(Opportunity.id)).all()
        claims = {c.id: c for c in session.scalars(select(Claim))}
        assert check_buyer_roles(session, run_id) == []
    road, warehouse = opps
    assert road.problem_id == ids["problem"] and road.status is None and road.knockouts == []
    r = road.buyer_roles
    assert r["budget_owner"] is None  # kept for Gate 2
    assert r["user"]["claim_ids"] == [ids["job_fact"]]
    assert r["buyer"]["claim_ids"] == [ids["job_fact"]]  # 999 dropped
    assert r["gap_claim_ids"] == [ids["gap"]]
    plan_claim = claims[r["economic_beneficiary"]["claim_ids"][0]]
    assert (plan_claim.kind, plan_claim.stage, plan_claim.meta) == (
        "hypothesis", "buyers", {"origin": "plan"},
    )  # fmt: skip
    hyp = claims[r["decision_maker"]["hypothesis_claim_id"]]
    assert hyp.kind == "hypothesis" and hyp.meta == {"opportunity_id": road.id,
                                                     "role": "decision_maker"}  # fmt: skip
    assert [(c["name"], c["cited"]) for c in road.accessibility["channels"]] == [
        ("UND", False), ("Forum", True),
    ]  # fmt: skip
    assert road.accessibility["channels"][1]["claim_ids"] == [ids["segment"]]
    assert road.market_breadth == {"hint": "TÜİK road freight firms", "status": "not_searched"}
    assert warehouse.buyer_roles["budget_owner"]["role"] == "genel müdür"
    assert claims[warehouse.buyer_roles["budget_owner"]["hypothesis_claim_id"]].kind == "hypothesis"

    # Re-running replaces the stage's outputs; the answer comes from the LLM cache.
    before, calls = _counts(db), len(llm.calls)
    assert run_stage(ctx, Buyers()).status == "completed"
    assert _counts(db) == before and len(llm.calls) == calls
    with db() as session:
        assert check_buyer_roles(session, run_id) == []


def test_exit_check_flags_a_bad_role(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    _seed(db, run_id)
    ctx = make_context(db, run_id, NoSearch(), FakeLLM({BuyerAnalysis: _analysis([])}))
    with db() as session:
        assert check_buyer_roles(session, run_id) == [
            f"run {run_id} has no opportunities; run buyers first"
        ]
    run_stage(ctx, Buyers())
    with db.begin() as session:
        opp = session.scalars(select(Opportunity).order_by(Opportunity.id)).first()
        roles = json.loads(json.dumps(opp.buyer_roles))
        roles["user"]["claim_ids"] = [999_999]
        roles["decision_maker"]["hypothesis_claim_id"] = None
        del roles["buyer"]
        opp.buyer_roles = roles
    with db() as session:
        errors = check_buyer_roles(session, run_id)
    assert len(errors) == 3
    assert any("user: cites claim 999999, which is not in this run" in e for e in errors)
    assert any("decision_maker: hypothesis not stored as a claim" in e for e in errors)
    assert any("buyer: missing" in e for e in errors)


def test_a_failed_call_leaves_one_problem_without_opportunities(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed(db, run_id)
    with db.begin() as session:
        session.get(ProblemCluster, ids["problem"] + 1).shortlisted = True

    def handle(prompt_input: str) -> BuyerAnalysis:
        if json.loads(prompt_input)["problem"]["name"] == "Customs paperwork":
            raise LLMError("output cut off")
        return _analysis([])(prompt_input)

    ctx = make_context(db, run_id, NoSearch(), FakeLLM({BuyerAnalysis: handle}))
    row = run_stage(ctx, Buyers())
    assert row.status == "completed", row.error
    assert (row.metrics["opportunities"], row.metrics["llm_failures"]) == (2, 1)
    assert [p["reason"] for p in row.metrics["per_problem"]] == ["ok", "llm_error"]


# --- role check -----------------------------------------------------------------------------


def test_a_failed_role_check_keeps_non_fact_citations_and_makes_a_hypothesis() -> None:
    kinds = {10: "fact", 12: "hypothesis"}
    (draft,) = validate_analysis(
        BuyerAnalysis(opportunities=[_opp("Carriers", _roles(buyer=_role("müdür", [1, 3])))]),
        TABLE,
        CFG,
    ).drafts
    rc = RoleCheck(draft, "buyer", "A müdür chooses and buys tools for this work: X.", [10])
    assert draft.basis["buyer"] == "fact"
    assert apply_role_verdict(rc, "not_supported", "names no buyer", kinds)
    role = draft.roles["buyer"]
    assert (role["claim_ids"], role["rejected_claim_ids"]) == ([12], [10])
    assert role["hypothesis"] == rc.statement and role["entailment"] == "not_supported"
    assert draft.basis["buyer"] == "hypothesis"
    # Supported and unchecked roles keep their citations.
    user = RoleCheck(draft, "user", "s", [10])
    draft.roles["user"] = {"role": "u", "claim_ids": [10], "hypothesis": None}
    assert not apply_role_verdict(user, "partial", "adds frequency", kinds)
    assert not apply_role_verdict(user, None, None, kinds)
    assert draft.roles["user"]["claim_ids"] == [10]


def _reject_buyers(prompt_input: str) -> EntailmentBatch:
    items = json.loads(prompt_input.split("# Claims\n", 1)[1])
    return EntailmentBatch(verdicts=[
        EntailmentVerdict(item=i["item"], note="n",
                          verdict="not_supported" if "chooses and buys" in i["claim"]
                          else "supported")
        for i in items
    ])  # fmt: skip


def test_buyers_stage_demotes_a_role_its_facts_do_not_state(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed(db, run_id)
    llm = FakeLLM({BuyerAnalysis: _analysis([]), EntailmentBatch: _reject_buyers})
    row = run_stage(make_context(db, run_id, NoSearch(), llm), Buyers())
    assert row.status == "completed", row.error
    # Only the road opportunity's user and buyer cite facts (the job ad); the buyer fails.
    check = row.metrics["role_check"]
    assert (check["checked"], check["supported"], check["not_supported"]) == (2, 1, 1)
    assert check["demoted"] == 1
    assert row.metrics["roles"]["fact"] == 1
    with db() as session:
        road = session.scalars(select(Opportunity).order_by(Opportunity.id)).first()
        claims = {c.id: c for c in session.scalars(select(Claim))}
        assert check_buyer_roles(session, run_id) == []
    buyer, user = road.buyer_roles["buyer"], road.buyer_roles["user"]
    assert (buyer["claim_ids"], buyer["rejected_claim_ids"]) == ([], [ids["job_fact"]])
    assert buyer["hypothesis"].startswith("A operasyon müdürü chooses and buys tools")
    assert claims[buyer["hypothesis_claim_id"]].kind == "hypothesis"
    assert (user["claim_ids"], user["entailment"]) == ([ids["job_fact"]], "supported")
    # The fact itself keeps its own entailment verdict: the role check is about the role.
    assert claims[ids["job_fact"]].entailment is None

    # The exit check catches a failed role that still cites its facts.
    with db.begin() as session:
        opp = session.get(Opportunity, road.id)
        roles = json.loads(json.dumps(opp.buyer_roles))
        roles["buyer"]["claim_ids"] = [ids["job_fact"]]
        opp.buyer_roles = roles
    with db() as session:
        assert any("buyer: still cites facts that failed the role check" in e
                   for e in check_buyer_roles(session, run_id))  # fmt: skip
