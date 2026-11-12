"""Keyword generation — fully automatic, no manual endpoint (SPEC).

Sources: BSR categories (title mining), Amazon autocomplete API, DDGS
seeds. Daily rotation seeded by run_date, stored in the `keywords` table,
7-day dedup (skip keywords used in the previous 7 days where possible).
If all live sources fail, fall back to the previous day's keywords and log
it — never crash the run. Offline / dry-run: evergreen seeds only.
"""
from __future__ import annotations

import hashlib
import random
import re
from datetime import date, timedelta

from backend import get_logger

log = get_logger(__name__)

BSR_CATEGORIES = [
    "/gp/bestsellers/kitchen/", "/gp/bestsellers/sporting-goods/",
    "/gp/bestsellers/pet-supplies/", "/gp/bestsellers/health/",
    "/gp/bestsellers/beauty/", "/gp/bestsellers/office-products/",
    "/gp/bestsellers/tools/", "/gp/bestsellers/baby-products/",
    "/gp/bestsellers/toys-and-games/", "/gp/bestsellers/home-garden/",
    "/gp/bestsellers/luggage/", "/gp/bestsellers/outdoor/",
]

AUTOCOMPLETE_SEEDS = [
    "silicone kitchen", "bamboo kitchen", "stainless steel kitchen",
    "kitchen organizer", "food storage container", "resistance band",
    "foam roller", "yoga mat", "posture corrector", "massage gun",
    "dog grooming", "cat litter mat", "dog slow feeder", "pet water fountain",
    "camping hammock", "hiking backpack", "water bottle insulated",
    "baby food maker", "diaper bag backpack", "laptop stand adjustable",
    "desk organizer", "cable management", "jade roller", "gua sha",
    "packing cubes", "travel toiletry bag", "passport holder",
]

DDGS_QUERIES = [
    "best amazon private label product ideas",
    "trending amazon FBA niche products",
    "low competition amazon private label products under 100 reviews",
]

EVERGREEN_SEEDS = [
    "silicone kitchen utensils set", "bamboo drawer organizer set",
    "yoga mat non slip thick", "travel packing cubes lightweight",
    "posture corrector back brace", "pet grooming brush self cleaning",
    "desk cable management box", "camping hammock lightweight portable",
    "foam roller high density", "kitchen herb garden starter kit",
    "reusable silicone food storage bags", "adjustable laptop stand ergonomic",
    "resistance bands set workout", "essential oil diffuser ultrasonic",
    "meal prep containers glass lid", "cat litter mat waterproof",
    "dog slow feeder bowl puzzle", "baby food storage containers",
    "travel toiletry organizer bag", "microfiber cleaning cloths set",
    "wooden cutting board handle", "cold brew coffee maker portable",
    "shower caddy rust proof", "under desk footrest ergonomic",
    "magnetic knife strip wall mount", "silicone baking mat non stick",
    "stainless steel water bottle insulated", "car phone mount dashboard",
    "portable blender usb rechargeable", "plant watering globes self watering",
    "garden kneeler pad foam", "kids art supply organizer",
    "bathroom shelf over toilet", "weighted sleep mask women",
    "dog harness no pull adjustable", "cat tree condo scratching post",
    "bike handlebar bag waterproof", "standing desk mat anti fatigue",
    "monitor riser wood", "hair diffuser curly hair",
    "beard grooming kit men", "jade roller face gua sha",
    "eyelash curler heated", "passport holder rfid blocking",
    "luggage scale digital", "neck pillow memory foam",
    "packing cubes compression", "diaper bag backpack large",
    "baby carrier ergonomic newborn", "toddler backpack harness",
]

GENERIC_STOP = {
    "amazon", "best", "top", "buy", "shop", "deals", "sale", "prime",
    "review", "reviews", "rating", "seller", "brand", "product", "products",
    "guide", "list", "ideas", "things", "stuff", "items", "category",
}


def daily_subset(items: list, count: int, run_date: str, salt: str = "") -> list:
    seed = int(hashlib.md5(f"{run_date}{salt}".encode()).hexdigest(), 16) % (10 ** 9)
    rng = random.Random(seed)
    shuffled = list(items)
    rng.shuffle(shuffled)
    return shuffled[:count]


def clean_keyword(phrase: str) -> str | None:
    if not phrase:
        return None
    cleaned = re.sub(r"\[.*?\]|\(.*?\)", "", phrase)
    cleaned = re.sub(r"[^a-z0-9\s-]", " ", cleaned.lower())
    tokens = [t for t in cleaned.split() if len(t) > 2 and t not in GENERIC_STOP]
    if len(tokens) < 2 or len(tokens) > 6:
        return None
    out = " ".join(tokens)
    if re.search(r"\d", out):
        return None
    return out


def _keywords_from_bsr(max_categories: int = 5, run_date: str | None = None) -> list[tuple[str, str]]:
    """Mine BSR pages for product-title keywords. Returns [(kw, 'bsr')]."""
    run_date = run_date or str(date.today())
    try:
        from backend.scraper import Scraper
        from bs4 import BeautifulSoup
    except Exception as exc:
        log.warning("bsr source unavailable", extra={"extra_fields": {"error": str(exc)}})
        return []
    scraper = Scraper()
    out: list[tuple[str, str]] = []
    for cat in daily_subset(BSR_CATEGORIES, max_categories, run_date, "bsr"):
        try:
            html = scraper.fetch(f"https://www.amazon.com{cat}")
        except Exception as exc:
            log.warning("bsr fetch failed", extra={"extra_fields": {"category": cat, "error": str(exc)}})
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select('a[href*="/dp/"]'):
            kw = clean_keyword(a.get_text(" ", strip=True))
            if kw:
                out.append((kw, "bsr"))
    return out


