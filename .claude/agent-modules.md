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
# src/signalforge/agents/__init__.py
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
| 4 | `evidence_validator.py` | `shortlist` (Gate 1), `verify` | fast / analysis | **yes** (`verify`) | M3 (`shortlist`), M4 (`verify`) | to build |
| 5 | `competitor_research.py` | `competitors` | analysis | **yes** | M4 | to build |
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
| `evidence/entailment.py` | verify, report | cheap-model entailment check of fact claims |
| `evidence/claims.py` | all analysis agents | helpers: `add_fact`, `add_inference`, `add_hypothesis`, `add_assumption`, `claim_table(run_id, ids)` |

### 0.5 The bounded tool loop (`agents/loop.py`)

`LLMService.parse` returns structured output only, so the loop is a **manual loop over structured
actions**, not provider-side function calling. Each step, the model sees the goal, what it has
collected so far and how much budget is left, and returns one action. Our own cached `search` /
`fetch` providers carry the action out, so results go through the cache, the source registry and
quote verification (plan §4).

```python
class LoopAction(BaseModel):
    action: Literal["search", "fetch", "finish"]
    query: str | None = None     # search: Turkish query, ≤ 10 words, `site:` only from offered domains
    url: str | None = None       # fetch: must be a URL already seen in this loop's search hits
    reason: str                  # English, ≤ 20 words; stored for debugging

class LoopObservation(BaseModel):
    step: int
    action: LoopAction
    hits: list[SearchHitView] = []   # id, url, title, snippet, domain tier
    page: PageView | None = None     # url, title, first N chars, or the error

@dataclass
class LoopBudget:
    max_steps: int
    max_searches: int
    max_fetches: int
    max_observation_chars: int   # older observations are summarised to title+url when exceeded

def run_loop(
    ctx: RunContext,
    *,
    stage: str,
    prompt: Prompt,
    goal: str,                     # serialised task context (problem, what is needed)
    budget: LoopBudget,
    allowed_domains: list[str],    # for `site:` and as a soft preference
    on_page: Callable[[Document, FetchedPage], None],  # agent-specific processing of each fetched page
) -> LoopTrace: ...
```

Mechanical guards in `run_loop`:
- reject a `fetch` URL that did not come from this loop's hits (no URLs from model memory),
- reject a duplicate query (Turkish-aware near-dup, same check as `query_gen`) and a duplicate URL,
- strip search operators except an allowed `site:`,
- stop when any budget runs out, after 2 rejected actions in a row, or on `finish`,
- write every search as a `Query` row (`intent` = `verify` or `competitor`, `meta.origin = "loop"`,
  `meta.problem_id`), and every fetched page as a `Document` through `evidence/documents.store_page`.

`LoopTrace` (steps, actions, rejections, stop reason) is stored in the stage's metrics, per problem.

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

**Follow-ups found in M1/M2, to build in this agent:**
- Re-tune `pain_signal_mix` from the per-query yield that M3 measures. Yield = signals kept per
  query, joined through `url_candidates.query_ids` → `documents` → `excerpts` → `signals`. Add
  `signalforge yield --run <id>` with a breakdown by `meta.signal_type`, `meta.source_hint` and
  `meta.origin`.
- Move `page_document` / `snippet_document` from `pipeline/stages/fetch.py` into
  `evidence/documents.py` (`store_page`), so the loop agents create documents the same way.

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
(`supports=[excerpt_id]`, `stage="extract"`).

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

class ExtractedSignals(BaseModel):
    signals: list[ExtractedSignal]   # may be empty; empty is the right answer for most pages
