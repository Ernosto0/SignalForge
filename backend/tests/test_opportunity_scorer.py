"""Opportunity scorer (M5): rubric factors with caps, code factors and the WTP floor, the median of
k judges, confidence, founder fit, rule-based categories, the validation experiment, and the
score-card exit check."""

import json

import pytest
from fakes import PACK, PLAN, FakeLLM, make_context
from sqlalchemy import func, select
from test_buyer_research import NoSearch
from test_monetization import _answer, _seed_opportunities

from signalforge.config import get_defaults
from signalforge.db.models import Excerpt, Opportunity, ProblemCluster, ScoreCard, Signal
from signalforge.domain.commercial import ValueModelDraft
from signalforge.domain.scoring import FactorJudgment, FeasibilityJudgment, RubricJudgments
from signalforge.evidence.claims import add_fact
from signalforge.evidence.opportunities import check_score_cards
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.monetization import Monetization
from signalforge.pipeline.stages.score import Score
from signalforge.providers.llm import LLMError
from signalforge.scoring.categories import ORDER, CategoryInputs, categorize
from signalforge.scoring.experiments import (
    load_catalogue,
    pick_experiment,
    rank_claims,
    uncertainty_key,
)
from signalforge.scoring.scorer import (
    Cited,
    attractiveness,
    code_factors,
    confidence,
    economic_impact,
    founder_fit,
    hypothesis_share,
    merge_judges,
    validate_judgments,
)

DEFAULTS = get_defaults()
CFG = DEFAULTS.score
FOUNDER = {"team": "solo developer", "mvp_months": 3, "min_customer_value_usd_month": 50.0}
TABLE = [
    Cited(10, "fact", "supported"),
    Cited(11, "inference"),
    Cited(12, "hypothesis"),
    Cited(13, "fact", "partial"),
]
MODEL_OK = {"status": "ok", "value_usd_month": [150.0, 470.0], "inputs": {"hours": 20, "wage": 21}}
BREADTH = {"hint": "TÜİK counts", "status": "not_searched"}


def _j(factor: str, level: int, ids: list[int]) -> FactorJudgment:
    return FactorJudgment(factor=factor, level=level, justification="j", claim_ids=ids)


def _rubric(**levels: tuple[int, list[int]]) -> RubricJudgments:
    base = {"severity": (4, [1]), "frequency": (4, [1]), "competition_gap": (3, [4]),
            "willingness_to_pay": (3, [1]), "customer_accessibility": (3, [4])}  # fmt: skip
    return RubricJudgments(judgments=[_j(f, *v) for f, v in (base | levels).items()])


def _code(has_gaps: bool = True, floor: dict | None = None, model: dict = MODEL_OK):
    return code_factors(model, BREADTH, has_gaps, floor or {}, CFG)


# --- factors --------------------------------------------------------------------------------


def test_an_uncited_factor_is_capped_at_2_and_flagged() -> None:
    rubric = _rubric(severity=(5, [2, 3]), frequency=(4, [99]))  # inference + hypothesis; bad id
    judge = validate_judgments(rubric, TABLE, _code(), CFG)
    sev = judge.factors["severity"]
    assert (sev.level, sev.capped, sev.claim_ids) == (2, True, [11, 12])
    assert (judge.factors["frequency"].level, judge.notes["invalid_citations"]) == (2, 1)
    assert {"rule": "uncited_cap", "factor": "severity", "fired": True,
            "inputs": {"from": 5, "to": 2}} in judge.events  # fmt: skip
    assert judge.factors["willingness_to_pay"].capped is False


def test_competition_gap_is_capped_without_a_gap_claim() -> None:
    judge = validate_judgments(_rubric(competition_gap=(4, [1])), TABLE, _code(False), CFG)
    assert judge.factors["competition_gap"].level == 2
    assert [e["rule"] for e in judge.events] == ["no_gap_cap"]


def test_code_factors_override_the_model() -> None:
    rubric = RubricJudgments(judgments=[*_rubric().judgments, _j("economic_impact", 5, [1]),
                                        _j("market_breadth", 5, [1])])  # fmt: skip
    judge = validate_judgments(rubric, TABLE, _code(), CFG)
    econ, breadth = judge.factors["economic_impact"], judge.factors["market_breadth"]
    assert (econ.level, econ.source, econ.claim_ids) == (4, "code", [20, 21])  # midpoint 310
    assert (breadth.level, breadth.source) == (2, "code")
    assert judge.notes["code_factor_judged"] == 2


