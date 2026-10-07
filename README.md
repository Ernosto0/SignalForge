# SignalForge

Open-source, evidence-backed market research engine for founders. *Evidence first. Ideas second.*

Design and roadmap: [.claude/plan.md](.claude/plan.md).

## Layout

```
backend/    Python 3.12 · FastAPI · SQLAlchemy/Alembic · research pipeline + CLI
frontend/   Next.js (App Router, TypeScript, Tailwind)
docker-compose.yml   Postgres 16 on host port 5433
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically)
- Node.js 20+ and pnpm
- Docker Desktop (for Postgres)

## Setup

```bash
cp .env.example .env              # then fill in OPENAI_API_KEY and SERPER_API_KEY (or SERP_API_KEY with SEARCH_PROVIDER=serpapi)

docker compose up -d db           # Postgres on localhost:5433

cd backend
uv sync                           # creates .venv with all dependencies
uv run alembic upgrade head       # apply migrations

cd ../frontend
pnpm install
```

## Run (dev)

```bash
# terminal 1
cd backend && uv run signalforge serve --reload        # http://127.0.0.1:8000  (docs at /docs)

# terminal 2
cd frontend && pnpm dev                                # http://localhost:3000
```

The frontend proxies `/api/*` to the backend (`BACKEND_URL`, default `http://127.0.0.1:8000`).
The home page shows setup status: API, database, and whether API keys are configured.

## CLI

Run from `backend/` with `uv run signalforge <command>`:

```bash
signalforge search "nakliye sevkiyat Excel'de takip"   # Serper by default (or SerpApi), Turkish locale from the TR pack
signalforge fetch https://example.com.tr/yazi          # robots.txt, rate limit, text extraction
signalforge llm-check                                  # one structured LLM call, logged with cost
signalforge ledger                                     # recent LLM calls and total spend
signalforge purge-cache --namespace page               # delete cached pages (search | page | llm)
signalforge query-gen --plan examples/tr-logistics.plan.yaml --out queries.csv
                                                       # new run from a reviewed plan -> Turkish queries
signalforge query-gen --run 3                          # regenerate a run's queries (LLM calls cached)
signalforge run --run 3 --from extract                 # re-run signals → clusters → Gate 1 (needs AUTHOR_HASH_SALT)
signalforge landscape 3 --out reports                  # problem landscape report: json, md, html (no LLM)
signalforge signals 3 --out signals.csv                # signals with quotes, sources, clusters, labels
signalforge label signals 3 --n 50                     # y/n: real first-hand B2B pain? (M3 exit: ≥ 70% y)
signalforge label clusters 3                           # y/n: one coherent, distinct problem?
signalforge labels 3                                   # label precision by signal type and source
signalforge run --run 3 --agent evidence_validator     # Gate 1, then verify: 2nd-round search + entailment
signalforge run --run 3 --agent competitor_research    # competitors, cited facts/prices, gap matrices
signalforge gaps 3                                     # gap matrices; exits 1 if a cell is uncited (M4 exit)
```

Search results, pages and LLM responses are cached in Postgres (`cache_entries`). The cache mode
comes from `CACHE_MODE` in `.env` or `--cache-mode` before the command:

- `live` (default): reuse fresh entries (TTLs in `backend/config/defaults.yaml`), otherwise call out.
- `record`: always call out and overwrite the cache.
- `replay`: cache only, no network or API keys needed; a miss is an error.

```bash
signalforge --cache-mode replay search "nakliye sevkiyat Excel'de takip"
```

Caps, TTLs and model prices live in `backend/config/defaults.yaml`; the Turkey market pack
(locale, pain phrases, source registry, seeds) lives in `backend/market_packs/tr/`.

## Checks

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest
cd frontend && pnpm lint && pnpm build
```

## License

Apache-2.0