```

**Prompt content** (`prompts/extract.md`, v1):
- Signal types with one Turkish and one English example each (plan §5).
- Only B2B operational evidence counts. Consumer complaints, vendor marketing and SEO text give no
  signals. A vendor page can still give a `regulatory` signal if it quotes an official deadline.
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

**Reads.** `problem_clusters`, `signals` → `excerpts` → `documents`, `independence_groups`.

**Writes.**
- `independence_groups` with `rule="same_author"`. This stage deletes and rewrites only that rule;
  `dedupe` owns `near_dup` / `syndicated`.
- Per cluster: `independent_source_count`, `source_category_mix`, `evidence_strength`, and the new
  columns `status` (`shortlisted` | `insufficient`) and `gate` (JSON: the values and which check
  failed).

**Algorithm.**
1. **Independence** (`evidence/independence.py`): union-find over document ids. Merge documents that
   share a `near_dup` / `syndicated` group, documents whose excerpts share an `author_hash`, and
   (`same_thread`) documents with the same canonical URL path apart from page or post parameters.
   The number of independent sources for a cluster = the number of distinct components among its
   signals' documents.
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
   The weights live in `scoring/config.yaml` (§8). `strength_breakdown()` returns every component,
   so reports and the `gate` JSON can explain the number.
3. **Gate 1:** `status = shortlisted` when `evidence_strength ≥ min_strength` **and**
   `n_independent ≥ min_independent_sources`. Among those that pass, keep the top `max_shortlisted`
   by strength; any that pass but miss the cap are `insufficient` with `failed="cap"`. Every
   cluster that fails is kept for the report's "insufficient evidence" list.

**Config:**
```yaml
shortlist:
  min_strength: 5.0              # provisional; calibrate in M3 against labelled clusters
  min_independent_sources: 3
  max_shortlisted: 10
```

**Metrics:** clusters in, shortlisted, insufficient by reason (`strength` / `sources` / `cap`),
strength distribution, duplicate collapse from author grouping.

### 4.2 `verify` — bounded loop + entailment

**Reads.** Shortlisted clusters, their signals and fact claims, the plan, and the pack's source
registry.

**Writes.**
- `queries` (`intent="verify"`) and `documents` (via `store_page`, origin `verify`).
- New `excerpts` / `signals` / fact claims from the verify pages, extracted with
  `evidence/extraction.extract_document` (the same prompt as `extract`) and appended to that
  cluster's `signal_ids`.
- Entailment verdicts on the cluster's key claims, and the recomputed independence / strength.
- Counter-evidence is stored as signals too, with `meta.counter=true` (new `Signal.meta` JSON
  column). Evidence that a problem is already solved is evidence.

**Model.**
- Loop: `analysis` tier, prompt `prompts/verify_loop.md`, at most `max_steps` per problem.
- Extraction: `fast` tier, `prompts/extract.md`.
- Entailment: `fast` tier, `prompts/entailment.md`.

**Loop goal** (the `goal` passed to `run_loop`): the cluster's name, description, top signals
(statement + submarket + source category), the source categories it is missing, and three tasks:
1. Find more **independent first-hand** reports of the problem from sources the cluster doesn't
   have yet (forums, job ads, tool reviews).
2. Look for **disconfirming** evidence: is it already solved by common tools, or is it only one
   vendor's marketing?
3. If any signal is `regulatory`, fetch the **official** source (gib.gov.tr, resmigazete.gov.tr, …)
   that states the obligation.

`on_page` runs `extract_document` with the cluster as context and attaches the verified signals.

**Entailment** (`evidence/entailment.py`):
```python
class EntailmentVerdict(BaseModel):
    claim_id: int
    verdict: Literal["supported", "partial", "not_supported", "contradicted"]
    note: str   # English, ≤ 20 words

def check(ctx, claims: list[ClaimWithExcerpts], *, stage: str) -> list[EntailmentVerdict]
def entail_pending(ctx, claim_ids: list[int], *, stage: str) -> None  # checks those not yet checked
```
- Each prompt item is the claim statement plus the original quotes and translations of its
  supporting excerpts. Batches of `entailment.batch_size`.
- Writes `Claim.entailment_checked=True` and the new `Claim.entailment` column.
- `not_supported` / `contradicted` facts are kept but excluded from evidence counts and citations.
  `partial` facts may be cited, with a flag.
- Which claims count as **key**: the cluster's inference claim's top `key_claims_per_problem`
  supporting facts, preferring first-hand, high-tier and distinct sources.

**Algorithm, per shortlisted cluster** (clusters run in parallel up to `concurrency`; steps within a
loop run in order):
1. Run the loop with `LoopBudget(max_steps, max_searches, max_fetches, max_observation_chars)`.
2. Entailment-check the key claims.
3. Recompute independence and strength. If the cluster now fails Gate 1 (for example because key
   claims were not supported), set `status="insufficient"` with `failed="verify"`.

**Config:**
```yaml
verify:
  max_steps: 12
  max_searches: 15            # plan §4: ≤15 queries per problem
  max_fetches: 10
  max_observation_chars: 30000
  key_claims_per_problem: 5
  concurrency: 3
  max_output_tokens: 3000