def test_economic_impact_levels() -> None:
    def level(low: float, high: float) -> int:
        return economic_impact({"status": "ok", "value_usd_month": [low, high]}, CFG)[0]

    assert [level(0, 40), level(20, 30), level(70, 80), level(190, 212), level(400, 700)] == [
        1, 2, 3, 4, 5,
    ]  # fmt: skip
    lvl, _, entry = economic_impact({"status": "invalid", "errors": ["x"]}, CFG)
    assert lvl == 1 and entry["note"] == "no valid economic model"


def test_a_missing_factor_is_level_1_and_traced() -> None:
    rubric = RubricJudgments(judgments=[j for j in _rubric().judgments if j.factor != "frequency"])
    judge = validate_judgments(rubric, TABLE, _code(), CFG)
    assert judge.factors["frequency"].level == 1 and judge.factors["frequency"].missing
    assert judge.events[-1]["rule"] == "missing_factor"


def test_the_wtp_floor_needs_distinct_competitors() -> None:
    low_wtp = _rubric(willingness_to_pay=(2, [2]))
    two = _code(floor={"Rota": [30], "Yol": [31]})
    wtp = validate_judgments(low_wtp, TABLE, two, CFG).factors["willingness_to_pay"]
    assert (wtp.level, wtp.floor, wtp.claim_ids) == (3, True, [11, 30, 31])
    assert two.trace[-1]["fired"] is True
    one = _code(floor={"Select Optimus": [40, 41, 42, 43, 44, 45]})  # 6 rows, one competitor
    wtp = validate_judgments(low_wtp, TABLE, one, CFG).factors["willingness_to_pay"]
    assert (wtp.level, wtp.floor) == (2, False)


def test_median_of_three_judges_and_the_spread() -> None:
    code = _code()
    judges = [
        validate_judgments(_rubric(severity=(lvl, [1])), TABLE, code, CFG) for lvl in (5, 3, 4)
    ]
    merged = merge_judges(judges, code, CFG)
    sev = merged.factors["severity"]
    assert (sev["level"], sev["judges"], sev["spread"]) == (4, [5, 3, 4], 2)
    assert (merged.max_spread, merged.judges) == (2, 3)
    assert merged.attractiveness == attractiveness(merged.levels, CFG.weights)
    # Caps apply per judge before the median: two uncited 5s become 2s.
    judges = [
        validate_judgments(_rubric(severity=(5, ids)), TABLE, code, CFG) for ids in ([3], [3], [1])
    ]
    merged = merge_judges(judges, code, CFG)
    assert merged.factors["severity"]["level"] == 2
    assert sum(e["rule"] == "uncited_cap" for e in merged.trace) == 2
    single = merge_judges(judges[:1], code, CFG)
    assert single.max_spread is None


def test_attractiveness_is_the_weighted_level_sum() -> None:
    assert attractiveness({f: 5 for f in CFG.weights}, CFG.weights) == 100
    assert attractiveness({f: 1 for f in CFG.weights}, CFG.weights) == 0
    assert attractiveness({f: 3 for f in CFG.weights}, CFG.weights) == 50


# --- confidence and founder fit -------------------------------------------------------------


def test_hypothesis_share_and_confidence() -> None:
    claims = {c.id: c for c in TABLE} | {
        20: Cited(20, "assumption", sourced=False),
        21: Cited(21, "assumption", sourced=True),
    }
    code = _code()
    merged = merge_judges([validate_judgments(_rubric(), TABLE, code, CFG)], code, CFG)
    share, by_factor, _ = hypothesis_share(merged.factors, claims, CFG.weights, {13})
    # market_breadth 10 + economic_impact 20 × ½ (one of two assumptions unsourced).
    assert by_factor["economic_impact"] == 0.5 and by_factor["market_breadth"] == 1.0
    assert share == 0.2
    assert confidence(8, 0.2, by_factor, 1, 3, CFG)[0] == "high"
    assert confidence(8, 0.2, by_factor, None, 1, CFG)[0] == "medium"  # one judge: no spread
    assert confidence(6.8, 0.3, by_factor, 0, 3, CFG)[0] == "medium"
    result, entry = confidence(4.9, 0.2, by_factor, 0, 3, CFG)
    assert result == "low" and entry["reasons"] == ["strength 4.9 < 5.0"]
    assert confidence(8, 0.6, by_factor, 0, 3, CFG)[0] == "low"
    assert confidence(8, 0.2, by_factor, 3, 3, CFG)[0] == "low"


