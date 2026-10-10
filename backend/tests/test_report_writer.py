"""Report writer (M6): the citation validator, deterministic sections, the stage with its
regeneration loop, and the report exit check."""

import json
from types import SimpleNamespace

import pytest
from fakes import PACK, PLAN, FakeLLM, make_context
from sqlalchemy import select
from test_buyer_research import NoSearch
from test_opportunity_scorer import _llm, _rubric_answer, _seed_scoring

from signalforge.config import get_defaults
from signalforge.db.models import Claim, Excerpt, ScoreCard
from signalforge.evidence.entailment import EntailmentBatch, clear_entailment
from signalforge.evidence.reports import check_report
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.report import ReportStage
from signalforge.pipeline.stages.score import Score
from signalforge.providers.llm import LLMError
from signalforge.reporting.deterministic import (
    INSUFFICIENT_QUOTES,
    dont_build,
    insufficient_evidence,
)
from signalforge.reporting.schema import (
    SECTION_KEYS,
    SUMMARY,
    UNCITED_ALLOWED,
    Bullet,
    ClaimView,
    DraftBullet,
    ExcerptView,
    SectionDraft,
    SummaryDraft,
)
from signalforge.reporting.tables import select_full
from signalforge.reporting.validator import (
    names_unsupported,
    number_supported,
    number_values,
    source_numbers,
    validate_bullet,
)

HEDGES = get_defaults().report.hedges


def claim(
    id: int, kind: str, statement: str, entailment=None, quote=None, translation=None, **meta
):
    excerpts = [ExcerptView(quote=quote, translation=translation, url="https://x.test",
                            domain="x.test", date=None)] if quote else []  # fmt: skip
    return ClaimView(id=id, kind=kind, statement=statement, entailment=entailment,
                     excerpts=excerpts, meta=meta)  # fmt: skip


CLAIMS = {
    1: claim(
        1,
        "fact",
        "Logo and Mikro Jump charge 1.234,56 TRY per month.",
        "supported",
        "Logo aylık 1.234,56 TL",
        "Logo costs 1,234.56 TRY a month",
    ),  # fmt: skip
    2: claim(2, "inference", "Forwarders lose hours on paperwork."),
    3: claim(3, "hypothesis", "The firm owner is a plausible budget owner."),
    4: claim(
        4,
        "assumption",
        "hours_saved_per_month = 15–40 hour/month: guess (claims 2, 3, 4)",
        sourced=False,
    ),  # fmt: skip
    5: claim(5, "fact", "Failed fact about Parasut.", "not_supported"),
    6: claim(6, "assumption", "wage = 500.87–577.92 TRY/hour: pack", sourced=True),
}
TABLE = {1, 2, 3, 4, 5}


def check(section, text, ids, kind="fact", extra=(), table=TABLE):
    return validate_bullet("p", section, Bullet(text=text, claim_ids=ids, kind=kind), table,
                           list(extra), CLAIMS, ["Turkey", "logistics"], HEDGES)  # fmt: skip


def rules(errors) -> set[str]:
    return {e.rule for e in errors}


def test_a_cited_supported_bullet_passes() -> None:
    assert check("evidence", "Logo charges 1,234.56 TRY per month.", [1]) == []
    assert check("evidence", "Mikro Jump and Logo are priced per month.", [1]) == []


