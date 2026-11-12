# Amazon PL Hunter v2 — Build Spec (frozen contracts)

Read the audit in `~/MEMORY.md` (PL Hunter entry) and `~/workspace/pl-hunter-resources/README.md` first.
Original repo `~/workspace/amazon-pl-hunter/` is READ-ONLY reference. Do not edit it.
No live Amazon scraping at scale during the build. Fixtures + dry-run only.
No install scripts from cloned repos. No secrets in code. No push to GitHub.

## Design (Jack, 2026-11-11)
1. Lead finding FULLY AUTOMATIC: no keyword input anywhere in the main flow. Keywords
   auto-generated + rotated daily (BSR / autocomplete / DDGS), capped, stored in DB.
   Home screen = leads already found + daily run status.
2. Brand Audit is a SEPARATE on-demand page/route (paste brand or ASIN). Never part of
   or blocking the automatic flow.
3. Persistence first: SQLite file `data/plhunter.db`, SQLAlchemy, `backend/db.py`
   `init_db()` + a `schema_version` table (documented Litestream path in README).
4. Fail-closed auditing; 5. Verified enrichment; 6. Capped resumable DB jobs;
   7. One React UI + thin extension; 8. Brevo lead-batch email (no per-run
   summary emails); 9. Token auth + restricted CORS;
   10. Tests + structured logging; 11. GitHub Action downloads DB artifact BEFORE the
   run and uploads AFTER.

## Stack
Python 3.12, FastAPI + SQLAlchemy 2 + Pydantic v2, curl_cffi, beautifulsoup4,
email-validator, dnspython. Frontend: React 18 + Vite + TypeScript, plain CSS
(no heavy component lib). Node 20+. pytest.

## DB tables (backend/db.py — single source of truth, all builders use these names)
- `runs`: id, started_at, finished_at, status (running/done/failed/partial), keywords_used,
  brands_seen, leads_new, leads_total, captcha_blocks, errors, email_status, summary_json
- `keywords`: id, keyword, source (bsr/autocomplete/ddgs), run_date, used (bool)
- `brands`: id, name (unique, normalised), seller_id (UNIQUE, dedup key), seller_name,
  seller_country, country_confidence, pl_score, pl_reason, created_at
- `asins`: id, asin (unique), brand_id FK, title, reviews, rating, price, first_available,
  bsr, url, fetched_at
- `leads`: id, brand_id FK, asin_id FK nullable, persona
  (NEW_LAUNCH/STAGNANT_VETERAN/DISTRESSED/UNKNOWN), contact_quality
  (DIRECT/WEBSITE_ONLY/UNCONTACTABLE), founder_name, founder_confidence,
  linkedin_url, linkedin_confidence, website, email, email_confidence,
  email_is_personal (bool), phone, pitch_hook, needs_review (bool),
  source (auto_hunt/serpapi/dry_run/audit/extension), run_id (nullable,
  hunt run that found the lead), batch_sent_at (nullable, NULL = unsent),
  verified_at, created_at
- `contacts`: id, lead_id FK, kind (email/phone/linkedin/website), value, confidence,
  verified (bool), source
- `outreach_status`: id, lead_id FK unique, status
  (NEW/SHORTLISTED/CONTACTED/SKIP), notes, updated_at
- `jobs`: id, run_id FK nullable, kind (keyword/brand/asin/enrich/audit), subject,
  stage, status (pending/running/done/failed), payload_json, result_json,
  attempts, error, created_at, updated_at

## API (backend/server.py, prefix /api) — token auth on ALL except /api/health
Auth: header `X-API-Token: <PLH_API_TOKEN>`. CORS: only origins in
`PLH_ALLOWED_ORIGINS` (default `http://localhost:5173,http://localhost:8000`).
Rate limit: max 1 live hunt run at a time; POST /api/runs and POST /api/audit
limited to 10/min per token (in-memory limiter is acceptable, document it).
- GET  /api/health -> {status, db, version}
- GET  /api/stats -> {leads_total, leads_new_today, runs_last_7d, captcha_rate}
- GET  /api/leads?persona=&contact_quality=&country=&max_reviews=&q=&status=&sort=&limit=&offset=
  -> {total, items:[LeadOut]}. LeadOut: id, brand, seller_id, seller_country,
  asin, title, reviews, rating, persona, contact_quality, founder_name,
  founder_confidence, linkedin_url, website, email, email_confidence,
  email_is_personal, phone, pitch_hook, needs_review, status, verified_at
