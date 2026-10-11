"""Regression tests for known research-quality failure modes (docs/eval/audit.md §5). Each test
names the failure it guards against; the ones already pinned elsewhere are listed in the audit."""

from fakes import PACK, PLAN, FakeLLM, make_context
from test_buyer_research import NoSearch
from test_opportunity_scorer import CFG, DEFAULTS, _code, _j, _seed_scoring
from test_report_writer import check, rules

from signalforge.db.models import Claim, Competitor
from signalforge.domain.scoring import RubricJudgments
from signalforge.evidence.claims import add_fact
from signalforge.evidence.quotes import verify_quote
from signalforge.pipeline.runner import create_run
from signalforge.pipeline.stages.score import load
from signalforge.scoring.categories import CategoryInputs, categorize
from signalforge.scoring.scorer import Cited, confidence, merge_judges, validate_judgments

# --- mismatched quotations --------------------------------------------------------------------


def test_a_quote_with_a_dropped_turkish_negation_is_stored_as_the_page_states_it() -> None:
    """The model quoted "gönderiyor" (sends) from a page that says "göndermiyor" (does not send):
    a two-letter suffix, so the partial ratio is 98 and the quote passes as fuzzy. The stored
    excerpt must be the page's words, flagged fuzzy, never the model's copy."""
    page = (
        "Muhasebe programımız e-faturayı otomatik olarak göndermiyor, her faturayı elle yüklüyoruz."
    )
    misread = "programımız e-faturayı otomatik olarak gönderiyor"

    match = verify_quote(misread, page, DEFAULTS.extract.fuzzy_min_ratio)

    assert match is not None and match.verified == "fuzzy"
    assert "göndermiyor" in match.quote and match.quote == page[match.char_start : match.char_end]


# --- high scores with missing evidence ----------------------------------------------------------


def test_a_top_score_with_too_few_supported_facts_is_a_false_positive() -> None:
    top = {"budget_owner": "evidence", "price_ceiling_high": 500.0, "min_customer_value": 50.0,
           "severity": 5, "competition_gap": 5, "willingness_to_pay": 5, "attractiveness": 95.0,
           "strength": 9.5}  # fmt: skip
    minimum = CFG.categories.false_positive.min_supported_facts

    assert categorize(CategoryInputs(supported_facts=minimum - 1, **top), CFG)[0] == (
        "false_positive"
    )
    assert categorize(CategoryInputs(supported_facts=minimum, **top), CFG)[0] == "strong"


# --- unsupported willingness-to-pay ------------------------------------------------------------


def test_wtp_judged_5_on_a_hypothesis_is_capped_and_one_priced_competitor_is_no_floor() -> None:
    table = [Cited(1, "hypothesis"), Cited(2, "fact", "supported")]
    rubric = RubricJudgments(
        judgments=[_j("willingness_to_pay", 5, [1])]
        + [_j(f, 3, [2]) for f in ("severity", "frequency", "competition_gap",
                                   "customer_accessibility")]
    )  # fmt: skip
    code = _code(floor={"Rota Pro": [2]})  # below wtp_floor.min_competitors

    wtp = validate_judgments(rubric, table, code, CFG).factors["willingness_to_pay"]

    assert (wtp.level, wtp.capped, wtp.floor) == (CFG.uncited_cap, True, False)


def test_a_price_whose_fact_failed_entailment_does_not_raise_the_wtp_floor(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed_scoring(db, run_id)
    with db.begin() as session:
        excerpt_id = session.get(Claim, ids["segment"]).supports[0]
        other = Competitor(run_id=run_id, problem_id=ids["problem"], name="Logo WMS",
                           url="https://logo.example")  # fmt: skip
        session.add(other)
        session.flush()
        meta = {"competitor_id": other.id, "kind": "price"}
        price = add_fact(session, run_id, "Logo WMS costs 9,000 TRY.", [excerpt_id],
                         stage="competitors", meta=meta)  # fmt: skip
        price.entailment = "not_supported"
        other.pricing = [{"amount": 9000, "currency": "TRY", "period": "month",
                          "claim_id": price.id}]  # fmt: skip
    ctx = make_context(db, run_id, NoSearch(), FakeLLM({}))

    road = next(t for t in load(ctx, CFG) if t.id == ids["Road freight firms"])

    assert "Logo WMS" not in road.floor
    assert price.id not in {r.id for r in road.table}


# --- unstable scores ---------------------------------------------------------------------------


def test_judges_disagreeing_by_four_levels_make_confidence_low_despite_strong_evidence() -> None:
    table = [Cited(1, "fact", "supported")]
    code = _code()
    judges = [
        validate_judgments(
            RubricJudgments(judgments=[_j("severity", level, [1])]
                            + [_j(f, 3, [1]) for f in ("frequency", "competition_gap",
                                                       "willingness_to_pay",
                                                       "customer_accessibility")]),
            table, code, CFG,
        )
        for level in (1, 3, 5)
    ]  # fmt: skip

    merged = merge_judges(judges, code, CFG)
    result, entry = confidence(9.5, 0.0, {}, merged.max_spread, len(judges), CFG)

    assert merged.levels["severity"] == 3 and merged.max_spread == 4
    assert result == "low" and any("spread 4" in r for r in entry["reasons"])


# --- hypotheses presented as facts ---------------------------------------------------------------


def test_a_hypothesis_bullet_needs_a_hedge_not_the_month_may() -> None:
    """ "May" the month matched the hedge "may" (casefolded), so a hypothesis stated flatly
    passed as hedged."""
    flat = "The firm owner pays for the software, as their May invoices show."

    assert "kind_mismatch" in rules(check("buyers", flat, [3], "hypothesis"))
    assert not rules(check("buyers", "The firm owner may pay for software.", [3], "hypothesis"))
    assert not rules(check("buyers", "May pay: the firm owner, we assume.", [3], "hypothesis"))