@pytest.mark.parametrize(
    ("section", "text", "ids", "kind", "rule"),
    [
        ("evidence", "Logo is expensive.", [], "fact", "uncited_fact"),
        ("gaps", "Logo charges 1.234,56 TRY.", [99], "fact", "unknown_citation"),
        ("evidence", "Parasut is cheap.", [5], "fact", "unknown_citation"),  # failed entailment
        ("evidence", "Logo charges 2.000 TRY per month.", [1], "fact", "unsupported_specific"),
        (
            "evidence",
            "Rivals like Netsis charge 1.234,56 TRY.",
            [1],
            "fact",
            "unsupported_specific",
        ),
        ("buyers", "The firm owner pays for software.", [3], "fact", "kind_mismatch"),
        ("buyers", "The firm owner may pay for software.", [3], "hypothesis", None),
        ("buyers", "The firm owner pays for software.", [3], "hypothesis", "kind_mismatch"),
        ("evidence", "Build a dashboard.", [1], "recommendation", "recommendation_outside_allowed"),
        ("mvp", "Build a dashboard.", [], "fact", "uncited_fact"),
        ("mvp", "Build a dashboard.", [], "recommendation", None),
        ("mvp", "Integrate with Netsis first.", [], "recommendation", "unsupported_specific"),
        ("mvp", "Integrate with Logo first.", [], "recommendation", None),
        ("economics", "Saves 15–40 hours, we assume.", [4], "assumption", None),
        ("economics", "Saves 15–40 hours.", [4], "assumption", "kind_mismatch"),  # needs a hedge
        ("economics", "Saves 60 hours, we assume.", [4], "assumption", "unsupported_specific"),
        ("economics", "Wage is 500.87–577.92 TRY.", [4], "assumption", "unsupported_specific"),
        ("economics", "Saves hours, we assume.", [2], "assumption", "kind_mismatch"),
        ("problem", "Forwarders lose hours.", [2], "fact", "kind_mismatch"),  # inference only
        ("problem", "Forwarders lose hours.", [2], "inference", None),
    ],
)
def test_validator_rules(section, text, ids, kind, rule) -> None:
    errors = check(
        section, text, ids, kind, table=TABLE | ({6} if section == "economics" else set())
    )
    assert (rules(errors) == set()) if rule is None else (rule in rules(errors)), errors


def test_a_summary_bullet_cannot_quote_the_scores() -> None:
    # The summary's extra sources are the segments only; scores live in the code-built ranking.
    errors = validate_bullet(
        "summary.bullets[0]", SUMMARY,
        Bullet(text="Logo users score attractiveness 55.0, the best.", claim_ids=[1], kind="fact"),
        TABLE, ["opportunity: SMB warehouses"], CLAIMS, ["Turkey", "logistics"], HEDGES,
    )  # fmt: skip
    assert [e.message for e in errors] == ["number '55.0' is not in the cited claims"]


def test_the_rationale_of_an_assumption_does_not_count_as_a_source() -> None:
    # "(claims 2, 3, 4)" sits in the statement but is another prompt's local numbering.
    errors = check("economics", "Saves 15–40 hours across 3 teams, we assume.", [4], "assumption")
    assert any("'3'" in e.message for e in errors)


def test_model_values_count_only_where_extra_sources_are_given() -> None:
    model = ["value_usd_month: 152.93–470.55"]
    text = "Estimated monthly value is $153–471 per company, we assume."
    assert check("economics", text, [4], "assumption", extra=model) == []
    assert "unsupported_specific" in rules(check("evidence", text, [1], "fact"))


@pytest.mark.parametrize(
    ("bullet", "source", "ok"),
    [
        ("1.234,56", "1,234.56", True),
        ("1,234.56", "1.234,56", True),
        ("1.234", "1,234", True),
        ("1.234", "1234", True),  # TR grouping
        ("1,5", "1.5", True),
        ("153", "152.93", True),  # rounded within 1%
        ("471", "470.55", True),
        ("1", "0.6", False),  # small numbers need an exact match
        ("2", "1.5", False),
        ("160", "152.93", False),
        ("30%", "0.3", True),
        ("%30", "0.3", True),
        ("10–30%", "0.1 0.3", True),
        ("15–40", "15 40", True),
        ("45", "15 40", False),
    ],
)
def test_number_formats(bullet, source, ok) -> None:
    assert (number_supported(bullet, source_numbers(source)) == []) is ok


def test_ambiguous_separators_have_both_readings() -> None:
    assert {v for v, _ in number_values("1.234")} == {1.234, 1234.0}
    assert {v for v, _ in number_values("1.234,5")} == {1234.5}
    assert {v for v, _ in number_values("1,234,567")} == {1234567.0}


def test_proper_nouns_are_turkish_aware_and_skip_sentence_starts() -> None:
    allowed = {"is", "bankasi", "logo"}
    assert names_unsupported("İş Bankası uses Logo.", allowed) == []  # İ/ı/I fold together
    assert names_unsupported("Prices rose. Netsis is dearer.", allowed) == []  # sentence start
    assert names_unsupported("They use Logo'nun and Netsis.", allowed) == ["Netsis"]
    assert names_unsupported("ERP and API are common.", allowed) == []


# --- selection and deterministic sections (pure) --------------------------------------------


