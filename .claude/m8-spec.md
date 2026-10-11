# M8 — API & UI: spec

> The contract between backend (`api/schemas.py`) and frontend (`frontend/src/lib/api.ts`).
> Change both sides and this file together. Status and exit: [plan.md §13 M8](plan.md).

## Ground rules (all sessions)

- Python: use `backend/.venv` directly (`backend/.venv/Scripts/python -m pytest`, `... -m ruff`). **No uv.**
- Postgres: docker compose, host port 5433. Tests use the `signalforge_test` DB (see `tests/conftest.py`).
- Do **not** commit. The lead reviews and commits. Backend touches only `backend/`, frontend only `frontend/`.
- Match the surrounding code style (comment density, naming, typed Pydantic/SQLAlchemy 2). No new
  dependencies unless the spec says so; ask the lead first.
- **No live research runs** (they cost money). Use existing runs in the dev DB (22, 30–34 have full
  results) and fake stages/providers in tests.
- Planner (`plan` stage) is **out of scope**: the form edits a plan directly, seeded from example plans.
- Privacy (plan §2 #17): never return full page text or author hashes from the API. Excerpts + URLs only.
- When done, report to the lead with: files changed, test/lint output, open questions.

## Backend

### B1. Job table + worker (plan §11 item 11: Postgres job table, `SKIP LOCKED`, no Redis)

- Model `Job` (`jobs`) + Alembic migration: `id`, `run_id` (FK cascade, indexed), `created_at`,
  `status` (`queued | running | completed | failed | cancelled`), `args` JSONB
  (`{resume, from_stage, until, agent}`), `worker_id`, `started_at`, `heartbeat_at`, `finished_at`,
  `error`. Index on `(status, id)`.
- `pipeline/jobs.py`:
  - `enqueue(db, run_id, args) -> int` — refuses (raises) if the run already has a queued/running job;
    validates args like the CLI `run` command (agent excludes from/until/resume; stage/agent names valid).
    Share the validation with `cli.py` instead of duplicating it.
  - `claim_next(db, worker_id) -> Job | None` — `SELECT … FOR UPDATE SKIP LOCKED` oldest queued.
  - `run_job(...)` — builds a `RunContext` for the run's pack and calls `run_pipeline(rc, STAGES, …)`;
    marks the job completed/failed (error text). Stages and context factory injectable for tests.
  - Heartbeat: a thread updates `heartbeat_at` every ~30 s while a job runs. When claiming, a `running`
    job whose heartbeat is older than `jobs.stale_after_s` (config, default 300) is marked `failed`
    ("worker lost") and its run set to `failed` (resumable).
  - `cancel(db, job_id)` — only for queued jobs.
- CLI: `signalforge worker [--once] [--poll-s N]`. `--once` processes at most one job and exits.
- Config: `jobs:` block in `defaults.yaml` (`poll_s`, `heartbeat_s`, `stale_after_s`).

### B2. API (FastAPI, all under `/api`)

Response models in `api/schemas.py`; routes in `api/routes/*.py`. DB access through a dependency
(`api/deps.py`, returns a `sessionmaker`) so tests can override it with the `db` fixture.
Times are ISO 8601 UTC; money is a JSON number (USD).

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/agents` | | `[{name, description, stages: [str]}]` in run order |
| GET | `/api/plans/examples` | | `[{id, title, plan}]` from `backend/examples/**/*.plan.yaml` (id = path relative to examples without `.plan.yaml`, e.g. `m7/customs`; title = first comment line or id; skip invalid files) |
| POST | `/api/runs` | `ResearchPlan` JSON; query `start: bool = true` | 201 `RunDetail`. Creates the run (`create_run`); enqueues a full-pipeline job if `start`. Unknown pack → 422 |
| GET | `/api/runs` | `limit` (default 50) | `[RunSummary]`, newest first |
| GET | `/api/runs/{id}` | | `RunDetail` (404 if missing) |
| POST | `/api/runs/{id}/jobs` | `{resume?: bool, from_stage?: str, until?: str, agent?: str}` | 201 `Job`; 409 if a job is queued/running; 422 invalid args |
| POST | `/api/jobs/{id}/cancel` | | 200 `Job`; 409 unless queued |
| GET | `/api/runs/{id}/opportunities` | | `[OpportunityCard]`, attractiveness desc, nulls (knocked out) last |
| GET | `/api/opportunities/{id}` | | `OpportunityDetail` |
| GET | `/api/runs/{id}/report/{fmt}` | `fmt ∈ json, md, html` | the file written by `report` (`<report.out_dir>/run-<id>/report.<fmt>`) with the right content type; 404 if absent |
| GET | `/api/runs/{id}/sources` | filters below | `SourcePage` |

Shapes (`?` = nullable):

```
Job            {id, run_id, status, args, created_at, started_at?, finished_at?, error?}
RunSummary     {id, created_at, updated_at, status, country, industry, problem_area?,
                budget_usd, spent_usd, current_stage?, stages_done, stages_total,
                latest_job?: Job, opportunity_count}
StageOut       {name, status: pending|running|completed|failed, started_at?, finished_at?,
                cost_usd, error?, metrics}
AgentProgress  {name, description, status: pending|running|completed|failed|partial,
                cost_usd, started_at?, finished_at?, stages: [StageOut]}
RunDetail      RunSummary + {plan, pack_id, pack_version, agents: [AgentProgress],
                jobs: [Job] (newest first), report_formats: [str] (which report files exist)}
OpportunityCard {id, run_id, problem_id, problem_name, segment, solution_angle,
                 gate2_status?, knockouts, category?, attractiveness?, confidence?,
                 founder_fit?, evidence_strength?, value_usd_month?: {low, high}}
ClaimOut       {id, kind, statement, entailment?, derived_from: [int], excerpts: [ExcerptRef]}
ExcerptRef     {id, quote, translation?, verified, url, domain, source_category?, quality_tier?}
OpportunityDetail OpportunityCard + {
                 problem: {id, name, description, independent_source_count, evidence_strength,
                           source_category_mix, claim_id?},
                 buyer_roles, accessibility, market_breadth, economic_model, wtp_signals,
                 score: {factors, rule_trace, experiment?}?,
                 gap_matrix: {dimensions, competitors: [{id, name, url}], cells, gaps}?,
                 claims: {"<id>": ClaimOut}}   # every claim id referenced anywhere in the payload
SourcePage     {total, offset, limit, items: [SourceItem], facets}
SourceItem     {document: {id, url, domain, title?, source_category?, quality_tier?,
                           published_at?, snippet_only, origin?},
                excerpts: [{id, quote, translation?, verified,
                            signals: [{id, type, statement, first_hand, cluster_ids: [int]}]}]}
facets         {categories: {name: n}, tiers: {name: n}, signal_types: {name: n},
                clusters: [{id, name, n}]}
```

- Agent/stage status: a stage with no StageRun is `pending`; agent = `completed` if all stages
  completed, `running`/`failed` if any is, `partial` if some completed, else `pending`.
- JSON blobs (`buyer_roles`, `economic_model`, `factors`, `cells`, …) are passed through as stored;
  `value_usd_month` is derived from the economic model's monthly value range.
- `claims` is collected by walking the payload for keys `claim_id`, `claim_ids`, `*_claim_id`,
  `*_claim_ids`, plus one level of `derived_from` of those claims (so an inference chip can show
  its sources). All JSON object keys that are ids (`claims`, gap-matrix `cells` competitor ids) are
  strings.
- Sources filters: `category`, `tier`, `domain`, `signal_type`, `cluster_id`, `q` (substring of quote,
  Turkish-aware via `text.py` where practical), `offset` (0), `limit` (50, max 200). Only documents
  with ≥1 excerpt in the run. Facets are computed over the run, not the filtered page.

### B3. Tests

`tests/test_jobs.py` (enqueue validation, claim with SKIP LOCKED, stale reclaim, cancel, run_job with
fake stages) and `tests/test_api.py` (every endpoint, incl. 404/409/422, on seeded rows). Full suite
and `ruff check` + `ruff format --check` green.

## Frontend

Next.js 16 App Router, TypeScript, Tailwind (already set up). No UI libraries. Server components fetch
`BACKEND_URL` (see `src/lib/api.ts`); client components call `/api/*` (proxied). Types for every shape
above in `src/lib/api.ts`. `pnpm lint` and `pnpm build` must pass. Light + dark mode, usable at phone width.

| Route | Content |
|---|---|
| `/` | Runs table (id, industry, status badge, `stages_done/stages_total`, spent / budget, created, latest job status) + compact setup status (existing health check) + "New research". Polls every 5 s while any run has a queued/running job. |
| `/runs/new` | Research form: template picker (`/api/plans/examples`), request fields (country, industry, problem area, target customer, business model, founder fields, report language, budget), submarket editor (name, name_tr, terms, actors, notes; add/remove; lists as one item per line), research questions / buyer hypotheses / avoid (one per line). Submit → `POST /api/runs` → redirect to `/runs/{id}`. Show 422 field errors. |
| `/runs/[id]` | Header (status, spend/budget, latest job, actions: Resume, Re-run agent ▾, Cancel queued). Progress: one row per agent with its stages (status, duration, cost, error; metrics expandable). Polls every 3 s while a job is queued/running. Opportunity cards (category, attractiveness, confidence, founder fit, segment, angle, value range, knock-out reasons). Links: report (html/md/json when present), sources. |
| `/opportunities/[id]` | Problem + evidence strength; score factors (level, justification, cited claims); rule trace; economic model; buyer roles + channels; gap matrix table; experiment. Every claim id renders as a chip that expands to its statement, kind, entailment and quotes with source links. |
| `/runs/[id]/sources` | Source explorer: facet filters + search in URL search params, paginated documents with their excerpts (quote, translation, verified) and signals (type, first-hand, cluster). |

Render stored JSON blobs defensively (fields can be missing on older runs). Read
`backend/src/signalforge/domain/*.py` and `db/models.py` comments for blob shapes.

## Exit (M8)

1. Backend tests, ruff, `pnpm lint`, `pnpm build` green.
2. With `signalforge serve` + `signalforge worker` + `pnpm dev`: create a run from a template in the
   browser, see a job queued → picked up, progress update per agent; re-run `report_writer` on run 22
   from the UI (cache hit, ≈$0) and watch it complete.
3. Run 22 and 31: cards, detail page (every claim chip resolves to quotes + URLs), source explorer
   filters, report links all work.
