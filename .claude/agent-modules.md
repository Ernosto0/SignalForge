# SignalForge — Agent modules (build spec)

> Companion to [plan.md](plan.md). plan.md defines the pipeline as 14 **stages**. This file groups
> those stages into 9 **agents**: one module per research job, each with a written contract.
> Agents are a layer on top of stages, not a replacement. The runner, `StageRun` bookkeeping,
> resume/`--from`, the cache and the budget cap stay as they are. If this file and plan.md disagree,
> fix one of them in the same change (same rule as plan.md).
>
> **Milestones.** Milestones and their exit criteria are defined in
> [plan.md §13](plan.md#13-milestones-each-with-an-exit-criterion); each milestone there has an
> *Agents:* line linking back here. [§11](#11-build-order-by-milestone) below is the same mapping
> from the agent side, with the shared code and migrations each milestone needs.

---

## 0. How agents fit the existing code

### 0.1 What an agent is

An agent is a named, ordered group of stages with one research job, a written input/output contract
and its own config block. Stage code stays in `pipeline/stages/` (plan §12). The agent module holds:

- the `Agent` definition (name, stages, description),
- logic that only that agent uses (e.g. the bounded tool-use loop for `evidence_validator` and
  `competitor_research`),
- a module docstring with the contract below, kept in sync with this file.

```python
# src/signalforge/agents/base.py
from dataclasses import dataclass

from signalforge.pipeline.runner import Stage


@dataclass(frozen=True)
class Agent:
    name: str                    # module name, e.g. "problem_discovery"
    description: str             # one line, shown by `signalforge agents`
    stages: tuple[Stage, ...]    # run order within the agent

    @property
    def first(self) -> str:
        return self.stages[0].name

    @property
    def last(self) -> str:
        return self.stages[-1].name
```

```python
# src/signalforge/agents/registry.py — re-exported lazily by agents/__init__.py, because stage
# modules import shared agent code (agents/loop.py) and agent modules import the stages.
from signalforge.agents.buyer_research import BUYER_RESEARCH
from signalforge.agents.competitor_research import COMPETITOR_RESEARCH
from signalforge.agents.evidence_validator import EVIDENCE_VALIDATOR
from signalforge.agents.monetization import MONETIZATION
from signalforge.agents.opportunity_scorer import OPPORTUNITY_SCORER
from signalforge.agents.problem_discovery import PROBLEM_DISCOVERY
from signalforge.agents.report_writer import REPORT_WRITER
from signalforge.agents.source_discovery import SOURCE_DISCOVERY

# `planner` is not in this tuple: it runs before a run exists (human checkpoint, §1).
AGENTS = (
    SOURCE_DISCOVERY,
    PROBLEM_DISCOVERY,
    EVIDENCE_VALIDATOR,
    COMPETITOR_RESEARCH,
    BUYER_RESEARCH,
    MONETIZATION,
    OPPORTUNITY_SCORER,
    REPORT_WRITER,
)
```

`pipeline/stages/__init__.py` then derives the stage order from the agents, so there is still one
source of truth for ordering:

```python
STAGES = tuple(stage for agent in AGENTS for stage in agent.stages)
```

### 0.2 CLI

- `signalforge agents` lists the agents with their stages and each one's status for a run.
- `signalforge run --run <id> --agent <name>` maps to `run_pipeline(from_stage=agent.first,
  until=agent.last)`, so an agent re-runs as a unit and replaces its own outputs.
- `signalforge run --resume` and `--from <stage>` work as they do now.
- `signalforge ledger --run <id> --by agent` sums `StageRun.cost_usd` over each agent's stages.

### 0.3 Rules every agent follows

1. **Idempotent stages.** Each stage deletes its own outputs for the run before writing new ones.
   It never deletes rows owned by another stage.
2. **Prompts** live in `prompts/<stage>.md` with an `id` / `version` header and are loaded with
   `load_prompt()`. Bump the version whenever the wording changes; it is part of the LLM cache key.
3. **LLM calls** go only through `ctx.llm.parse(tier, prompt, input, Schema, stage=...)`, so every
   call is cached, priced, written to the ledger and checked against the budget. Put the stable
   instructions and pack context first and the per-item content last, so prompt caching works.
4. **Evidence integrity (plan §7).** A model can propose a fact. It becomes a `fact` Claim only when
   a verified excerpt from a fetched document backs it. Model background knowledge is only a search
   seed.
5. **Caps and thresholds** live in `config/defaults.yaml`, under a block per stage, with a typed
   `<Name>Defaults` model in `config.py`. Nothing is hard-coded in stage code.
6. **Metrics.** Every stage returns `StageResult(metrics=...)` with the counts named in its spec, so
   `signalforge status` and the eval reports can show them.
7. **Tests** use fake providers, never the network. Follow the existing fakes: `FakeLLMClient`
   (tests/test_query_gen.py), `FakeLLM` / `FakeSearch` (tests/test_collection.py), and `FakeFetcher`
   (tests/test_cache.py). Each agent gets `tests/test_<agent>.py`.

### 0.4 Overview

Milestones refer to [plan.md §13](plan.md#13-milestones-each-with-an-exit-criterion).

| # | Agent module | Stages | LLM tier | Loop? | Milestone | Status |
|---|---|---|---|---|---|---|
| 1 | `planner.py` | `plan` (outside a run) | analysis | no | M1 | to build |
| 2 | `source_discovery.py` | `query_gen`, `search`, `triage`, `fetch`, `dedupe` | fast | no | M1 (`query_gen`), M2 (rest) | ✅ stages built; add agent wrapper |
| 3 | `problem_discovery.py` | `extract`, `cluster` | fast / analysis | no | M3 | to build |
| 4 | `evidence_validator.py` | `shortlist` (Gate 1), `verify` | fast / analysis | **yes** (`verify`) | M3 (`shortlist`), M4 (`verify`) | ✅ built (M4 live run pending) |
| 5 | `competitor_research.py` | `competitors` | analysis | **yes** | M4 | ✅ built (M4 live run pending) |
| 6 | `buyer_research.py` | `buyers` | analysis | no | M5 | to build |
| 7 | `monetization.py` | `monetization` (Gate 2) | analysis + code | no | M5 | to build |
| 8 | `opportunity_scorer.py` | `score` | analysis + code | no | M5 | to build |
| 9 | `report_writer.py` | `report` | synthesis | no | M6 | to build |

Shared code these agents use (new files, under plan §12's layout):

| File | Used by | Contents |
|---|---|---|
| `agents/loop.py` | evidence_validator, competitor_research | bounded structured-output tool loop (§0.5) |
| `evidence/quotes.py` | problem_discovery, evidence_validator, competitor_research, buyer_research | verbatim / fuzzy quote verification (plan §7.1) |
| `evidence/documents.py` | fetch, verify, competitors, buyers | `store_page(ctx, page, origin) -> Document`, moved out of `pipeline/stages/fetch.py` (`page_document`, `snippet_document`) |
| `evidence/extraction.py` | extract, verify, competitors, buyers | `extract_document(ctx, doc, text, context) -> list[ExtractedSignal]` plus excerpt/signal/claim writing |
| `evidence/independence.py` | shortlist, verify | source components from dedupe groups and author hashes |
| `evidence/strength.py` | shortlist, verify, score | deterministic evidence strength (plan §8.1) |
| `evidence/entailment.py` | verify, competitors, report | cheap-model entailment check of fact claims: `entail_pending`, `failed`, `clear_entailment` |
| `evidence/claims.py` | all analysis agents | helpers: `add_fact`, `add_inference`, `add_hypothesis`, `add_assumption`, `claim_table(run_id, ids)` |
| `evidence/clusters.py` | competitors and later | read a cluster after verify: `cluster_signal_ids`, `current_strength`, `current_sources`; `pick_quotes` |
| `evidence/gaps.py` | competitors, `signalforge gaps` | `check_gap_matrices`: the M4 exit rule over stored matrices |
| `queries.py` | query_gen, agents/loop.py | `clean_query` and `Deduper` (moved out of the query_gen stage so the loop can import them without a cycle) |

### 0.5 The bounded tool loop (`agents/loop.py`)

`LLMService.parse` returns structured output only, so the loop is a **manual loop over structured
actions**, not provider-side function calling. Each step, the model sees the goal, what it has
collected so far and how much budget is left, and returns one action. Our own cached `search` /
`fetch` providers carry the action out, so results go through the cache, the source registry and
quote verification (plan §4).

```python
class LoopAction(BaseModel):
    action: Literal["search", "fetch", "finish"]
    query: str | None = None     # search: words only, market language, ≤ 10 words
    site: str | None = None      # search: one offered registry domain, or null
    hit_id: int | None = None    # fetch: the [h<id>] of a hit shown in this loop's history
    reason: str                  # English, ≤ 20 words; stored for debugging

def run_loop(
    ctx: RunContext,
    *,
    stage: str,
    prompt: Prompt,
    goal: str,                         # serialised task context (problem, what is needed)
    budget: LoopDefaults,              # max_steps / searches / fetches, max_observation_chars, …
    allowed_domains: Sequence[str],    # registry domains the model may use as `site:`
    known_urls: Collection[str],       # canonical URLs already in the run's evidence (rejected)
    known_queries: Collection[str],    # query strings the run already searched (rejected)
    on_page: Callable[[LoopPage], str],  # agent-specific processing; returns a note the model sees
) -> LoopTrace: ...
```

The model fetches by **hit id**, not URL, so a URL from model memory cannot even be expressed.
Hits on `snippet_only` registry domains are "read" from their search snippet, without a download.
Each observation shows a hit's domain, registry category and tier, and flags `already in
evidence`, `off-site` (the `site:` hint was ignored by Google, see M1) and `snippet only`.

Mechanical guards in `run_loop`:
- reject a fetch of a hit id this loop has not shown, of a known URL, or of a URL already read,
- clean the query with `queries.clean_query` (operators drop the query; `site:` only for an offered
  domain) and reject Turkish-aware near-duplicates (`queries.Deduper`) and run-known queries,
- stop on `finish`, when steps run out, when searches and fetches both run out, or after
  `max_rejections_in_row` rejected actions in a row,
- shorten the oldest observations to one line once the history exceeds `max_observation_chars`.

**The loop never writes to the database.** It returns a `LoopTrace` (searches with hits, pages
read, failed fetches, every action with its outcome, rejections, stop reason). Problems run in
parallel, so the stage stores the traces afterwards with one writer: `store_searches` (in
`agents/loop.py`) writes `Query` rows (`intent` = `verify` / `competitor`, `meta.origin = "loop"`,
`meta.problem_ids`) with their `SearchResult`s, and documents go through
`evidence/documents.store_document` (get-or-create by canonical URL, so two problems reading one
page share one document). Only pages that yield evidence become documents. `LoopTrace.summary()`
is stored per problem in the stage's metrics (and, for verify, in `verification.loop`).

---

## 1. `agents/planner.py` — Planner

**Job.** Turn a research request into an editable `plan.yaml`: submarkets as Turkish
practitioners name them, the terms and actors to search for, research questions and buyer
hypotheses. A person reviews and edits the file before a run is created (plan §2 row 14, the
cheapest quality lever in the system).

**Stages.** `plan` (`pipeline/stages/plan.py`). It runs **outside** `run_pipeline`, because no
`ResearchRun` exists until a person has approved the plan. LLM calls are logged with
`run_id = None`, `stage = "plan"`.

**CLI.**
```
signalforge plan --request request.yaml --out plan.yaml   # writes the draft for human review
signalforge run --plan plan.yaml                           # existing: creates the run, starts the pipeline
```

**Reads.** `ResearchRequest` (`domain/plan.py`); the market pack: `industries/<industry>.yaml`
(seed terms, seed submarkets), `pain_phrases`, `regulatory.mandates`, `sources` (only the
categories, so the model knows what kinds of sources exist).

**Writes.** A `plan.yaml` file on disk with a comment header saying it is a draft to review. Nothing
is written to the database.

**Model.** `analysis` tier, prompt `prompts/plan.md`, one call per request.
`max_output_tokens: 8000`.

**Schemas** (`domain/plan.py`, next to `ResearchPlan`):
```python
class PlanDraft(BaseModel):
    """Output of the `plan` prompt. Combined with the request to form a ResearchPlan."""
    submarkets: list[PlanSubmarket]          # existing model: name, name_tr, terms, actors, notes
    research_questions: list[str]            # English, 3–6
    buyer_hypotheses: list[str]              # English, 2–6, "<who> pays for <what> because <why>"
    avoid: list[str]                         # result types / topics to steer away from
    dropped_seed_submarkets: list[DroppedSeed] = []   # pack seeds left out, with a reason

class DroppedSeed(BaseModel):
    name: str
    reason: str
```

**Prompt content** (`prompts/plan.md`, v1):
- Role: plans market research that collects first-hand evidence of B2B pain in one industry of one
  country.
- Context: the request (industry, problem_area, target_customer, business_model, founder profile)
  and the pack's industry seeds.
- Instructions:
  - 4–8 submarkets that are distinct operational businesses, not product categories.
  - `name_tr` is what practitioners actually say.
  - `terms` are 6–15 concrete workflows, documents, systems and tools in Turkish (as in
    `examples/tr-logistics.plan.yaml`). Avoid ambiguous single words; M1 found that "planlama",
    "depo" and "nakliye" return noise.
  - `actors` are the roles and company types who do the work.
  - Keep every pack seed submarket, or list it in `dropped_seed_submarkets` with a reason.
  - If `problem_area` is set, every submarket must relate to it.
  - Buyer hypotheses must be falsifiable.
- No search operators, no English terms unless Turkish practitioners use the English word
  (e.g. "booking", "WMS").

**Algorithm.**
1. Load the request and its pack (`request.pack_id`). Fail if `request.industry` is not in
   `pack.industries`. Without industry seeds the plan would be generic.
2. Build the input: request JSON, then industry seeds, then mandate names.
3. Call the model → `PlanDraft`.
4. Validate and repair (no second LLM call):
   - Drop submarkets with duplicate names (case-insensitive) and keep the first.
   - Strip operators and quotes from terms. Turkish-aware dedupe of terms within each submarket
     (`text.tr_casefold`).
   - Drop terms longer than 5 words.
   - Check that `name_tr` and most terms are Turkish (`text.guess_language`); record any that are
     not as warnings.
   - Clamp the counts to the config ranges. If fewer than `min_submarkets` survive, fail with the
     warnings.
5. Build `ResearchPlan(request=..., **draft)`, which runs the existing validators, and write the
   YAML in a stable key order. The header comment lists the warnings and the dropped seeds, so the
   reviewer sees them.

**Config** (`config/defaults.yaml`):
```yaml
plan:
  min_submarkets: 4
  max_submarkets: 8
  min_terms: 6
  max_terms: 15
  max_term_words: 5
  max_output_tokens: 8000
```

**Metrics** (printed by the CLI; there is no StageRun): submarket count, terms per submarket,
warnings, dropped seeds, cost.

**Tests** (`tests/test_planner.py`): a fake LLM returns a draft with a duplicate submarket, an
operator in a term and an English term. The written plan has them fixed or warned about and loads
with `load_plan()`. An unknown industry fails. The output round-trips through `ResearchPlan`.

**Done when.** For TR logistics, a Turkish-speaking reviewer would change at most a few terms. The
plan generated from the request at the top of `examples/tr-logistics.plan.yaml` is about as good as
the hand-written one, judged by the reviewer and by `query_gen` yield in M2.

---

## 2. `agents/source_discovery.py` — Source discovery

**Job.** Find and download the pages most likely to hold first-hand evidence for the plan:
generate Turkish queries, search, triage results by title, snippet and source tier, fetch the kept
pages, and collapse duplicate documents.

**Stages.** Already built: `query_gen` → `search` → `triage` → `fetch` → `dedupe`
(`pipeline/stages/`). This agent only adds the wrapper:

```python
SOURCE_DISCOVERY = Agent(
    name="source_discovery",
    description="Plan → Turkish queries → SERP → triaged URLs → fetched, de-duplicated documents",
    stages=(QueryGen(), Search(), Triage(), Fetch(), Dedupe()),
)
```

**Reads.** `ResearchRun.plan`, the market pack, `config` blocks `query_gen`, `collect`, `triage`,
`fetch_stage`, `dedupe`.

**Writes.** `queries`, `search_results`, `url_candidates`, `documents` (full text in the page
cache), `independence_groups` (`near_dup` / `syndicated`).

**Software-shaped retarget (77f5b72, 2026-10-08).** `prompts/query_gen.md` is v6. The query mix
is aimed at recurring manual work that a small team's software can address:
- `intent_mix`: pain 0.55 / jobs 0.35 / regulatory 0.10.
- `pain_signal_mix`: complaint 0.30 / workaround 0.25 / wish 0.20 / tool_complaint 0.15 /
  price_signal 0.10.
- `tool_complaint` queries cover software products only, not carriers, banks or devices.
- Regulatory queries are only for obligations that create recurring paperwork.
- Workaround queries avoid tutorial and template words.

**Follow-ups found in M1/M2, to build in this agent:**
- Re-tune `pain_signal_mix` from the per-type yield of the market scan, and from the per-query
  yield that M3 measures. Yield = signals kept per
  query, joined through `url_candidates.query_ids` → `documents` → `excerpts` → `signals`. Add
  `signalforge yield --run <id>` with a breakdown by `meta.signal_type`, `meta.source_hint` and
  `meta.origin`.
- ✅ (M4) `page_document` / `snippet_document` moved from `pipeline/stages/fetch.py` into
  `evidence/documents.py`, next to the loop helpers (`loop_page_document`,
  `loop_snippet_document`, `store_document`), so the loop agents create documents the same way.

**Done when.** This is the M2 exit: ≥150 documents for TR logistics, with the duplicate collapse
rate and fetch success reported.

---

## 3. `agents/problem_discovery.py` — Problem discovery

**Job.** Read every document, pull out typed **signals** that are backed by verbatim quotes, and
group the signals into ≤25 **problem clusters**. This agent produces the problem landscape. It
does not decide which problems are well evidenced; that is `evidence_validator`.

**Stages.** `extract` (`pipeline/stages/extract.py`) → `cluster` (`pipeline/stages/cluster.py`).

### 3.1 `extract`

**Reads.** `documents` for the run, page text from `ctx.fetcher.cached(url)`, or the snippet for
`snippet_only` documents (stored on the `url_candidates` row). Also the plan (industry, submarkets,
actors) and the pack's signal-type definitions.

**Writes.** `excerpts`, `signals`, and one `fact` Claim per signal
(`supports=[excerpt_id]`, `stage="extract"`). `write_signals` stores each signal's software-fit
facts in `Signal.meta["fit"]` (`evidence/fit.fit_facts`).

**Model.** `fast` tier, prompt `prompts/extract.md`, one call per document chunk. Batch-eligible
under `--batch` (plan §10).

**Schemas** (`domain/evidence.py`):
```python
class ExtractedSignal(BaseModel):
    quote: str            # copied verbatim from the text, original language, ≤ 500 chars
    translation: str      # English
    type: SignalType
    actor: str | None     # role / company type exactly as stated ("nakliye firması sahibi")
    workflow: str | None  # the business process affected, English, ≤ 8 words
    statement: str        # English, one sentence, what the quote shows, no extrapolation
    first_hand: bool      # the writer describes their own work / company
    submarket: str | None # one of the plan's submarket names, or null
    author: str | None    # name/handle shown next to the quote, if any — hashed, never stored
    # Software-fit facts (77f5b72). Required in the model schema; the Python defaults serve tests only.
    recurring: bool               # the work or problem repeats (per order, file, day, month)
    manual_task: str | None       # what people do by hand, English, a few words
    data_kind: DataKind           # documents | messages | spreadsheets | forms | system_data | physical | none
    cause: Cause                  # own_process | tool | third_party | regulation | hardware | other

class ExtractedSignals(BaseModel):
    signals: list[ExtractedSignal]   # may be empty; empty is the right answer for most pages
```

**Software fit** (`evidence/fit.py`). The model states the facts, and a configurable rule decides.
That way the rule can be re-tuned without re-extracting. `software_fit(facts, cfg)` is true when:
- the signal recurs (if `require_recurring`),
- `data_kind` is in `data_kinds`, and
- `cause` is in `causes`.

Signals stored before the facts existed (`fit` is null) never fit.
```yaml
software_fit:
  require_recurring: true
  data_kinds: [documents, messages, spreadsheets, forms, system_data]
  causes: [own_process, tool, regulation]
```

**Prompt content** (`prompts/extract.md`, v4; `prompts/verify_extract.md` v3 has the same type,
exclusion and field docs):
- The four software-fit facts above, with definitions. `data_kind`: appointments, records,
  attendance and payroll data are `system_data`; `none` only when no information is handled.
- Signal types with one Turkish and one English example each (plan §5).
- Only B2B operational evidence counts. Consumer complaints, vendor marketing and SEO text give no
  signals. A vendor page can still give a `regulatory` signal if it quotes an official deadline.
- v4 (2026-10-09, after the proxy M3 review in `reports/m3-review-2026-10-09/`):
  - `labor_spend` needs a duty that names both what is handled and what is done to it. Not role
    summaries, physical or customer-facing duties, or a bare "… takibi".
  - `regulatory` needs an obligation in force (or with a fixed start date) that creates
    recurring work. Not one-off option deadlines, drafts, or explanations of a law.
  - Excluded:
    - how-to questions, unless they describe a problem in the writer's work;
    - court and authority decisions (KVKK), and law-firm, consultant or FAQ case write-ups.
      These give at most a `regulatory` signal with `first_hand=false`;
    - non-operational problems (sales, marketing, demand).
- The quote must be copied exactly (no fixing typos, no joining sentences from different places)
  and be the shortest span that supports the statement.
- `first_hand` is true only if the writer does the work themselves.
- At most `max_signals_per_doc` signals; prefer distinct workflows.
- Do not infer company size, prices or frequencies that the quote does not state.

**Algorithm.**
1. Select documents: everything in the run except the non-canonical members of `near_dup` /
   `syndicated` groups. Their quotes would only repeat evidence, and they would still count as one
   source.
2. Chunk the text: `max_chars_per_chunk` with `overlap_chars`, split at paragraph boundaries. Cap
   at `max_chunks_per_doc`, and prefer the first chunks (that is where trafilatura puts the main
   text).
3. Call the model per chunk, in parallel (`concurrency`).
4. Verify each quote with `evidence/quotes.py`:
   - Normalise both sides: NFKC, collapse whitespace, unify quotes and dashes, `tr_casefold`.
   - An exact substring match → `verified="exact"`, with `char_start` / `char_end` mapped back to
     the original text.
   - Otherwise `rapidfuzz.fuzz.partial_ratio ≥ fuzzy_min_ratio` → `"fuzzy"`, using the best
     alignment span.
   - Otherwise drop the signal and count it in `quote_failed`.
5. Drop quotes longer than `max_quote_chars` and signals whose quote duplicates another quote in
   the same document.
6. `author_hash = sha256(salt + tr_casefold(author))`, with the salt from `settings`. Fall back to
   the page-level author from `FetchedPage.author`. The raw name is never written.
7. Write `Excerpt` → `Signal` → `Claim(kind="fact", statement=signal.statement, supports=[excerpt.id])`.

**Config:**
```yaml
extract:
  max_chars_per_chunk: 12000
  overlap_chars: 500
  max_chunks_per_doc: 4
  max_signals_per_doc: 8
  max_quote_chars: 500
  fuzzy_min_ratio: 95
  concurrency: 8
  max_output_tokens: 6000
```

**Metrics:** documents read, chunks, signals proposed, `quote_exact`, `quote_fuzzy`, `quote_failed`,
pass rate, signals per document, first-hand share, type mix, signals per submarket.

### 3.2 `cluster`

**Reads.** `signals` for the run (id, type, submarket, actor, workflow, statement). It does **not**
read quotes, which keeps the input small and in English.

**Writes.** `problem_clusters` (`name`, `description`, `signal_ids`), plus one `inference` Claim per
cluster: "<actor> struggle with <workflow problem>", with `derived_from` = the cluster's signal fact
claims and `stage="cluster"`. Evidence fields stay empty; `shortlist` fills them.

**Model.** `analysis` tier, prompt `prompts/cluster.md`. If there are more than
`max_signals_per_call` signals, chunk them and run a merge call (`prompts/cluster_merge.md`).

**Schemas** (`domain/problems.py`):
```python
class ClusterDraft(BaseModel):
    key: str              # short slug, unique within the call
    name: str             # English, ≤ 8 words, names the problem, not a solution
    description: str      # English, 2–3 sentences: who, which workflow, what goes wrong
    signal_ids: list[int]

class ClusterDrafts(BaseModel):
    clusters: list[ClusterDraft]
    unassigned: list[int]   # signals that are not a B2B problem or fit no cluster (noise)

class ClusterMerge(BaseModel):
    merges: list[list[str]]   # groups of cluster keys from different chunks that are the same problem
```

**Prompt content** (`prompts/cluster.md`, v1):
- A cluster is one problem in one workflow, felt by one kind of actor. Do not group by signal type
  or by submarket alone.
- Every signal id appears exactly once, either in one cluster or in `unassigned`.
- Prefer fewer, sharper clusters. A cluster needs at least 2 signals; single signals go to
  `unassigned` unless they are `regulatory`.
- Names describe the pain ("Manual delivery-note reconciliation"), not a product ("AI irsaliye
  app").

**Algorithm.**
1. Load the signals and sort them by id (deterministic input).
2. If n ≤ `max_signals_per_call`: one call. Otherwise split them into chunks (grouped by submarket
   first, so related signals share a chunk), call once per chunk, then make one merge call over
   (key, name, description) of all chunk clusters and union the merged groups.
3. **Validate the assignment** (plan §6 guard): compute the missing ids, the duplicate ids and the
   unknown ids.
   - Unknown ids: drop them.
   - Duplicate ids: keep the cluster with the most other signals from the same document, otherwise
     the first one.
   - Missing ids: one repair call (`prompts/cluster_assign.md`, fast tier) assigns them to an
     existing key or to `unassigned`. If any are still missing, put them in `unassigned`.
4. If there are more than `max_clusters` clusters, merge the smallest ones into `unassigned` by
   signal count until the cap holds, and record this in the metrics. (Model-led merging is a later
   option, once we can see how often this happens.)
5. Write the clusters (excluding `unassigned`) and their inference claims. Store the unassigned
   signal ids in the metrics.

**Config:**
```yaml
cluster:
  max_signals_per_call: 400
  max_clusters: 25
  min_signals_per_cluster: 2
  max_output_tokens: 12000
```

**Metrics:** signals in, clusters, unassigned count / share, repair calls, ids repaired, size
distribution (min / median / max), clusters per submarket.

**Tests** (`tests/test_problem_discovery.py`):
- Quote verification: exact, fuzzy, rejected, Turkish casefold (`İ`/`i`, `I`/`ı`), offsets mapped
  back to the original text.
- An extracted author is hashed and never stored raw.
- Cluster validation handles duplicate, missing and unknown ids. The repair call is used. The
  chunk-and-merge path unions groups correctly.

**Done when.** This is the M3 exit: quote verification passes for ≥95% of kept excerpts, and ≥70%
of 50 labelled signals are real first-hand B2B pain (`signalforge label signals`).

---

## 4. `agents/evidence_validator.py` — Evidence validator

**Job.** Decide which problems have enough independent, good-quality evidence to research further.
Then strengthen or weaken each shortlisted problem with targeted second-round searches, and check
that the key fact claims are really supported by their excerpts.

Most of this agent is **deterministic code** (plan §2 row 9). The LLM is used only to choose
verification searches (in the bounded loop) and for entailment (cheap model). Real source, matched
quote, duplicate, independence and recency are all checked mechanically.

**Stages.** `shortlist` (`pipeline/stages/shortlist.py`) → `verify` (`pipeline/stages/verify.py`).
It also exports `entail_pending(ctx, claim_ids)`, which `report_writer` calls on facts from other
agents.

### 4.1 `shortlist` (Gate 1) — no LLM

**As built (M3).** Independence and strength are computed when clusters are written (`cluster`
stage, `cluster_stats`), from the `same_author` groups extract writes and the `near_dup` /
`syndicated` groups dedupe writes; `shortlist` only reads them and decides. The decision is stored
as `ProblemCluster.shortlisted` (bool) and `gate_trace` (JSON list of `{rule, value, threshold,
passed}`), not as the `status` / `gate` columns first planned. A shortlist re-run also clears
`verification` (verify must re-run after a new Gate-1 decision).

**Reads.**
- `problem_clusters` (`evidence_strength`, `independent_source_count`).
- Signals with their software-fit facts and their independence units, via
  `cluster.load_evidence` (`SignalItem.fit`).
- The `software_fit` config, which is part of the stage's input hash.

**Writes.** Per cluster: `shortlisted`, `gate_trace`; `verification = null`.

**How the inputs are computed.**
1. **Independence** (`evidence/independence.py`): union-find over document ids. Merge documents that
   share a `near_dup` / `syndicated` group and documents whose excerpts share an `author_hash`
   (`same_thread` grouping is not built). The number of independent sources for a cluster = the
   number of distinct components among its signals' documents.
2. **Evidence strength** (`evidence/strength.py`, plan §8.1). All components are in [0, 1]:
   ```
   count      = min(1, ln(1 + n_independent) / ln(1 + count_saturation))     # saturation 10
   diversity  = min(1, distinct source categories / diversity_saturation)    # saturation 4
   quality    = mean over components of max(tier_weight[doc.tier] × (0.5 if snippet_only else 1))
   recency    = mean over components of (1 if newest published_at ≤ 24 months old,
                                         0 if older, 0.5 if unknown)
   directness = share of the cluster's signals with first_hand = true
   strength   = 10 × Σ weight_k × component_k
   ```
   The weights live in the `strength` block of `config/defaults.yaml` (moves to
   `scoring/config.yaml` in M5). `Strength.components` holds every component, stored in
   `ProblemCluster.strength`, so reports can explain the number.
3. **Software-fit sources:** the independent sources with at least one software-fit signal
   (`evidence/fit.software_fit`), counted as distinct independence units after duplicate,
   same-author and same-quote collapse.
4. **Gate 1:** shortlisted when all three hold:
   - `evidence_strength ≥ min_strength`,
   - `n_independent ≥ min_independent_sources`,
   - `software_fit_sources ≥ min_software_fit_sources` (added in 77f5b72; 0 turns it off).

   Strong evidence of a problem that software can't touch is not an opportunity. This rule appears
   in `gate_trace` and in the landscape report. Among the clusters that pass, keep the top `max_shortlisted`
   by strength; any that pass but miss the cap get a failed `max_shortlisted` trace entry. Every
   cluster that fails is kept for the report's "insufficient evidence" list.

**Config:**
```yaml
shortlist:
  min_strength: 4.0              # provisional; calibrate on the first labelled runs
  min_independent_sources: 3
  min_software_fit_sources: 2
  max_shortlisted: 10
```

**Metrics:** clusters in, shortlisted, insufficient by rule (`failed_by_rule`: `min_strength` /
`min_independent_sources` / `min_software_fit_sources` / `max_shortlisted`),
strength distribution, duplicate collapse from author grouping.

### 4.2 `verify` — bounded loop + entailment

**Reads.** Shortlisted clusters, their extract signals and fact claims, the cluster's inference
claim, the plan, and the pack's source registry.

**Writes.** Everything below is replaced on a re-run; Gate 1's own columns are never touched.
- `queries` (`intent="verify"`, via `store_searches`) and `documents` (origin `verify`,
  `problem_id`) for pages that yielded evidence.
- `excerpts` (`stage="verify"`) → `signals` (`meta = {origin: "verify", problem_id, counter}`) →
  fact claims (`stage="verify"`). Counter-evidence is stored too (`counter: true`) but never
  counts. Evidence that a problem is already solved is evidence.
- `independence_groups` (`same_author`, `near_dup`, `syndicated`) that involve a verify document:
  verify documents are compared with the shortlisted clusters' collected documents.
- `Claim.entailment` (+ `meta.entailment_note`, `meta.entailment_by = "verify"`).
- `ProblemCluster.verification` (JSON): `passed`, `signal_ids_added`, `counter_signal_ids`,
  `excluded_signal_ids` (facts that failed entailment), `document_ids_added`,
  `strength_before` / `strength_after` (full breakdowns), `key_claim_ids`, `key_claims`
  (verdicts), `loop` (trace summary), `extraction` (quote counts).
- `gate_trace` gets `verify_min_strength`, `verify_min_independent_sources` and
  `verify_min_key_claims_supported` entries; `shortlisted = false` if any fails.

The new signals are **not** appended to `signal_ids`, and `evidence_strength` keeps its Gate-1
value. That keeps shortlist and verify idempotent (a shortlist re-run never sees verify's evidence).
Later stages read a cluster through `evidence/clusters.py` (`cluster_signal_ids`,
`current_strength`), and `cluster` reads only `stage="extract"` excerpts.

**Model.**
- Loop: `analysis` tier, prompt `prompts/verify_loop.md`, at most `max_steps` per problem.
- Extraction: `fast` tier, `prompts/verify_extract.md`: the extract rules, with the problem
  prepended to extract's input, and a `stance` per signal (`VerifySignal`: `supports` | `counter`
  | `unrelated`; `unrelated` is dropped). It still runs through `extraction.extract_document`, so
  quotes are verified exactly as in extract. The extract prompt and schema are untouched, so
  extract's cached answers stay valid.
- Entailment: `fast` tier, `prompts/entailment.md`.

**Loop goal** (JSON): market, industry, the problem, evidence so far (signal and source counts,
category mix, the strongest signals picked one per independent source), the
`verify.source_categories` it has no evidence from, and the tasks:
1. Find more **independent first-hand** reports from the missing source categories.
2. Look for **disconfirming** evidence: is it already solved by common tools, or is it only one
   vendor's marketing?
3. If any signal is `regulatory`, find the **official** source that states the obligation (the
   `verify.official_categories` domains are offered as `site:` only then).

**Entailment** (`evidence/entailment.py`):
```python
class EntailmentVerdict(BaseModel):
    item: int        # number of the claim within the batch (not a database id: cache-stable)
    verdict: Literal["supported", "partial", "not_supported", "contradicted"]
    note: str        # English, ≤ 20 words

def check(items: list[ClaimEvidence], ask, batch_size) -> tuple[dict[int, Judged], Counter]
def entail_pending(ctx, claim_ids, *, stage: str) -> dict[str, int]  # facts without a verdict only
def clear_entailment(session, run_id, stage)  # a stage forgets its own verdicts on re-run
def failed(entailment: str | None) -> bool    # not_supported | contradicted
```
- Each item is the claim statement plus the original quotes and translations of its excerpts (up
  to `max_quotes_per_claim`). Batches of `entailment.batch_size`; a claim the model skips stays
  unchecked.
- `not_supported` / `contradicted` facts are kept but excluded from evidence counts and citations.
  `partial` facts may be cited, with a flag.
- **Key claims**: the facts behind the cluster's signals, picked like the report's quotes:
  first-hand, high tier, one per independent source first; top `key_claims_per_problem`.
  Verify also checks every new supporting fact it adds.

**Algorithm.**
1. Reset this stage's outputs (above), restore `shortlisted` from the Gate-1 trace entries.
2. Per shortlisted cluster, in parallel (`concurrency`): run the loop; `on_page` extracts with the
   problem as context and keeps the verified `supports` / `counter` signals in memory.
3. Store all rounds with one writer, in cluster order: queries, documents (get-or-create),
   signals and facts, independence groups.
4. One `entail_pending` pass over all key claims and new supporting facts.
5. Per cluster: recompute strength over extract + new supporting signals, without counter signals
   and without signals whose fact failed entailment, with the new independence groups. Re-check
   Gate 1 on the new values, plus `min_key_claims_supported` (capped at the number of key claims).
   The re-check (`regate`) does **not** repeat `min_software_fit_sources`: that rule is decided
   once, at Gate 1.

**Config:**
```yaml
verify:
  max_steps: 12
  max_searches: 15            # plan §4: ≤15 queries per problem
  max_fetches: 10
  max_observation_chars: 30000
  page_preview_chars: 1500
  max_rejections_in_row: 2
  concurrency: 3
  max_output_tokens: 3000
  source_categories: [forum, complaints, jobs, social, association]
  official_categories: [official]
  top_signals: 8
  key_claims_per_problem: 5
  min_key_claims_supported: 2
entailment:
  batch_size: 10
  max_quotes_per_claim: 3
  max_output_tokens: 4000
```

**Metrics:** totals (problems, passed, failed_verify, searches, fetches, new / counter / unrelated
signals, quote counts, stop reasons, rejections, entailment verdicts, independence groups added)
and `per_problem` (steps, searches, fetches, new and counter signals, excluded signals, sources and
strength before → after, key claims supported, stop reason, passed).

**Tests** (`tests/test_evidence_validator.py`, `tests/test_loop.py`, `tests/test_entailment.py`):
- Gate 1 outcomes, including the cap; the landscape report, including the second round.
- Verify end to end with fake providers: supporting, counter and unrelated signals; a
  `not_supported` key claim stops counting; the Gate-1 re-check passes and fails with `verify_*`
  trace entries; a re-run leaves the same rows and is served from the LLM cache; a shortlist
  re-run restores Gate 1 and clears `verification`; two problems reading one page share one
  document; same author across a collected and a verify document is one source; cluster never
  reads verify signals.
- Loop guards: unknown hit ids, known and repeated URLs, duplicate / operator / run-known queries,
  `site:` only for offered domains, off-site flag, every budget, snippet-only reads, observation
  shortening, replay from the cache.
- Entailment: local numbering, skipped and bogus items, each fact checked once, a stage clears
  only its own verdicts.

**Done when.** Gate 1 decisions are explainable from `gate_trace` alone, verify's from
`verification`, and the M4 exit holds: every gap-matrix cell is a cited fact or `unknown` (with
`competitor_research`).

---

## 5. `agents/competitor_research.py` — Competitor research

**Job.** For each shortlisted problem, find the products that already address it, record what they
do and what they cost **as cited facts** from their own pages and reviews, and build a gap matrix
showing where they are weak. Competition is not automatically bad (plan §1).

**Stages.** `competitors` (`pipeline/stages/competitors.py`).

**Reads.** Shortlisted clusters (after verify) and their counted signals
(`evidence/clusters.cluster_signal_ids`, minus `verification.excluded_signal_ids`).
`tool_complaint` quotes name vendors, which are the best seeds. Also the pack registry
(`competitors.source_categories`: `complaints`, `reviews`, `forum` domains as `site:` hints).

**Writes.** Everything below is replaced on a re-run.
- `queries` (`intent="competitor"`, via `store_searches`), `documents` (origin `competitor`;
  a page the run already has is reused, not copied).
- `excerpts` (`stage="competitors"`, no Signal) and fact claims (`stage="competitors"`,
  `meta = {competitor_id, kind, page_kind}`).
- `competitors`: name, url, segment, geo, and `pricing[]` entries of
  `{amount, currency, period, plan_name, observed_at, claim_id}`.
- `gap_matrices` (now with `run_id`): `{dimensions: [{key, label, from_signal_ids, fixed}],
  competitor_ids, cells: {dim: {competitor_id: {value, claim_id}}}, gaps: [{dimension,
  claim_id}]}`, where every cell is a cited fact or `{"value": "unknown", "claim_id": null}`.
- One inference claim per gap (`meta = {problem_id, dimension, gap: true}`).

**Model.**
- Seeds: `fast` tier, `prompts/competitor_seeds.md`.
- Loop: `analysis` tier, `prompts/competitors_loop.md`.
- Page facts: `fast` tier, `prompts/competitor_facts.md`.
- Matrix: `analysis` tier, `prompts/gap_matrix.md`.
- Entailment of the claims the matrix cites: `fast` tier, `prompts/entailment.md`.

**Schemas** (`domain/competitors.py`). Numbers the model uses for products, claims and signals
are local to one prompt input, never database ids, so cached answers survive renumbering.
```python
class CompetitorSeeds(BaseModel):
    from_signals: list[str]    # products named in the evidence quotes
    suggested: list[str]       # the model's own suggestions: search seeds only

class CompetitorFact(BaseModel):
    kind: Literal["feature", "price", "segment", "integration", "limitation", "review_complaint"]
    quote: str                 # verbatim from the page
    translation: str
    statement: str             # English
    amount: float | None = None      # price only; must appear in the quote
    currency: str | None = None
    period: Literal["month", "year", "one_time", "per_user_month", "per_document", "unknown"] | None = None
    plan_name: str | None = None

class CompetitorPage(BaseModel):
    competitor_name: str | None    # null if the page is not about one product
    product_url: str | None        # the product's own website
    page_kind: Literal["own_site", "review", "complaint", "comparison", "other"]
    segment: str | None
    geo: Literal["turkey", "international", "unknown"]
    facts: list[CompetitorFact]

class GapDimensionDraft(BaseModel):
    key: str
    label: str                 # English, e.g. "e-İrsaliye integration", "Price for ≤10 users"
    from_signals: list[int]    # signal numbers in the input

class GapCell(BaseModel):
    dimension: str
    competitor: int            # product number in the input
    value: Literal["yes", "partial", "no", "unknown"]
    claim: int | None          # claim number in the input; required unless value == "unknown"

class GapMatrixDraft(BaseModel):
    dimensions: list[GapDimensionDraft]
    cells: list[GapCell]
```

**Algorithm.**
1. Reset this stage's outputs.
2. Per shortlisted cluster, in parallel (`concurrency`):
   - **Seeds:** one `competitor_seeds` call over the problem and its quotes (`tool_complaint`
     first). Names are search seeds only.
   - **Loop** (`run_loop`) with the goal: find up to `max_competitors` products serving this
     workflow in this market; for each, read a page on its own website, its pricing page, a
     feature page and one review / complaint page.
   - `on_page`: one `competitor_facts` call over the first `fact_chars` of the page; each fact's
     quote is verified with `evidence/quotes.py` (unverifiable → dropped), and a price is kept
     only if its amount appears in the matched quote (`numbers_in` reads Turkish `1.250,50` and
     English `1,250.50`). Results stay in memory.
3. **Competitors** (`resolve`): a product becomes a competitor only through a page on its **own**
   website, i.e. the page's domain matches the `product_url` the page names (`same_site`). Other
   pages (reviews, complaints) attach by Turkish-aware name match (`same_product`: equal, or one
   name is the other plus more words). Facts about a product never confirmed on its own site are
   dropped and counted (`unconfirmed_pages`), so a name from model memory is never stored.
4. **Store** all problems with one writer: queries, documents (get-or-create), competitors,
   excerpts and fact claims, prices with `observed_at = document.fetched_at`. Prices are never
   converted here; `monetization` converts with a dated rate.
5. **Matrix** (in parallel): one `gap_matrix` call per problem with competitors, given the
   problem, its signals, the `always_dimensions` and each product's claims, all numbered locally.
6. If `entail_cells`, one `entail_pending` pass over the claims non-`unknown` cells cite.
7. **Validate** (`validate_matrix`):
   - Dimensions: fixed ones are always kept (added if the model left one out); others need at
     least one valid signal number, and at most `max_signal_dimensions` are kept.
   - Cells: a non-`unknown` cell must cite a claim of **that** product that has not failed
     entailment, or it is demoted to `unknown` (counted by reason). Missing cells are `unknown`.
8. **Gaps:** a dimension where no product is `yes` and at least one cell is known becomes an
   inference claim derived from those cells' claims. `opportunity_scorer` reads these for
   "competition gap".

**Exit check.** `evidence/gaps.check_gap_matrices(session, run_id)` re-reads the stored matrices:
every (dimension, competitor) has a cell, and every non-`unknown` cell cites a fact of the same run
and the same competitor whose excerpts exist and which has not failed entailment.
`signalforge gaps <run>` prints the matrices and exits 1 on any violation.

**Config:**
```yaml
competitors:
  max_competitors: 5            # plan §4
  model_seed_names: 5
  max_steps: 20
  max_searches: 12
  max_fetches: 20
  max_observation_chars: 30000
  page_preview_chars: 1500
  max_rejections_in_row: 2
  concurrency: 3
  max_output_tokens: 3000
  source_categories: [complaints, reviews, forum]
  always_dimensions: [price_for_smb, turkish_localization, e_document_integration, setup_effort]
  max_signal_dimensions: 6
  max_facts_per_page: 12
  fact_chars: 12000
  entail_cells: true
  seed_output_tokens: 2000
  matrix_output_tokens: 8000
```

**Metrics:** totals (competitors, pages read, searches, facts proposed / verified / unverified,
prices, prices dropped for an amount not in the quote, cells by value, demoted cells, gaps, stop
reasons, rejections, entailment) and `per_problem` (seeds, pages, competitors, facts, prices,
cells, demoted, gaps, unconfirmed pages).

**Tests** (`tests/test_competitor_research.py`):
- A product seen only on a review site, or only suggested by the model, is never stored.
- A complaint page attaches to its product by name.
- A price keeps its currency, period and observed date; a price whose amount is not in the quote
  and an unverifiable quote are dropped.
- A cell citing another product's claim, a missing claim or a failed-entailment claim is demoted;
  a dimension without valid signals is dropped; fixed dimensions are always present.
- A gap is derived only when evidence exists, not from all-`unknown` columns.
- `check_gap_matrices` passes on stage output and catches seeded bad cells; a re-run is
  idempotent and served from the LLM cache.

**Done when.** This is the M4 exit: every gap-matrix cell is a cited fact or `unknown`
(`signalforge gaps <run>` exits 0 on a live TR logistics run).

---

## 6. `agents/buyer_research.py` — Buyer research

**Job.** For each shortlisted problem, decide **who** would buy a solution: the segments, and per
segment the user, buyer, decision maker and economic beneficiary (plan §2, kept from the vision),
plus the budget owner, how reachable those buyers are, and how many companies have the problem. It
proposes 1–3 **opportunities** (problem × segment × solution angle).

> **Adopted in plan.md (2026-10-10, §14).** Plan §6 first merged buyer research, monetization and
> WTP into one `commercial` stage, to limit error compounding. Here they are two agents with a narrow hand-off:
> buyer_research writes `Opportunity` rows with cited buyer roles, and monetization only adds the
> economics. If this split causes compounding errors in M5 evaluation, merge them back.

**Stages.** `buyers` (`pipeline/stages/buyers.py`).

**Reads.** Shortlisted clusters; signals (`actor`, `labor_spend` job ads, which say which role does
the work and often who they report to); competitor `segment` facts; gap inference claims; the plan's
`buyer_hypotheses`; the request's `target_customer`.

**Writes.**
- `opportunities`: `segment`, `solution_angle`, `buyer_roles`, plus the new columns
  `accessibility` (JSON) and `market_breadth` (JSON).
- Claims: `fact` where a verified excerpt states the point; `inference` where it is derived from
  facts; `hypothesis` otherwise. A hypothesis is allowed but always labelled.
- Optional market-breadth documents and claims (step 3). **Deferred** (2026-10-10): the stage
  stores the model's `breadth_hint` with `status: "not_searched"`; nothing is searched yet.

Stored JSON shapes (claim ids are database ids):
```text
buyer_roles    = {user|buyer|decision_maker|economic_beneficiary:
                    {role, claim_ids, hypothesis, hypothesis_claim_id,
                     entailment, entailment_note, rejected_claim_ids?},
                  budget_owner: null | {…same…}, gap_claim_ids: [...]}
accessibility  = {channels: [{kind, name, claim_ids, cited}]}   # cited = claim_ids non-empty
market_breadth = {hint: str | null, status: "not_searched"}
```

**Model.** `analysis` tier, prompt `prompts/buyers.md`, one call per problem.

**Schemas** (`domain/commercial.py`):
```python
class RoleClaim(BaseModel):
    role: str                     # as named in Turkey, e.g. "operasyon müdürü", "firma sahibi"
    claim_ids: list[int] = []     # existing fact/inference claims that support it
    hypothesis: str | None = None # required when claim_ids is empty

class BuyerRoles(BaseModel):
    user: RoleClaim
    buyer: RoleClaim
    decision_maker: RoleClaim
    economic_beneficiary: RoleClaim
    budget_owner: RoleClaim | None     # a hypothesis when unstated; null only when no one in
                                       # the segment plausibly pays → Gate 2 knock-out

class Channel(BaseModel):
    kind: Literal["association", "directory", "community", "marketplace", "event", "other"]
    name: str
    claim_ids: list[int] = []

class OpportunityDraft(BaseModel):
    segment: str                  # English, specific: "Road freight firms with 5–50 trucks"
    solution_angle: str           # English, one sentence; what the product does, tied to gaps
    gap_claim_ids: list[int]      # gap inferences it targets ([] allowed, scored low)
    buyer_roles: BuyerRoles
    channels: list[Channel]
    breadth_hint: str | None      # what statistic would count these companies (for step 3)

class BuyerAnalysis(BaseModel):
    opportunities: list[OpportunityDraft]   # 1–3 per problem
```

**Prompt content** (`prompts/buyers.md`, v2):
- Input is a claim table (id, kind, statement), never raw text, so the model can only cite what
  exists.
- One segment per opportunity, and segments must not overlap.
- Prefer the request's `target_customer`.
- A role is only a fact if a claim names it in that function, e.g. a job ad saying "operasyon
  müdürüne bağlı". A complaint or a job ad's duty list states the user, not the buyer or the
  economic beneficiary.
- The economic beneficiary is a person or function, not the company.
- v2 (2026-10-10): the budget owner is a hypothesis when the evidence is silent (in SMBs usually
  "firma sahibi"); `null` only when no one plausibly pays. v1's "do not guess" left it null on 2
  of 3 live opportunities, which Gate 2 would have knocked out for a prompt artefact.
- List 1–4 real, specific channels; uncited ones are allowed and stored as `cited: false`.
- The solution angle must be buildable as B2B SaaS and must name which gap it targets.
- Do not invent statistics; fill `breadth_hint` instead.

**Algorithm, per shortlisted cluster:**
1. Build the claim table: the cluster's inference, its counted signal facts (extract + verify,
   minus `verification.excluded_signal_ids`, only non-failed entailment; job ads first, then
   first-hand, capped at `max_claims_per_problem`), its gap inferences, and its competitors'
   segment facts. Add the plan's buyer hypotheses as `hypothesis` claims (`stage="buyers"`,
   `meta.origin="plan"`, so `reset` removes them and re-runs don't duplicate them) so they can be
   cited and labelled as hypotheses. The model sees local numbers 1..n, mapped back in code.
2. Call the model → `BuyerAnalysis`.
3. **Market breadth** (optional, deterministic, not a loop): for each `breadth_hint`, run up to
   `breadth_queries` fixed-template searches against the pack's statistics sources (TÜİK, TOBB,
   associations). Fetch the top hits, and extract numeric facts with verbatim quotes using a
   `fast` call with prompt `prompts/breadth_facts.md`. Store them as fact claims; if nothing is
   found, store `{"status": "unknown"}`.
4. **Validate:** every cited number is in the table (others are dropped and counted). A role
   with no valid `claim_ids` must have a `hypothesis`, or its opportunity is dropped; a null
   `budget_owner` is kept. `gap_claim_ids` must be this problem's gap inferences. Channels without
   a citation are kept as `cited: false`. Drop opportunities whose segment duplicates another
   (`queries.Deduper`, `segment_dup_ratio`). Cap at `max_opportunities_per_problem`. A failed call
   leaves that problem without opportunities (counted); `BudgetExceeded` stops the stage.
5. **Role check** (`entail_roles: true`): each role that cites facts is checked as one claim
   (e.g. "A <role> approves purchases for this work: <problem>") against those facts' quotes,
   with the `entailment` prompt on the fast tier. Quote verification and fact entailment only
   prove a fact states its own statement, not that it names the role. A role that fails keeps its
   non-fact citations, lists the facts under `rejected_claim_ids`, and becomes a hypothesis (its
   claim text is the hypothesis if it had none). The facts' own `entailment` is not touched.
6. Write the `Opportunity` rows. Store `buyer_roles` with the claim ids, and create a `hypothesis`
   claim for each role hypothesis (`meta.opportunity_id`, `meta.role`, derived from the role's
   citations) so the report can cite it.

**Config:**
```yaml
buyers:
  max_opportunities_per_problem: 3
  max_claims_per_problem: 60    # signal facts in one problem's claim table
  segment_dup_ratio: 85         # queries.Deduper ratio for near-duplicate segments
  concurrency: 4
  max_output_tokens: 6000
  entail_roles: true            # role check (step 5); batch size / tokens from `entailment`
  role_quotes: 6                # quotes per role sent to the role check
  # breadth_queries / breadth_fetches: added when breadth search is built
```

**Metrics:** opportunities per problem; roles backed by facts vs inferences vs hypotheses; budget
owners identified (count); channels per opportunity (and cited); dropped opportunities by reason;
invalid citations; failed calls; `role_check` (checked, verdicts, demoted); a `per_problem` list with each problem's reason. (Breadth found /
unknown once breadth is built.)

**Tests** (`tests/test_buyer_research.py`):
- A role that cites a non-existent claim is rejected.
- A role with neither citation nor hypothesis is rejected.
- A null budget owner is preserved for Gate 2.
- Duplicate segments collapse.
- Also: failed-entailment, unshortlisted and verify-excluded facts stay out of the table; plan
  hypotheses are citable and labelled; a re-run is idempotent; `check_buyer_roles` flags a bad
  role; one failed call doesn't fail the stage; a role whose facts fail the role check becomes a
  hypothesis and the exit check flags one that still cites them.

**Done when.** Each opportunity's buyer roles can be traced to cited claims or are explicitly
labelled hypotheses (part of the M5 exit). Checked by `evidence/opportunities.check_buyer_roles`.

---

## 7. `agents/monetization.py` — Monetization

**Job.** For each opportunity, estimate what solving the problem is worth to one customer per
month, as a **range** built from explicit, dated assumptions (plan §2 row 15). Collect
willingness-to-pay signals, and knock out opportunities that cannot pay (Gate 2).

**Stages.** `monetization` (`pipeline/stages/monetization.py`).

**Reads.**
- `opportunities` and their problem's signals: `labor_spend`, `price_signal`, and workflow
  frequency stated in quotes.
- Competitor `pricing[]`.
- The pack's `economics.yaml`: dated wage references with source URLs, and the dated `usd_try`
  rate. **Add a `usd_try` entry to the pack.**
- The request's `founder.min_customer_value_usd_month`.

**Writes.**
- `Opportunity.economic_model`, `Opportunity.wtp_signals`, and the new columns `status`
  (`passed` | `knocked_out`) and `knockouts` (JSON list of `{rule, detail}`).
- `assumption` claims with the new `Claim.meta` JSON: `{name, value_low, value_high, unit,
  currency, as_of, source_url | null, sourced: bool}`.

**Model.** `analysis` tier, prompt `prompts/monetization.md`, one call per opportunity. The model
chooses the value **drivers** and their ranges with citations. **Code** computes every number
(`scoring/economics.py`).

**Schemas** (`domain/commercial.py`):
```python
class AssumptionDraft(BaseModel):
    name: str                      # snake_case, e.g. "hours_saved_per_month"
    low: float
    high: float
    unit: str                      # "hour/month", "TRY/hour", "documents/month", "fraction"
    currency: str | None = None
    claim_ids: list[int] = []      # facts it rests on (quote, competitor price, pack reference)
    pack_reference: str | None = None   # name of an economics.yaml entry, if used
    rationale: str                 # English, ≤ 30 words

class ValueModelDraft(BaseModel):
    formula: Literal["labor_savings", "error_cost_avoided", "revenue_recovered", "compliance_cost"]
    assumptions: list[AssumptionDraft]   # must cover the formula's required inputs

class WTPSignal(BaseModel):
    kind: Literal["competitor_price", "labor_spend", "price_signal", "paid_workaround"]
    claim_id: int
    note: str
```

**Formulas** (`scoring/economics.py`; inputs from the assumptions; low/high propagate through
interval arithmetic):
```
labor_savings       = hours_saved_per_month × loaded_hourly_cost
error_cost_avoided  = errors_per_month × cost_per_error × share_avoidable
revenue_recovered   = lost_revenue_per_month × share_recoverable
compliance_cost     = penalty_or_outsourcing_cost_per_month × share_replaceable

value_usd_month     = value_local / usd_try(as_of)      # dated rate; the date is stored
price_ceiling       = value_usd_month × capture_share   # capture_share in config, e.g. 0.1–0.3
competitor_anchor   = min/median of observed competitor prices (converted, dated)
```

**Algorithm, per opportunity:**
1. Build the claim table: the problem's facts, competitor price facts, and pack references (each
   rendered as `name = value unit, as_of, source_url`).
2. Call the model → `ValueModelDraft`.
3. **Validate:**
   - The formula's required inputs are present.
   - `low ≤ high`, and units match the formula.
   - A cited `pack_reference` exists, and the value then comes **from the pack, not the model**.
   - Every assumption without citation or pack reference becomes `sourced=false`.
   - Currencies are known, and money values have a date.
4. Compute the economic model in code and store
   `{formula, inputs: {name: assumption_claim_id}, value_local: [lo, hi], currency, value_usd_month: [lo, hi], fx: {rate, as_of, source_url}, price_ceiling_usd_month: [lo, hi], competitor_anchor_usd_month}`.
5. **WTP signals** (deterministic): competitor prices for this problem, `labor_spend` signals,
   `price_signal` signals, and workarounds that cost money (e.g. outsourced data entry). Each one
   cites a claim.
6. **Gate 2** (plan §4):
   - `no_budget_owner`: `buyer_roles.budget_owner` is null.
   - `value_below_minimum`: `price_ceiling_usd_month.high < founder.min_customer_value_usd_month`.
   - Write `status` and `knockouts`. Knocked-out opportunities are not scored, but they go to the
     report's "Don't build" list with these reasons.

**Config:**
```yaml
monetization:
  capture_share: { low: 0.10, high: 0.30 }
  default_loaded_cost_multiplier: 1.3     # applied to net wage references; provisional
  max_output_tokens: 4000
```

**Metrics:** opportunities in, passed, knocked out by rule; share of sourced assumptions;
value-range width (high/low); opportunities with a competitor price anchor.

**Tests** (`tests/test_monetization.py`):
- Interval arithmetic for each formula.
- The USD conversion uses the dated rate and records it.
- A pack reference overrides a model value.
- An unsourced assumption is marked `sourced=false`.
- Both Gate 2 rules fire on fixtures.
- A missing `usd_try` entry fails loudly.

**Done when.** Every number in an economic model traces to an assumption claim with a source or an
"unsourced" label (part of the M5 exit).

---

## 8. `agents/opportunity_scorer.py` — Opportunity scorer

**Job.** For each opportunity that passed Gate 2, produce the three independent outputs from plan
§8 (attractiveness, confidence, founder fit), a rule-based category, and the cheapest validation
experiment, all fully explainable from a rule trace and cited claims.

**Stages.** `score` (`pipeline/stages/score.py`). The logic lives in `scoring/` as plan §12 says:
`rubric.yaml`, `config.yaml`, `scorer.py`, `categories.py`, `experiments.py`, `experiments.yaml`.

**Reads.** `opportunities` (passed), their clusters (with evidence strength), the claim table
(signals, gap inferences, buyer roles, economic model, WTP signals), and the request's `founder`.

**Writes.** `score_cards`: `factors`, `attractiveness`, `confidence`, `founder_fit`, `category`,
`rule_trace`, and the new column `experiment` (JSON). Knocked-out opportunities get a ScoreCard
with `category="weak"`, no factors, and a rule trace that copies their knock-outs, so the report
treats every opportunity the same way.

**Model.** `analysis` tier, prompt `prompts/score.md` (rubric judgments) and
`prompts/founder_fit.md` (feasibility only).

**Schemas** (`domain/scoring.py`):
```python
Factor = Literal["severity", "economic_impact", "frequency", "competition_gap",
                 "willingness_to_pay", "customer_accessibility", "market_breadth"]

class FactorJudgment(BaseModel):
    factor: Factor
    level: int = Field(ge=1, le=5)
    justification: str            # English, ≤ 40 words, must reference the cited claims
    claim_ids: list[int]

class RubricJudgments(BaseModel):
    judgments: list[FactorJudgment]   # the factors judged by the model (see table)

class FeasibilityJudgment(BaseModel):
    mvp_feasible: Literal["yes", "stretch", "no"]
    hard_barriers: list[str]          # licences, certifications, deep integrations
    barrier_claim_ids: list[int]
    sales_motion: Literal["self_serve", "inside_sales", "field_sales", "enterprise"]
    justification: str
```

**Factors** (plan §8.2; weights sum to 100; levels 1–5 with concrete anchors in `rubric.yaml`):

| Factor | Weight | How the level is set |
|---|---|---|
| severity | 20 | model on the rubric, citing signal claims |
| economic_impact | 20 | **code**: `value_usd_month` midpoint → level, via thresholds in `scoring/config.yaml` |
| frequency | 10 | model on the rubric (daily → rare) |
| competition_gap | 15 | model, citing gap inferences; no gap claims → max level 2 |
| willingness_to_pay | 15 | model, citing WTP signals; a floor of 3 if ≥2 independent paid competitors with observed prices |
| customer_accessibility | 10 | model, citing channel claims |
| market_breadth | 10 | model, citing breadth facts; `unknown` → max level 2 |

**Algorithm, per passed opportunity:**
1. **First pass:** one rubric call → `RubricJudgments`.
2. **Validate:**
   - Each judged factor appears exactly once.
   - Cited ids exist, belong to this opportunity's claim table, and have not failed entailment.
   - **A factor whose citations include no fact claim is capped at level 2 and flagged** (plan
     §8.2).
   - Code-computed factors overwrite model output for that factor.
3. `attractiveness = Σ weight_f × (level_f − 1) / 4` (0–100).
4. **Final candidates:** the top `k_judge_top_n` by first-pass attractiveness are judged
   `k_judges` times in total, using the same prompt with `judge_index` in the input so cache keys
   differ. Each factor takes the median level. The spread (max − min per factor) is recorded.
5. **Confidence** (`scorer.py`, rule-based, thresholds in config):
   - from evidence strength,
   - the share of factor weight resting only on hypotheses or unsourced assumptions,
   - the maximum judgment spread.
   - High needs all three; Low if any one fails its floor.
6. **Founder fit** (plan §8.4):
   - Feasibility call → `FeasibilityJudgment`.
   - Then rules: `not_fit` if there is any hard barrier backed by a fact claim (e.g. GİB özel
     entegratör licence), or if the price ceiling high is below the founder minimum.
   - `stretch` if mvp is `stretch` or the sales motion doesn't match the team (e.g. field sales for
     a solo developer).
   - Otherwise `fit`.
7. **Category** (`categories.py`, plan §8.5, evaluated in this order, first match wins; every rule
   checked is appended to `rule_trace` with its inputs):
   - `false_positive` — the cluster's inference has fewer than `min_supported_facts` facts that
     passed entailment.
   - `weak` — no budget owner, ceiling < minimum, or severity ≤ 2.
   - `competitive` — competition_gap ≤ 2 and WTP ≥ 4.
   - `strong` — attractiveness ≥ 75, evidence strength ≥ 7, budget owner, and gap ≥ 3.
   - `interesting` — otherwise.
8. **Validation experiment** (`experiments.py`, plan §8.6):
   - For each claim the score rests on: `importance = weight of the factors citing it`, and
     `uncertainty` from its kind (`hypothesis` 1.0, unsourced `assumption` 0.9, sourced
     `assumption` 0.5, `inference` 0.4, `partial` fact 0.3, `supported` fact 0.1).
   - Pick the claim with the maximum `importance × uncertainty`.
   - Choose the cheapest experiment in `experiments.yaml` whose `tests` list includes that claim's
     factor (catalogue: interviews, outreach, landing page, concierge, paid pilot; each with
     cost, duration and a pass/fail template).
   - Fill the template with the claim, e.g. "≥ 3 of 10 contacted fleet owners agree to a paid
     pilot at ≥ $X/month".

**Config** (`scoring/config.yaml`, which also holds the evidence strength weights):
```yaml
weights: { severity: 20, economic_impact: 20, frequency: 10, competition_gap: 15,
           willingness_to_pay: 15, customer_accessibility: 10, market_breadth: 10 }
uncited_cap: 2
k_judges: 3
k_judge_top_n: 5
economic_impact_levels_usd_month: [25, 75, 200, 500]   # level boundaries 1|2|3|4|5; provisional
confidence:
  high: { min_strength: 7, max_hypothesis_share: 0.25, max_spread: 1 }
  low:  { min_strength: 5, max_hypothesis_share: 0.5,  max_spread: 2 }
categories:
  strong: { min_attractiveness: 75, min_strength: 7, min_gap: 3 }
  competitive: { max_gap: 2, min_wtp: 4 }
  weak: { max_severity: 2 }
  false_positive: { min_supported_facts: 3 }
strength:
  count_saturation: 10
  diversity_saturation: 4
  tier_weights: { high: 1.0, medium: 0.6, low: 0.2 }
  snippet_only_factor: 0.5
  recency_months: 24
  weights: { count: 0.35, diversity: 0.15, quality: 0.20, recency: 0.10, directness: 0.20 }
```

**Metrics:** cards by category; confidence mix; founder fit mix; capped factors; median judgment
spread; cost of the k-judge pass.

**Tests** (`tests/test_opportunity_scorer.py`):
- An uncited factor is capped at 2.
- A code factor overrides the model.
- The median of 3 judges.
- Each category rule fires on a fixture, and the rule trace lists every checked rule.
- Founder fit `not_fit` on a fact-backed licence barrier.
- The experiment picks the max importance × uncertainty claim.

**Done when.** This is the M5 exit: each ScoreCard is fully explainable from its rule trace and
cited claims.

---

## 9. `agents/report_writer.py` — Report writer

**Job.** Write the final research report: full reports for the top 3–5 opportunities, short
summaries for the rest, a "Don't build" section with reasons, and an "insufficient evidence" list.
The writer is **constrained** (plan §7.4): it can only cite claim ids, and a validator rejects
factual bullets that don't trace to their citations.

**Stages.** `report` (`pipeline/stages/report.py`). Rendering lives in `reporting/`: `schema.py`,
`validator.py`, `render.py`, and `templates/report.md.j2` / `report.html.j2`.

**Reads.** Score cards, opportunities, clusters (shortlisted and insufficient), competitors, gap
matrices, and the claims they reference with their excerpts and document URLs. Also the request's
`report_language`.

**Writes.** `reports/<run_id>/report.json` (source of truth), `report.md`, `report.html`. Before
writing, it runs `evidence_validator.entail_pending` on every fact claim the report will cite. Full
page text is **never** exported (plan §2 row 17): reports contain excerpts ≤ 500 chars, their
translations and URLs.

**Model.** `synthesis` tier, prompt `prompts/report.md`, **one call per opportunity section**. This
keeps inputs small and lets a failed section regenerate alone. The run-level summary is one more
call over the section summaries.

**Schemas** (`reporting/schema.py`):
```python
SectionKey = Literal[
    "problem", "evidence", "who_has_it", "current_solutions", "gaps", "buyers",
    "economics", "risks", "proposed_product", "mvp", "validation_experiment",
]
UNCITED_ALLOWED = {"proposed_product", "mvp", "validation_experiment"}  # labelled as recommendations

class Bullet(BaseModel):
    text: str
    claim_ids: list[int]          # empty only in UNCITED_ALLOWED sections
    kind: Literal["fact", "inference", "hypothesis", "assumption", "recommendation"]

class Section(BaseModel):
    key: SectionKey
    bullets: list[Bullet]

class OpportunityReport(BaseModel):
    opportunity_id: int
    title: str
    one_liner: str
    sections: list[Section]

class Report(BaseModel):          # report.json
    run_id: int
    generated_at: datetime
    request: ResearchRequest
    summary: list[Bullet]
    opportunities: list[OpportunityReport]          # full reports, top N
    other_opportunities: list[OpportunitySummary]   # deterministic: name, category, scores, top reason
    dont_build: list[DontBuildItem]                 # deterministic: from rule traces and knock-outs
    insufficient_evidence: list[InsufficientItem]   # deterministic: from Gate 1 / verify failures
    claims: list[ClaimView]                         # id, kind, statement, excerpts (quote, translation, url, date)
    method: MethodView                              # counts, costs, models, prompt versions, pack version
```

**Validator** (`reporting/validator.py`, plan §7.4). Each rule returns errors with the bullet path:
1. **Uncited fact:** a bullet outside `UNCITED_ALLOWED` with no `claim_ids`.
2. **Unknown citation:** a claim id that is not in the section's claim table, or a fact that failed
   entailment.
3. **Unsupported specifics:** a number (Turkish and English formats, currency symbols, `%`), or a
   proper noun (capitalised token not at sentence start, not in a stoplist), in the bullet that
   appears in none of the cited claims' statements, quotes, translations or the economic model
   values. Matching is Turkish-aware casefolded.
4. **Kind mismatch:** a bullet marked `fact` that cites only hypothesis or assumption claims, or a
   bullet citing a hypothesis whose text has no hedge marker (see `hedges` in config).
5. **Recommendation outside allowed sections.**

On errors, regenerate **that section only**, with the errors appended to the input, up to
`max_regenerations` times. If errors remain, drop the failing bullets and record them in
`report.json.method.dropped_bullets`. A report never ships an invalid bullet.

**Algorithm.**
1. Select the top `full_reports` opportunities by category order (`strong` > `interesting` >
   `competitive`), then attractiveness, then confidence.
2. Collect each one's claim table: claims reachable from its score card factors, buyer roles,
   economic model, gap matrix and cluster.
3. Run `entail_pending` on the fact claims in those tables.
4. Generate and validate each section, regenerating as needed. Run the summary call.
5. Build the deterministic sections (`other_opportunities`, `dont_build`,
   `insufficient_evidence`, `method`) in code. No LLM is involved, so those sections can't
   contradict the rule traces.
6. Write `report.json`, then render the md/html with Jinja2:
   - Every bullet links to its claims' excerpts.
   - Hypotheses and assumptions are visually marked.
   - Quotes stay in Turkish with an English translation.

**Config:**
```yaml
report:
  full_reports: 5
  min_full_reports: 3          # fewer only if fewer opportunities are strong / interesting / competitive
  max_regenerations: 2
  hedges: [may, might, likely, hypothesis, unverified, "we assume", "not yet confirmed"]
  out_dir: reports
  max_output_tokens: 8000
```

**Metrics:** sections generated, regenerations, bullets dropped, % factual bullets cited (target
100%), claims cited, entailment checks run here, cost.

**Tests** (`tests/test_report_writer.py`):
- Seeded bad bullets are caught: no citation; a number not in the cited claim; a proper noun not in
  the cited claim; a hypothesis phrased as fact; a recommendation in `evidence`.
- Regeneration fixes a section with the fake LLM.
- A bullet still invalid after `max_regenerations` is dropped and recorded.
- The deterministic sections match the rule traces.
- No full document text appears in any output file.

**Done when.** This is the M6 exit: 100% of factual bullets are cited, and the validator catches
seeded uncited or hallucinated bullets in tests.

---

## 10. Schema changes (one Alembic migration per milestone)

| Table | Change | Needed by |
|---|---|---|
| `problem_clusters` | ✅ M3 (`c4d8e1f2a7b9`): `shortlisted` Bool, `gate_trace` JSONB, `strength` JSONB, `signal_type_mix`, `rank`; `claim_id` (`d5e9a2b3c4f6`) | evidence_validator (M3) |
| `problem_clusters` | ✅ M4 (`e6f1a3b5c7d9`): `verification` JSONB nullable | evidence_validator (M4) |
| `signals` | ✅ M3: `meta` JSONB default `{}` (`submarket`, `fit` = software-fit facts; verify adds `origin`, `problem_id`, `counter`) | problem_discovery, evidence_validator |
| `claims` | ✅ M4: `entailment` String(16) nullable, `meta` JSONB default `{}`, index on `(run_id, kind)` | evidence_validator (M4), monetization (M5) |
| `documents` | ✅ M4: `origin` String(16) default `collect` (`collect` / `verify` / `competitor` / `buyers`), `problem_id` nullable FK (SET NULL) | loop agents (M4) |
| `excerpts` | ✅ M4: `stage` String(32) default `extract` (each stage deletes only its own; cluster reads only `extract`) | loop agents (M4) |
| `gap_matrices` | ✅ M4: `run_id` FK (run-scoped deletes and the exit check) | competitor_research (M4) |
| `opportunities` | ✅ M5 (`f1a7c3d9e2b4`): `status` String(16), `knockouts` JSONB, `accessibility` JSONB, `market_breadth` JSONB | buyer_research, monetization (M5) |
| `score_cards` | `experiment` JSONB | opportunity_scorer (M5) |

Collection stages (`extract`, `dedupe`) read only `documents.origin = 'collect'`, and `search`
searches only collection intents, so loop rows never leak into a collection re-run.

The pack also needs `economics.yaml` entries for `usd_try` and at least one wage reference per
target role, each with `as_of` and `source_url` (M5). ✅ 2026-10-10 (pack tr 0.1.2): `usd_try`
(TCMB), 2026 minimum wage net/gross/employer cost, `working_hours_per_month`, and net wages for 11
role families seen in `labor_spend` signals (kariyer.net). Look entries up with
`MarketPack.economic(name)`, which raises on a missing name. Employer cost / net is ≈ 1.43 at
minimum wage, so the provisional `default_loaded_cost_multiplier: 1.3` (§7) is low.

---

## 11. Build order by milestone

The milestones and their exit criteria are defined in
[plan.md §13](plan.md#13-milestones-each-with-an-exit-criterion). This table says which agent work
closes each one. A milestone is done when its exit criterion in plan.md holds, not when its code
is merged. Each agent's "Done when" line above repeats the exit it is responsible for.

| Milestone | Agents and stages built | Shared code | Migration / pack changes (§10) | Exit check owned by |
|---|---|---|---|---|
| **M0** Foundations ✅ | none | providers, LLM ledger, cache, TR pack skeleton | initial schema | — |
| **M1** Plan & queries | [planner](#1-agentsplannerpy--planner): `plan` (to do) · [source_discovery](#2-agentssource_discoverypy--source-discovery): `query_gen` ✅ | — | curate `sources.yaml` | source_discovery (query review) |
| **M2** Collection | [source_discovery](#2-agentssource_discoverypy--source-discovery): `search`, `triage`, `fetch`, `dedupe` (built) · agent framework | `agents/base.py`, `agents/__init__.py`, `run --agent`, `signalforge agents`, `evidence/documents.py` | none | source_discovery |
| **M3** Signals & landscape | [problem_discovery](#3-agentsproblem_discoverypy--problem-discovery): `extract`, `cluster` · [evidence_validator](#4-agentsevidence_validatorpy--evidence-validator) part 1: `shortlist` (Gate 1) · landscape report | `evidence/quotes.py`, `extraction.py`, `claims.py`, `independence.py`, `strength.py`; `scoring/config.yaml` (strength block only) | `problem_clusters.status/gate`, `signals.meta` | problem_discovery (quote pass rate, labelled signals) + a founder reading the landscape report |
| **M4** Verification & competitors | [evidence_validator](#4-agentsevidence_validatorpy--evidence-validator) part 2: `verify` + entailment · [competitor_research](#5-agentscompetitor_researchpy--competitor-research): `competitors` | `agents/loop.py`, `evidence/entailment.py`, `evidence/documents.py`, `evidence/clusters.py`, `evidence/gaps.py`, `queries.py`, `signalforge gaps` | `claims.entailment/meta`, `documents.origin/problem_id`, `excerpts.stage`, `problem_clusters.verification`, `gap_matrices.run_id` | competitor_research (gap-matrix cells) |
| **M5** Commercial & scoring | [buyer_research](#6-agentsbuyer_researchpy--buyer-research): `buyers` · [monetization](#7-agentsmonetizationpy--monetization): `monetization` (Gate 2) · [opportunity_scorer](#8-agentsopportunity_scorerpy--opportunity-scorer): `score` | `scoring/economics.py`, `rubric.yaml`, `scorer.py`, `categories.py`, `experiments.py`/`.yaml` | `opportunities.*` columns, `score_cards.experiment`; pack `economics.yaml` (`usd_try`, wage references) | opportunity_scorer (explainable ScoreCards) |
| **M6** Final report | [report_writer](#9-agentsreport_writerpy--report-writer): `report` | `reporting/schema.py`, `validator.py`, `render.py`, templates | none | report_writer (citation validator) |
| **M7** Multi-market evaluation | no new agents: tune each agent's config block and prompts on fixtures | `evals/` (fixtures, labelling, metrics reports), `signalforge ledger --by agent` | more market packs / industries as needed | all agents |
| **M8** API & UI | no new agents: endpoints and UI show progress per agent (StageRuns grouped by `AGENTS`) | `api/routes`, worker, frontend | job table | — |

**Order within each milestone.** Each step ends with its tests green and the agent's metrics
showing in `signalforge status`.

- **M1:** `planner` can be built in parallel with M2–M3. It doesn't block them, because the
  hand-written `examples/tr-logistics.plan.yaml` already feeds `query_gen`. M1 closes when both the
  planner and the query review are done.
- **M2:** agent framework first (`base.py`, `AGENTS`, `STAGES` derived from it, CLI), then the
  `source_discovery` wrapper, then move the document helpers into `evidence/documents.py`. Close
  M2 with a full collection run on TR logistics, recording document count, duplicate collapse
  rate and fetch success.
- **M3:**
  1. Shared evidence code: `quotes.py`, `extraction.py`, `claims.py`.
  2. `extract`, then label 50 signals and tune until the extract exit holds.
  3. `cluster`.
  4. `independence.py` and `strength.py`, then `shortlist`.
  5. Render the landscape report.
  6. **Stop until a founder finds the landscape report useful** (plan M3). Measure query yield
     here and feed it back into `query_gen.pain_signal_mix` (§2).
- **M4:** `agents/loop.py` and `entailment.py` first, since both loop agents need them. Then
  `verify`, then `competitors`. ✅ Code and fake-provider tests done (2026-10-06). Still open:
  a live run on TR logistics once the M3 founder check has passed (`run --agent
  evidence_validator`, then `--agent competitor_research`, then `signalforge gaps`), and
  curating B2B software review / comparison domains into `sources.yaml` (category `reviews`)
  from live SERP probes.
- **M5:** the `commercial` split is settled ([plan.md §14](plan.md#14-open-decisions-defaults-chosen-change-here-if-needed),
  2026-10-10: `buyers` + `monetization`). Order: the pack economics entries, `buyer_research`, `monetization`, and finally
  `opportunity_scorer`.
- **M6:** schema and validator first (test them against seeded bad bullets before any LLM call),
  then section generation and rendering.
