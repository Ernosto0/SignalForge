"""Monetization (M5): value ranges computed in code over dated assumption claims, pack references
over model values, WTP signals, Gate 2, and the economic-model exit check."""

import json
from datetime import date

import pytest
from fakes import PACK, PLAN, FakeLLM, make_context
from sqlalchemy import func, select
from test_buyer_research import NoSearch, _seed

from signalforge.config import get_defaults
from signalforge.db.models import Claim, Competitor, Opportunity
from signalforge.domain.commercial import AssumptionDraft, ValueModelDraft
from signalforge.evidence.claims import add_fact
from signalforge.evidence.opportunities import check_economic_models
from signalforge.packs import MarketPack
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.buyers import SIGNAL, TableRow
from signalforge.pipeline.stages.monetization import (
    PRICE,
    Economics,
    Monetization,
    Target,
    knockouts,
    validate_model,
)
from signalforge.providers.llm import LLMError
from signalforge.scoring.economics import Interval, monthly_usd, value_local

DEFAULTS = get_defaults()
CFG = DEFAULTS.monetization
ECON = Economics.load(PACK, CFG, date(2026, 10, 10))
RATE = PACK.economic("usd_try").value
WAGE = "wage_net_monthly_ihracat_operasyon_uzmani"
TABLE = [
    TableRow(10, "fact", SIGNAL, "An exporter hires a clerk to prepare bills of lading.",
             "labor_spend", "ihracatçı"),
    TableRow(11, "fact", PRICE, "Rota Pro costs 1,000 TRY per month."),
]  # fmt: skip


def _a(name: str, low: float, high: float, unit: str, **kw) -> AssumptionDraft:
    return AssumptionDraft(name=name, low=low, high=high, unit=unit,
                           claim_ids=kw.pop("claim_ids", []), rationale="r", **kw)  # fmt: skip


def _labor(**hourly) -> ValueModelDraft:
    cost = hourly or {"low": 300, "high": 500, "unit": "TRY/hour", "currency": "TRY"}
    return ValueModelDraft(formula="labor_savings", assumptions=[
        _a("hours_saved_per_month", 20, 40, "hour/month", claim_ids=[1]),
        _a("loaded_hourly_cost", **cost),
    ])  # fmt: skip


# --- arithmetic -----------------------------------------------------------------------------


def test_interval_arithmetic_for_each_formula() -> None:
    i = Interval
    cases = [
        ("labor_savings", {"hours_saved_per_month": i(10, 20), "loaded_hourly_cost": i(300, 400)},
         i(3000, 8000)),
        ("error_cost_avoided", {"errors_per_month": i(2, 4), "cost_per_error": i(500, 1000),
                                "share_avoidable": i(0.5, 0.8)}, i(500, 3200)),
        ("revenue_recovered", {"lost_revenue_per_month": i(10_000, 20_000),
                               "share_recoverable": i(0.1, 0.2)}, i(1000, 4000)),
        ("compliance_cost", {"penalty_or_outsourcing_cost_per_month": i(5000, 6000),
                             "share_replaceable": i(0.5, 1)}, i(2500, 6000)),
    ]  # fmt: skip
    for formula, inputs, expected in cases:
        assert value_local(formula, inputs) == expected
    with pytest.raises(ValueError):
        Interval(5, 1)


def test_competitor_prices_convert_to_usd_per_month() -> None:
    assert monthly_usd(1200, "TRY", "year", "TRY", 40.0) == pytest.approx(2.5)
    assert monthly_usd(30, "USD", "per_user_month", "TRY", 40.0) == 30
    assert monthly_usd(2172, "TRY", "one_time", "TRY", 40.0) is None
    assert monthly_usd(10, "EUR", "month", "TRY", 40.0) is None


# --- validation -----------------------------------------------------------------------------


def test_usd_value_is_converted_at_the_dated_rate_and_recorded() -> None:
    checked = validate_model(_labor(low=6, high=10, unit="USD/hour", currency="USD"), TABLE, ECON)
    assert checked.errors == []
    m = checked.model
    hourly = m.assumptions[1]
    assert (hourly.unit, hourly.currency) == ("TRY/hour", "TRY")
    assert hourly.value == Interval(6 * RATE, 10 * RATE)
    fx_url = PACK.economic("usd_try").source_url
    assert hourly.derivation["converted"] == {
        "from": "USD", "rate": RATE, "as_of": "2026-10-09", "source_url": fx_url,
    }  # fmt: skip
    assert m.value_usd.low == pytest.approx(20 * 6) and m.value_usd.high == pytest.approx(40 * 10)
    assert m.ceiling_usd == m.value_usd * Interval(0.10, 0.30)