def _opp(id: int, category: str, attractiveness: float | None, confidence: str = "medium", **kw):
    card = {"category": category, "attractiveness": attractiveness, "confidence": confidence,
            "founder_fit": "fit", "factors": {}, "rule_trace": kw.pop("trace", [])}  # fmt: skip
    return SimpleNamespace(id=id, card=card, category=category, segment=f"seg {id}",
                           status=kw.get("status"), knockouts=kw.get("knockouts", []))  # fmt: skip


def test_full_reports_are_ordered_by_category_then_attractiveness_then_confidence() -> None:
    opps = [
        _opp(1, "competitive", 90.0),
        _opp(2, "interesting", 50.0, "low"),
        _opp(3, "interesting", 50.0, "high"),
        _opp(4, "strong", 40.0),
        _opp(5, "weak", None),
        _opp(6, "false_positive", 99.0),
    ]
    assert [o.id for o in select_full(opps, 5)] == [4, 3, 2, 1]
    assert [o.id for o in select_full(opps, 2)] == [4, 3]


def test_dont_build_comes_from_knockouts_and_category_traces() -> None:
    weak_trace = [{"rule": "category.weak", "fired": True, "inputs": {"severity": 2}}]
    opps = [
        _opp(
            1,
            "weak",
            None,
            status="knocked_out",
            knockouts=[{"rule": "value_below_minimum", "detail": "ceiling < minimum"}],
        ),  # fmt: skip
        _opp(2, "weak", 30.0, trace=weak_trace),
        _opp(3, "interesting", 50.0),
    ]
    items = {d.opportunity_id: d for d in dont_build(opps)}
    assert set(items) == {1, 2}
    assert [(r.rule, r.detail) for r in items[1].reasons] == [
        ("gate2.value_below_minimum", "ceiling < minimum")
    ]
    assert [(r.rule, r.detail) for r in items[2].reasons] == [("category.weak", "severity 2")]


# --- stage ----------------------------------------------------------------------------------

DEFAULTS = get_defaults()


def _first_claim_bullet(payload: dict, bad: str = "") -> list[DraftBullet]:
    claims = payload["claims"]
    if not claims:
        return []
    c = claims[0]
    text = c["statement"]
    if c["kind"] == "hypothesis":
        text = f"It may be that {text}"
    elif c["kind"] == "assumption" and c.get("sourced") is False:
        text = f"We assume {text}"
    return [DraftBullet(text=text + bad, claim_ids=[c["n"]], kind=c["kind"])]


def _good(seen: list[dict] | None = None, bad_in: dict[str, str] | None = None, fail_in=()):
    """Section writer: restates the first claim of the table; a recommendation where allowed.
    ``bad_in`` section → text that breaks the validator (always, or until the retry)."""

    def handle(prompt_input: str) -> SectionDraft:
        payload = json.loads(prompt_input)
        if seen is not None:
            seen.append(payload)
        key = payload["section"]["key"]
        if key in fail_in:
            raise LLMError("output cut off")
        bad = (bad_in or {}).get(key, "")
        if bad.startswith("once:") and "errors" in payload:
            bad = ""
        bullets = _first_claim_bullet(payload, bad.removeprefix("once:"))
        if key in UNCITED_ALLOWED:
            bullets.append(
                DraftBullet(text="Build a small delivery photo tool.", claim_ids=[],
                            kind="recommendation")
            )  # fmt: skip
        return SectionDraft(bullets=bullets)

    return handle


def _summary(prompt_input: str) -> SummaryDraft:
    payload = json.loads(prompt_input)
    c = payload["claims"][0]
    kind = "fact" if c["kind"] == "fact" else "inference"
    return SummaryDraft(bullets=[DraftBullet(text=c["statement"], claim_ids=[c["n"]], kind=kind)])


def _setup(db, tmp_path, section=None, entailment=None, plan=PLAN):
    run_id = create_run(db, plan, PACK, DEFAULTS)
    ids = _seed_scoring(db, run_id)
    score_ctx = make_context(db, run_id, NoSearch(), _llm(_rubric_answer([])))
    assert run_stage(score_ctx, Score()).status == "completed"
    handlers = {SectionDraft: section or _good(), SummaryDraft: _summary}
    if entailment:
        handlers[EntailmentBatch] = entailment
    llm = FakeLLM(handlers)
    ctx = make_context(db, run_id, NoSearch(), llm)
    cfg = ctx.defaults.report.model_copy(update={"out_dir": tmp_path})
    ctx.defaults = ctx.defaults.model_copy(update={"report": cfg})
    return run_id, ids, ctx, llm


