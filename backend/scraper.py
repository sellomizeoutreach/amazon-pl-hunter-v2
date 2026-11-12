"""Scraper — curl_cffi, rotating impersonation, polite pacing, fail-closed
CAPTCHA detection over the FULL response body (v1 only checked the first
4k chars). On block: raise BlockedError; callers record captcha_blocks.
"""
from __future__ import annotations

import random
import re
import time
import urllib.parse
from typing import Any

from backend import get_logger

log = get_logger(__name__)

IMPERSONATIONS = ["chrome124", "chrome120", "safari15_5"]
BASE_URL = "https://www.amazon.com"

CAPTCHA_MARKERS = (
    "robot-check", "validatecaptcha", "bm-verify", "enter the characters",
    "automated access", "sorry, we just need to make sure",
    "api-services-support", "captcha",
    # Egress/proxy block pages (e.g. the Muse runtime shadowban fallback
    # served instead of Amazon): small static page, no CAPTCHA wording,
    # so the markers above miss it. Found on the first live run, 2026-11-12.
    "shadowban",
)

# A 503 / empty-results page is also a block, not an empty catalogue.
BLOCK_STATUS = {503, 429, 403}


class BlockedError(Exception):
    """Amazon returned a CAPTCHA / bot wall / block status."""


class Scraper:
    def __init__(self, delay_min: float = 1.0, delay_max: float = 2.0,
                 max_retries: int = 2):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_retries = max_retries
        self.captcha_blocks = 0

    # -- pacing ------------------------------------------------------------
    def _pace(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max) + random.uniform(0, 0.5))

    @staticmethod
    def _session_id() -> str:
        return f"{random.randint(100, 999)}-{random.randint(1000000, 9999999)}-{random.randint(1000000, 9999999)}"

    # -- fetch -------------------------------------------------------------
    def fetch(self, url: str) -> str:
        """Return HTML or raise BlockedError. Retries rotate impersonation;
        a CAPTCHA on any attempt is a hard block (no silent pass)."""
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("curl_cffi is required") from exc
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            imp = IMPERSONATIONS[attempt % len(IMPERSONATIONS)]
            self._pace()
            try:
                resp = cffi_requests.get(
                    url,
                    impersonate=imp,
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                    cookies={"i18n-prefs": "USD", "session-id": self._session_id()},
                    timeout=15,
                )
            except Exception as exc:
                log.warning("fetch failed", extra={"extra_fields": {"url": url, "attempt": attempt, "error": str(exc)}})
                last_error = exc
                continue
            body = resp.text or ""
            lowered = body.lower()
            if resp.status_code in BLOCK_STATUS or any(m in lowered for m in CAPTCHA_MARKERS):
                self.captcha_blocks += 1
                log.warning("blocked by amazon", extra={"extra_fields": {"url": url, "status": resp.status_code}})
                raise BlockedError(f"Amazon block (status {resp.status_code}) on {url}")
            if resp.status_code != 200:
                log.warning("non-200 response", extra={"extra_fields": {"url": url, "status": resp.status_code}})
                last_error = RuntimeError(f"HTTP {resp.status_code}")
                continue
            # A real Amazon search/PDP page is >100 KB. A few-KB 200 page is
            # a proxy/block fallback, not an empty result — treat as blocked
            # so the run reports captcha_blocks instead of "0 leads, done".
            if len(body) < 8000:
                self.captcha_blocks += 1
                log.warning("suspiciously small response; treating as block",
                            extra={"extra_fields": {"url": url, "bytes": len(body)}})
                raise BlockedError(f"Amazon block (tiny {len(body)}B page) on {url}")
            return body
        raise RuntimeError(f"fetch failed after retries: {last_error}")

    # -- URL builders ------------------------------------------------------
    @staticmethod
    def search_url(keyword: str, page: int = 1) -> str:
        return f"{BASE_URL}/s?k={urllib.parse.quote_plus(keyword)}&page={page}&ref=sr_pg_{page}"

    @staticmethod
    def pdp_url(asin: str) -> str:
        return f"{BASE_URL}/dp/{asin}"

    @staticmethod
    def seller_url(seller_id: str) -> str:
        return f"{BASE_URL}/sp?seller={seller_id}"

    @staticmethod
    def catalogue_url(brand_name: str, page: int = 1) -> str:
        return f"{BASE_URL}/s?k=%22{urllib.parse.quote(brand_name)}%22&page={page}"

    # -- parsing (pure functions over HTML — testable offline) -------------
    @staticmethod
    def parse_search(html: str) -> list[dict[str, Any]]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        out, seen = [], set()
        for item in soup.select('div[data-asin]:not([data-asin=""])'):
            asin = (item.get("data-asin") or "").strip()
            if not asin or len(asin) != 10 or asin in seen:
                continue
            title_node = item.select_one("h2")
            title = title_node.get_text(strip=True) if title_node else ""
            if not title:
                continue
            seen.add(asin)
            reviews = 0
            for node in item.select('span[aria-label*="review"], a[aria-label*="review"]'):
                lbl = node.get("aria-label", "")
                if "out of" in lbl.lower():
                    continue
                m = re.search(r"([\d,]+)", lbl)
                if m:
                    reviews = int(m.group(1).replace(",", ""))
                    break
            if reviews == 0:
                for sel in ('.review-count', 'a[href*="product-reviews"]',
                            'a[href*="#customerReviews"] span'):
                    node = item.select_one(sel)
                    if node:
                        m = re.search(r"([\d,]+)", node.get_text(strip=True).replace(",", ""))
                        if m:
                            reviews = int(m.group(1).replace(",", ""))
                            break
            rating = 0.0
            r_node = item.select_one(".a-icon-alt")
            if r_node:
                m = re.search(r"([0-9.]+)\s+out of", r_node.get_text())
                if m:
                    rating = float(m.group(1))
            price_node = item.select_one(".a-price .a-offscreen")
            out.append({
                "asin": asin, "title": title, "reviews": reviews,
                "rating": rating,
                "price": price_node.get_text(strip=True) if price_node else "",
                "url": f"{BASE_URL}/dp/{asin}",
            })
        return out

    @staticmethod
    def parse_pdp(html: str, asin: str) -> dict[str, Any]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        brand, has_store = "", False
        byline = soup.find(id="bylineInfo")
        if byline:
            text = byline.get_text(strip=True)
            href = byline.get("href", "") or ""
            has_store = "/stores/" in href or "Visit the" in text
            brand = re.sub(r"^(Visit the|Brand:)\s*", "", text, flags=re.I)
            brand = re.sub(r"\s+Store$", "", brand, flags=re.I).strip()
        if not brand:
            node = soup.select_one(".po-brand .po-break-word")
            if node:
                brand = node.get_text(strip=True)
        buybox_seller, seller_id = "", ""
        sp = soup.find(id="sellerProfileTriggerId")
        if sp:
            buybox_seller = sp.get_text(strip=True)
            href = sp.get("href", "") or ""
            m = re.search(r"[?&](?:seller|merchant)=([A-Z0-9]{10,25})", href)
            if m:
                seller_id = m.group(1)
        if not seller_id:
            for a in soup.find_all("a", href=True):
                m = re.search(r"[?&](?:seller|merchant)=([A-Z0-9]{10,25})", a["href"])
                if m:
                    seller_id = m.group(1)
                    break
        rating, reviews = 0.0, 0
        r_node = soup.select_one('[data-hook="rating-out-of-text"], span.a-icon-alt')
        if r_node:
            m = re.search(r"([0-9.]+)\s+out of\s+5", r_node.get_text())
            if m:
                rating = float(m.group(1))
        rev_node = soup.select_one("#acrCustomerReviewText, [data-hook='total-review-count']")
        if rev_node:
            m = re.search(r"(\d+)", rev_node.get_text(strip=True).replace(",", ""))
            if m:
                reviews = int(m.group(1))
        date_first, age_months = "", None
        for li in soup.select("#detailBulletsWrapper_feature_div li"):
            txt = li.get_text(separator=" ")
            if "date first available" in txt.lower():
                m = re.search(r"date first available\s*[:\u200f\u200e]\s*([A-Za-z0-9,\s]+)", txt, re.I)
                if m:
                    date_first = m.group(1).strip()
        health = {
            "is_suppressed": False, "is_buybox_suppressed": False,
            "has_frequently_returned_badge": "frequently returned item" in soup.get_text(" ").lower(),
            "rating": rating, "reviews": reviews,
            "has_aplus": bool(soup.select_one("#aplus, .aplus-v2")),
            "has_video": bool(soup.select_one("[data-video-url]")),
            "bullet_count": len(soup.select("#feature-bullets ul li:not(.aok-hidden)")),
        }
        avail = soup.select_one("#availability")
        if avail and any(w in avail.get_text(" ").lower() for w in
                         ("currently unavailable", "we don't know when or if")):
            health["is_suppressed"] = True
        if not soup.select_one("#add-to-cart-button, #buy-now-button"):
            health["is_buybox_suppressed"] = True
        title_el = soup.select_one("#productTitle")
        return {
            "asin": asin, "brand": brand, "buybox_seller": buybox_seller,
            "seller_id": seller_id, "has_brand_store": has_store,
            "has_aplus": health["has_aplus"], "reviews": reviews,
            "rating": rating, "date_first_available": date_first,
            "age_months": age_months, "title": title_el.get_text(strip=True) if title_el else "",
            "health": health, "page_fetched": True,
        }

    @staticmethod
    def parse_seller_page(html: str) -> dict[str, Any]:
        """Parse Detailed Seller Information; returns business name, address
        lines, country (via scoring.detect_seller_country)."""
        from bs4 import BeautifulSoup
        from backend.scoring import detect_seller_country
        soup = BeautifulSoup(html, "html.parser")
        business_name, address_lines = "", []
        text = soup.get_text(separator="\n", strip=True)
        m = re.search(r"Business Name:\s*([^\n]+)", text, re.I)
        if m:
            business_name = m.group(1).strip()
        m2 = re.search(r"Business Address:\s*(.*?)(?:\n|$)", text, re.I | re.S)
        if m2:
            address_lines = [l.strip() for l in m2.group(1).split("\n") if l.strip()]
        if not address_lines:
            for row in soup.find_all(class_="indent-left"):
                line = row.get_text(strip=True)
                if line:
                    address_lines.append(line)
        country, conf, reason = detect_seller_country(business_name, address_lines, text)
        return {"business_name": business_name, "address_lines": address_lines,
                "seller_country": country, "country_confidence": conf,
                "country_reason": reason}


def extract_asin(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"/(?:dp|gp/product|product)/([A-Z0-9]{10})(?:[/?&#]|$)", text, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"[?&]asin=([A-Z0-9]{10})(?:[&#]|$)", text, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(B[0-9A-Z]{9}|[0-9]{9}[0-9X])\b", text, re.I)
    return m.group(1).upper() if m else None
