# Research quality evaluation: audit

> **Status 2026-10-11 (after implementation).** The fixtures, benchmark, label schema and
> regression tests are in place (`metrics.md`, `optimization.md`). Corrections and new findings:
>
> - **R1 restated.** Snippet-only documents skip dedupe *by design*. Copies quoting the same
>   ≥ 8-word passage are still merged by `same_quote` at extract. The remaining risk is real but
>   unmeasured: `dedupe_unchecked_share` is 40% (dev) and 57% (holdout). Experiment E2 measures it.
> - **New: replay fidelity bug, fixed.** Transient fetch and search failures were never cached,
>   so a replay hit a `CacheMiss` where the recording had a failure. They now replay as failures.
> - **New: stale recordings.** No M7 run replays past `cluster`–`buyers`, because the code
>   changed after each run was recorded. Replay detects this and reports it.
> - **New: negation.** A dropped Turkish negation suffix passes fuzzy quote matching at ratio 98.
>   The stored excerpt stays verbatim; experiment E1 measures the impact.
> - **New: hedge bug, fixed.** The month "May" counted as the hedge "may" in report validation.
> - **No mismatches found.** Quote integrity is 100% of 3,399 checkable stored excerpts.

**Date:** 2026-10-11 · **Commit:** `4837e35` · **Scope:** read-only audit. No production code was
changed.

**Commands run**

- `backend/.venv/Scripts/python.exe -m pytest -q`: **308 passed** in 40.7 s (Postgres
  `signalforge_test` on the docker-compose DB).
- Read-only `psql` queries against the dev database (runs, labels, cache, claims, stage metrics).

Facts below come from the code and from the database as of this date. Where something is an
inference, it is marked **(inference)**.

---

## 1. What already works

| Area | What exists | Where |
|---|---|---|
| Record/replay cache | `live` / `record` / `replay` for search, page and LLM. In replay a miss raises `CacheMiss`. The LLM key is (model, `prompt@version`, hash of instructions + input + schema + max tokens). | `providers/cache.py`, `providers/llm.py:214-268` |
| Prompt versioning | Every prompt has `id` + `version` in its header. The version is part of the cache key and stored on each `LLMCall`. Stages store an `input_hash` over config, prompts and inputs. | `prompts/__init__.py`, `pipeline/stages/*.py` (`input_hash`) |
| Quote verification | A quote is matched after Turkish-aware normalisation: exact, or fuzzy at rapidfuzz partial ratio ≥ 95. The stored quote is always the span of the *source*, so stored excerpts are verbatim by construction. | `evidence/quotes.py` |
| Claim types | `fact` (needs ≥ 1 excerpt), `inference` (needs ≥ 1 parent claim), `hypothesis`, `assumption` (needs `sourced` plus value meta). Enforced at write time. | `evidence/claims.py` |
| Entailment | A cheap model checks a fact against its quotes: `supported` / `partial` / `not_supported` / `contradicted`. Failed facts leave evidence counts and citations. | `evidence/entailment.py` |
| Independence | Collapses `near_dup` / `syndicated` (MinHash) and `same_author` (salted hash) duplicates, plus `same_quote` (≥ 8 identical words). | `evidence/dedup.py`, `evidence/independence.py` |
| Evidence strength | Deterministic 0–10 score from sources, diversity, tier, recency and directness. | `evidence/strength.py` |
| Scoring | 5 model factors on an anchored rubric (`rubric.yaml` v1). A factor with no cited fact is capped at 2. Code sets economic impact and market breadth. Median of 3 judges for the top 5. Confidence comes from strength, hypothesis share and judge spread. Category is rule-based. Every rule is traced. | `scoring/scorer.py`, `scoring/categories.py` |
| Report validator | Checks citations, unknown or failed citations, numbers and proper nouns not found in sources, hedges for hypotheses, and kind mismatches. | `reporting/validator.py` |
| Exit checks | `check_gap_matrices`, `check_buyer_roles`, `check_economic_models`, `check_score_cards`, `check_report`: structural invariants over stored runs. | `evidence/gaps.py`, `evidence/opportunities.py`, `evidence/reports.py` |
| Stage metrics | Every stage writes detailed `StageRun.metrics` (yield, rejections, entailment counts, cache hits, judge spread, …). | `pipeline/runner.py` + stages |
| Cost ledger | Every LLM call (tokens, cost, latency, cache hit). `ledger --by agent\|stage`. | `providers/llm.py`, `cli.py ledger` |
| Multi-run evaluation | `signalforge evaluate <runs>`: funnel, categories, founder verdicts, clean-pass cost (LLM + live searches), wall time. | `reporting/evaluation.py` |
| Labeling | `label signals\|clusters\|opportunities` (y/n plus the founder checklist), `labels <run>` (precision as share of `y`). | `cli.py:818-1005`, `evals/founder_checklist.yaml` |
| Test suite | 308 tests: unit tests on pure functions and stage tests against Postgres with fake search, LLM and fetch (`tests/fakes.py`). | `backend/tests/` |

