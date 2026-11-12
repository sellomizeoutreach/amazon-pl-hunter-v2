"""Runner — orchestrates a hunt run: keywords -> jobs -> leads -> summary.

Hard caps (SPEC): MAX_KEYWORDS_PER_RUN=30, MAX_PAGES_PER_KEYWORD=2,
MAX_ASINS_PER_KEYWORD=20, MAX_CATALOGUE_PAGES=5, wall-clock target <40 min
(enforced by caps, not by timeout). Dry-run uses tests/fixtures/*.html
when present, otherwise a built-in synthetic seed list — never fails and
never touches the network. Live mode requires PLH_LIVE=1 (enforced by
server.py; runner also refuses live without the flag).
"""
from __future__ import annotations

import json
import os
import random
from datetime import date, datetime, timezone
from pathlib import Path

from backend import get_logger
from backend.db import Asin, Brand, Keyword, Lead, Run, get_session, init_db
from backend.keywords import generate_keywords
from backend.batcher import maybe_send_batch
from backend.mailer import write_summary_locally
from backend.pipeline import (
    audit_catalogue, create_job, enrich_brand, normalise_brand,
    requeue_stale_jobs, save_lead, set_stage,
)
from backend.scoring import classify_persona, is_non_cn, pitch_hook, pl_score
from backend.scraper import BlockedError, Scraper
from backend.serpapi_client import SerpApiBudgetError, SerpApiClient, SerpApiError

log = get_logger(__name__)

MAX_KEYWORDS_PER_RUN = 30
MAX_PAGES_PER_KEYWORD = 2
MAX_ASINS_PER_KEYWORD = 20
MAX_CATALOGUE_PAGES = 5

# Built-in dry-run seed brands — synthetic, clearly labelled, zero network.
# Used when tests/fixtures/*.html are absent (they are owned by the tests
# agent and may not exist yet). Fixture HTML, when present, takes precedence.
DRY_SEED_BRANDS = [
    {"brand": "Aurora Peak Goods", "asin": "B0DRY00001", "title": "Aurora Peak Silicone Kitchen Utensils Set", "reviews": 12, "rating": 4.6, "seller_id": "A1DRYSEED001", "seller_country": "US", "age_months": 6, "keyword": "silicone kitchen utensils set"},
    {"brand": "Willow & Fern Co", "asin": "B0DRY00002", "title": "Willow Fern Bamboo Drawer Organizer Set", "reviews": 34, "rating": 4.4, "seller_id": "A1DRYSEED002", "seller_country": "US", "age_months": 9, "keyword": "bamboo drawer organizer set"},
    {"brand": "Northlight Outdoors", "asin": "B0DRY00003", "title": "Northlight Camping Hammock Lightweight Portable", "reviews": 58, "rating": 4.2, "seller_id": "A1DRYSEED003", "seller_country": "CA", "age_months": 30, "keyword": "camping hammock lightweight portable"},
    {"brand": "Harbor & Hound", "asin": "B0DRY00004", "title": "Harbor Hound Dog Slow Feeder Bowl Puzzle", "reviews": 21, "rating": 4.7, "seller_id": "A1DRYSEED004", "seller_country": "GB", "age_months": 4, "keyword": "dog slow feeder bowl puzzle"},
    {"brand": "Ember Lane Home", "asin": "B0DRY00005", "title": "Ember Lane Desk Cable Management Box", "reviews": 47, "rating": 3.9, "seller_id": "A1DRYSEED005", "seller_country": "US", "age_months": 40, "keyword": "desk cable management box"},
]


