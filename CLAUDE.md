# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

FastAPI service deployed on Railway that produces a daily BRVM (Bourse Régionale des Valeurs Mobilières de l'UEMOA) market brief. It collects quotes and news, enriches news with Claude Sonnet, synthesizes a brief with Claude Opus, and delivers it by email (Brevo SMTP) and WhatsApp (Brevo API).

**Language convention**: code is English, user-facing strings / logs / prompts / docstrings are French (Ivory Coast market).

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys

# Local Postgres (needed for the app to start — DATABASE_URL is required)
docker run -d --name brvm-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16

# Run the full app (FastAPI + APScheduler in lifespan)
uvicorn src.main:app --reload
# Swagger: http://localhost:8000/docs   Healthcheck: /health

# Deploy entrypoint (also used by Railway via railway.json)
uvicorn src.main:app --host 0.0.0.0 --port $PORT
```

No tests, linter, or formatter are configured in this repo. If adding them, use `pytest`, `ruff`, and `black` per global rules.

## Triggering the pipeline manually

The daily pipeline normally fires on the DB-stored cron (default `0 8 * * *` Africa/Abidjan). To trigger it on demand during development:

```bash
# Either hit the admin API (requires ADMIN_API_TOKEN in env)
curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" http://localhost:8000/api/schedule/run-now

# Or call run_daily_pipeline() directly in a Python shell after setting env vars
python -c "from src.pipeline import run_daily_pipeline; print(run_daily_pipeline())"
```

All `/api/*` routes require the `X-Admin-Token` header.

## Architecture

Single FastAPI process; APScheduler runs **inside** the web service (not a separate worker). The scheduler is started/stopped in the FastAPI `lifespan` (`src/main.py`), reads its cron from the `schedule_config` table, and hot-reloads when the admin API mutates it — so cron changes never require a redeploy.

### Daily pipeline (`src/pipeline.py::run_daily_pipeline`)

Orchestrator with 7 sequential steps, each step isolated in its own DB session:

1. **Collect** — for each enabled row in `sources`, build a `Collector` via `collectors/registry.py::build_collector(type, config)` and call `.collect()`. Collectors never raise; they return `CollectionResult` with errors inline (see `collectors/base.py`).
2. **Persist raw** — upsert `quotes` by `(ticker, quote_date)`, skip `news` whose `url` already exists. Returns the list of newly inserted `NewsItem` so only new articles are sent to the LLM.
3. **Enrich** — `analysis/enrichment.py::NewsEnricher` calls Sonnet per new article and writes `tickers_mentioned` + `enrichment` JSON back on the `news` row. It then reloads recent (`news_lookback_hours`) already-enriched articles so Opus sees a rolling window, and filters items without BRVM relevance.
4. **Market snapshot** — from the latest `quote_date` in DB: top gainers/losers/volumes.
5. **Historical context** — last 5 briefs (to help Opus avoid repeating calls).
6. **Synthesize** — `analysis/synthesis.py::BriefSynthesizer` sends snapshot + enriched news + history to Opus. The system prompt is `prompts/synthesis.md` with `{{investor_profile}}` substituted from settings. Response must be strict JSON; bare-```` fence stripping is handled, JSON errors fall back to a stub payload.
7. **Persist + deliver** — write `briefs` + one `signals` row per opportunity (with `price_at_signal` snapshotted for later backtesting), then send email + WhatsApp. Delivery errors are stored on the `briefs` row, they don't abort the pipeline.

### Data model (`src/models.py`)

6 tables, all SQLAlchemy 2.0 typed: `sources` (dynamic config), `quotes` (unique per `(ticker, quote_date)`), `news` (unique per `url`), `briefs` (stores full Opus JSON payload), `signals` (one per opportunity, FK→brief, captures `price_at_signal` for backtest), `schedule_config` (single-row cron store).

`init_db()` in `src/database.py` calls `Base.metadata.create_all` at startup — **no Alembic migrations**. Any schema change must stay backward compatible or be handled manually (comment in `database.py` flags Alembic as future work).

### Adding a new data source

- **RSS feed** — no code needed. POST to `/api/sources` with `"type": "rss"` and the URL; `RssCollector` in `collectors/sika_finance.py` handles it.
- **Custom scraper** — add a class inheriting `Collector` in `src/collectors/`, register it in `COLLECTOR_CLASSES` in `src/collectors/registry.py`, then POST a source with matching `type`. The BRVM official scraper (`collectors/brvm_official.py`) is the reference — if `brvm.org`'s HTML changes, patch `_parse_quotes` there.

### Scheduler hot-reload (`src/scheduler.py`)

`SchedulerManager.reload()` is called at startup and after any `PATCH /api/schedule`. It removes the existing `daily_brief` job, validates the new cron (invalid → fallback to `settings.default_cron`), and re-adds the job with `misfire_grace_time=600` + `coalesce=True` (so a brief missed during a restart runs once, not N times).

### Secrets & config

All config flows through `src/config.py::Settings` (pydantic-settings, reads `.env`). `DATABASE_URL` is auto-rewritten from `postgres://` → `postgresql://` (Railway legacy format). WhatsApp delivery is optional — if `WHATSAPP_TEMPLATE_ID` is empty, `WhatsAppSender.enabled` is false and the pipeline skips it silently.

`ADMIN_API_TOKEN` protects every admin route via `src/api/deps.py::require_admin`. Never commit a real token; default value is `change-me`.

### Prompts

`prompts/enrichment.md` (Sonnet) and `prompts/synthesis.md` (Opus) are read from disk on `__init__` of the analyzer classes — editing them takes effect on next process restart, not next run.

## Deploy notes (Railway)

`railway.json` points Nixpacks at `uvicorn src.main:app` and healthchecks `/health`. Attach the Postgres plugin — Railway injects `DATABASE_URL` automatically. The scheduler runs inside this single web dyno; there is no separate worker service.