entailment:
  batch_size: 10
  max_output_tokens: 4000
```

**Metrics:** per problem: steps, searches, fetches, new signals, new independent sources, counter
signals, strength before → after, entailment verdict mix, loop stop reason. Totals across problems.

**Tests** (`tests/test_evidence_validator.py`):
- Independence: same author on two domains → 1 source; a syndicated pair → 1 source.
- Strength: hand-computed fixtures, including unknown dates and a snippet-only penalty.
- Gate 1 outcomes, including the cap.
- Loop guards: a fetch of a URL that isn't in the hits is rejected, a duplicate query is rejected,
  budgets stop the loop.
- An entailment `not_supported` verdict removes the claim from the counts.

**Done when.** Gate 1 decisions are explainable from the `gate` JSON alone, and the M4 exit holds:
every gap-matrix cell is a cited fact or `unknown` (with `competitor_research`).

---

## 5. `agents/competitor_research.py` — Competitor research

**Job.** For each shortlisted problem, find the products that already address it, record what they
do and what they cost **as cited facts** from their own pages and reviews, and build a gap matrix
showing where they are weak. Competition is not automatically bad (plan §1).

**Stages.** `competitors` (`pipeline/stages/competitors.py`).

**Reads.** Shortlisted clusters and their signals. `tool_complaint` signals name vendors, which are
the best seeds. Also the pack registry (`reviews`, `complaints` categories).

**Writes.**
- `queries` (`intent="competitor"`), `documents` (origin `competitor`).
- `competitors`: name, url, segment, geo, and `pricing[]` entries of
  `{amount, currency, period, plan_name, observed_at, claim_id}`.
- Fact claims per feature / price / weakness (`stage="competitors"`).
- `gap_matrices`: `{dimensions, competitor_ids, cells: {dim: {competitor_id: {value, claim_id}}}}`,
  where every cell is a claim or `{"value": "unknown"}`.

**Model.**
- Loop: `analysis` tier, `prompts/competitors_loop.md`.
- Page facts: `fast` tier, `prompts/competitor_facts.md`.
- Matrix: `analysis` tier, `prompts/gap_matrix.md`.

**Schemas** (`domain/competitors.py`):
```python
class CompetitorFact(BaseModel):
    kind: Literal["feature", "price", "segment", "integration", "limitation", "review_complaint"]
    quote: str                 # verbatim from the page
    statement: str             # English
    amount: float | None = None      # price only
    currency: str | None = None
    period: Literal["month", "year", "one_time", "per_user_month", "per_document", "unknown"] | None = None
    plan_name: str | None = None

class CompetitorPage(BaseModel):
    competitor_name: str | None    # null if the page is not about a product
    product_url: str | None
    facts: list[CompetitorFact]

class GapDimensionDraft(BaseModel):
    key: str
    label: str                 # English, e.g. "e-İrsaliye integration", "Price for ≤10 users"
    from_signal_ids: list[int] # the problem signals this dimension comes from

class GapCell(BaseModel):
    dimension: str
    competitor_id: int
    value: Literal["yes", "partial", "no", "unknown"]
    claim_id: int | None       # required unless value == "unknown"

class GapMatrixDraft(BaseModel):
    dimensions: list[GapDimensionDraft]
    cells: list[GapCell]
