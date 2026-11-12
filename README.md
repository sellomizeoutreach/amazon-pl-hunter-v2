# Amazon PL Hunter v2

A from-scratch rebuild of the v1 lead hunter, fixing every flaw in the 2026-11-11
audit (see `SPEC.md` and the PL Hunter entry in `~/MEMORY.md`). The original repo
(`~/workspace/amazon-pl-hunter/`) was left untouched — it is reference only.

## The two design rules (Jack, 2026-11-11)

1. **Lead finding is FULLY AUTOMATIC.** No keyword input anywhere in the main flow.
   Keywords are auto-generated and rotated daily (BSR / autocomplete / DDGS, capped
   at 30/run, stored in the DB, 7-day dedup). The home screen shows leads already
   found plus the latest daily-run status.
2. **Brand Audit is a SEPARATE on-demand page** (`/audit`): paste a brand name or
   ASIN, get catalogue health, persona, listing problems and a pitch hook. It never
   runs as part of, or blocks, the automatic lead flow.

## What was fixed vs v1

| v1 flaw | v2 fix |
|---|---|
| Dedup lost between GitHub Actions runs (seen_leads.json only uploaded, never downloaded) | SQLite DB (`data/plhunter.db`) is the dedup store; the Action downloads the DB artifact **before** the run and uploads it **after** |
| Catalogue audit failed open on CAPTCHA and checked page 1 only | Fail-closed: blocked fetch → `all_under_100=false`, `incomplete=true`, `fail_reason` set; paginates up to 5 catalogue pages |
| 0.2–0.5 s delays, 4 workers, CAPTCHA check on first 4 k chars, old TLS | curl_cffi, rotating profiles, 1–2 s + jitter, full-body CAPTCHA detection → `BlockedError` |
| 45-min run, no checkpointing — a kill meant zero output | DB-backed jobs (one brand/ASIN = one job), stages persisted, stale running jobs re-queued on the next run |
| Extension export ignored on-screen filters | Export calls `/api/leads/export` with the **current** filters — filtered rows only |
| Side panel broken (manifest had both `default_popup` + `side_panel`) | Side-panel only, distila pattern: no `default_popup`, `setPanelBehavior({openPanelOnActionClick:false})`, `chrome.action.onClicked` → `chrome.sidePanel.open` |
| Enrichment was unverified DDGS guessing | Website domain must contain a brand token; founder needs 2 corroborations for confidence ≥ 0.8; emails MX-checked; generic inboxes never labelled personal; per-field 0–1 confidence + `needs_review` |
| 63 bare `except Exception`, zero logging, no tests | Structured JSON-ish logging everywhere; 78 pytest tests (1 honest skip); no bare swallowing |
| API unauthenticated, CORS `*` | `X-API-Token` auth on everything except `/api/health`; CORS restricted to `PLH_ALLOWED_ORIGINS`; 10/min rate limit on run/audit endpoints |
| `app.py` duplicated in `dashboard/app.py` | One codebase: FastAPI serves the React SPA; no Streamlit, no duplication |
| Gmail app-password SMTP | Brevo transactional email (brevo-python SDK, lazy import, requests fallback) |

## Layout

```
backend/        FastAPI + SQLAlchemy (db, scoring, scraper, serpapi_client, keywords, pipeline, runner, mailer, batcher, server)
frontend/       React 18 + Vite + TS SPA (Leads home, /audit, /runs) -> frontend/dist (served by FastAPI)
extension/      MV3 side-panel-only capture extension (distila pattern)
tests/          pytest + saved HTML fixtures (offline, network-guarded)
scripts/        run_hunt.py CLI
.github/        daily_hunt.yml — 08:00 UTC, DB artifact download-before / upload-after
SPEC.md         Frozen contracts (DB tables, API, scoring rules)
data/           plhunter.db (shipped with 3 demo leads from a dry-run) + run_summaries/
```

## Run it

```bash
cd ~/workspace/amazon-pl-hunter-v2
cp .env.example .env        # set PLH_API_TOKEN (required), BREVO_* (optional)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/run_hunt.py --mode dry        # dry-run hunt, prints a JSON summary
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000
# dashboard at http://localhost:8000/ (enter the API token once when prompted)
# or frontend dev: cd frontend && npm install && npm run dev   (proxies /api to :8000)
pytest -q                                    # 61 passed, 1 skipped, offline
```

## Environment

