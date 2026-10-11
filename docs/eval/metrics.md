# Research-quality benchmark: how to run it and what each metric means

The benchmark measures the quality, cost and latency of research runs that have been frozen
(recorded once, then kept unchanged). It runs offline: it never calls a search provider, a web page
or a model. The code is in `backend/src/signalforge/reporting/benchmark.py`. The scenarios are
listed in `backend/evals/benchmark/v1.yaml`.

## Run it

```sh
cd backend
# 1. Create an empty database and migrate it. Rows keep their ids, so never use the dev database.
docker exec signalforge-db-1 psql -U signalforge -d postgres -c "create database signalforge_bench"
export DATABASE_URL=postgresql+psycopg://signalforge:signalforge@localhost:5433/signalforge_bench
.venv/Scripts/alembic upgrade head
# 2. Import the fixtures. The data files are git-ignored and shared out of band.
for f in evals/fixtures/*/; do .venv/Scripts/signalforge fixture import $f; done
# 3. Run the benchmark. --replay also re-runs each plan in replay mode. --baseline compares
#    against an earlier benchmark.json.
.venv/Scripts/signalforge benchmark run --replay --out reports/benchmark/<name> \
    [--baseline reports/benchmark/baseline/benchmark.json]
```

To freeze a new run, use `signalforge fixture export <run> --out evals/fixtures/<id>` against
the database that holds the run. Then add the run to the manifest. Commit the `manifest.json`;
it holds counts, prompt versions, the commit and the data's sha256. Do not commit
`data.jsonl.gz`: it contains scraped third-party text that can include personal data (KVKK).

## How to read the output

Each metric has a **tier**:

| Tier | Source | Trust |
|---|---|---|
| `deterministic` | Computed by code from stored rows and cached pages | Exact for what it counts |
| `llm_judged` | Verdicts the pipeline's own cheap model gave (entailment) | Never audited: its precision is unknown |
| `proxy_label` | Labels written by a model standing in for the founder (`proxy:*`, today Claude in `evals/benchmark/proxy-verdicts-m7.tsv`) | A model judging a model; it is not ground truth and probably more lenient than a founder (not measured) |
| `human_label` | Labels written by people in the `labels` table | Reference judgments, with the limits listed in `evals/label_schema.yaml` |

How values are shown:

- **Shares** show `value (n)`. They are pooled per split by adding numerators and denominators.
- **Counts** are added across scenarios.
- **Costs per unit and the judge range** are pooled as the median over scenarios.
- **`n/a`** means the data cannot support the metric. The reason is listed in the report. The
  benchmark never substitutes 0 for a missing value.

**Splits.** No scenario is a clean held-out. All six runs were tuned on during M7.
`holdout` (customs-export, construction) is held out *from now on*: don't inspect its outputs
while tuning, and report it separately. A real held-out needs new recordings, which cost live
spend.

## Metric definitions

### Evidence integrity (deterministic)

| Metric | Definition |
|---|---|
| `quote_pass_rate` | Quotes that passed verification (exact or fuzzy) ÷ quotes proposed, from `extract` metrics. |
| `quote_fuzzy_share` | Fuzzy matches ÷ verified quotes. A fuzzy match is a near-verbatim match at partial ratio ≥ 95. |
| `quote_integrity` | Stored text excerpts whose `quote` equals the cached page text at `char_start:char_end` ÷ excerpts that could be checked. |
| `quote_integrity_unchecked_share` | Excerpts that could not be checked (snippet excerpts, or the page is not in the fixture) ÷ all excerpts. |
| `duplicate_collapse_rate` | (documents − independent sources) ÷ documents, from `dedupe`. |
| `dedupe_unchecked_share` | Documents that dedupe never compared ÷ documents. These are snippet-only documents or pages missing from the cache. A syndicated copy among them counts as an independent source (audit R1/R2), so a high value means the source counts are less trustworthy. |
| `assumption_sourced_share` | Assumption claims with `meta.sourced` ÷ assumption claims. |
| `sourced_assumption_url_and_date_share` | Sourced assumptions that have both a `source_url` and an `as_of` ÷ sourced assumptions. |
| `price_dated_share` | Competitor price rows that have `observed_at` and a `claim_id` ÷ price rows. `observed_at` is the page's fetch date, not the date the price was valid (audit R9). |
| `exit_check_violations` | The total from `check_gap_matrices`, `check_buyer_roles`, `check_economic_models` and `check_score_cards`. `check_report` is not run: report files are not part of a fixture. |

### Funnel, failures, cost and latency (deterministic)