```

**Algorithm, per shortlisted cluster:**
1. **Seeds:** vendor names from `tool_complaint` signals of the cluster, plus up to
   `model_seed_names` names the model suggests. The suggested names are search seeds only; a
   competitor is stored only after one of its own pages has been fetched.
2. **Loop** (`run_loop`) with the goal: find up to `max_competitors` products serving this
   workflow for this actor in Turkey (international products sold in Turkey count; `geo` records
   this). For each, fetch the home page, pricing page, feature/docs page and one review/complaint
   source.
3. `on_page`: run `competitor_facts` and verify each quote with `evidence/quotes.py` (unverifiable
   → dropped). Attach the page to the competitor by `competitor_name`, matched Turkish-aware
   against the existing competitors of this problem. Write the facts as claims. Price facts also
   go into `pricing[]` with `observed_at = document.fetched_at`. Prices are never converted here;
   `monetization` converts with a dated rate.
4. **Matrix:** derive 4–8 dimensions from the cluster's signals (what users say goes wrong), plus
   the config dimensions that always apply (`always_dimensions`). Ask the model for cells, citing
   **only** existing claim ids of that competitor.
5. **Validate:** every non-`unknown` cell must cite a claim that is a fact, belongs to that
   competitor, and has not failed entailment. Anything else becomes `unknown` (counted). Each
   dimension must come from at least one signal or from `always_dimensions`.
6. Add an inference claim per **gap** (a dimension where no competitor is `yes`, and at least one
   competitor has a non-`unknown` cell), derived from those cell claims. `opportunity_scorer` reads
   these for "competition gap".

**Config:**
```yaml
competitors:
  max_competitors: 5            # plan §4
  model_seed_names: 5
  max_steps: 20
  max_searches: 12
  max_fetches: 20
  max_observation_chars: 30000
  always_dimensions: [price_for_smb, turkish_localization, e_document_integration, setup_effort]
  concurrency: 3
  max_output_tokens: 4000
```

**Metrics:** per problem: competitors found, pages fetched, facts proposed / verified, price
observations, matrix cells by value, cells demoted to `unknown`, gaps found.

**Tests** (`tests/test_competitor_research.py`):
- A competitor named only by the model is never stored without a fetched page.
- A cell citing a claim from another competitor is demoted to `unknown`.
- A price keeps its currency, period and observed date.
- A gap is derived only when evidence exists, not from all-`unknown` columns.

**Done when.** This is the M4 exit: every gap-matrix cell is a cited fact or `unknown`.

---

## 6. `agents/buyer_research.py` — Buyer research

**Job.** For each shortlisted problem, decide **who** would buy a solution: the segments, and per
segment the user, buyer, decision maker and economic beneficiary (plan §2, kept from the vision),
plus the budget owner, how reachable those buyers are, and how many companies have the problem. It
proposes 1–3 **opportunities** (problem × segment × solution angle).

> **Divergence from plan.md:** plan §6 merges buyer research, monetization and WTP into one
> `commercial` stage, to limit error compounding. Here they are two agents with a narrow hand-off:
> buyer_research writes `Opportunity` rows with cited buyer roles, and monetization only adds the
> economics. If this split causes compounding errors in M5 evaluation, merge them back. On
> adoption, update plan §4, §6 and §12 (`commercial` → `buyers` + `monetization`).

**Stages.** `buyers` (`pipeline/stages/buyers.py`).

**Reads.** Shortlisted clusters; signals (`actor`, `labor_spend` job ads, which say which role does
the work and often who they report to); competitor `segment` facts; gap inference claims; the plan's
`buyer_hypotheses`; the request's `target_customer`.

**Writes.**
- `opportunities`: `segment`, `solution_angle`, `buyer_roles`, plus the new columns
  `accessibility` (JSON) and `market_breadth` (JSON).
- Claims: `fact` where a verified excerpt states the point; `inference` where it is derived from
  facts; `hypothesis` otherwise. A hypothesis is allowed but always labelled.
- Optional market-breadth documents and claims (step 3).

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
    budget_owner: RoleClaim | None     # null = no identifiable budget owner → Gate 2 knock-out

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

**Prompt content** (`prompts/buyers.md`, v1):
- Input is a claim table (id, kind, statement), never raw text, so the model can only cite what
  exists.
- One segment per opportunity, and segments must not overlap.
- Prefer the request's `target_customer`.
- A role is only a fact if a claim says it, e.g. a job ad saying "operasyon müdürüne bağlı".
- The solution angle must be buildable as B2B SaaS and must name which gap it targets.
- Do not invent statistics; fill `breadth_hint` instead.

**Algorithm, per shortlisted cluster:**
1. Build the claim table: the cluster's facts (only non-failed entailment), gap inferences, and
   competitor segment facts. Add the plan's buyer hypotheses as `hypothesis` claims
   (`stage="plan"`) so they can be cited and labelled as hypotheses.
2. Call the model → `BuyerAnalysis`.
3. **Market breadth** (optional, deterministic, not a loop): for each `breadth_hint`, run up to
   `breadth_queries` fixed-template searches against the pack's statistics sources (TÜİK, TOBB,
   associations). Fetch the top hits, and extract numeric facts with verbatim quotes using a
   `fast` call with prompt `prompts/breadth_facts.md`. Store them as fact claims; if nothing is
   found, store `{"status": "unknown"}`.
4. **Validate:** every cited id exists, belongs to the run and is not failed. A role with no
   `claim_ids` must have a `hypothesis`. Drop opportunities whose segment duplicates another
   (Turkish-aware near-dup). Cap at `max_opportunities_per_problem`.
5. Write the `Opportunity` rows. Store `buyer_roles` with the claim ids, and create a `hypothesis`
   claim for each role hypothesis so the report can cite it.

**Config:**
```yaml
buyers:
  max_opportunities_per_problem: 3
  breadth_queries: 3
  breadth_fetches: 4
  max_output_tokens: 6000