def _fixture_checks(session, run: Run) -> tuple[int, list[dict]]:
    """Exercise the real pipeline logic offline against tests/fixtures/*.html.

    Returns (brands_seen, checks). Each check records the fail-closed
    outcome for one fixture search item. NOTE: the Nova Home fixture is
    designed to FAIL the catalogue audit — catalogue page 2 has a
    312-review SKU, so page-1-only auditing would wrongly pass it.
    Fixture brands therefore produce no leads here; synthetic seeds below
    are the dry-run lead source.
    """
    from backend.scraper import Scraper as _S
    fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    search_file = fixtures / "search_results.html"
    if not search_file.exists():
        return 0, []
    items = _S.parse_search(search_file.read_text(encoding="utf-8", errors="ignore"))
    checks: list[dict] = []
    for item in items:
        job = create_job(session, run_id=run.id, kind="asin", subject=item["asin"],
                         payload={"asin": item["asin"], "source": "fixture"})
        outcome: dict = {"asin": item["asin"], "title": item.get("title"), "reviews": item.get("reviews")}
        try:
            # Brand from the fixture's brand-line (search card).
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(search_file.read_text(encoding="utf-8", errors="ignore"), "html.parser")
            card = soup.select_one(f'div[data-asin="{item["asin"]}"]')
            brand_name = ""
            if card:
                bn = card.select_one(".brand-name")
                brand_name = bn.get_text(strip=True) if bn else ""
                seller_el = card.select_one(".sold-by")
                seller_name = seller_el.get_text(strip=True).replace("Sold by ", "") if seller_el else ""
            else:
                seller_name = ""
            outcome["brand"] = brand_name
            if (item.get("reviews") or 0) >= 100:
                outcome["result"] = "excluded: reviews>=100"
                set_stage(session, job, "pdp", "done", outcome)
                checks.append(outcome)
                continue
            pdp_file = fixtures / "pdp.html"
            if not pdp_file.exists() or item["asin"] not in pdp_file.read_text(errors="ignore"):
                outcome["result"] = "excluded: no PDP fixture -> catalogue incomplete (fail closed)"
                set_stage(session, job, "pdp", "failed", outcome, error="no_pdp_fixture")
                checks.append(outcome)
                continue
            pdp = _S.parse_pdp(pdp_file.read_text(encoding="utf-8", errors="ignore"), item["asin"])
            seller_file = fixtures / "seller_page.html"
            country = "UNKNOWN"
            if seller_file.exists():
                info = _S.parse_seller_page(seller_file.read_text(encoding="utf-8", errors="ignore"))
                country = info["seller_country"]
            outcome["seller_country"] = country
            catalog_items = []
            pages = 0
            for p in (1, 2):
                cf = fixtures / f"catalogue_page{p}.html"
                if cf.exists():
                    catalog_items.extend(
                        {"asin": i["asin"], "reviews": i["reviews"]}
                        for i in _S.parse_search(cf.read_text(encoding="utf-8", errors="ignore")))
                    pages += 1
            catalog = audit_catalogue(catalog_items, pages_checked=pages)
            outcome["catalog"] = catalog
            if not catalog["all_under_100"]:
                outcome["result"] = f"excluded: catalogue max reviews {catalog['max_reviews']} (>=100)"
            else:
                outcome["result"] = "passed catalogue audit"
            set_stage(session, job, "catalogue", "done", outcome)
        except Exception as exc:
            log.warning("fixture check failed", extra={"extra_fields": {"asin": item.get("asin"), "error": str(exc)}})
            outcome["result"] = f"error: {exc}"
            set_stage(session, job, "pdp", "failed", outcome, error=str(exc))
        checks.append(outcome)
    return len(items), checks