| Metric | Definition |
|---|---|
| `stages_completed`, `stage_failures` | Latest `StageRun` per stage, by status. |
| `documents`, `signals`, `clusters`, `shortlisted`, `opportunities`, `gate2_passed` | Stage yields, from `StageRun.metrics`. |
| `cards_<category>` | Score cards by category. |
| `cost_usd` | One clean pass: the LLM cost of each stage's latest `StageRun`, plus live searches × `search.usd_per_query`. This is the same rule as `signalforge evaluate`. It undercounts stages that were re-run from the cache (plan §13 M7 open item d). |
| `llm_usd`, `live_searches`, `wall_min` | The parts of `cost_usd`, and the sum of stage durations. |
| `cost_per_gate2_survivor_usd` | `cost_usd` ÷ opportunities that passed Gate 2. |

### Claim support and stability (LLM-judged)

| Metric | Definition |
|---|---|
| `fact_checked_share` | Fact claims that have an entailment verdict ÷ fact claims. Most signal facts are never checked. |
| `fact_supported_share`, `fact_partial_share`, `fact_failed_share` | Verdicts ÷ checked facts. `failed` means `not_supported` or `contradicted`. |
| `cited_fact_*_share` | The same, over the facts that score cards cite. |
| `price_fact_supported_share`, `price_fact_failed_share`, `price_fact_unchecked_share` | Entailment of the fact behind each competitor price row. `supported` counts supported or partial. |
| `attractiveness_judge_range_median` | For cards scored by several judges (the top `k_judge_top_n`): the attractiveness each judge alone would give (code factors are shared), then max − min. The value is the median over cards, in points on the 0–100 scale. |
| `factor_spread_ge2_share` | Multi-judge cards where some model factor has a spread of ≥ 2 levels ÷ multi-judge cards. |

Judge stability is measured *between* the k judges of one scoring pass, whose inputs differ only
in `judge_index`. Re-running the scorer with identical inputs returns cached answers, so a re-run
can never show instability.

### Commercial usefulness (proxy labels)

| Metric | Definition |
|---|---|
| `precision_at_3_proxy` | Among the 3 highest-attractiveness scored cards that have a proxy label: `investigate` ÷ labelled. |
| `precision_at_3_lenient_proxy` | The same, counting `investigate` or `maybe`. |
| `false_positive_rate_<category>_proxy` | Cards of that category labelled `no` ÷ labelled cards of that category. `strong` is unavailable while no run produces a strong card (audit R8). |
| `investigate_share_confidence_<level>_proxy` | `investigate` ÷ labelled cards at that confidence level. This is the input for calibration. It is too small to be one yet. |
| `cost_per_investigate_proxy_usd` | `cost_usd` ÷ opportunities labelled `investigate`. This is the "cost per useful opportunity" metric, on proxy labels. |

### Human labels

The benchmark counts the human labels on each run. These metrics stay `n/a` until the labels
described in `backend/evals/label_schema.yaml` exist, with at least 20 per target type:

- claim support precision
- independent-source precision
- problem discovery precision and recall
- Precision@3
- false-positive rate of strong cards
- confidence calibration
- competitor fact accuracy

### Replay (`--replay`)

`--replay` creates a new run from each scenario's stored plan and runs it in replay mode.

- **Reproduced** counts the stages whose research metrics equal the recording's. Cache counters
  and lists are excluded from the comparison, because the lists carry run-specific ids.
- **Stopped at** names the first stage whose LLM input is not in the recording. Its prompt,
  config or upstream code changed since the recording, so the recording is stale from that
  stage on.
- A mismatch in a stage that did run means current code computes something different from
  recorded inputs. That is what a deterministic regression check should catch.

Known limits:

- A transient fetch or search failure is not cached, so replay reproduces it as a
  `network_error` or a failed search. A recorded `http_error` 5xx therefore shows up as a
  `fetch: by_status` mismatch.
- Replay cannot re-run stages beyond the frontier without new recordings (`record` mode, live
  cost).

## Baseline (2026-10-11, fixtures of runs 22, 30–34, code `4837e35` + working tree)

These are measured values from `reports/benchmark/baseline/benchmark.md`. They are not targets.

- **Quotes:** quote integrity is 100% of 3,399 checkable excerpts, and the pass rate is 99.4%.
- **Dedupe coverage:** dedupe never compared 40% (dev) and 57% (holdout) of documents.
- **Entailment of checked facts:** about half are `partial` (dev 39%, holdout 51%). Only 34% and
  46% of facts are checked at all.
- **Score stability:** judges rarely disagree by ≥ 2 levels (0 of 22 cards). The median
  attractiveness range between judges is 1.9 / 1.3 points.
- **Usefulness:** Precision@3 (proxy) is 10% on dev and 50% on holdout. The proxy false-positive
  rate of `interesting` cards is 55% on dev and 33% on holdout.
- **Cost per Gate-2 survivor:** the median is $0.14 (dev) and $0.11 (holdout).
- **Replay:** every scenario stops early because its recordings are stale (logistics at
  `cluster`, accounting at `verify`, e-commerce and insurance at `competitors`). Customs and
  construction stop at `buyers` only because the working tree has an uncommitted buyers loop.
  Gate 1 traces differ for runs 31 and 34, because `min_software_fit_share` was added after
  those runs were recorded.