def test_accessibility_is_fact_backed_only_through_a_buyer_role_fact() -> None:
    """A competitor segment fact still lifts the uncited cap, but in the hypothesis share only a
    fact a buyer role cites shows these buyers are reachable."""
    claims = {c.id: c for c in TABLE}
    code = _code()
    segment_only = _rubric(customer_accessibility=(3, [1]))  # claim 10: a fact, not a role's
    merged = merge_judges([validate_judgments(segment_only, TABLE, code, CFG)], code, CFG)
    assert merged.levels["customer_accessibility"] == 3  # not capped
    _, by_factor, notes = hypothesis_share(merged.factors, claims, CFG.weights, role_fact_ids={13})
    assert by_factor["customer_accessibility"] == 1.0 and "customer_accessibility" in notes
    _, entry = confidence(8, 0.3, by_factor, 0, 3, CFG, notes)
    assert entry["inputs"]["hypothesis_share_notes"] == notes
    _, by_factor, notes = hypothesis_share(merged.factors, claims, CFG.weights, role_fact_ids={10})
    assert by_factor["customer_accessibility"] == 0.0 and notes == {}


def _feasible(**kw) -> FeasibilityJudgment:
    base = {"mvp_feasible": "yes", "hard_barriers": [], "barrier_claim_ids": [],
            "sales_motion": "inside_sales", "justification": "j"}  # fmt: skip
    return FeasibilityJudgment(**(base | kw))


def test_founder_fit_rules() -> None:
    licence = _feasible(hard_barriers=["GİB özel entegratör licence"], barrier_claim_ids=[1])
    result, entry = founder_fit(licence, [10], 140.0, FOUNDER, CFG)
    assert result == "not_fit" and entry["inputs"]["barrier_fact_ids"] == [10]
    assert founder_fit(licence, [], 140.0, FOUNDER, CFG)[0] == "fit"  # hypothesis-only barrier
    assert founder_fit(_feasible(), [], 40.0, FOUNDER, CFG)[0] == "not_fit"  # below minimum
    assert founder_fit(_feasible(sales_motion="field_sales"), [], 140.0, FOUNDER, CFG)[0] == (
        "stretch"
    )
    result, entry = founder_fit(_feasible(mvp_feasible="no"), [], 140.0, FOUNDER, CFG)
    assert result == "stretch" and entry["inputs"]["mvp_feasible"] == "no"
    result, entry = founder_fit(None, [], 140.0, FOUNDER, CFG)
    assert result == "stretch" and entry["reasons"] == ["feasibility_unavailable"]
    assert founder_fit(_feasible(), [], 140.0, FOUNDER, CFG)[0] == "fit"


# --- categories -----------------------------------------------------------------------------


def _inputs(**kw) -> CategoryInputs:
    base = {"supported_facts": 5, "budget_owner": "evidence", "price_ceiling_high": 140.0,
            "min_customer_value": 50.0, "severity": 4, "competition_gap": 3,
            "willingness_to_pay": 3, "attractiveness": 60.0, "strength": 7.5}  # fmt: skip
    return CategoryInputs(**(base | kw))


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"supported_facts": 2}, "false_positive"),
        ({"budget_owner": "none"}, "weak"),
        ({"price_ceiling_high": 40.0}, "weak"),
        ({"severity": 2}, "weak"),
        ({"competition_gap": 2, "willingness_to_pay": 4}, "competitive"),
        ({"attractiveness": 80.0}, "strong"),
        ({"attractiveness": 80.0, "budget_owner": "hypothesis"}, "interesting"),
        ({"attractiveness": 80.0, "strength": 6.9}, "interesting"),
        ({}, "interesting"),
    ],
)
def test_each_category_rule_fires_and_every_rule_is_traced(change: dict, expected: str) -> None:
    category, trace = categorize(_inputs(**change), CFG)
    assert category == expected
    assert [e["rule"] for e in trace] == [f"category.{n}" for n in ORDER]
    assert next(e["rule"] for e in trace if e["fired"]) == f"category.{expected}"
    assert all("inputs" in e for e in trace)


# --- experiment -----------------------------------------------------------------------------


def test_uncertainty_keys() -> None:
    assert uncertainty_key("assumption", None, False) == "assumption_unsourced"
    assert uncertainty_key("assumption", None, True) == "assumption_sourced"
    assert uncertainty_key("fact", "supported", None) == "fact_supported"
    assert uncertainty_key("fact", "partial", None) == "fact_partial"
    assert uncertainty_key("fact", None, None) == "fact_unchecked"
    assert uncertainty_key("hypothesis", None, None) == "hypothesis"