def _dry_run_leads(session, run: Run, keywords: list[str]) -> tuple[int, int, int]:
    """Dry-run: (1) fixture checks (real pipeline logic, offline, fail-closed),
    (2) synthetic seed brands -> leads. Dedup via save_lead (seller_id).
    Returns (brands_seen, leads_new, errors, fixture_checks)."""
    fixture_seen, fixture_checks = _fixture_checks(session, run)
    brands_seen = fixture_seen
    leads_new = 0
    errors = 0
    pool = list(DRY_SEED_BRANDS)
    rng = random.Random(42)  # deterministic dry-run ordering
    rng.shuffle(pool)
    for seed in pool:
        if seed["keyword"] not in keywords and keywords:
            continue
        brands_seen += 1
        job = create_job(session, run_id=run.id, kind="brand", subject=seed["brand"],
                         payload={"asin": seed["asin"], "keyword": seed["keyword"]})
        try:
            set_stage(session, job, "pdp", "running")
            score, reason = pl_score(seed["brand"], buybox_seller=seed["brand"],
                                     all_sellers=[seed["brand"]], seller_count=1)
            set_stage(session, job, "catalogue", "running")
            catalog = audit_catalogue([{"asin": seed["asin"], "reviews": seed["reviews"]}],
                                      pages_checked=1)
            set_stage(session, job, "enrich", "running")
            contacts = enrich_brand(seed["brand"])
            set_stage(session, job, "score", "running")
            persona = classify_persona(seed["age_months"], seed["reviews"], [], False)
            problems = ["Missing A+ content"] if seed["rating"] < 4.0 else []
            hook = pitch_hook(seed["brand"], persona, seed["asin"], seed["reviews"], problems)
            needs_review = not is_non_cn(seed["seller_country"]) or bool(contacts.get("needs_review"))
            set_stage(session, job, "save", "running")
            # Dry-run: skip brands whose country would fail NON_CN (none in
            # seed list — all are US/CA/GB), but record the check.
            lead = save_lead(
                session, brand_name=seed["brand"], seller_id=seed["seller_id"],
                seller_name=seed["brand"], seller_country=seed["seller_country"],
                country_confidence=0.9, score=score, reason=reason,
                asin_data={"asin": seed["asin"], "title": seed["title"],
                           "reviews": seed["reviews"], "rating": seed["rating"],
                           "url": f"https://www.amazon.com/dp/{seed['asin']}"},
                persona=persona, contacts=contacts, pitch=hook,
                # Dry-run leads are synthetic/demo data: source dry_run so
                # they can NEVER be counted toward (or sent in) a lead batch.
                needs_review=needs_review, source="dry_run", run_id=run.id)
            if lead is not None:
                leads_new += 1
            set_stage(session, job, "save", "done", {"lead_id": lead.id if lead else None})
        except Exception as exc:
            errors += 1
            log.warning("dry-run job failed", extra={"extra_fields": {"brand": seed["brand"], "error": str(exc)}})
            set_stage(session, job, "save", "failed", error=str(exc))
    return brands_seen, leads_new, errors, fixture_checks


def _live_hunt(session, run: Run, keywords: list[str]) -> tuple[int, int, int, int]:
    scraper = Scraper()
    brands_seen = leads_new = errors = captcha_blocks = 0
    for kw in keywords:
        for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
            job = create_job(session, run_id=run.id, kind="keyword", subject=kw,
                             payload={"keyword": kw, "page": page})
            try:
                set_stage(session, job, "search", "running")
                html = scraper.fetch(scraper.search_url(kw, page))
                products = scraper.parse_search(html)[:MAX_ASINS_PER_KEYWORD]
                set_stage(session, job, "search", "done", {"asins": len(products)})
                for prod in products:
                    brands_seen += 1
                    try:
                        lead = _process_asin(session, run, prod, scraper)
                        if lead is not None:
                            leads_new += 1
                    except BlockedError:
                        captcha_blocks += 1
                        errors += 1
                    except Exception as exc:
                        errors += 1
                        log.warning("asin processing failed", extra={"extra_fields": {"asin": prod.get("asin"), "error": str(exc)}})
            except BlockedError as exc:
                captcha_blocks += 1
                errors += 1
                log.warning("keyword page blocked", extra={"extra_fields": {"keyword": kw, "page": page, "error": str(exc)}})
                set_stage(session, job, "search", "failed", error=str(exc))
            except Exception as exc:
                errors += 1
                log.warning("keyword page failed", extra={"extra_fields": {"keyword": kw, "page": page, "error": str(exc)}})
                set_stage(session, job, "search", "failed", error=str(exc))
    return brands_seen, leads_new, errors, captcha_blocks