- PATCH /api/leads/{id} {status?, notes?}
- GET  /api/leads/export?format=csv|xlsx&<same filters as /api/leads>
  -> EXACTLY the filtered rows (never the whole job). CSV via stdlib, XLSX via openpyxl.
- GET  /api/runs, GET /api/runs/{id}, POST /api/runs {mode: dry|live, max_keywords?: int}
  -> 202 {run_id}. Live mode requires PLH_LIVE=1 else 409 with a clear error.
- GET  /api/keywords -> auto-generated keywords for today (read-only, NO add endpoint)
- POST /api/audit {brand?: str, asin?: str} -> audit result (see below). One of brand/asin required.
- POST /api/capture {asin?: str, url?: str} (extension) -> creates an audit job, returns {job_id, deep_link}

## Audit result shape (used by /api/audit and the Audit page)
{brand, asin, seller_id, seller_country, country_confidence, pl_score, pl_reason,
 catalog: {total_products, max_reviews, all_under_100, pages_checked, incomplete: bool,
           fail_reason: str|null},
 persona, listing_problems: [str], pitch_hook, contacts: {...}, needs_review: bool}
FAIL-CLOSED: if the catalogue fetch is blocked/CAPTCHA/error -> all_under_100=false,
incomplete=true, fail_reason set, persona=UNKNOWN, needs_review=true. Never pass on failure.

## Scoring rules (backend/scoring.py — shared by backend + tests)
- PL score 0-1: name-match (token overlap; a SINGLE generic token such as 'nova'
  in 'Innovatech' scores <=0.3), megabrand list negates to 0, Brand Store / A+
  adds at most +0.2, seller-count >5 subtracts. Return (score, reason string).
- Seller country: explicit country token wins; a 2-letter code counts ONLY if it is
  a known ISO country code AND not a US state code (so 'NY' and 'Central Ave, NY'
  -> UNKNOWN). UNKNOWN fails closed for the NON_CN filter (lead excluded from
  auto flow, flagged needs_review).
- Enrichment confidence 0-1 per field. Website domain MUST contain a brand token
  (token >=3 chars) or website=None. Founder needs 2 independent corroborations
  for confidence >=0.8; 1 source caps at 0.5. NEVER derive a name from a URL slug.
  Generic inboxes (info@, support@, sales@, hello@, contact@, wholesale@, careers@,
  partnerships@, admin@) are never personal (email_is_personal=false).
  Email MX check via email-validator + dnspython; if DNS is unavailable, mark
  email_confidence <=0.4 and needs_review=true (do not claim verified).