def test_a_pack_reference_overrides_the_model_value() -> None:
    draft = _labor(low=1, high=2, unit="TRY/hour", currency="TRY", pack_reference=WAGE)
    hourly = validate_model(draft, TABLE, ECON).model.assumptions[1]
    wage = PACK.economic(WAGE).value
    hours = PACK.economic("working_hours_per_month").value
    assert hourly.value.low == pytest.approx(wage * 1.43 / hours)
    assert hourly.value.high == pytest.approx(wage * 1.65 / hours)
    assert (hourly.sourced, hourly.pack_reference) == (True, WAGE)
    assert hourly.source_url == PACK.economic(WAGE).source_url
    assert hourly.derivation["hours_per_month"] == "working_hours_per_month"
    # An unknown reference falls back to the model's own range.
    unknown = _labor(low=300, high=500, unit="TRY/hour", currency="TRY", pack_reference="nope")
    checked = validate_model(unknown, TABLE, ECON)
    assert checked.model.assumptions[1].value == Interval(300, 500)
    assert checked.notes["unknown_pack_reference"] == 1


def test_unsourced_inputs_are_labelled_and_bad_inputs_invalidate() -> None:
    m = validate_model(_labor(), TABLE, ECON).model
    hours, hourly = m.assumptions
    # A model estimate is unsourced even when it cites claims; the citations stay as context.
    assert (hours.sourced, hours.claim_ids, hours.as_of) == (False, [10], None)
    assert (hourly.sourced, hourly.source_url, hourly.as_of) == (False, None, "2026-10-10")

    bad = ValueModelDraft(formula="error_cost_avoided", assumptions=[
        _a("errors_per_month", 5, 2, "errors/month"),
        _a("cost_per_error", 100, 200, "EUR/error", currency="EUR"),
        _a("share_avoidable", 0.5, 1.5, "fraction"),
    ])  # fmt: skip
    checked = validate_model(bad, TABLE, ECON)
    assert checked.model is None and len(checked.errors) == 3
    missing = ValueModelDraft(formula="labor_savings", assumptions=[
        _a("hours_saved_per_month", 1, 2, "hour/month"), _a("extra", 1, 2, "x")])  # fmt: skip
    checked = validate_model(missing, TABLE, ECON)
    assert checked.errors == ["loaded_hourly_cost: missing"]
    assert checked.notes["extra_assumptions"] == 1


def test_both_gate_2_rules_fire() -> None:
    target = Target(1, 1, "s", "a", {}, has_budget_owner=False)
    model = validate_model(_labor(), TABLE, ECON).model  # ceiling high = 40 × 500 × 0.3 / RATE
    rules = [k["rule"] for k in knockouts(target, model, model.ceiling_usd.high + 1)]
    assert rules == ["no_budget_owner", "value_below_minimum"]
    owner = Target(1, 1, "s", "a", {}, has_budget_owner=True)
    assert knockouts(owner, model, model.ceiling_usd.high - 1) == []
    assert knockouts(owner, None, 10_000) == []  # no model: only the budget-owner rule


def test_a_missing_usd_try_entry_fails_loudly() -> None:
    pack = MarketPack.model_validate(
        PACK.model_dump() | {"economics": [r for r in PACK.economics if r.name != "usd_try"]}
    )
    with pytest.raises(KeyError, match="usd_try"):
        Economics.load(pack, CFG, date(2026, 10, 10))


# --- stage ----------------------------------------------------------------------------------


def _seed_opportunities(db, run_id: int) -> dict[str, int]:
    ids = _seed(db, run_id)
    with db.begin() as session:
        competitor = session.scalars(select(Competitor)).one()
        excerpt_id = session.get(Claim, ids["segment"]).supports[0]
        price = add_fact(session, run_id, "Rota Pro costs 1,200 TRY per year.", [excerpt_id],
                         stage="competitors",
                         meta={"competitor_id": competitor.id, "kind": "price"})  # fmt: skip
        competitor.pricing = [
            {"amount": 1200, "currency": "TRY", "period": "year", "claim_id": price.id},
            {"amount": 5000, "currency": "TRY", "period": "one_time", "claim_id": price.id},
        ]
        ids["price"] = price.id
        role = {"role": "firma sahibi", "claim_ids": [], "hypothesis": "h"}
        for segment, owner in (("Road freight firms", role), ("Carriers without owner", None)):
            roles = {"user": role, "budget_owner": owner}
            o = Opportunity(run_id=run_id, problem_id=ids["problem"], segment=segment,
                            solution_angle="a", buyer_roles=roles)  # fmt: skip
            session.add(o)
            session.flush()
            ids[segment] = o.id
    return ids


def _answer(seen: list[dict]):
    def handle(prompt_input: str) -> ValueModelDraft:
        payload = json.loads(prompt_input)
        seen.append(payload)
        job = next(c["n"] for c in payload["claims"] if c.get("signal_type") == "labor_spend")
        return ValueModelDraft(formula="labor_savings", assumptions=[
            _a("hours_saved_per_month", 20, 40, "hour/month", claim_ids=[job, 999]),
            _a("loaded_hourly_cost", 1, 2, "TRY/hour", currency="TRY", pack_reference=WAGE),
        ])  # fmt: skip

    return handle