def _load(tmp_path, run_id):
    d = tmp_path / f"run-{run_id}"
    return json.loads((d / "report.json").read_text(encoding="utf-8")), d


def _section(report: dict, key: str) -> dict:
    return next(s for s in report["opportunities"][0]["sections"] if s["key"] == key)


def test_report_stage_writes_a_valid_cited_report(db, tmp_path) -> None:
    run_id, ids, ctx, llm = _setup(db, tmp_path)
    row = run_stage(ctx, ReportStage())
    assert row.status == "completed", row.error
    m = row.metrics
    assert (m["full_reports"], m["eligible"], m["below_min_full_reports"]) == (1, 1, True)
    assert m["factual_cited_pct"] == 1.0 and m["bullets_dropped"] == 0
    assert m["sections_generated"] >= 9 and m["failed_sections"] == 0
    assert check_report(ctx) == []

    report, d = _load(tmp_path, run_id)
    opp = report["opportunities"][0]
    assert opp["opportunity_id"] == ids["Road freight firms"]
    assert [s["key"] for s in opp["sections"]] == list(SECTION_KEYS)
    first = _section(report, "validation_experiment")["bullets"][0]  # built in code from the card
    assert first["kind"] == "recommendation" and first["text"].startswith("Run interviews:")
    assert report["summary"] and report["summary"][0]["claim_ids"]
    assert [x["segment"] for x in report["dont_build"]] == ["Carriers without owner"]
    assert report["dont_build"][0]["reasons"][0]["rule"] == "gate2.no_budget_owner"
    assert [x["name"] for x in report["insufficient_evidence"]] == ["Customs paperwork"]
    assert report["other_opportunities"] == []
    assert report["method"]["pack"] == report["method"]["run_pack"] == f"{PACK.id}@{PACK.version}"
    assert not any("run created with" in n for n in report["method"]["notes"])
    with db() as session:
        card = session.scalars(
            select(ScoreCard).where(ScoreCard.opportunity_id == ids["Road freight firms"])
        ).one()
    assert report["summary_ranking"] == [
        {"opportunity_id": ids["Road freight firms"], "title": "Road freight firms",
         "category": card.category, "attractiveness": card.attractiveness,
         "confidence": card.confidence, "founder_fit": card.founder_fit}
    ]  # fmt: skip
    assert report["summary_extra"] == ["opportunity: Road freight firms"]
    assert all(len(i["quotes"]) <= INSUFFICIENT_QUOTES for i in report["insufficient_evidence"])

    md = (d / "report.md").read_text(encoding="utf-8")
    html = (d / "report.html").read_text(encoding="utf-8")
    for text in (md, html):
        assert "Assumption, unsourced" in text  # unsourced assumptions are marked
        assert "Alıntı" in text and "Customs paperwork" in text
    assert "> — *t*" in md  # the original quote with its English translation
    assert "| Road freight firms | interesting |" in md  # the ranking line, from the card
    assert "Carriers without owner" in md and "gate2.no_budget_owner" in md


def test_a_failing_section_is_regenerated_with_its_errors(db, tmp_path) -> None:
    seen: list[dict] = []
    run_id, _, ctx, _ = _setup(db, tmp_path, _good(seen, {"evidence": "once: Costs 999 TRY."}))
    row = run_stage(ctx, ReportStage())
    assert row.status == "completed", row.error
    retries = [p for p in seen if "errors" in p]
    assert len(retries) == 1 and retries[0]["section"]["key"] == "evidence"
    assert any("number '999'" in e for e in retries[0]["errors"])
    assert retries[0]["previous"][0]["claim_ids"][0] >= 1  # the model's own numbering
    assert row.metrics["regenerations"] == 1 and row.metrics["bullets_dropped"] == 0
    report, _ = _load(tmp_path, run_id)
    evidence = _section(report, "evidence")
    assert evidence["bullets"] and "999" not in evidence["bullets"][0]["text"]
    assert check_report(ctx) == []