def _process_asin(session, run: Run, prod: dict, scraper: Scraper):
    job = create_job(session, run_id=run.id, kind="asin", subject=prod["asin"],
                     payload={"asin": prod["asin"]})
    set_stage(session, job, "pdp", "running")
    html = scraper.fetch(scraper.pdp_url(prod["asin"]))
    pdp = scraper.parse_pdp(html, prod["asin"])
    brand_name = pdp.get("brand") or ""
    if not brand_name or len(brand_name) < 2:
        set_stage(session, job, "pdp", "done", {"skipped": "no_brand"})
        return None
    set_stage(session, job, "seller", "running")
    seller_country, country_conf, seller_id = "UNKNOWN", 0.0, pdp.get("seller_id")
    if seller_id:
        try:
            info = scraper.parse_seller_page(scraper.fetch(scraper.seller_url(seller_id)))
            seller_country, country_conf = info["seller_country"], info["country_confidence"]
        except BlockedError:
            raise
        except Exception as exc:
            log.warning("seller fetch failed; country UNKNOWN (fail closed)",
                        extra={"extra_fields": {"seller_id": seller_id, "error": str(exc)}})
    # NON_CN fail-closed: UNKNOWN country -> excluded from auto flow.
    if not is_non_cn(seller_country):
        set_stage(session, job, "seller", "done", {"skipped": "country_not_non_cn", "country": seller_country})
        return None
    set_stage(session, job, "catalogue", "running")
    catalog_items = [{"asin": prod["asin"], "reviews": pdp.get("reviews", prod.get("reviews", 0))}]
    try:
        for page in range(1, MAX_CATALOGUE_PAGES + 1):
            items = scraper.parse_search(scraper.fetch(scraper.catalogue_url(brand_name, page)))
            if not items:
                break
            catalog_items.extend({"asin": i["asin"], "reviews": i["reviews"]} for i in items)
    except BlockedError:
        # Fail closed: catalogue incomplete -> do not create a lead.
        set_stage(session, job, "catalogue", "failed", error="catalogue_blocked")
        return None
    catalog = audit_catalogue(catalog_items, pages_checked=1)
    if not catalog["all_under_100"]:
        set_stage(session, job, "catalogue", "done", {"skipped": "catalogue_over_100", "max_reviews": catalog["max_reviews"]})
        return None
    set_stage(session, job, "enrich", "running")
    contacts = enrich_brand(brand_name)
    set_stage(session, job, "score", "running")
    score, reason = pl_score(brand_name, buybox_seller=pdp.get("buybox_seller", ""),
                             all_sellers=[], has_brand_store=pdp.get("has_brand_store", False),
                             has_aplus=pdp.get("has_aplus", False), seller_count=1)
    if score < 0.5:
        set_stage(session, job, "score", "done", {"skipped": "low_pl_score", "score": score})
        return None
    health = pdp.get("health", {})
    from backend.scoring import listing_problems
    problems = listing_problems(health)
    persona = classify_persona(pdp.get("age_months"), pdp.get("reviews", 0), problems, pdp.get("has_aplus", False))
    hook = pitch_hook(brand_name, persona, prod["asin"], pdp.get("reviews", 0), problems)
    set_stage(session, job, "save", "running")
    lead = save_lead(session, brand_name=brand_name, seller_id=seller_id,
                     seller_name=pdp.get("buybox_seller", brand_name),
                     seller_country=seller_country, country_confidence=country_conf,
                     score=score, reason=reason,
                     asin_data={**prod, "first_available": pdp.get("date_first_available")},
                     persona=persona, contacts=contacts, pitch=hook,
                     needs_review=bool(contacts.get("needs_review")), source="auto_hunt",
                     run_id=run.id)
    set_stage(session, job, "save", "done", {"lead_id": lead.id if lead else None})
    return lead