def test_monetization_stage_writes_models_wtp_and_gate_2(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed_opportunities(db, run_id)
    seen: list[dict] = []
    llm = FakeLLM({ValueModelDraft: _answer(seen)})
    ctx = make_context(db, run_id, NoSearch(), llm)

    row = run_stage(ctx, Monetization())
    assert row.status == "completed", row.error
    assert len(seen) == 2
    abouts = [c["about"] for c in seen[0]["claims"]]
    assert abouts[0] == "signal" and seen[0]["claims"][0]["signal_type"] == "labor_spend"
    assert abouts.count("competitor_price") == 2  # one row per pricing entry
    assert "usd_try" not in {r["name"] for r in seen[0]["pack_references"]}
    assert seen[0]["opportunity"]["roles"]["user"] == "firma sahibi"

    m = row.metrics
    assert (m["opportunities"], m["passed"], m["knocked_out"]) == (2, 1, 1)
    assert m["models"] == {"ok": 2} and m["sourced_share"] == 0.5  # pack wage only
    assert (m["pack_referenced"], m["with_price_anchor"], m["invalid_citations"]) == (2, 2, 2)

    with db() as session:
        road = session.get(Opportunity, ids["Road freight firms"])
        no_owner = session.get(Opportunity, ids["Carriers without owner"])
        claims = {c.id: c for c in session.scalars(select(Claim))}
        assert check_economic_models(session, run_id) == []
    em = road.economic_model
    assert em["status"] == "ok" and em["currency"] == "TRY"
    assert em["fx"] == {"rate": RATE, "as_of": "2026-10-09",
                        "source_url": PACK.economic("usd_try").source_url}  # fmt: skip
    hourly = claims[em["inputs"]["loaded_hourly_cost"]]
    assert (hourly.kind, hourly.stage, hourly.meta["pack_reference"]) == (
        "assumption", "monetization", WAGE)  # fmt: skip
    assert claims[em["inputs"]["hours_saved_per_month"]].derived_from == [ids["job_fact"]]
    assert em["competitor_anchor_usd_month"]["n"] == 1  # the one-time price does not convert
    assert em["competitor_anchor_usd_month"]["min"] == round(100 / RATE, 2)
    kinds = [w["kind"] for w in road.wtp_signals]
    assert kinds.count("labor_spend") == 1 and kinds.count("competitor_price") == 2
    # Ceiling high = 40 h × (wage × 1.65 / 195 h) × 30 % in USD: above the founder's minimum.
    wage, hours = PACK.economic(WAGE).value, PACK.economic("working_hours_per_month").value
    ceiling = 40 * wage * 1.65 / hours * 0.30 / RATE
    assert em["price_ceiling_usd_month"][1] == pytest.approx(ceiling, abs=0.01)
    assert ceiling > PLAN.request.founder.min_customer_value_usd_month
    assert (road.status, road.knockouts) == ("passed", [])
    assert "no_budget_owner" in [k["rule"] for k in no_owner.knockouts]
    assert no_owner.status == "knocked_out"

    # Re-running replaces the stage's outputs; answers come from the LLM cache.
    with db() as session:
        before = session.scalar(select(func.count()).select_from(Claim))
    calls = len(llm.calls)
    assert run_stage(ctx, Monetization()).status == "completed"
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Claim)) == before
        assert check_economic_models(session, run_id) == []
    assert len(llm.calls) == calls


def test_a_failed_call_still_runs_gate_2_and_the_exit_check_flags_bad_models(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed_opportunities(db, run_id)

    def handle(prompt_input: str) -> ValueModelDraft:
        if json.loads(prompt_input)["opportunity"]["segment"] == "Carriers without owner":
            raise LLMError("output cut off")
        return _answer([])(prompt_input)

    ctx = make_context(db, run_id, NoSearch(), FakeLLM({ValueModelDraft: handle}))
    row = run_stage(ctx, Monetization())
    assert row.status == "completed", row.error
    assert row.metrics["models"] == {"ok": 1, "llm_error": 1}
    with db.begin() as session:
        failed_opp = session.get(Opportunity, ids["Carriers without owner"])
        assert failed_opp.economic_model == {"status": "llm_error"}
        assert [k["rule"] for k in failed_opp.knockouts] == ["no_budget_owner"]
        road = session.get(Opportunity, ids["Road freight firms"])
        claim = session.get(Claim, road.economic_model["inputs"]["hours_saved_per_month"])
        claim.meta = {**claim.meta, "sourced": True}
        claim.derived_from = []
        road.economic_model = {**road.economic_model, "fx": {"rate": RATE}}
        failed_opp.status = None
    with db() as session:
        errors = check_economic_models(session, run_id)
    assert len(errors) == 3
    assert any("hours_saved_per_month: labelled sourced without a source" in e for e in errors)
    assert any("exchange rate without a date or source" in e for e in errors)
    assert any("not judged by Gate 2" in e for e in errors)