def test_a_bullet_still_invalid_after_the_regenerations_is_dropped(db, tmp_path) -> None:
    seen: list[dict] = []
    bad = {"gaps": " Rivals like Netsis cost 999."}
    run_id, _, ctx, _ = _setup(db, tmp_path, _good(seen, bad))
    row = run_stage(ctx, ReportStage())
    assert row.status == "completed", row.error
    assert sum(p["section"]["key"] == "gaps" for p in seen) == 3  # 1 + max_regenerations
    m = row.metrics
    assert (m["bullets_dropped"], m["regenerations"]) == (1, 2)
    assert m["dropped_by_rule"] == {"unsupported_specific": 2}
    report, d = _load(tmp_path, run_id)
    dropped = report["method"]["dropped_bullets"]
    assert len(dropped) == 1 and dropped[0]["section"] == "gaps"
    assert "Netsis" in dropped[0]["text"] and len(dropped[0]["errors"]) == 2
    gaps = _section(report, "gaps")
    assert gaps["bullets"] == []  # the section never ships an invalid bullet
    assert "Netsis" not in json.dumps(gaps)
    assert "Dropped (gaps)" in (d / "report.md").read_text(encoding="utf-8")
    assert check_report(ctx) == []


def test_a_failed_section_call_leaves_the_section_empty(db, tmp_path) -> None:
    run_id, ids, ctx, _ = _setup(db, tmp_path, _good(fail_in=("risks",)))
    row = run_stage(ctx, ReportStage())
    assert row.status == "completed", row.error
    assert row.metrics["failed_sections"] == 1 and row.metrics["failed_calls"] == 3
    report, _ = _load(tmp_path, run_id)
    assert report["method"]["failed_sections"] == [f"{ids['Road freight firms']}:risks"]
    assert _section(report, "risks")["bullets"] == []
    assert check_report(ctx) == []


def test_no_page_text_leaves_the_excerpt_clip(db, tmp_path) -> None:
    run_id, ids, ctx, _ = _setup(db, tmp_path)
    marker = "FULLTEXTMARKER"
    with db.begin() as session:
        claim = session.get(Claim, ids["pain_fact"])
        excerpt = session.get(Excerpt, claim.supports[0])
        excerpt.quote = "Uzun alıntı " + "x" * 900 + marker
        excerpt.translation = "Long quote " + "y" * 900 + marker
    row = run_stage(ctx, ReportStage())
    assert row.status == "completed", row.error
    report, d = _load(tmp_path, run_id)
    limit = ctx.defaults.report.excerpt_max_chars
    quotes = [e for c in report["claims"] for e in c["excerpts"]]
    assert quotes and all(len(e["quote"]) <= limit for e in quotes)
    assert all(len(e["translation"] or "") <= limit for e in quotes)
    for name in ("report.json", "report.md", "report.html"):
        assert marker not in (d / name).read_text(encoding="utf-8")
    assert check_report(ctx) == []


def test_a_rerun_replaces_the_report_and_is_served_from_the_cache(db, tmp_path) -> None:
    run_id, _, ctx, llm = _setup(db, tmp_path)
    assert run_stage(ctx, ReportStage()).status == "completed"
    first, d = _load(tmp_path, run_id)
    calls = len(llm.calls)
    (d / "stale.txt").write_text("x")
    row = run_stage(ctx, ReportStage())
    assert row.status == "completed", row.error
    assert len(llm.calls) == calls  # every answer came from the cache
    second, _ = _load(tmp_path, run_id)
    for r in (first, second):
        r.pop("generated_at")
        r["method"].pop("stage_costs")  # the first report stage's own cost shows up in the second
    assert first == second and not (d / "stale.txt").exists()
    assert check_report(ctx) == []


def test_facts_failing_entailment_leave_the_tables(db, tmp_path) -> None:
    run_id, ids, ctx, _ = _setup(db, tmp_path)
    with db.begin() as session:
        clear_entailment(session, run_id, "score")  # this stage checks the facts itself
        session.get(Claim, ids["pain_fact"]).entailment = "contradicted"
    row = run_stage(ctx, ReportStage())
    assert row.status == "completed", row.error
    assert row.metrics["entailment"]["checked"] > 0
    report, _ = _load(tmp_path, run_id)
    tables = {i for s in report["opportunities"][0]["sections"] for i in s["table"]}
    assert ids["pain_fact"] not in tables and ids["job_fact"] in tables
    with db() as session:
        by = session.get(Claim, ids["job_fact"]).meta.get("entailment_by")
    assert by in ("report", "score")
    assert check_report(ctx) == []