def test_the_experiment_tests_the_max_importance_times_uncertainty_claim() -> None:
    cited = {
        "severity": [10, 13],  # supported fact 20+10 = 30 × 0.1 = 3; partial fact 30 × 0.3 = 9
        "frequency": [10, 13],
        "economic_impact": [20, 21],  # unsourced 20 × 0.9 = 18; sourced 20 × 0.5 = 10
        "customer_accessibility": [12],  # hypothesis 10 × 1.0 = 10
    }
    keys = {10: "fact_supported", 13: "fact_partial", 20: "assumption_unsourced",
            21: "assumption_sourced", 12: "hypothesis"}  # fmt: skip
    ranked = rank_claims(cited, keys, CFG.weights, CFG.uncertainty)
    assert [(c.claim_id, c.score) for c in ranked][:3] == [(20, 18.0), (21, 10.0), (12, 10.0)]
    fields = {i: {"segment": "SMB forwarders", "statement": f"claim {i}", "price_usd": 15}
              for i in keys}  # fmt: skip
    experiment = pick_experiment(ranked, load_catalogue(), fields)
    assert experiment["claim_id"] == 20 and experiment["factor"] == "economic_impact"
    assert experiment["name"] == "interviews"  # cheapest that tests economic_impact
    assert experiment["pass_fail"] == "≥ 6 of 10 interviewed SMB forwarders confirm it: claim 20"
    wtp = rank_claims({"willingness_to_pay": [12]}, keys, CFG.weights, CFG.uncertainty)
    assert pick_experiment(wtp, load_catalogue(), fields)["name"] == "outreach"


# --- stage ----------------------------------------------------------------------------------


def _seed_scoring(db, run_id: int) -> dict[str, int]:
    """Two opportunities after monetization: road freight passed (it targets the gap), the one
    without a budget owner knocked out. A third counted signal, so Gate-1 facts suffice."""
    ids = _seed_opportunities(db, run_id)
    with db.begin() as session:
        cluster = session.get(ProblemCluster, ids["problem"])
        doc_id = session.get(Excerpt, session.get(Signal, ids["pain"]).excerpt_id).document_id
        e = Excerpt(run_id=run_id, document_id=doc_id, quote="Alıntı pain2.", translation="t",
                    verified="exact", char_start=0, char_end=1)  # fmt: skip
        session.add(e)
        session.flush()
        s = Signal(run_id=run_id, excerpt_id=e.id, type="complaint", actor="nakliyeci",
                   workflow="delivery", statement="Drivers send photos late.",
                   first_hand=True)  # fmt: skip
        session.add(s)
        session.flush()
        add_fact(session, run_id, s.statement, [e.id], stage="extract")
        cluster.signal_ids = [*cluster.signal_ids, s.id]
        road = session.get(Opportunity, ids["Road freight firms"])
        road.buyer_roles = {**road.buyer_roles, "gap_claim_ids": [ids["gap"]]}
    ctx = make_context(db, run_id, NoSearch(), FakeLLM({ValueModelDraft: _answer([])}))
    assert run_stage(ctx, Monetization()).status == "completed"
    return ids


def _rubric_answer(seen: list[dict]):
    def handle(prompt_input: str) -> RubricJudgments:
        payload = json.loads(prompt_input)
        seen.append(payload)
        n: dict[str, int] = {}
        for c in payload["claims"]:
            n.setdefault(c["about"], c["n"])
        severity = (4, 5, 3)[payload["judge_index"]]
        return RubricJudgments(judgments=[
            _j("severity", severity, [n["signal"]]),
            _j("frequency", 4, [n["signal"]]),
            _j("competition_gap", 3, [n["gap"], n["gap_evidence"]]),
            _j("willingness_to_pay", 3, [n["competitor_price"]]),
            _j("customer_accessibility", 3, [n["problem"]]),  # an inference only: capped
        ])  # fmt: skip

    return handle


def _llm(rubric=None, fit=None) -> FakeLLM:
    return FakeLLM({RubricJudgments: rubric or _rubric_answer([]),
                    FeasibilityJudgment: fit or (lambda _: _feasible())})  # fmt: skip


def _card(session, opportunity_id: int) -> ScoreCard:
    return session.scalars(
        select(ScoreCard).where(ScoreCard.opportunity_id == opportunity_id)
    ).one()