## 2. What is currently measured

From stored metrics of the six completed runs (22 logistics, 30 accounting, 31 customs/export,
32 e-commerce, 33 construction, 34 insurance):

**Deterministic, computed by code:**

- Quote verification: `extract.quote_pass_rate` (99–100%), `quotes_exact` vs `quotes_fuzzy`
  (run 30: 333 / 44).
- First-hand share, signals by type, yield by source category.
- Duplicate collapse rate, independent sources per cluster, evidence strength per cluster.
- Gate 1 failures by rule and Gate 2 knock-outs by rule.
- Gap-matrix cells by value, prices found, share of sourced economic assumptions
  (`monetization.sourced_share`, run 30: 0.5).
- Report: `factual_cited_pct`, dropped bullets by rule, regenerations.
- Cost per stage, agent and run, live searches and wall time.
- Judge spread per factor (`ScoreCard.factors[*].judges`, `score.judge_spread_median`).

**LLM-judged (same cheap model family as the generator):**

- Entailment verdicts for facts that get checked. Across the dev DB: 879 supported, **679 partial**,
  26 not_supported. **4,048 facts never checked** (signal facts that are not key claims and are not
  cited).

**Human-labeled:**

- 10 signal labels, 4 cluster labels (all on run 5, an old stopped run) and **1** opportunity label
  (run 31). That is not enough for any metric.

**Proxy model labels (not human, not in the DB, in git-ignored `reports/`):**

- `reports/m3-review-2026-10-09/labels*.tsv`: about 50 signals, Claude standing in for the founder.
- `reports/m7-evaluation-2026-10-11/verdicts.tsv`: 25 Gate-2 survivors with the founder checklist
  and verdicts, also Claude.

## 3. What cannot currently be measured

| Requested metric | Status | Why |
|---|---|---|
| Quote verification rate | **Available** | Already in metrics. It proves a quote exists, not that it means what the claim says. |
| Claim support precision | **Unavailable (human)**; LLM-only proxy | No human entailment labels. The LLM verdicts are never audited, so the precision of the "verified" label itself is unknown. |
| Independent-source precision | **Unavailable** | No labels on whether two sources are truly independent. |
| Problem discovery precision | **Unavailable** | 4 cluster labels on one obsolete run. |
| Problem discovery recall | **Unavailable** | No reference list of problems per market. Labels key on DB ids, which change on every re-run (see risk R7). |
| Opportunity Precision@3 | **Proxy only** | Needs human verdicts. Only 1 exists; 25 proxy verdicts exist outside the DB. |
| False-positive rate for strong opportunities | **Not computable** | **No run has ever produced a `strong` card.** The top score is 48.8–60 and the threshold is 75 (plan §13 M7). |
| Score stability | **Partially available** | Judge spread for the top 5 per run is stored. Running the scorer repeatedly on identical inputs is not possible: the cache returns the same answer. |
| Confidence calibration | **Unavailable** | Needs dozens of human verdicts per confidence level. Only 1 exists. |
| Competitor fact accuracy | **LLM-only** | Entailment of cited cells exists. There is no human check, and price freshness is never validated. |
| Economic assumption provenance | **Available** | `sourced_share`, `pack_referenced`, and `meta.sourced` / `source_url` / `as_of` on assumptions. |
| Stage failure and yield | **Available** | `StageRun.status` and metrics. |
| Cost / latency per useful opportunity | **Proxy only** | Cost and latency are known. "Useful" needs human verdicts. Cost per Gate-2 survivor is computable now. |
| Deterministic offline benchmark | **Unavailable** | The cache lives only in the Postgres dev DB. `fixture export` (plan §11) was never built, so there is no portable, frozen fixture. |

