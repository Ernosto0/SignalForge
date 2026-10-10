# SignalForge — Architecture & Build Plan (v0)

> Status: approved design baseline, pre-code. Supersedes the original vision draft.
> Rule for implementers: if code and this file disagree, fix one of them in the same change.
> Companion: [agent-modules.md](agent-modules.md) groups the stages below into 9 agent modules with
> build-ready specs. Each milestone in §13 says which agents it builds; the same mapping, seen from
> the agent side, is in [agent-modules.md §11](agent-modules.md#11-build-order-by-milestone).
> Keep the two files in step.

---

## 1. What SignalForge is

An open-source, self-hostable research engine that finds **evidence-backed** business problems in a
market and evaluates whether they are worth building a product around. First market: **Turkey**.

> Evidence first. Ideas second. "Don't build this" is a successful result.

Core principles (unchanged from the vision draft, now enforced mechanically — see §7):

1. **Evidence over imagination** — every important claim traces to a stored source excerpt.
2. **Search first, reason second** — the LLM reasons over collected evidence; it is never the source of truth.
3. **Fact / inference / hypothesis are separate types** in the data model, not just in prose.
4. **Competition is not automatically bad** — the question is where existing solutions are weak.

Target user: solo founders, indie hackers, small teams, agencies.

---

## 2. Review of the original plan — problems and changes

| # | Problem in original plan | Change |
|---|---|---|
| 1 | **Language blind spot.** Target is Turkey, but pain phrases, source list and examples are English. Turkish B2B pain is written in Turkish, on Turkish sites. | Introduce **market packs** (§9): per-country language, pain-phrase lexicon, source registry with quality tiers, search locale, currency, wage references. Turkey is pack #1. All matching code is Turkish-aware (İ/ı casefolding). |
| 2 | **Source list is not accessible in practice.** LinkedIn forbids scraping; Reddit API is restricted and has little Turkish B2B content; Google reviews/YouTube need separate APIs. 50 adapters = 50 maintenance burdens. | V0 uses **one SERP API + a generic page fetcher**, steered by `site:` queries and the pack's source registry. Login-walled sites are used as **snippet-only** evidence. Platform-specific adapters come later, behind the same interface. |
| 3 | **B2C bias.** Complaint sites (Şikayetvar etc.) are overwhelmingly consumer complaints; the product targets B2B SaaS. | Prioritise B2B-native signals: **job postings** (proof of labor spend on manual work), **regulatory mandates** (e-Fatura/e-İrsaliye/e-Defter, KVKK — a major B2B SaaS driver in Turkey), **reviews of existing B2B tools**, professional forums, trade associations. Signals are typed (§5) so a job posting and a complaint support different claims. |
| 4 | **Hallucination prevention is a rule, not a mechanism.** "Never invent evidence" cannot be enforced by prompt alone. | (a) Extracted quotes must be found **verbatim** in the fetched text or they are discarded. (b) A **Claim** entity with `kind ∈ {fact, inference, hypothesis, assumption}`; facts must link to verified excerpts. (c) The report writer can only cite claim IDs; a validator rejects uncited factual bullets. (§7) |
| 5 | **Evidence model mixes levels.** `relevance` and `problem_signal` sit on the source object, but one page contains many signals; `evidence_count` ignores independence. | Split **Document → Excerpt → Signal → Claim**. Count **independent sources** (after near-duplicate collapse and author/domain grouping), never raw rows. |
| 6 | **Linear pipeline (§6) contradicts iterative funnel (§31).** No gates, no budgets. | A **staged funnel with explicit gates and per-stage caps** (§4). Expensive stages only run on survivors. Hard per-run USD budget enforced by the runner. |
| 7 | **Scoring is false precision.** A weighted sum of LLM-produced 1–10 numbers is uncalibrated and unstable between runs. Evidence strength is both a score factor and confidence (double counted). Market size is missing despite "market too small" being a listed failure mode. Founder constraints are mixed into attractiveness. | Three separate outputs: **Attractiveness score**, **Confidence**, **Founder fit** (§8). Evidence strength is **computed deterministically** and feeds confidence + a hard gate, not the weighted score. LLM judgments use **anchored 1–5 rubrics** with mandatory claim citations; uncited judgments are capped. Add **market breadth** factor. Categories are assigned by **rules**, not by the LLM. |
| 8 | **"Agents" framing invites over-engineering.** 9 agents, most of which are one structured LLM call. | Call them **stages**: typed input → typed output, persisted. Only two places get a bounded tool-use loop (verification search, competitor research). Buyer + monetization + WTP merge into one **commercial analysis** stage (fewer hops = less error compounding). |
| 9 | **"Evidence validation agent" asks an LLM questions it can't answer** ("is the source real?", "are these duplicates?"). | Answer mechanically where possible: we fetched it (real), quote matched (supported), MinHash (duplicate), domain+author grouping (independence), date parse (recency). The LLM only does **entailment** ("does this excerpt support this claim?") with a cheap model. |
| 10 | **Embeddings/pgvector assumed, **, and Turkish needs a multilingual model. At V0 scale (≤ a few hundred signals) vectors are unnecessary. | V0: **LLM clustering** with deterministic validation (every signal assigned exactly once), MinHash for dedup. No vector store in V0. Embeddings (OpenAI embeddings or a local multilingual model) arrive in V2+ when cross-run / continuous monitoring needs them. |
| 11 | **Celery + Redis + Next.js + FastAPI before the core works.** Two languages and four services for a product whose only open question is research quality. | V0 is a **Python CLI** writing Markdown/HTML + JSON reports. Postgres is the only service. The runner is resumable so an API/worker can wrap it later (Postgres job table with `SKIP LOCKED`, no Redis). UI is the last milestone. |
| 12 | **No way to measure or improve quality.** Every prompt change costs money and is non-deterministic; §36 evaluation is fully manual. | **Record/replay cache** for search, pages and LLM calls; frozen **fixture markets**; per-stage metrics; a small **labeling CLI** so human judgments accumulate as data (§11). |
| 13 | **Research state is a bag of counters.** Can't resume, re-run a stage, or attribute cost. | `research_runs` + `stage_runs` (status, input hash, cost, error, timings). Counters are queries over stored rows. |
| 14 | **Planner output is unchecked LLM text.** Generic submarkets → generic queries → generic results. | **Human checkpoint**: `plan` writes an editable YAML; `run` consumes it. Cheapest quality lever in the system. |
| 15 | **Monetization numbers have no provenance**; TRY inflation makes undated prices meaningless. | Economic model is a formula over **assumption claims**, each with source or "unsourced" label. All money stored with currency **and date**; USD conversion uses a dated rate. Output is a range, never a point. |
| 16 | **Validation experiment is free-form.** | Mechanically chosen: target the claim with the highest *importance × uncertainty* (usually an unsourced assumption or a hypothesis behind WTP). |
| 17 | **Privacy / copyright under-specified.** Author names of complainants are personal data (KVKK). | Store salted **author hash** only (for independence counting). Full page text lives in a local cache (needed for quote verification and replay), never in exported reports; reports contain short excerpts + URLs. `purge` command. Respect robots.txt; no login bypass. |
| 18 | **Duplicate module layout** (`services/search.py` and `research/search.py`; domain models mixed with persistence). | Layout in §12: `providers/` (I/O), `pipeline/stages/`, `domain/` (Pydantic), `db/` (ORM), `evidence/`, `scoring/`. |

Kept as-is from the vision: opportunity categories, fact/inference/hypothesis distinction, competitor gap
matrix, buyer roles (user / buyer / decision maker / economic beneficiary), cheap-vs-strong model split,
Apache-2.0, bring-your-own keys, "what not to build" list.

---

## 3. V0 scope

**In:** CLI, one market pack (TR), one search provider, generic fetcher, full funnel through final
report, cost ledger + budget cap, record/replay cache, labeling CLI, eval fixtures.

**Out (unchanged list, plus):** billing, teams, auth, marketplace, autonomous browser agents,
platform-specific scrapers, vector search, continuous monitoring, web UI (until M8), multi-provider
LLM implementations (interface only).

**Input (research request):**

```yaml
country: TR                # selects market pack
industry: logistics
problem_area: null         # optional focus
target_customer: SMB
business_model: B2B SaaS
founder:
  team: solo developer
  budget: low
  mvp_months: 3
  preferred_tech: [AI, automation]
  min_customer_value_usd_month: 50
report_language: en        # quotes stay in original language + translation
budget_usd: 10             # hard cap for the run
```

**Output:** `report.md` / `report.html` (human), `report.json` (source of truth), plus the stored
evidence graph in Postgres.

---

## 4. Pipeline — a funnel with gates

```
request ─► [plan] ─► human edits plan.yaml
                │
                ▼
         [query_gen]  submarkets × pack pain phrases × source hints        cap ≈150 queries
                ▼
         [search]     SERP API, locale from pack, cached                   ≈10 results/query
                ▼
         [triage]     cheap model on title+snippet+domain tier             keep ≤200 URLs
                ▼
         [fetch]      httpx + trafilatura, robots.txt, per-domain limits   ≤200 documents
                ▼
         [extract]    cheap model → typed signals with verbatim quotes;    quotes verified,
                      failed quotes dropped                                 unverifiable dropped
                ▼
         [dedupe]     URL canonicalisation, MinHash near-dups, independence groups
                ▼
         [cluster]    LLM clustering, validated assignment                 ≤25 problem clusters
                ▼
  ══ GATE 1 ══ evidence strength ≥ threshold AND ≥3 independent sources   ≤10 shortlisted
               AND ≥2 independent sources with a software-fit signal (evidence/fit.py)
                ▼                          (the rest → "insufficient evidence" list, kept in report)
         [verify]     bounded loop: targeted 2nd-round searches per problem (in Turkish),
                      entailment check of key claims                        ≤15 queries/problem
                ▼
         [competitors] bounded loop: find products, fetch home/pricing/docs/reviews,
                      extract features + prices as facts, build gap matrix  ≤5 competitors/problem
                ▼
         [buyers]     buyer roles, budget owner, channels, market breadth
                      → opportunities (problem × segment × angle)
                ▼
         [monetization] economic model (ranges over dated assumptions), WTP signals
                ▼
  ══ GATE 2 ══ knock-outs: no budget owner, value ceiling < min customer value
                ▼
         [score]      deterministic + rubric judgments → attractiveness, confidence,
                      founder fit, category
                ▼
         [report]     top 3–5 full reports; others summarised incl. "Don't build" with reasons
```

Every cap and threshold lives in config (`config/defaults.yaml`) and is overridable per run.
Each stage is idempotent: `run(ctx) -> StageResult`, reads its inputs from the DB, writes outputs keyed
by `run_id`, and records a `stage_run`. `signalforge run --resume` skips completed stages;
`--from <stage>` deletes downstream outputs and re-runs.

**Bounded loops** (verify, competitors) are manual tool-use loops over *our own* `search` and `fetch`
tools — not the LLM provider's built-in web search — so results go through our cache, source registry and
quote verification. Hard caps: max steps, max fetches, max tokens per problem.

---

## 5. Domain model

All stage I/O is Pydantic (`domain/`); persisted via SQLAlchemy (`db/`) with JSONB for payloads.

| Entity | Key fields | Notes |
|---|---|---|
| `ResearchRun` | request, plan, pack_version, config_hash, status, budget_usd, spent_usd | |
| `StageRun` | run_id, stage, status, input_hash, started/finished, cost_usd, error, metrics (JSON) | progress + resume |
| `Query` | run_id, text (incl. any `site:` suffix), lang, intent (`pain` / `verify` / `competitor` / `regulatory` / `jobs`), submarket, meta (`signal_type`, `source_hint`, `origin` = `llm` / `pack_seed`) | yield tracked per query, broken down by meta |
| `SearchResult` | query_id, url, title, snippet, rank, provider | |
| `Document` | canonical_url, domain, source_category, quality_tier, published_at (nullable), fetched_at, text_hash, lang, `snippet_only` flag | full text in cache table, not exported |
| `Excerpt` | document_id, quote (original), translation, char_start/end, verified (`exact` / `fuzzy`), author_hash | ≤ ~500 chars |
| `Signal` | excerpt_id, type, actor (role / company type as stated), workflow, statement, first_hand (bool) | types below |
| `IndependenceGroup` | id, rule (`same_author` / `same_quote` / `syndicated` / `near_dup`), member document ids | |
| `ProblemCluster` | run_id, name, description, signal_ids, independent_source_count, source_category_mix, evidence_strength | |
| `Claim` | kind (`fact` / `inference` / `hypothesis` / `assumption`), statement, supports (excerpt ids), derived_from (claim ids), stage, entailment_checked | §7 |
| `Competitor` | name, url, segment, geo, pricing (amount, currency, period, observed_at) | attributes stored as fact claims |
| `GapMatrix` | problem_id, dimensions × competitors → value + claim_id | |
| `Opportunity` | problem_id, segment, solution_angle, buyer roles, economic model, wtp signals | one problem → possibly several opportunities |
| `ScoreCard` | factor levels + justifications + claim ids, attractiveness, confidence, founder_fit, category, rule trace | fully explainable |
| `LLMCall` | stage, model, prompt_id@version, input/output/cached tokens, cost, latency, cache_hit | cost ledger |
| `Label` | target (signal / cluster / opportunity), labeler, value, note | human eval data |

**Signal types:** `complaint` (first-hand pain), `workaround` (describes manual process / tool combo),
`labor_spend` (job posting for manual work), `wish` ("keşke…", "… yok mu?"), `tool_complaint`
(review/complaint about an existing product), `price_signal` (mentions paying / price / budget),
`regulatory` (new obligation or deadline). Each type maps to what it can support, e.g.
`labor_spend` → "money is already spent on this workflow"; `tool_complaint` → competitor weakness.

Timestamps (`published_at`, `fetched_at`, first/last seen) are kept everywhere so V3 continuous
monitoring is possible without a schema rewrite.

---

## 6. Stages — contracts

| Stage | Model tier | Input → Output | Mechanical guards |
|---|---|---|---|
| plan | analysis | request + pack industry seeds → submarkets, research questions, buyer hypotheses, plan.yaml | human checkpoint |
| query_gen | fast | plan × pack pain phrases × source hints → queries (Turkish); one call per submarket, pack regulatory seeds added verbatim | operators stripped, `site:` hints only from the registry domains offered for the intent, Turkish-aware dedupe (exact + near-dup), per-submarket / intent / signal-type quotas, cap |
| search | — | queries → results | cache, rate limit |
| triage | fast (batch-eligible) | title/snippet/domain → keep/drop + reason | domain tier pre-filter (low tier needs stronger signal) |
| fetch | — | URLs → documents | robots.txt, per-domain concurrency, size limit, lang detect |
| extract | fast (batch-eligible) | document text → excerpts + signals | **quote must match source text**; non-matching dropped and counted |
| dedupe | — | documents/excerpts → independence groups | MinHash (shingles), canonical URL, author hash |
| cluster | analysis | signals (id + statement) → clusters | every signal id assigned exactly once; chunk + merge if >400 signals |
| shortlist | — | clusters → shortlist | Gate 1 (deterministic) |
| verify | analysis + fast | problem → extra evidence, entailment-checked claims | step/fetch caps |
| competitors | analysis | problem → competitors, feature/price facts, gap matrix | every matrix cell is a claim or `unknown` |
| buyers | analysis | problem + evidence + competitors → opportunities, buyer roles, channels, market breadth | every role cites a claim or is a labelled hypothesis |
| monetization | analysis (+ code) | opportunity + evidence + competitor prices → economic model, WTP, Gate 2 | assumptions explicit; ranges only; code computes every number |
| score | analysis (+ code) | opportunities → ScoreCards | §8 rules; uncited judgments capped |
| report | synthesis | ScoreCards + claim table → report.json → md/html | citation validator (§7) |

Prompts live in `prompts/<stage>.md` with a version header; the version is recorded on every `LLMCall`.

Each stage belongs to exactly one agent module ([agent-modules.md §0.4](agent-modules.md#04-overview)):
`plan` → planner; `query_gen`…`dedupe` → source_discovery; `extract`, `cluster` → problem_discovery;
`shortlist`, `verify` → evidence_validator; `competitors` → competitor_research; `buyers` →
buyer_research; `monetization` → monetization; `score` → opportunity_scorer; `report` → report_writer.

---

## 7. Evidence integrity (hallucination prevention)

1. **Verbatim quotes.** Extraction returns quotes; `evidence/quotes.py` verifies them against the
   fetched text after normalisation (Unicode NFKC, whitespace collapse, quote/dash unification,
   **Turkish-aware casefold**: treat `I/ı/İ/i` as one class for matching — Python `str.lower()` is wrong
   for Turkish). Exact match → `exact`; else rapidfuzz partial ratio ≥ 95 → `fuzzy` (flagged); else drop.
2. **Typed claims.**
   - `fact`: ≥1 verified excerpt. Entailment-checked (cheap model) for any fact that reaches a report.
   - `inference`: derived from ≥1 fact claim; shows its chain.
   - `hypothesis`: may have no support; always rendered as such.
   - `assumption`: numeric input to the economic model (e.g. hourly cost); sourced or marked unsourced.
   - Model background knowledge (e.g. "Competitor X exists") is only a **search seed**; it becomes a
     fact only after we fetch a page that states it.
3. **Independence.** Evidence counts use independence groups: same author, syndicated/copied text
   (MinHash ≥ threshold), the same quoted passage on several pages (`same_quote`, ≥ 8 words: one
   complaint shown on several listing pages of a complaint site), and same-origin reposts collapse
   to one source.
4. **Report writer is constrained.** It receives the claim table and returns `report.json`, where every
   bullet carries `claim_ids`. The validator rejects / regenerates when: a factual section bullet has no
   citation; a number or proper noun in the bullet doesn't appear in its cited claims; a hypothesis is
   phrased as a fact (kind mismatch). Only *Proposed product, MVP, Validation experiment* sections may
   contain uncited recommendations, and they are labelled as such.
5. **Insufficient evidence is an output.** Clusters failing Gate 1 are listed with what was found;
   the system says "Evidence insufficient" rather than filling gaps.

---

## 8. Scoring

Three independent outputs per opportunity — never multiplied together:

### 8.1 Evidence strength (deterministic, 0–10) → feeds Confidence and Gate 1

Computed in `evidence/strength.py` from:
- independent source count (log-scaled, saturates ≈10)
- source category diversity
- quality-weighted share (tier weights: high 1.0, medium 0.6, low 0.2; snippet-only × 0.5)
- recency (share within 24 months; unknown date = 0.5)
- directness (share of first-hand signals)

Weights in `scoring/config.yaml`.

### 8.2 Attractiveness (0–100) — "how good is this *if* the claims are true"

Each factor is a **1–5 level on an anchored rubric** (`scoring/rubric.yaml`, each level described
concretely). The judging model must cite claim IDs per factor; a factor with no cited fact claim is
capped at level 2 and flagged. For final candidates, judge k=3 times and take the median; the spread
feeds Confidence.

| Factor | Weight | Basis |
|---|---|---|
| Problem severity | 20 | rubric over signals |
| Economic impact | 20 | economic model range → level |
| Frequency | 10 | rubric (daily → rare) |
| Competition gap | 15 | gap matrix; each gap needs ≥1 competitor fact |
| Willingness to pay | 15 | paid competitors exist, observed prices, labor spend, price signals |
| Customer accessibility | 10 | identifiable + reachable buyers (associations, directories, communities) |
| Market breadth | 10 | how many companies have it (registries/statistics where available, else rubric) |

### 8.3 Confidence (Low / Medium / High)

From evidence strength, share of factor judgments resting on hypotheses/unsourced assumptions, and
judgment spread.

### 8.4 Founder fit (Fit / Stretch / Not fit) — from the request's constraints

MVP feasibility within `mvp_months` for the stated team, price ceiling ≥ `min_customer_value`,
hard barriers (licences — e.g. GİB özel entegratör for e-invoicing — certifications, deep enterprise
integrations), sales motion vs team. Shown alongside, used as a filter, not part of the score.

### 8.5 Category (rule-based, thresholds in config, calibrated after first runs)

- **False positive / insufficient evidence** — failed Gate 1 or entailment checks.
- **Weak** — no identifiable budget owner, or economic value ceiling < min customer value, or severity ≤ 2.
- **Competitive** — competition gap ≤ 2 while WTP ≥ 4.
- **Strong opportunity** — attractiveness ≥ 75, evidence strength ≥ 7, budget owner identified, gap ≥ 3.
- **Interesting** — everything else.

The ScoreCard stores which rule fired (rule trace) so every category is explainable.

### 8.6 Validation experiment

Pick the claim with max (factor weight × uncertainty) — usually a WTP hypothesis or unsourced economic
assumption — and recommend the cheapest experiment from the catalogue (interviews, outreach,
landing page, concierge, paid pilot) that tests it, with a pass/fail threshold.

---

## 9. Market packs

```
market_packs/tr/
  pack.yaml           # language: tr, country: TR, currency: TRY, search locale (gl=tr, hl=tr),
                      # default report language, version
  pain_phrases.yaml   # "Excel'de takip ediyoruz", "elle giriyoruz", "manuel olarak",
                      # "WhatsApp üzerinden", "çok vakit alıyor", "… programı yok mu",
                      # "keşke", "çok pahalı", "her seferinde tekrar", "telefonla arayıp" …
  sources.yaml        # domain → category, quality tier, access mode (fetch / snippet_only), notes
  economics.yaml      # dated wage / cost references WITH source URLs (manually maintained)
  regulatory.yaml     # official sources + known mandates as search seeds
  industries/
    logistics.yaml    # optional submarket + terminology seeds
```

Candidate TR sources to verify during M1 (tier is a starting guess):
- **High:** resmigazete.gov.tr, gib.gov.tr, TÜİK, TOBB, trade associations (e.g. UTİKAD, UND for logistics)
- **Medium:** kariyer.net / secretcv / yenibiris (job postings), sikayetvar.com (B2C-heavy; use for
  `tool_complaint` on B2B products), Google Play reviews of B2B apps, technopat.net / donanimhaber.com
  forums, industry blogs/publications, LinkedIn (snippet-only)
- **Low:** eksisozluk.com (useful but anonymous/opinion), SEO listicles, idea sites, AI-generated content

Adding a country = adding a pack; no code changes expected.

---

## 10. Providers

**LLM** (`providers/llm.py`): a thin `LLMClient` protocol with one implementation (OpenAI Python SDK,
Responses API).
- Structured output only: Pydantic schemas via `client.responses.parse(..., text_format=Model)`,
  read from `response.output_parsed`.
- Tiers mapped in config (`LLM_MODEL_*` env vars), not hard-coded. **During development every tier
  defaults to `gpt-6-luna`** ($0.10 / $0.50 per MTok) so iterating on prompts and stages stays cheap:
  - `fast`: triage, extraction, entailment, query gen
  - `analysis`: clustering, verification, competitors, buyers, monetization, scoring
  - `synthesis`: final report
- Stronger models are opt-in per tier via env, for quality-gate runs (M3 exit labeling, M7 evaluation)
  and for production defaults once we've measured what they buy on the replay fixtures (§11):
  `LLM_MODEL_ANALYSIS=gpt-6.1-sol` ($2 / $10, 20× luna), `LLM_MODEL_SYNTHESIS=gpt-6.1-sol` or
  `gpt-6-astra` ($10 / $50, 100× luna). If luna fails a stage's mechanical guards (e.g. cluster
  assignment validation) too often to develop against, raise only that tier.
- **Batch API** (`/v1/responses`, 50% cheaper, ≤24 h window) for triage/extract when `--batch` is set;
  sync with bounded concurrency is the default for dev iteration.
- **Prompt caching** is automatic on OpenAI for repeated prefixes: keep the stable system prompt + pack
  context first, per-item content last.
- **Jev (planned, post-V0):** a `JEV_API_KEY` is reserved. Once the pipeline works, evaluate adding Jev
  steps or routing specific stages to Jev. Because stages call a tier (`fast` / `analysis` / `synthesis`)
  rather than a provider, this should be a second `LLMClient` implementation plus per-stage routing in
  config, compared on the replay fixtures (§11) before switching any stage.
- Every call → `LLMCall` row; the runner checks `spent_usd` against `budget_usd` before each call and
  stops cleanly (resumable) when exceeded.

**Search** (`providers/search/`): `SearchProvider.search(query, locale, n) -> list[SearchHit]`
(`SearchHit` is the provider-level result; the run-scoped `SearchResult` row is written by the search stage).
Default: **Serper** (serper.dev), Google results with `gl=tr`, `hl=tr` (no `google_domain` parameter).
Chosen over SerpApi on 2026-10-07 after an A/B on run #4's 192 queries: no timeouts (SerpApi: 12 of 42
loop searches), `site:` always honoured (SerpApi: 49%), ≈ 25× cheaper per search. **SerpApi** stays
selectable (`SEARCH_PROVIDER=serpapi`, adds `google_domain=google.com.tr`). Other providers (e.g. Brave)
can be added behind the same interface later.

**Fetch** (`providers/fetch.py`): httpx + trafilatura, robots.txt, per-domain rate limits, no JS
rendering in V0 (JS-heavy pages are skipped and counted).

---

## 11. Cache, replay and evaluation

- **Cache tables:** search responses keyed by (provider, query, locale, params); documents by canonical
  URL; LLM responses by (model, prompt_id@version, input hash). TTLs in config.
- **Modes:** `live` (default), `record`, `replay` (cache-only; a miss is an error). Replay makes reruns
  deterministic and free for everything upstream of the stage being changed.
- **Fixtures:** `signalforge fixture export <run>` freezes a run's searches + documents. Golden markets:
  TR logistics, accounting, e-commerce.
- **Per-stage metrics** (stored in `StageRun.metrics`): query yield, fetch success rate, signals/doc,
  quote-verification pass rate, first-hand share, duplicate collapse rate, cluster count, claim-kind mix,
  % report bullets cited, cost per stage.
- **Labeling CLI:** `signalforge label signals|clusters|opportunities <run>` — quick y/n/1–5 judgments
  stored as `Label`; the §36 founder checklist becomes the opportunity rubric. Precision is tracked over
  time instead of re-judged from scratch.

---

## 12. Tech stack and layout

Backend: Python 3.12, uv, FastAPI + uvicorn, Pydantic v2 + pydantic-settings, SQLAlchemy 2 + Alembic,
psycopg 3, Postgres 16 (docker compose, host port 5433), httpx, trafilatura, datasketch (MinHash),
rapidfuzz, openai SDK, Typer (CLI), Jinja2 (reports), pytest, ruff.
Frontend: Next.js 16 (App Router, TypeScript, Tailwind), pnpm. Server components call FastAPI directly;
browser calls go to `/api/*`, which Next.js rewrites to FastAPI (no CORS).
Apache-2.0. Bring-your-own keys via root `.env` (see `.env.example`).

```
docker-compose.yml
.env.example
frontend/              # Next.js app (src/app, src/lib/api.ts)
backend/
  pyproject.toml
  alembic.ini
  config/defaults.yaml
  market_packs/tr/...
  src/signalforge/
    cli.py               # typer: version, serve, search, fetch, llm-check, ledger, purge-cache,
                         # (later) plan / run / label / fixture
    config.py            # pydantic-settings (root .env) + typed config/defaults.yaml
    text.py              # Turkish-aware normalisation (I/ı/İ/i casefold) for dedupe + quote matching
    queries.py           # search-query cleanup + Turkish near-dup check (query_gen, agents/loop.py)
    packs.py             # market pack loader + source registry lookup
    api/                 # FastAPI app + routes (health exists)
    agents/              # base.py, loop.py, registry.py (AGENTS → STAGES), one module per agent
    pipeline/
      runner.py          # ordering, resume, --from, budget enforcement
      context.py         # RunContext: db, llm, search, fetcher, pack, budget, cache mode
      stages/            # plan, query_gen, search, triage, fetch, extract, dedupe, cluster,
                         # shortlist, verify, competitors, buyers, monetization, score, report
    domain/              # Pydantic models (stage I/O, plan.yaml schema, report.json schema)
    db/                  # base.py, session.py, migrations/ (Alembic)
    providers/           # llm.py, fetch.py, search/{base,serper,serpapi}.py, cache.py, urls.py
    evidence/            # quotes, dedup, independence, strength, entailment, claims, extraction,
                         # documents, clusters (post-verify reads), gaps (M4 exit check)
    scoring/             # rubric.yaml, config.yaml, scorer.py, categories.py, experiments.py
    prompts/             # <stage>.md, versioned
    reporting/           # templates, citation validator, renderers
  evals/                 # fixtures, labeling, metrics reports
  tests/
```

---

## 13. Milestones (each with an exit criterion)

Each milestone lists the agents it builds (*Agents:* line, linking to the spec in
[agent-modules.md](agent-modules.md)). An agent that spans two milestones is split by stage.

**M0 — Foundations.** ✅ *Done 2026-10-05.* Repo, config, DB schema + migrations, LLM client with ledger + cache, SERP +
fetch providers with cache, TR pack skeleton.
*Agents:* none; this is the provider, ledger and cache layer every agent uses.
*Exit:* `signalforge search "<turkish query>"` returns cached results; a structured LLM call is logged
with cost; replay mode works.

**M1 — Plan & queries.** Request → plan.yaml (editable) → Turkish query set. Verify/curate `sources.yaml`.
*Progress 2026-10-05:* `query_gen` done (`signalforge query-gen --plan examples/tr-logistics.plan.yaml`):
150 queries for TR logistics, ≈$0.011 and ≈45 s per run. The plan.yaml schema is fixed (`domain/plan.py`);
the `plan` stage that writes it is still to do. Five prompt iterations were checked against ≈40 live
SerpApi probes. What worked: job ads on kariyer.net (duty lists name the manual work being paid for);
`<vendor> şikayet` on sikayetvar.com (first-hand complaints from business customers); a specific technical
anchor plus "forum" (practitioner threads on accountants', e-commerce and transport forums). What failed:
first-person sentences and Excel/WhatsApp workaround keywords (tutorials/templates, 0/7); ambiguous anchors
("planlama", "depo", "nakliye", "booking"); `site:` hints for sites that don't carry the topic. Google
silently drops a `site:` restriction that matches nothing, so the search stage should flag off-domain hits
for hinted queries. Pain signal-type weights in `defaults.yaml` are provisional and get re-tuned from
per-query yield once M2 measures it.
*Agents:* [planner](agent-modules.md#1-agentsplannerpy--planner) (`plan`, to do) ·
[source_discovery](agent-modules.md#2-agentssource_discoverypy--source-discovery) (`query_gen` ✅).
*Exit:* for TR logistics, ≥100 queries a Turkish-speaking reviewer judges sensible.

**M2 — Collection.** search → triage → fetch → dedupe.
*Agents:* [source_discovery](agent-modules.md#2-agentssource_discoverypy--source-discovery) (`search`,
`triage`, `fetch`, `dedupe`: stages built, exit not yet recorded) plus the agent framework
(`agents/base.py`, `AGENTS`, `run --agent`) and `evidence/documents.py`.
*Exit:* ≥150 documents for TR logistics; duplicate collapse rate and fetch success reported.

**M3 — Signals & problem landscape.** extract (verified quotes), independence, clustering, evidence
strength, Gate 1; render an intermediate **problem landscape report**.
*Agents:* [problem_discovery](agent-modules.md#3-agentsproblem_discoverypy--problem-discovery)
(`extract`, `cluster`) · [evidence_validator](agent-modules.md#4-agentsevidence_validatorpy--evidence-validator)
part 1 (`shortlist` / Gate 1).
*Exit:* quote verification pass ≥ 95% of kept excerpts; on 50 labeled signals ≥ 70% are real first-hand
B2B pain (tune until met). **This is the first test of the core thesis — don't proceed until a founder
reading the landscape report finds it useful.**
*Progress 2026-10-09:* a proxy review (Claude, not a human; `reports/m3-review-2026-10-09/`) labelled
50 signals from the market-scan runs 7–13: 58% strict / 78% lenient real first-hand B2B pain, so the
signal exit fails. The misses were how-to questions, legal case write-ups marked first-hand, generic
job-ad duties and regulatory text with no recurring burden. Fixed in `extract` v4 / `verify_extract` v3:
a fresh 50-signal sample from the re-run scan (runs 14, 16–21) gives 72% strict (narrow pass). Still
open: logistics run 15 shortlists fleet-tracking device complaints at #1 (the model gives them
`cause=tool`), portal FAQ pages and core secretary duties still yield signals. v5 (`extract` v5 / `verify_extract` v4,
2026-10-10): 70% strict on a fresh sample (80% without regulatory signals); a proxy founder read found
problems worth investigating (field/site attendance → payroll re-entry; bill-of-lading drafts). M3 is
treated as passed on the proxy review; a human read is still recommended before M5's exit check. Open
for M7: fleet tracking still passes Gate 1 with 3 of 6 sources fit; consider a fit-share rule.

**M4 — Verification & competitors.** Bounded loops, entailment checks, competitor facts, gap matrix.
*Progress 2026-10-06:* code done and tested with fake providers (`verify`, `competitors`, the shared
loop, entailment, `signalforge gaps` as the exit check; migration `e6f1a3b5c7d9`). Verify keeps its
second round in `ProblemCluster.verification` and never edits Gate 1's columns; loop documents carry
`origin`, and excerpts carry `stage`, so collection re-runs ignore them. Open: a live TR logistics
run once the M3 founder check has passed, and adding B2B software review sites to `sources.yaml`.
*Live run 2026-10-10 (run 22, TR logistics, gpt-6-luna):* verify kept 3 of 3 shortlisted problems
(29 searches, 7 fetches, 5 new signals, 1 counter; entailment supported 8, partial 10, failed 0;
$0.023). Competitors confirmed 11 products (3–5 per problem; 53 searches, 22 on own sites; 167
verified facts, 18 prices, 9 site languages; $0.074). 107 matrix cells: yes 23, partial 29, no 3,
unknown 52; 0 demoted; 11 gaps. `signalforge gaps 22` passes: **M4 exit holds**. Weak spot:
`e_document_integration` is unknown in every cell, and one problem lists a support line and an
e-Arşiv product as competitors. Still open: B2B review sites in `sources.yaml`.
*Agents:* [evidence_validator](agent-modules.md#4-agentsevidence_validatorpy--evidence-validator) part 2
(`verify`, entailment) · [competitor_research](agent-modules.md#5-agentscompetitor_researchpy--competitor-research)
(`competitors`). Both use the shared loop in `agents/loop.py`.
*Exit:* every gap-matrix cell is a cited fact or `unknown`.

**M5 — Commercial analysis & scoring.** Buyer roles, economic model, WTP, Gate 2, ScoreCards,
categories, founder fit, experiment selection.
*Agents:* [buyer_research](agent-modules.md#6-agentsbuyer_researchpy--buyer-research) (`buyers`) ·
[monetization](agent-modules.md#7-agentsmonetizationpy--monetization) (`monetization`, Gate 2) ·
[opportunity_scorer](agent-modules.md#8-agentsopportunity_scorerpy--opportunity-scorer) (`score`).
The `commercial` split is settled (§14, 2026-10-10): two stages, `buyers` + `monetization`.
*Progress 2026-10-10:* `buyers` built and tested (migration `f1a7c3d9e2b4` adds `opportunities.status`,
`knockouts`, `accessibility`, `market_breadth`; exit check `evidence/opportunities.check_buyer_roles`).
Market-breadth search is deferred (`market_breadth.status = "not_searched"`). Live on run 22: 3
opportunities (1 per problem; none dropped, 0 bad citations), every one gap-targeted; roles 6
fact-backed, 7 hypotheses; 1 budget owner of 3 (and it only cites a plan hypothesis); 0 channels;
$0.0024. `check_buyer_roles` is empty. Read by hand: segments are specific (e.g. "SMB road-freight
firms operating 5–50 trucks") and angles name their gaps, and Turkish role names are plausible. But
the model sometimes cites a fact that doesn't state the role (a complaint cited for the buyer), and
uses a company ("nakliye firması") or the user as the economic beneficiary. Before `monetization`:
consider entailment on role citations, and a prompt v2 for channels and beneficiaries.
*Fix 2026-10-10:* `buyers` prompt v2 (budget owner as a hypothesis unless no one plausibly pays;
1–4 specific channels; cite a claim only if it names the role in that function) plus a role
check: each fact-citing role is entailment-checked against its facts' quotes and demoted to a
hypothesis if they don't state it. Re-run on run 22: 3 budget owners of 3 (all "firma sahibi",
hypotheses), 5 channels (UND, UTİKAD, LODER, Logitrans; uncited), roles 2 fact / 13 hypothesis,
role check 0 of 2 demoted, `check_buyer_roles` empty, $0.0028. Roles now honestly rest on
hypotheses where the evidence (complaints, job ads) names only the user; the scorer's confidence
must reflect that. Next: `monetization`.
*Progress 2026-10-10:* `monetization` built and tested (`scoring/economics.py` interval
arithmetic; exit check `evidence/opportunities.check_economic_models`). Wages from the pack become
hourly employer cost via `loaded_cost_multiplier` 1.43–1.65; only pack values count as sourced
(model ranges citing job ads are estimates). Live on run 22 ($0.0021): 3 valid `labor_savings`
models; forwarders $153–471/month value, ceiling $15–141 → passed; warehouse $90–313, ceiling
$9–94 → passed; road freight $27–83, ceiling $3–25 → knocked out (`value_below_minimum`, < $50).
Sourced share 50% (the wages); both exit checks empty. Weak spots: hours saved are the model's
guesses; the competitor price anchor's `min` picks up per-vehicle/add-on prices (median is
saner). Next: `opportunity_scorer`.
*Progress 2026-10-10:* `score` built and tested (migration `a2b8d4e6f1c3`: `score_cards.experiment`,
score fields nullable for knocked-out cards; exit check `evidence/opportunities.check_score_cards`;
`score:` block in defaults.yaml, anchors in `scoring/rubric.yaml`, catalogue in
`scoring/experiments.yaml`). The model judges 5 factors; `economic_impact` (value midpoint) and
`market_breadth` (not searched → 2) are code. The tables carry the competitor facts behind each
gap (gap inferences alone always hit the uncited cap), and every table fact is entailment-checked
first (`entail_cited`), so `false_positive` counts supported + partial facts. `strong` needs a
budget owner backed by a fact or inference. First live run on run 22 ($0.0105; entailment
checked 27 facts: 11 supported, 16 partial, 0 failed) gave opp 6 `high` confidence because
`customer_accessibility` cited competitor segment facts and so counted as fact-backed, and gave
opp 5 WTP 4 from two price snapshots of one competitor. *Fix (same day):* in the hypothesis share
`customer_accessibility` is fact-backed only through a fact a buyer role cites (the uncited cap is
unchanged), and competitor facts carry a `competitor` field in the prompt, which counts distinct
competitors (`score` and `founder_fit` prompts v2). Re-run ($0.0098; 6 judge calls): opp 4 road
freight `weak` (Gate 2 knock-out); opp 5 forwarders `interesting`, 51.25, medium (strength 6.81,
hypothesis share 0.30), stretch (`mvp_feasible: stretch`), WTP 3 (one competitor); opp 6
warehouse `interesting`, 55.0, medium (share 0.30, frequency spread 2), fit, WTP 4 (Logo and
Mikro Jump). Both experiments: interviews testing the unsourced `hours_saved_per_month`.
`check_score_cards` is empty: **M5 exit holds**. Weak spots: opp 6's fit flipped stretch → fit
between runs on a new model sample (feasibility is one call, not k judges); frequency is 2 where
no claim states a cadence; every buyer role and channel is still a hypothesis, which now caps
confidence at medium as it should.
*Exit:* each ScoreCard is fully explainable from its rule trace and cited claims.

**M6 — Final report.** report.json → md/html; citation validator; "Don't build" section.
*Agents:* [report_writer](agent-modules.md#9-agentsreport_writerpy--report-writer) (`report`).
*Exit:* 100% of factual bullets cited; validator catches seeded uncited/hallucinated bullets in tests.

**M7 — Multi-market evaluation.** Run on ≥5 TR markets (logistics, construction, accounting, export,
e-commerce, …), label with the founder checklist, tune thresholds/weights/prompts against fixtures.
*Agents:* no new agents; tune every agent's config block and prompts against the fixtures. Each
agent's metrics come from `StageRun.metrics`; `signalforge ledger --by agent` gives cost per agent.
*Exit:* founder verdict "something I would investigate" for ≥1 opportunity in ≥3 markets; cost per run
known.

**M8 — API & UI features.** Research endpoints over the same runner, Postgres job table worker,
research form, progress (from StageRuns), opportunity cards, detail page, source explorer.
(The FastAPI app and Next.js frontend are scaffolded already — see §12 — but feature work waits until
the pipeline proves research quality. Small UI views may be added earlier where they speed up review,
e.g. a landscape-report viewer in M3.)
*Agents:* no new agents; the API and UI show progress per agent by grouping StageRuns with `AGENTS`.

Roadmap after V0: V1 evidence-backed discovery (M0–M7) → V1.5 better competitor/buyer research +
platform adapters → V2 iterative autonomous research + embeddings → V3 continuous market intelligence.

---

## 14. Open decisions (defaults chosen; change here if needed)

| Decision | Default |
|---|---|
| SERP provider | Serper (Google, `gl=tr&hl=tr`); SerpApi selectable |
| LLM provider | OpenAI; Jev to be evaluated per stage after V0 works |
| LLM models | `gpt-6-luna` for all tiers during development; `gpt-6.1-sol` / `gpt-6-astra` opt-in for quality evaluation (§10) |
| Report language | English, with original Turkish quotes + translations |
| Per-run budget | $10 hard cap (estimate $3–8/run; measure in M7) |
| Frontend stack | Next.js (App Router, TS, Tailwind) — scaffolded; `/api/*` proxied to FastAPI |
| Project name / license | SignalForge (temporary) / Apache-2.0 |
| Commercial analysis | **Decided 2026-10-10:** two stages, `buyers` + `monetization`, as in [agent-modules.md §6–7](agent-modules.md#6-agentsbuyer_researchpy--buyer-research). The narrow hand-off (cited buyer roles → economics) keeps each output checkable, and monetization's numbers come from code, not the model. Merge back into one stage if M5 evaluation shows errors compounding across the hand-off. §4, §6 and §12 updated. |