def test_score_stage_writes_explainable_cards(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed_scoring(db, run_id)
    seen: list[dict] = []
    llm = _llm(_rubric_answer(seen))
    ctx = make_context(db, run_id, NoSearch(), llm)

    row = run_stage(ctx, Score())
    assert row.status == "completed", row.error
    assert sorted(p["judge_index"] for p in seen) == [0, 1, 2]  # k = 3 judges for the top one
    abouts = {c["about"] for c in seen[0]["claims"]}
    assert {"gap", "gap_evidence", "competitor_price", "assumption"} <= abouts
    competitor_rows = [c for c in seen[0]["claims"] if c["about"] in ("competitor_price",
                                                                      "gap_evidence")]  # fmt: skip
    assert competitor_rows and {c["competitor"] for c in competitor_rows} == {"Rota Pro"}
    assert "Failed fact." not in {c["statement"] for c in seen[0]["claims"]}
    m = row.metrics
    assert (m["cards"], m["scored"], m["knocked_out"], m["judge_calls"]) == (2, 1, 1, 3)
    assert m["by_category"] == {"interesting": 1, "weak": 1}
    assert m["capped"] == {"customer_accessibility": 1} and m["entailment"]["checked"] > 0
    assert m["unchecked_facts"] == 0

    with db() as session:
        road = _card(session, ids["Road freight firms"])
        out = _card(session, ids["Carriers without owner"])
        assert check_score_cards(session, run_id) == []
    f = road.factors
    assert (f["severity"]["level"], f["severity"]["judges"], f["severity"]["spread"]) == (
        4, [4, 5, 3], 2,
    )  # fmt: skip
    assert f["customer_accessibility"]["level"] == 2 and f["customer_accessibility"]["capped"]
    assert f["economic_impact"]["source"] == "code" and f["market_breadth"]["level"] == 2
    assert road.confidence == "low"  # the fixture cluster has no evidence strength
    assert road.founder_fit == "fit" and road.category == "interesting"
    assert road.experiment["claim_id"] in {i for x in f.values() for i in x["claim_ids"]}
    # The unsourced hours assumption (20 × 0.9) wins; its rationale stays out of the template.
    assert road.experiment["factor"] == "economic_impact"
    assert road.experiment["pass_fail"].endswith("hours_saved_per_month = 20–40 hour/month")
    rules = {e["rule"] for e in road.rule_trace}
    assert {"factor.economic_impact", "factor.market_breadth", "wtp_floor", "confidence",
            "founder_fit", "category.strong"} <= rules  # fmt: skip
    assert (out.category, out.factors, out.attractiveness, out.experiment) == (
        "weak", {}, None, None,
    )  # fmt: skip
    assert [e["rule"] for e in out.rule_trace] == ["gate2.no_budget_owner"]

    # Re-running replaces the cards and the stage's entailment verdicts; answers are cached.
    calls = len(llm.calls)
    assert run_stage(ctx, Score()).status == "completed"
    with db() as session:
        assert session.scalar(select(func.count()).select_from(ScoreCard)) == 2
        assert check_score_cards(session, run_id) == []
    assert len(llm.calls) == calls


def test_failed_calls_and_the_exit_check(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed_scoring(db, run_id)

    def fail(_: str):
        raise LLMError("output cut off")

    row = run_stage(make_context(db, run_id, NoSearch(), _llm(fail, fail)), Score())
    assert row.status == "completed", row.error
    assert row.metrics["llm_failures"] == {"rubric": 1, "extra_judges": 0, "feasibility": 1}
    assert row.metrics["cards"] == 1
    with db() as session:
        errors = check_score_cards(session, run_id)
    assert errors == [
        f"opportunity {ids['Road freight firms']} (Road freight firms): no score card"
    ]

    row = run_stage(make_context(db, run_id, NoSearch(), _llm(fit=fail)), Score())
    assert row.status == "completed", row.error
    with db.begin() as session:
        road = _card(session, ids["Road freight firms"])
        assert road.founder_fit == "stretch"
        assert check_score_cards(session, run_id) == []
        # Tamper: an uncited level above the cap (attractiveness no longer adds up), a category
        # its trace doesn't give, an experiment claim nobody cites, a knocked-out card's score.
        factors = dict(road.factors)
        factors["customer_accessibility"] = {**factors["customer_accessibility"], "level": 4,
                                             "capped": False}  # fmt: skip
        road.factors = factors
        road.category = "strong"
        road.experiment = {**road.experiment, "claim_id": 10**9}
        _card(session, ids["Carriers without owner"]).attractiveness = 50.0
    with db() as session:
        errors = check_score_cards(session, run_id)
    assert len(errors) == 5, errors
    assert any("customer_accessibility: level 4 above the uncited cap" in e for e in errors)
    assert any("attractiveness" in e and "is not" in e for e in errors)
    assert any("category 'strong' is not the first fired rule" in e for e in errors)
    assert any("experiment claim" in e for e in errors)
    assert any("knocked out but has attractiveness" in e for e in errors)