## 4. Reliability risks

Each risk was found in the code. Where a risk is (inference), it needs a test to confirm.

| # | Risk | Evidence |
|---|---|---|
| R1 | **Snippet-only documents never go through near-dup/syndication detection.** They collapse only through `same_author` / `same_quote`. In run 30, 80 of 200 documents were snippet-only. Two snippets of one syndicated text on different domains count as two independent sources unless their extracted quotes share ≥ 8 identical words. | `pipeline/stages/dedupe.py:68` |
| R2 | Documents whose page was purged from the cache skip dedupe silently (only counted as `text_missing`), so they count as independent. | `dedupe.py:70-72` |
| R3 | **`partial` entailment counts as support.** The false-positive rule counts "supported or partial" facts, and partial is 43% of checked facts. The plan already notes that partial facts are sometimes stated flatly (M6 open item a). | `scoring/categories.py:16`, plan §13 M6 |
| R4 | The entailment judge is unaudited and runs on the same cheap model as extraction. Its error rate is unknown, and every downstream "verified" claim inherits it. | `evidence/entailment.py` (FAST tier) |
| R5 | Most signal facts are never entailment-checked (4,048 unchecked). Evidence strength and Gate 1 count signals whose facts were never checked, only quote-verified. **(inference)**: needs confirmation in `shortlist`/`strength` inputs. | DB counts; `evidence/strength.py` takes signals, not checked facts |
| R6 | Score stability is measured only as spread between judges that differ by a `judge_index` string. Identical inputs return cached answers, so a re-run can never show instability. | `pipeline/stages/score.py` docstring, `providers/llm.py` |
| R7 | **Labels key on DB ids.** A re-run, replay or import creates new ids, so labels do not carry over to the output being evaluated. Recall and precision across pipeline versions need stable keys (canonical URL + quote span; normalised claim text). | `db/models.py:411-421` |
| R8 | `strong` is unreachable today because `market_breadth` is fixed at 2 and `customer_accessibility` is 2 everywhere. Category-level metrics only test `interesting` vs `weak`. | plan §13 M7 findings |
| R9 | Competitor price `observed_at` is the page fetch time, which can be up to 90 days old under the cache TTL. It is not the date the price was valid. There is no check that a price is current or that the plan or period is right. | `pipeline/stages/competitors.py:587-596` |
| R10 | Gap inferences read as market facts ("none of the reviewed products covers X"). This is plan M7 open item b. | plan §13 M7 |
| R11 | Proxy labels (Claude) are the only multi-market quality signal. They are git-ignored and not versioned, and a model judging a model's output overestimates agreement **(inference)**. | `reports/` in `.gitignore` |
| R12 | No held-out data. All six completed runs were used to tune thresholds and prompts in M7, so any metric on them is a development-set number. | plan §13 M7 progress |
| R13 | Fixture privacy. Cached pages hold third-party text (forum posts, complaint sites) that can include personal data (KVKK). Committing them to a public repository is a legal risk. | cache `page` namespace (~5 MB) |

## 5. Missing tests

Status of each roadmap failure mode:

| Failure mode | Existing coverage | Gap |
|---|---|---|
| Fabricated or mismatched quotes | `test_evidence.py::test_invented_or_stitched_quotes_are_rejected`, `test_problem_discovery.py::test_extract_keeps_only_verified_quotes…` | Fuzzy-boundary cases: a quote at ratio 95–96 that changes meaning (e.g. a negation dropped). |
| Duplicate/syndicated sources counted as independent | Full-text near-dup and syndication; same author; same quote | **Snippet-only syndication (R1)** and purged-page documents (R2): untested and likely failing. |
| Claims unsupported by cited evidence | Entailment plumbing with fake verdicts; failed facts leave tables | Nothing checks that a `partial` fact cannot carry a category or a high factor alone (R3). |
| Hypotheses presented as facts | Validator `kind_mismatch` with hedge rule | A hedge word anywhere passes ("may"), even when the bullet asserts a fact. Gap inferences phrased as facts (R10). |
| Competitor prices without valid source or date | `test_price_amounts_must_be_in_the_quote` | A price whose fact failed entailment still populating `Competitor.pricing`; a missing `observed_at`; a stale price. |
| Unsupported WTP conclusions | `test_the_wtp_floor_needs_distinct_competitors`, `…counts_prices_of_any_period` | The WTP floor firing on a price fact that failed entailment (code excludes it; no test pins it). WTP above the cap without a fact. |
| High scores with missing evidence | `test_an_uncited_factor_is_capped_at_2_and_flagged` | An end-to-end case: an opportunity with ≤ 2 supported facts must be `false_positive`, whatever the judges say. |
| Unstable scores across judgments | `test_median_of_three_judges_and_the_spread` | Spread > 2 must force `low` confidence, and a single judge must never be `high`. Both are implemented, but only partly pinned. |
| Weak opportunities passing quality gates | Gate 1/Gate 2 rule tests | A combined fixture: a cluster that passes Gate 1 only through snippet duplicates, or only through partial facts. |
| Benchmark reproducibility | `test_cache.py` covers replay of single calls | **No test replays a whole pipeline from a frozen fixture with zero provider calls.** |