```

**Metrics:** opportunities per problem; share of roles backed by facts vs hypotheses; budget owner
identified (count); channels per opportunity; breadth found / unknown.

**Tests** (`tests/test_buyer_research.py`):
- A role that cites a non-existent claim is rejected.
- A role with neither citation nor hypothesis is rejected.
- A null budget owner is preserved for Gate 2.
- Duplicate segments collapse.

**Done when.** Each opportunity's buyer roles can be traced to cited claims or are explicitly
labelled hypotheses (part of the M5 exit).

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
| `problem_clusters` | `status` String(16) (`shortlisted` / `insufficient`), `gate` JSONB | evidence_validator (M3) |
| `signals` | `meta` JSONB default `{}` (`counter`, `origin`, `submarket`) | problem_discovery, evidence_validator (M3) |
| `claims` | `entailment` String(16) nullable, `meta` JSONB default `{}`, index on `(run_id, kind)` | evidence_validator (M4), monetization (M5) |
| `opportunities` | `status` String(16), `knockouts` JSONB, `accessibility` JSONB, `market_breadth` JSONB | buyer_research, monetization (M5) |
| `score_cards` | `experiment` JSONB | opportunity_scorer (M5) |
| `documents` | `origin` String(16) default `collect` (`collect` / `verify` / `competitor` / `buyers`), `problem_id` nullable FK | loop agents (M4) |
| `excerpts` | — (snippet text for snippet-only documents comes from `url_candidates.snippet`) | — |

The pack also needs `economics.yaml` entries for `usd_try` and at least one wage reference per
target role, each with `as_of` and `source_url` (M5).

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
| **M4** Verification & competitors | [evidence_validator](#4-agentsevidence_validatorpy--evidence-validator) part 2: `verify` + entailment · [competitor_research](#5-agentscompetitor_researchpy--competitor-research): `competitors` | `agents/loop.py`, `evidence/entailment.py` | `claims.entailment/meta`, `documents.origin/problem_id` | competitor_research (gap-matrix cells) |
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
  `verify`, then `competitors`.
- **M5:** settle the `commercial` split ([plan.md §14](plan.md#14-open-decisions-defaults-chosen-change-here-if-needed))
  first. Then the pack economics entries, `buyer_research`, `monetization`, and finally
  `opportunity_scorer`.
- **M6:** schema and validator first (test them against seeded bad bullets before any LLM call),
  then section generation and rendering.