def _keywords_from_autocomplete(max_seeds: int = 10, run_date: str | None = None) -> list[tuple[str, str]]:
    run_date = run_date or str(date.today())
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:
        log.warning("autocomplete unavailable: curl_cffi missing", extra={"extra_fields": {"error": str(exc)}})
        return []
    out: list[tuple[str, str]] = []
    for seed in daily_subset(AUTOCOMPLETE_SEEDS, max_seeds, run_date, "autocomplete"):
        url = ("https://completion.amazon.com/api/2017/suggestions?limit=10"
               f"&prefix={seed.replace(' ', '+')}&suggestion-type=KEYWORD"
               "&page-type=Search&alias=aps&site-variant=desktop&version=3"
               "&mid=ATVPDKIKX0DER&plain-mid=1&client-info=amazon-search-ui")
        try:
            resp = cffi_requests.get(url, impersonate="chrome124", timeout=8,
                                     headers={"Accept": "application/json"})
            for item in (resp.json() or {}).get("suggestions", []):
                kw = clean_keyword(item.get("value", ""))
                if kw:
                    out.append((kw, "autocomplete"))
        except Exception as exc:
            log.warning("autocomplete failed", extra={"extra_fields": {"seed": seed, "error": str(exc)}})
    return out


def _keywords_from_ddgs(max_queries: int = 2, run_date: str | None = None) -> list[tuple[str, str]]:
    run_date = run_date or str(date.today())
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError as exc:
            log.warning("ddgs unavailable", extra={"extra_fields": {"error": str(exc)}})
            return []
    out: list[tuple[str, str]] = []
    for q in daily_subset(DDGS_QUERIES, max_queries, run_date, "ddgs"):
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(q, max_results=10):
                    kw = clean_keyword(f"{r.get('title', '')} {r.get('body', '')}")
                    if kw:
                        out.append((kw, "ddgs"))
        except Exception as exc:
            log.warning("ddgs failed", extra={"extra_fields": {"query": q, "error": str(exc)}})
    return out


def previous_day_keywords(session, run_date: str) -> list[str]:
    """Keywords stored for the previous calendar day (fallback source)."""
    from backend.db import Keyword
    prev = str(date.fromisoformat(run_date) - timedelta(days=1))
    rows = session.query(Keyword).filter(Keyword.run_date == prev).all()
    return [r.keyword for r in rows]


def generate_keywords(session, *, run_date: str | None = None, target: int = 30,
                      live: bool = False) -> list[tuple[str, str]]:
    """Generate today's keywords, store them, apply 7-day dedup.

    live=False (dry-run / offline): evergreen seeds only, no network.
    live=True: BSR + autocomplete + DDGS, padded with seeds; on total
    failure fall back to the previous day's keywords.
    Returns list of (keyword, source) actually stored for today.
    """
    from backend.db import Keyword
    run_date = run_date or str(date.today())
    # Reuse today's stored keywords (idempotent re-run).
    existing = session.query(Keyword).filter(Keyword.run_date == run_date).all()
    if existing:
        return [(r.keyword, r.source) for r in existing]

    candidates: list[tuple[str, str]] = []
    if live:
        for fn in (_keywords_from_bsr, _keywords_from_autocomplete, _keywords_from_ddgs):
            try:
                candidates.extend(fn(run_date=run_date))
            except Exception as exc:
                log.warning("keyword source failed", extra={"extra_fields": {"source": fn.__name__, "error": str(exc)}})
    if not candidates:
        if live:
            prev = previous_day_keywords(session, run_date)
            if prev:
                log.warning("all live keyword sources failed; falling back to previous day",
                            extra={"extra_fields": {"run_date": run_date, "count": len(prev)}})
                candidates = [(k, "fallback") for k in prev]
        if not candidates:
            pool = [s for s in EVERGREEN_SEEDS]
            candidates = [(k, "seed") for k in daily_subset(pool, target, run_date, "seed")]

    # 7-day dedup: skip keywords used in the previous 7 days where possible.
    week_ago = str(date.fromisoformat(run_date) - timedelta(days=7))
    recent = {r.keyword for r in session.query(Keyword).filter(
        Keyword.run_date >= week_ago, Keyword.run_date < run_date).all()}
    fresh = [(k, s) for k, s in candidates if k not in recent]
    chosen = fresh if len(fresh) >= min(target, 5) else candidates
    # Dedup preserving order, cap at target.
    seen: set[str] = set()
    final: list[tuple[str, str]] = []
    for k, s in chosen:
        if k not in seen:
            seen.add(k)
            final.append((k, s))
        if len(final) >= target:
            break
    # Pad from evergreen seeds if short.
    if len(final) < target:
        for k in daily_subset(EVERGREEN_SEEDS, target, run_date, "seed-pad"):
            if k not in seen and k not in recent:
                seen.add(k)
                final.append((k, "seed"))
            if len(final) >= target:
                break
    for kw, src in final:
        session.add(Keyword(keyword=kw, source=src, run_date=run_date, used=False))
    session.commit()
    log.info("keywords generated", extra={"extra_fields": {"run_date": run_date, "count": len(final)}})
    return final