| Variable | Required | Notes |
|---|---|---|
| `PLH_API_TOKEN` | yes | Single shared token for API + extension |
| `PLH_ALLOWED_ORIGINS` | no | Default `http://localhost:5173,http://localhost:8000` |
| `PLH_DATABASE_URL` | no | Default `sqlite:///data/plhunter.db` |
| `PLH_LIVE` | no | `1` enables live hunting; default dry-run. `POST /api/runs` live returns 409 without it |
| `SERPAPI_API_KEY` | no | When set, live runs use SerpApi (see "SerpApi live mode"); when unset, live runs scrape Amazon directly |
| `SERPAPI_MAX_SEARCHES_PER_RUN` / `SERPAPI_MAX_PRODUCTS_PER_RUN` | no | Defaults 120 / 60; hitting either ends the run gracefully with `serpapi_budget_hit: true` |
| `PLH_BATCH_MIN_LEADS` | no | Default 100 — unsent live leads needed before the batch CSV email goes out (see "Lead batch emails") |
| `BREVO_API_KEY` / `BREVO_SENDER` / `BREVO_RECEIVER` | no | Needed for the batch email; without them, batches stay pending and run summaries are local HTML only |
| `PLH_LOG_LEVEL` | no | Default `INFO`; logs go to stderr, stdout stays clean for JSON |

## Lead batch emails

This retires the old "always send a run summary" rule (Jack, 2026-11-12).

- Every run writes a local HTML summary to `data/run_summaries/<run_id>.html`.
  That, plus the dashboard `/runs` page, is how runs are monitored.
- **No email is ever sent for a run that found 0 new leads**
  (`email_status=skipped_zero_leads` on the run row).
- New **live** leads accumulate unsent. Once `PLH_BATCH_MIN_LEADS`
  (default 100) unsent live leads exist, **one** email goes out with all of
  them in a single attached CSV, subject like "PL Hunter: 100 new leads".
  The CSV has one row per lead: brand, ASIN, title, reviews, rating,
  price, seller name, seller country, persona, PL score, best contact
  email, pitch hook, and the run it came from. The email body is a short
  summary (count, date range of runs covered, SerpApi searches used if
  any). Exactly the leads in that email are then marked sent
  (`leads.batch_sent_at`); a failed send marks nothing and the next live
  run retries the same batch. While the unsent count is below the
  threshold, **no email at all** is sent — not even a per-run summary.
- Only real live leads count (`source` auto_hunt / serpapi). Dry-run and
  fixture leads (`source` dry_run) are never counted or emailed.
- One nuance: the batch is a *leads* email, not a run summary, so if 100+
  leads are already owed from earlier runs (e.g. a previous send failed),
  the next live run sends the batch even if that run itself found 0 new
  leads. A run that adds leads but doesn't reach the threshold records
  `email_status=batch_pending`; a run whose batch goes out records
  `batch_sent`; a failed send records `batch_failed`.

## SerpApi live mode

Direct Amazon scraping is blocked from this VM: amazon.com returns 503 /
a shadowban page at egress, and the cloud browser only gets Amazon's
"Sorry! Something went wrong!" page. SerpApi (serpapi.com) is reachable
from here, so live runs can go through it instead.

Enable it:

```bash
# in .env, alongside PLH_LIVE=1
SERPAPI_API_KEY=<your SerpApi key>        # from serpapi.com/manage-api-key
SERPAPI_MAX_SEARCHES_PER_RUN=120         # default; engine=amazon calls
SERPAPI_MAX_PRODUCTS_PER_RUN=60          # default; engine=amazon_product calls
python scripts/run_hunt.py --mode live
```

When the key is set, live mode uses `_serpapi_hunt` (SerpApi) instead of
`_live_hunt` (direct scraping). When it is unset, live mode still uses
direct scraping — that path is for Jack's own machine, where Amazon loads
normally. Dry-run mode is untouched either way.

**Cost per full run, in SerpApi calls** (SerpApi bills per search, so
check your plan's allowance — a run that hits its budget stops early and
still emails its summary):

| Step | Calls |
|---|---|
| 30 keywords × 2 pages of Amazon search | 60 search calls minimum |
| Product lookup, only for sub-100-review ASINs (sponsored and ≥100-review results are skipped first) | ≤ 60 product calls (capped) |
| Catalogue audit per candidate brand, via brand search, up to 5 pages | up to 5 **search** calls per candidate — these come out of the same 120-search budget |

So a full run costs at least 60 searches, and every candidate that reaches
the catalogue audit costs up to 5 more searches: with the default 120-search
budget, roughly 12 candidates can get a full 5-page audit per run. The
product budget (60) is separate. Hitting either budget ends the run
gracefully with status `done`, `serpapi_budget_hit: true` in the summary,
and the searches-used count — never a crash with no summary/email.

**Known deviation from the direct path:** SerpApi product responses do
not reliably carry a seller country, so an UNKNOWN country does **not**
exclude a lead here (that would discard nearly every SerpApi lead). UNKNOWN
leads are saved with `seller_country="UNKNOWN"`, `country_confidence=0.0`
and `needs_review=True` for Jack to hand-check. A *known* CN/HK country
still excludes, exactly like the direct path. Two more SerpApi limits:
leads without a seller block in the product response cannot be verified
as private label (the PL score needs a seller name to match against), so
they are skipped; and listing problems are limited to what SerpApi
actually reports (low rating) — A+/video/bullet problems are not invented.

## Chrome extension