def _serpapi_hunt(session, run: Run, keywords: list[str],
                  client: SerpApiClient | None = None) -> dict:
    """Live hunt via SerpApi instead of direct Amazon scraping.

    Used when SERPAPI_API_KEY is set (direct scraping is blocked from this
    VM's egress). Flow per keyword/page: search_amazon -> skip sponsored ->
    pre-filter reviews < 100 BEFORE any product call -> product() for the
    brand -> catalogue audit via brand_search -> score -> save_lead.

    Returns a dict (brands_seen, leads_new, errors, captcha_blocks=0,
    searches_used, products_used, budget_hit, note). A budget hit or a
    search-level SerpApi failure ends the run gracefully (status done,
    note recorded) — never a crash with no summary/email.
    """
    client = client or SerpApiClient()
    brands_seen = leads_new = errors = 0
    budget_hit = False
    stop = False
    note: str | None = None
    for kw in keywords:
        if stop:
            break
        for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
            job = create_job(session, run_id=run.id, kind="keyword", subject=kw,
                             payload={"keyword": kw, "page": page, "source": "serpapi"})
            try:
                set_stage(session, job, "search", "running")
                results = client.search_amazon(kw, page)
                set_stage(session, job, "search", "done", {"asins": len(results)})
            except SerpApiBudgetError as exc:
                budget_hit, note, stop = True, str(exc), True
                log.warning("serpapi budget hit; ending run gracefully",
                            extra={"extra_fields": {"keyword": kw, "page": page, "error": str(exc)}})
                set_stage(session, job, "search", "failed", error=str(exc))
                break
            except SerpApiError as exc:
                # A search-level failure (bad key, API outage) would fail
                # every keyword the same way — stop the run instead of
                # burning the rest of the budget on identical failures.
                errors += 1
                note, stop = f"serpapi search failed: {exc}", True
                log.warning("serpapi search failed; stopping run",
                            extra={"extra_fields": {"keyword": kw, "page": page, "error": str(exc)}})
                set_stage(session, job, "search", "failed", error=str(exc))
                break
            for prod in results:
                if prod.get("sponsored"):
                    continue
                brands_seen += 1
                # Pre-filter BEFORE any product call: product calls are
                # capped and billed, so >=100-review ASINs never cost one.
                if (prod.get("reviews") or 0) >= 100:
                    continue
                try:
                    lead = _process_serpapi_asin(session, run, prod, client)
                    if lead is not None:
                        leads_new += 1
                except SerpApiBudgetError as exc:
                    budget_hit, note, stop = True, str(exc), True
                    log.warning("serpapi budget hit; ending run gracefully",
                                extra={"extra_fields": {"asin": prod.get("asin"), "error": str(exc)}})
                    break
                except SerpApiError as exc:
                    errors += 1
                    log.warning("serpapi product failed; skipping asin",
                                extra={"extra_fields": {"asin": prod.get("asin"), "error": str(exc)}})
                except Exception as exc:
                    errors += 1
                    log.warning("serpapi asin processing failed",
                                extra={"extra_fields": {"asin": prod.get("asin"), "error": str(exc)}})
            if stop:
                break
    return {
        "brands_seen": brands_seen, "leads_new": leads_new, "errors": errors,
        "captcha_blocks": 0, "searches_used": client.searches_used,
        "products_used": client.products_used, "budget_hit": budget_hit,
        "note": note,
    }


