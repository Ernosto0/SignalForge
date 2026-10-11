# Controlled optimisation: proposed experiments (Phase 5)

**Status:** proposals only. Nothing below is implemented, and none of these improvements has been
measured. Baselines come from `backend/reports/benchmark/baseline/benchmark.md` (2026-10-11;
metric definitions in `metrics.md`). The cost figures are measured averages from the LLM ledger of
runs 22 and 30–34.

**Ground rule.** Every change is evaluated against the baseline with
`signalforge benchmark run --replay --baseline …`:

1. Measure on `dev` first.
2. Read `holdout` once, after the change is final.

Changes to stages before the replay frontier can be measured at **$0**. Changes to later stages
need re-recording, which is live spend and was **declined** (2026-10-11). Those experiments stay
blocked until it is approved.

## Can be measured now, at $0

### E1. Flag fuzzy quotes that flip a Turkish negation

- **Problem.** A quote that drops or adds a negation suffix (-me/-ma, *değil*, *yok*) passes
  fuzzy matching at ratio ≥ 95 (`test_regressions.py`). The stored excerpt stays verbatim, but
  the model's signal statement can state the opposite. Only 34% of facts are entailment-checked.
- **Hypothesis.** Some of the fuzzy matches (7.8% of verified quotes on dev, n = 825) differ from
  the page by a negation. Dropping them, or sending them to entailment, removes misread signals
  and leaves shortlisted problems unchanged.
- **Baseline:**
  - `quote_fuzzy_share`: dev 7.8%, holdout 0.4%
  - `shortlisted`: dev 25, holdout 14
- **Method.**
  1. Offline: rerun quote verification on the cached `extract` answers in the fixtures and count
     fuzzy matches whose diff contains a negation morpheme.
  2. If that count is not 0: implement the rule, replay `extract → shortlist` (extract answers
     are cached, so the rule only post-processes them), and compare `signals`, `shortlisted` and
     Gate 1 traces.
- **Cost:** $0 to run. A gain in precision can't be shown without human claim labels; only the
  number of affected signals can be measured.

### E2. Snippet-only sources in independence counting

- **Problem.** Dedupe never compares 40% (dev) and 57% (holdout) of documents (snippet-only or
  missing pages). The `same_quote` rule catches copies that quote ≥ 8 identical words; how many
  syndicated snippets get through is unmeasured.
- **Hypothesis.** Exact-hashing snippet texts across domains finds few extra duplicates
  (`duplicate_collapse_rate` +< 1 point) and changes no Gate 1 decision. If so, close audit R1.
  If not, the source counts behind Gate 1 are inflated.
- **Baseline:**
  - `duplicate_collapse_rate`: dev 1.8%, holdout 1.0%
  - `dedupe_unchecked_share`: dev 40.4%, holdout 56.5%
- **Method.** Add snippet texts (exact hash only) to dedupe's comparison, replay `dedupe →
  shortlist`, and compare collapse rate, independent sources per cluster, and shortlisted
  problems.
- **Cost:** $0.

### E3. Count only `supported` facts toward the false-positive rule

- **Problem.** `partial` verdicts count as support (`categories.py`), and they are 39% (dev) and
  51% (holdout) of checked facts.
- **Hypothesis.** Requiring `supported` facts moves weakly evidenced `interesting` cards to
  `false_positive`, and those cards are mostly proxy-`no`.
- **Baseline:**
  - `false_positive_rate_interesting_proxy`: dev 54.5% (n = 11), holdout 33.3% (n = 12)
  - `precision_at_3_proxy`: dev 10%, holdout 50%
- **Method.** Recompute categories from the stored inputs of each card's rule trace, with
  supported-only counting; this needs no LLM call. Report which cards change category and their
  proxy verdicts.
- **Cost:** $0.
- **Caveat.** n is about 23 proxy labels, so any difference is anecdotal, not significant.

## Blocked on live spend

| # | Area | Hypothesis | Baseline | Estimated cost |
|---|---|---|---|---|
| E0 | Prerequisite | Re-recording `verify → report` moves the replay frontier past every stage, so downstream changes can be measured at $0 afterwards. | Frontier: cluster, verify, competitors ×2, buyers ×2 | ≈ $0.27 LLM per run (stage averages: verify $0.040, competitors $0.167, score $0.026, report $0.027, …) + verify/competitor searches not cached ≈ **$2–3 for 6 runs** |
| E4 | Evidence validation | Entailment-checking every signal fact (not only key and cited facts) changes the shortlist where Gate 1 rests on unchecked facts. | `fact_checked_share` dev 33.8%, holdout 45.8% | ≈ 2,300 unchecked facts ÷ 10 per call × $0.00049 ≈ **$0.11** for 6 runs, plus E0 |
| E5 | Opportunity scoring | Evaluating `market_breadth` instead of the fixed level 2 makes `strong` reachable, so strong-card metrics become measurable. | `cards_strong` = 0 everywhere; top score 48.8–60 | New search + LLM per opportunity; needs a design first |
| E6 | Buyer and monetization research | The buyer-evidence loop being built in the working tree (uncommitted, `buyers@3`, `buyers_loop`) raises `cited` budget owners above 0 of 43. | Proxy checklist `payer` answers; `customer_accessibility` 2 everywhere | Being spent in another session now, **without a recorded baseline**. Run `benchmark run --replay` before and after. |
| E7 | Turkish query generation, source selection | Not proposed yet. The benchmark shows no specific weakness in collection: quote pass ≥ 98.8%, and triage and fetch reproduce exactly. A change here needs human labels of signals to be judged. | — | — |

No new agents or infrastructure are proposed. The benchmark shows no need for them yet.
