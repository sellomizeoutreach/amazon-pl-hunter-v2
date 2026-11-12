# Tests — Amazon PL Hunter v2

pytest, offline, no network in any test (a socket guard in `conftest.py`
fails any test that tries real I/O).

## Run

```bash
pip install -r tests/requirements-test.txt
pytest -v
```

## Layout

- `fixtures/*.html` — hand-written saved pages: `search_results.html`
  (3 products), `pdp.html` (Nova Home, 42 reviews), `seller_page.html`
  (Nova Home LLC, Austin TX US), `catalogue_page1.html` (max 87 reviews),
  `catalogue_page2.html` (hides a 312-review SKU — proves pagination is
  checked), `captcha.html` (Amazon robot check, no product data).
- `conftest.py` — project-root sys.path, test token, temp DB env vars,
  network guard, `api_client` fixture (TestClient, temp cwd + temp DB).
- `helpers.py` — adapters between tests and backend modules. **Read this
  first if a backend test skips**: it lists the exact function names probed.
- `test_fixtures.py` — backend-independent fixture integrity checks.
- `test_scoring.py` — PL golden tests (nova/Innovatech trap, genuine
  Nova Home match, Nike -> 0), seller-country tests (Central Ave NY ->
  UNKNOWN, bare NY -> UNKNOWN, Shenzhen -> CN, Austin -> US), enrichment
  rules (generic inbox never personal, website domain needs a brand token,
  no slug-derived founder names, founder confidence caps).
- `test_catalogue.py` — fail-closed on CAPTCHA + page-2 detection.
- `test_api.py` — auth, health, filtered export, dry-run hunt, CORS.
- `test_dedup.py` — seller_id UNIQUE dedup incl. restart persistence.

## Green-but-honest contract

While the backend is still landing, any test whose backend module does not
exist yet **skips** with a reason naming the missing module/function —
never fails, never silently passes. `pytest -v` shows the skips; when the
backend lands, the same tests execute for real with no test changes
(assuming the backend exposes the names in `helpers.py`).

## Backend contract the tests assume (backend agent: align to these)

These now match the landed backend (verified 2026-11-11):

- `backend/scoring.py`: `pl_score(brand_name, buybox_seller,
  all_sellers, has_brand_store, has_aplus, seller_count)` returning
  `(score 0-1, reason str)`; `detect_seller_country(business_name,
  address_lines, full_text="")` returning `(code, confidence, reason)`;
  `email_is_personal(email) -> bool`, `website_confidence(brand, website)
  -> (website|None, confidence)`, `founder_confidence(corroborations: int)
  -> float`, `audit_catalogue(reviews_by_asin=[{asin, reviews}], *,
  blocked, error, pages_checked) -> dict` (flat catalog dict).
  Enrichment lives in `scoring.py`, not a separate `enrichment.py`.
- `backend/pipeline.py`: `audit_brand(session, brand=, asin=, scraper=)`
  returning the full SPEC audit shape; accepts an injectable scraper, which
  is how the fail-closed path is tested offline.
- `backend/scraper.py`: `Scraper.parse_search / parse_pdp /
  parse_seller_page` (static, pure over HTML); `BlockedError` on CAPTCHA.
- `backend/server.py`: FastAPI `app`; DB path honours
  `PLH_DATABASE_URL` (a SQLAlchemy URL, e.g. `sqlite:////abs/path.db`) —
  the ONLY db-path variable the backend reads; `init_db(url)` creates the
  SPEC tables at startup; `reset_engine_for_tests()` drops the cached
  engine so a new URL takes effect.