def _process_serpapi_asin(session, run: Run, prod: dict, client: SerpApiClient):
    """One SerpApi candidate ASIN -> lead or None (same gates as the direct
    path, except the seller-country rule — see the deviation note below)."""
    job = create_job(session, run_id=run.id, kind="asin", subject=prod["asin"],
                     payload={"asin": prod["asin"], "source": "serpapi"})
    set_stage(session, job, "pdp", "running")
    detail = client.product(prod["asin"])
    brand_name = detail.get("brand") or ""
    if not brand_name or len(brand_name) < 2:
        set_stage(session, job, "pdp", "done", {"skipped": "no_brand"})
        return None
    seller_name = detail.get("seller_name") or ""
    set_stage(session, job, "score", "running")
    # PL score against the seller SerpApi actually reports. When there is
    # no seller block, buybox_seller is empty (never the brand itself —
    # scoring a brand against itself would fabricate a 0.9 match), so the
    # score is meaningless — but the candidate is NOT dropped. Per Jack
    # (2026-11-12), seller-less candidates are saved as needs_review for
    # him to hand-check (private-label status unverified), instead of
    # being silently discarded. The <0.5 gate below only applies when a
    # seller was actually reported.
    no_seller = not seller_name
    score, reason = pl_score(brand_name, buybox_seller=seller_name,
                             all_sellers=[seller_name] if seller_name else [],
                             has_brand_store=False, has_aplus=False, seller_count=1)
    if not no_seller and score < 0.5:
        set_stage(session, job, "score", "done", {"skipped": "low_pl_score", "score": score})
        return None
    if no_seller:
        reason = (reason + " (no seller info from SerpApi — verify private-label by hand)")
    # Catalogue audit via brand_search — fail closed: a failed brand search
    # means no lead (catalogue incomplete), exactly like a blocked fetch
    # in the direct path. A budget hit propagates to end the run gracefully.
    set_stage(session, job, "catalogue", "running")
    catalog_items = [{"asin": prod["asin"],
                      "reviews": prod.get("reviews", detail.get("reviews", 0))}]
    pages_checked = 0
    try:
        for page in range(1, MAX_CATALOGUE_PAGES + 1):
            items = client.brand_search(brand_name, page)
            pages_checked = page
            if not items:
                break
            catalog_items.extend({"asin": i["asin"], "reviews": i["reviews"]} for i in items)
    except SerpApiBudgetError:
        raise
    except SerpApiError as exc:
        set_stage(session, job, "catalogue", "failed", error=f"brand_search_failed: {exc}")
        return None
    catalog = audit_catalogue(catalog_items, pages_checked=pages_checked)
    if not catalog["all_under_100"]:
        set_stage(session, job, "catalogue", "done",
                  {"skipped": "catalogue_over_100", "max_reviews": catalog["max_reviews"]})
        return None
    # Seller country — DEVIATION FROM THE DIRECT PATH (deliberate, see
    # README "SerpApi live mode"): SerpApi product responses do not
    # reliably carry a seller country, so UNKNOWN does NOT exclude the
    # lead here (that would discard nearly every SerpApi lead). UNKNOWN
    # leads are saved with country_confidence 0.0 and needs_review=True
    # for Jack to hand-check. A KNOWN CN/HK country still excludes,
    # exactly like the direct path's NON_CN gate.
    seller_country = detail.get("seller_country") or "UNKNOWN"
    country_conf = detail.get("country_confidence") or 0.0
    set_stage(session, job, "seller", "running")
    if seller_country in ("CN", "HK"):
        set_stage(session, job, "seller", "done",
                  {"skipped": "country_cn_hk", "country": seller_country})
        return None
    set_stage(session, job, "seller", "done", {"country": seller_country})
    set_stage(session, job, "enrich", "running")
    contacts = enrich_brand(brand_name)
    rating = detail.get("rating") or prod.get("rating") or 0.0
    reviews = detail.get("reviews") or prod.get("reviews") or 0
    # SerpApi does not reliably report A+/video/bullet data, so listing
    # problems are limited to what the response actually carries (rating);
    # A+/video problems are NOT invented here.
    problems = []
    if rating and rating < 3.8:
        problems.append(f"Low rating ({rating} stars, below 3.8)")
    persona = classify_persona(None, reviews, problems, False)
    hook = pitch_hook(brand_name, persona, prod["asin"], reviews, problems)
    needs_review = (bool(contacts.get("needs_review"))
                    or seller_country == "UNKNOWN" or no_seller)
    set_stage(session, job, "save", "running")
    lead = save_lead(
        session, brand_name=brand_name, seller_id=detail.get("seller_id"),
        # NOTE: SerpApi seller ids are often absent, so the brand-name
        # fallback in save_lead is the effective dedup key in this path.
        seller_name=seller_name or brand_name, seller_country=seller_country,
        country_confidence=country_conf, score=score, reason=reason,
        asin_data={**prod, "first_available": None,
                   "url": prod.get("url") or f"https://www.amazon.com/dp/{prod['asin']}"},
        persona=persona, contacts=contacts, pitch=hook,
        needs_review=needs_review, source="serpapi", run_id=run.id)
    set_stage(session, job, "save", "done", {"lead_id": lead.id if lead else None})
    return lead