## Pipeline (backend/pipeline.py + backend/runner.py)
- DB jobs, one brand/ASIN = one job. Stages persisted after each stage
  (search -> pdp -> seller -> catalogue -> enrich -> score -> save).
  A killed run resumes pending/running jobs on the next run (stale running jobs
  are re-queued). Hard caps: MAX_KEYWORDS_PER_RUN=30, MAX_PAGES_PER_KEYWORD=2,
  MAX_ASINS_PER_KEYWORD=20, MAX_CATALOGUE_PAGES=5, run wall-clock target <40 min
  (enforced by cap, not by timeout). Dry-run mode uses tests/fixtures/*.html and
  produces a real run row + summary with zero network.
- Keyword generation (backend/keywords.py): BSR categories + autocomplete +
  DDGS seeds, deduped, daily rotation seeded by run_date, stored in `keywords`,
  skip keywords used in the previous 7 days where possible. NO manual keyword
  endpoint. If all live sources fail, fall back to the previous day's keywords
  and log it — never crash the run.
- Structured logging everywhere: `logging` with JSON-ish lines
  (ts, level, module, run_id, msg). NO bare `except Exception: pass` — every
  failure is logged with context and recorded on the job/run.
- Scraper (backend/scraper.py): curl_cffi, rotating impersonation profiles
  (chrome124/chrome120/safari15_5), delay 1.0-2.0s + jitter, CAPTCHA detection
  over the FULL body (robot-check, validatecaptcha, bm-verify, 503, empty
  results). On block: raise BlockedError, record captcha_blocks, fail closed.

## Email (backend/mailer.py + backend/batcher.py)
Brevo transactional API — requests REST first, brevo-python SDK second
(document which path was used in the returned status string). Env:
BREVO_API_KEY, BREVO_SENDER, BREVO_RECEIVER; PLH_BATCH_MIN_LEADS
(default 100). The old "always send a run summary" rule is RETIRED
(Jack, 2026-11-12):
- Every run ALWAYS writes a local HTML summary to
  data/run_summaries/<run_id>.html (counts, new leads, captcha rate,
  errors) — that, plus the /runs API, is how runs are monitored.
- NO email is ever sent for a run that found 0 new leads
  (email_status=skipped_zero_leads on the run row).
- The ONLY email is the lead batch: once PLH_BATCH_MIN_LEADS unsent LIVE
  leads have accumulated (source auto_hunt/serpapi; dry_run/fixture leads
  never count), ONE email goes out with all of them in a single attached
  CSV (brand, ASIN, title, reviews, rating, price, seller name, seller
  country, persona, PL score, best contact email, pitch hook, run id),
  subject "PL Hunter: N new leads", body a short summary (count, date
  range of runs covered, SerpApi searches used if known). Exactly those
  leads are then marked leads.batch_sent_at (nullable; NULL = unsent).
  A failed send marks NOTHING; the next live run retries the same batch.
  Below the threshold, NO email at all — not even a per-run summary.
  email_status on a run row is one of: skipped_zero_leads,
  skipped_dry_run, batch_pending, batch_sent, batch_failed.
Never Gmail SMTP as primary.

## Frontend (frontend/) — React + Vite + TS
Routes: `/` Leads (home: run status banner + lead queue; NO keyword input anywhere),
`/audit` Brand Audit (separate page: brand/ASIN input, result card with catalogue,
persona, problems, pitch), `/runs` Run history.
Lead queue: filters (persona, contact quality, country, max reviews, text search),
sort, row select, per-lead status (New/Shortlisted/Contacted/Skip) + notes,
copy-email button, export button that calls /api/leads/export with the CURRENT
filters (filtered rows only). Token: prompt once, store in localStorage.
Build: `npm run build` outputs to frontend/dist, served by FastAPI as static files.

## Extension (extension/) — MV3, side-panel ONLY (distila pattern)
manifest.json: `side_panel.default_path: sidepanel.html`, `action` with NO
`default_popup`, permissions: activeTab, sidePanel, storage, host_permissions for
the backend origin only. background.js: setPanelBehavior({openPanelOnActionClick:false})
on install, chrome.action.onClicked -> chrome.sidePanel.open({windowId}).
sidepanel.html/js: capture-only — paste ASIN/Amazon URL, POST /api/capture with the
stored token, show job status, deep-link "Open in dashboard". No pipeline logic,
no results table in the panel.

## Tests (tests/)
pytest. Saved HTML fixtures in tests/fixtures/ (search page, PDP, seller page,
catalogue page 1 + 2, CAPTCHA page). Golden tests: PL detection incl. the
nova/Innovatech trap (score <=0.3, not a match); seller-country incl.
'123 Central Ave, New York' -> UNKNOWN (not HK) and a bare 'NY' line -> UNKNOWN;
catalogue audit fail-closed on CAPTCHA fixture; export returns only filtered rows;
dry-run hunt completes and writes a run row + summary; auth rejects missing token;
CORS rejects an unlisted origin. `pytest` must pass offline (no network in tests).

## GitHub Action (.github/workflows/daily_hunt.yml)
schedule 08:00 UTC + workflow_dispatch. Steps: checkout, setup Python 3.12,
download-artifact `plhunter-db` (if exists — `if: always()`/continue-on-error so
a first run works), run dry-or-live hunt (live only if PLH_LIVE secret = 1),
upload-artifact `plhunter-db` (data/plhunter.db, retention 90d) ALWAYS.
The download-before / upload-after pair is the whole point — it is what fixes
the original dedup bug.

## Files / ownership (avoid collisions — each builder owns its tree)
- backend agent: backend/, requirements.txt, requirements.lock, scripts/run_hunt.py,
  .github/workflows/daily_hunt.yml, .env.example, .gitignore
- frontend agent: frontend/, extension/
- tests agent: tests/, plus fixtures (may read backend code but must not edit it;
  report test failures to the coordinator instead)
All: README.md is owned by the COORDINATOR (do not edit it).