def test_only_english_reports_are_supported(db, tmp_path) -> None:
    request = PLAN.request.model_copy(update={"report_language": "tr"})
    _, _, ctx, _ = _setup(db, tmp_path, plan=PLAN.model_copy(update={"request": request}))
    with pytest.raises(ValueError, match="report_language"):
        run_stage(ctx, ReportStage())


def test_check_report_flags_tampering_but_not_a_changed_method(db, tmp_path) -> None:
    run_id, ids, ctx, _ = _setup(db, tmp_path)
    assert run_stage(ctx, ReportStage()).status == "completed"
    original, d = _load(tmp_path, run_id)
    path = d / "report.json"

    def tampered(edit) -> list[str]:
        data = json.loads(json.dumps(original))
        edit(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return check_report(ctx)

    def uncite(data):
        _section(data, "evidence")["bullets"][0]["claim_ids"] = []

    def unknown(data):
        _section(data, "gaps")["bullets"][0]["claim_ids"] = [10**9]

    def number(data):
        _section(data, "evidence")["bullets"][0]["text"] += " Costs 4242 TRY."

    def hypothesis_as_fact(data):
        b = _section(data, "economics")["bullets"][0]
        b["kind"], b["text"] = "fact", b["text"].replace("We assume ", "")

    def recommendation(data):
        _section(data, "evidence")["bullets"][0]["kind"] = "recommendation"

    def reasons(data):
        data["dont_build"][0]["reasons"][0]["detail"] = "forged"

    def long_quote(data):
        data["claims"][0]["excerpts"][0]["quote"] = "z" * 600

    def statement(data):
        data["claims"][0]["statement"] = "A different statement."

    def ranking(data):
        data["summary_ranking"][0]["attractiveness"] = 99.0

    def method(data):
        data["method"]["stage_costs"]["report"] = 9.99
        data["method"]["notes"].append("anything")
        data["generated_at"] = "2020-01-01T00:00:00Z"

    expect = {
        uncite: "uncited",
        unknown: "unknown_citation",
        number: "number '4242'",
        hypothesis_as_fact: "kind_mismatch",
        recommendation: "recommendation_outside_allowed",
        reasons: "dont_build differs",
        long_quote: "longer than",
        statement: "differs from the stored",
        ranking: "summary_ranking differs",
    }
    for edit, needle in expect.items():
        errors = tampered(edit)
        assert any(needle in e for e in errors), (edit.__name__, errors)
    assert tampered(method) == []
    path.unlink()
    assert "does not exist" in check_report(ctx)[0]


def test_a_newer_pack_than_the_run_was_created_with_is_noted(db, tmp_path) -> None:
    run_id, _, ctx, _ = _setup(db, tmp_path)
    ctx.pack = PACK.model_copy(update={"version": "9.9.9"})
    assert run_stage(ctx, ReportStage()).status == "completed"
    report, d = _load(tmp_path, run_id)
    m = report["method"]
    assert (m["pack"], m["run_pack"]) == (f"{PACK.id}@9.9.9", f"{PACK.id}@{PACK.version}")
    note = (
        f"run created with {PACK.id}@{PACK.version}; economics and this report use {PACK.id}@9.9.9."
    )
    assert note in m["notes"]
    head = (d / "report.md").read_text(encoding="utf-8").splitlines()[2]
    assert f"pack {PACK.id}@9.9.9 (run created with {PACK.id}@{PACK.version})" in head
    assert check_report(ctx) == []


def test_insufficient_evidence_shows_at_most_two_quotes() -> None:
    quote = {"quote": "Alıntı " + "q" * 600, "translation": "t", "url": "https://x.test",
             "domain": "x.test", "published": None, "statement": "s"}  # fmt: skip
    cluster = {"id": 1, "name": "Customs", "failed_rules": ["too few independent sources"],
               "signals": 5, "independent_sources": 1, "evidence_strength": 3.0,
               "verification": None, "quotes": [quote] * 5}  # fmt: skip
    [item] = insufficient_evidence(
        {"insufficient_evidence": [cluster], "shortlist": []}, set(), 500
    )
    assert len(item.quotes) == INSUFFICIENT_QUOTES == 2
    assert all(len(q.quote) <= 500 for q in item.quotes) and item.why == "failed Gate 1"