## 6. Smallest plan that closes the most important gaps

Ordered by value per effort. Each step is a small, separately testable change. Nothing here adds
agents or infrastructure.

**Step 1: Portable fixtures (closes the "no offline benchmark" gap).**
`signalforge fixture export <run> --out <dir>` writes the run's plan, a manifest (commit, pack
version, prompt refs, counts, sha256) and the cache entries the run used as gzipped JSONL:

- LLM entries, from `llm_calls` (model, prompt ref, input hash)
- search entries, from the run's queries
- page entries, from the documents' canonical URLs

`fixture import <dir>` loads them. A test exports a fake run, truncates the DB, imports, replays the
pipeline in `replay` mode, and asserts zero provider calls and identical stage metrics.
*Limitation:* replay only reproduces runs where every LLM input is unchanged. A prompt or upstream
code change causes cache misses, so the changed stages must be re-recorded (live cost).

**Step 2: Benchmark manifest and split.** `backend/evals/benchmark/v1.yaml` lists the scenarios
(industry, plan, fixture, sha256, split):

- **dev:** the six existing runs (logistics, accounting, customs/export, e-commerce, construction,
  insurance). All were tuned on (R12).
- **held-out:** new recordings not inspected during tuning: manufacturing, export/wholesale and one
  more. This costs about $0.85 per run live (measured M7 median), roughly $2.50 in total.

**Step 3: Label schema with stable keys (closes R7, prerequisite for human metrics).**
`evals/label_schema.yaml` (versioned) covers:

- problem supported by evidence
- source pair independent
- claim entailed by its excerpts
- buyer identifiable
- WTP supported
- opportunity worth investigating
- category and confidence justified

It reuses the `labels` table (`target_type` is a free string, no migration). The `note` holds a
stable content key (canonical URL + quote hash, normalised claim hash) so labels match across
re-runs. Add `label claims` and `label sources` with stratified sampling. Import the proxy TSVs with
`labeler=proxy:claude` so model and human labels are never mixed. Document the limitations: one
founder, no inter-rater agreement until a second labeler exists, and anchoring on the report text.

**Step 4: `signalforge benchmark report`.** Extends `reporting/evaluation.py`. It emits every metric
from §3 in three tiers (deterministic / LLM-judged / human), each with `n`, its definition, and
`unavailable: <reason>` when the data is missing. It also runs the exit checks and compares quality,
cost and latency against a stored baseline JSON. Metric definitions go in `docs/eval/metrics.md`.

**Step 5: Regression tests for §5.** Deterministic, using `tests/fakes.py`. Tests for real bugs
(R1, R2) are added first as failing tests, then fixed in their own small change. Fixes go in only
when the bug is confirmed.

**Then (Phase 5): controlled optimisation.** Only after Steps 1–4 produce a baseline. Each change
gets a hypothesis, a baseline number, an evaluation method and a cost delta, measured on dev and
confirmed once on held-out.

### Decisions needed before Step 1

1. **Fixture storage.** Fixtures contain scraped third-party text (R13). Recommendation: git-ignore
   the fixture data, commit only the manifest with checksums, and share fixtures out of band.
2. **Live spend for held-out recordings:** about $2.50 plus Serper credits, needed for Step 2.
3. **Human labeling time.** The minimum for useful human metrics is about 100 claims, 50 sources
   and 25 opportunities, roughly 2–3 hours of founder time **(estimate)**.