def run_hunt(*, mode: str = "dry", max_keywords: int | None = None,
             session=None, serpapi_client: SerpApiClient | None = None) -> dict:
    """Run one hunt. Returns the summary dict (also stored on the run row).

    Live mode: when SERPAPI_API_KEY is set, the hunt goes through SerpApi
    (_serpapi_hunt) instead of direct Amazon scraping (_live_hunt);
    direct scraping stays the default when no key is set (e.g. Jack
    running from his own machine). `serpapi_client` is an injection point
    for tests — production code always leaves it None.
    """
    logger = get_logger(__name__)
    own_session = session is None
    session = session or get_session()
    if mode == "live" and os.environ.get("PLH_LIVE") != "1":
        raise RuntimeError("live mode requires PLH_LIVE=1")
    init_db()
    requeue_stale_jobs(session)
    run_date = str(date.today())
    kw_limit = min(max_keywords or MAX_KEYWORDS_PER_RUN, MAX_KEYWORDS_PER_RUN)
    run = Run(status="running", keywords_used="[]")
    session.add(run)
    session.commit()
    logger = get_logger(__name__, run_id=run.id)
    logger.info("run started", extra={"extra_fields": {"mode": mode, "max_keywords": kw_limit}})
    captcha_blocks = errors = 0
    fixture_checks: list = []
    serpapi_mode = False
    serpapi_searches_used = 0
    serpapi_products_used = 0
    serpapi_budget_hit = False
    serpapi_note: str | None = None
    try:
        kws = generate_keywords(session, run_date=run_date, target=kw_limit, live=(mode == "live"))
        kw_list = [k for k, _ in kws]
        run.keywords_used = json.dumps(kw_list)
        session.commit()
        if mode == "dry":
            brands_seen, leads_new, errors, fixture_checks = _dry_run_leads(session, run, kw_list)
        elif os.environ.get("SERPAPI_API_KEY", "").strip() or serpapi_client is not None:
            serpapi_mode = True
            result = _serpapi_hunt(session, run, kw_list, client=serpapi_client)
            brands_seen = result["brands_seen"]
            leads_new = result["leads_new"]
            errors = result["errors"]
            captcha_blocks = result["captcha_blocks"]
            serpapi_searches_used = result["searches_used"]
            serpapi_products_used = result["products_used"]
            serpapi_budget_hit = result["budget_hit"]
            serpapi_note = result["note"]
        else:
            brands_seen, leads_new, errors, captcha_blocks = _live_hunt(session, run, kw_list)
        total_leads = session.query(Lead).count()
        run.status = "done" if errors == 0 else "partial"
        run.finished_at = datetime.now(timezone.utc)
        run.brands_seen = brands_seen
        run.leads_new = leads_new
        run.leads_total = total_leads
        run.captcha_blocks = captcha_blocks
        run.errors = errors
    except Exception as exc:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.errors = errors + 1
        logger.warning("run failed", extra={"extra_fields": {"error": str(exc)}})
        session.commit()
        raise
    summary = {
        "run_id": run.id, "mode": mode, "status": run.status,
        "keywords_used": json.loads(run.keywords_used or "[]"),
        "brands_seen": run.brands_seen, "leads_new": run.leads_new,
        "leads_total": run.leads_total, "captcha_blocks": run.captcha_blocks,
        "captcha_rate": (run.captcha_blocks / max(run.brands_seen, 1)),
        "errors": run.errors,
        "serpapi_mode": serpapi_mode,
        "serpapi_searches_used": serpapi_searches_used,
        "serpapi_products_used": serpapi_products_used,
        "serpapi_budget_hit": serpapi_budget_hit,
        "serpapi_note": serpapi_note,
        "fixture_checks": fixture_checks if mode == "dry" else [],
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    run.summary_json = json.dumps(summary)
    session.commit()
    # Email follows the batch rule (Jack, 2026-11-12 — supersedes the old
    # "always send a run summary" rule). The local HTML summary is ALWAYS
    # written (that is how runs are monitored); the ONLY email ever sent is
    # the 100-lead CSV batch (see backend/batcher.py). A run that found 0
    # new leads never sends anything (skipped_zero_leads). Dry runs never
    # send and their leads never count toward a batch. Email never fails
    # the run.
    write_summary_locally(summary)
    try:
        if mode == "live":
            email_status = maybe_send_batch(session, new_leads_this_run=run.leads_new)
        else:
            email_status = ("skipped_zero_leads" if run.leads_new == 0
                            else "skipped_dry_run")
    except Exception as exc:
        logger.warning("batch email check failed", extra={"extra_fields": {"error": str(exc)}})
        email_status = "batch_failed"
    run.email_status = email_status
    summary["email_status"] = email_status
    run.summary_json = json.dumps(summary)
    session.commit()
    logger.info("run finished", extra={"extra_fields": summary})
    if own_session:
        session.close()
    return summary