Load unpacked from `extension/` (`chrome://extensions` → Developer mode).
Side panel only — click the icon, paste an ASIN or Amazon URL, it posts to
`/api/capture` and deep-links to the dashboard. Set the API token + backend
origin in the panel settings first. Not load-tested in a live Chrome yet
(manifest + JS syntax verified statically).

## Verified 2026-11-11 (coordinator, by running it)

- `pytest -q`: **61 passed, 1 skipped**, 1 warning (Starlette/httpx deprecation, unrelated)
- Dry-run on a fresh DB: run 1 → 3 new leads, status done; run 2 → 0 new (seller_id dedup persists)
- Live server: `/api/health` ok; `/api/leads` without token → 401; with token → leads/stats/runs; CORS preflight from an unlisted origin → 400 "Disallowed CORS origin", from localhost:5173 → 200; frontend served at `/`; CSV export with `persona=NEW_LAUNCH` returned only that row
- `/api/audit` with live Amazon unreachable: fail-closed as specified (`all_under_100=false`, `incomplete=true`, `fail_reason` set, country UNKNOWN) — but it took ~50 s (fetch retries). The audit endpoint is synchronous; a slow/blocked Amazon means a slow audit response. Acceptable for on-demand use; a background-job audit is a future improvement.
- Keywords endpoint falls back to the built-in seed list when live sources are unreachable (sandbox has no Amazon access), and logs it.

## Verified 2026-11-12 (SerpApi live path + lead batch emails)

- `pytest -q`: **78 passed, 1 skipped**, 1 warning (Starlette/httpx deprecation, unrelated) — up from 61, all offline with fixtures, zero live SerpApi/Brevo calls.
- SerpApi client + runner path verified by tests only (no key exists yet): search parsing, sponsored skip, reviews<100 pre-filter, brand parsing in both forms, budget caps, UNKNOWN-country leads saved with `needs_review`, fail-closed catalogue on brand-search failure, budget hit ending a run gracefully with status `done`.
- Batch rule verified by tests: 0 new leads → no Brevo call, `skipped_zero_leads`; 99 unsent → no send; 100 unsent → exactly one send with a 100-row CSV, all 100 marked `batch_sent_at`; failed send → nothing marked, retry succeeds; 150 dry-run leads never count. Migration tested: a v2-schema DB gains `batch_sent_at` + `run_id` with rows preserved.
- Dry-run after the change (run 8): done, 0 new (dedup held), `email_status=skipped_zero_leads`, local summary `data/run_summaries/8.html` written; real DB migrated to schema v3 with all rows intact. The 4 shipped demo leads were relabelled `source=dry_run` (they were dry-run leads saved as `auto_hunt` before the `dry_run` source existed) so they can never be batched.

## Still needs Jack / keys / decisions

- **Brevo API key** (`BREVO_API_KEY`, sender, receiver) — verified live 2026-11-12 (sellomizeoutreach@gmail.com, sender active; test summaries sent). It is only used for the lead batch email (see "Lead batch emails"); without it, batches stay pending and run summaries are local HTML only. On Jack's own machine the key goes in `.env` (the stored key here is only reachable via the Secure Vault, never shown in chat).
- **Live hunting has never run against real Amazon** — only dry-run + fixtures. The first live run (2026-11-12, run 7) never reached Amazon: direct scraping is blocked from this VM (503 / shadowban page) and the cloud browser only gets Amazon's error page. The SerpApi path is now built (see "SerpApi live mode") but has had zero live calls — it needs a real `SERPAPI_API_KEY` and a first watched run: searches used, keyword quality, lead precision (hand-check 20–30 leads before trusting it).
- **SP-API eligibility** — SP-API is verification-only; it cannot do unauthenticated competitor discovery. If Sellomize can get SP-API access it strengthens verification, but scraping (or a paid SERP/data provider) remains the discovery engine.
- **Paid data/enrichment** — SerpApi / Keepa / Hunter / Apollo / Crustdata credits are all optional; the build spends $0 by default and only uses free paths (DDGS, MX checks). Decide a per-lead budget before enabling paid enrichment for top leads.
- **GitHub Action** — the workflow exists (`.github/workflows/daily_hunt.yml`) but has not run in CI; the DB artifact download/upload pair is what makes dedup persist, and it needs one real Actions run to prove.
- **Extension** — needs a real Chrome load test (manifest + JS verified statically only).
- **Known wart (non-blocking):** `POST /api/runs` runs the hunt synchronously in-process and returns 202 after completion (the DB-job design supports moving it to a background task later). Rate limiting is in-memory per token, not distributed.

## Deliberately left out

- No live Amazon scraping at scale during the build (fixtures + dry-run only).
- No LinkedIn scraping — resellers were rejected (LinkedIn sued Nubela/Proxycurl); enrichment uses licensed data + brand-published LinkedIn URLs only.
- No Streamlit dashboard, no Gmail SMTP, no manual keyword UI.
- No push to GitHub — Jack deploys himself.
