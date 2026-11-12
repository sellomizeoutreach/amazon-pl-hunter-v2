"""SerpApi client — the live discovery path when direct Amazon scraping is
blocked (this VM's egress returns 503 / a shadowban page for amazon.com,
and the cloud browser only gets Amazon's error page).

SerpApi API shape (serpapi.com docs, verified 2026-11-12):
- Search:  GET https://serpapi.com/search.json?engine=amazon&k=<keyword>
  &page=<n>&amazon_domain=amazon.com&api_key=<key>
  -> organic_results[]: asin, title, rating, reviews, extracted_price,
     sponsored (bool), link_clean, thumbnail.
- Product: GET https://serpapi.com/search.json?engine=amazon_product
  &asin=<asin>&amazon_domain=amazon.com&api_key=<key>
  -> product_results: brand ("Brand: X", or item_specifications.brand),
     rating, reviews, extracted_price, and sometimes a seller block
     (name/id). Seller country is NOT reliably present in the response.

Budget: every engine=amazon call (keyword search AND brand/catalogue
search) counts against SERPAPI_MAX_SEARCHES_PER_RUN (default 120); every
engine=amazon_product call counts against SERPAPI_MAX_PRODUCTS_PER_RUN
(default 60). Counters are incremented BEFORE the HTTP call — a failed
call may still be billed by SerpApi — and hitting a budget raises
SerpApiBudgetError so the runner ends the run gracefully instead of
silently spending. Non-200 responses and SerpApi "error" fields raise
SerpApiError, never an empty list pretending the search was clean.
The API key is never logged.
"""
from __future__ import annotations

import os
import re
from typing import Any

import requests

from backend import get_logger

log = get_logger(__name__)

BASE_URL = "https://serpapi.com/search.json"
AMAZON_DOMAIN = "amazon.com"

DEFAULT_MAX_SEARCHES_PER_RUN = 120
DEFAULT_MAX_PRODUCTS_PER_RUN = 60


class SerpApiError(Exception):
    """SerpApi returned an error (HTTP non-200, "error" field, transport)."""


class SerpApiBudgetError(SerpApiError):
    """A per-run SerpApi budget (searches or products) was hit."""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _coerce_reviews(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        m = re.search(r"([\d,]+)", value)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                return 0
    return 0


def _coerce_rating(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class SerpApiClient:
    """Thin requests-based client for SerpApi's Amazon engines.

    `request_fn` is injectable (defaults to requests.get) so tests can
    serve canned responses offline; production code never passes it.
    """

    def __init__(self, api_key: str | None = None,
                 max_searches: int | None = None,
                 max_products: int | None = None,
                 request_fn=None, timeout: float = 30.0):
        key = (api_key or os.environ.get("SERPAPI_API_KEY", "")).strip()
        if not key:
            raise SerpApiError("SERPAPI_API_KEY is not set")
        self.api_key = key
        self.max_searches = (max_searches if max_searches is not None
                             else _int_env("SERPAPI_MAX_SEARCHES_PER_RUN",
                                           DEFAULT_MAX_SEARCHES_PER_RUN))
        self.max_products = (max_products if max_products is not None
                             else _int_env("SERPAPI_MAX_PRODUCTS_PER_RUN",
                                           DEFAULT_MAX_PRODUCTS_PER_RUN))
        self.request_fn = request_fn or requests.get
        self.timeout = timeout
        # Every call is counted here: searches_used covers engine=amazon
        # calls (keyword + brand/catalogue searches); products_used covers
        # engine=amazon_product calls.
        self.searches_used = 0
        self.products_used = 0

    # -- transport ---------------------------------------------------------
    def _get(self, params: dict, kind: str) -> dict:
        if kind == "search":
            if self.searches_used >= self.max_searches:
                raise SerpApiBudgetError(
                    f"SerpApi search budget hit: {self.searches_used}/"
                    f"{self.max_searches} searches used this run")
            self.searches_used += 1
        else:
            if self.products_used >= self.max_products:
                raise SerpApiBudgetError(
                    f"SerpApi product budget hit: {self.products_used}/"
                    f"{self.max_products} product calls used this run")
            self.products_used += 1
        try:
            resp = self.request_fn(BASE_URL, params=params, timeout=self.timeout)
        except Exception as exc:
            raise SerpApiError(f"SerpApi request failed: {exc}") from exc
        status = getattr(resp, "status_code", None)
        if status != 200:
            body = (getattr(resp, "text", "") or "")[:200]
            raise SerpApiError(f"SerpApi HTTP {status}: {body}")
        try:
            data = resp.json()
        except Exception as exc:
            raise SerpApiError(f"SerpApi returned non-JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SerpApiError("SerpApi returned a non-object response")
        if data.get("error"):
            raise SerpApiError(f"SerpApi error: {data['error']}")
        return data

    # -- search ------------------------------------------------------------
    def search_amazon(self, keyword: str, page: int = 1) -> list[dict]:
        """Amazon search via SerpApi. Returns plain dicts in the shape the
        runner consumes: asin/title/reviews/rating/price/url/sponsored."""
        data = self._get({
            "engine": "amazon", "k": keyword, "page": page,
            "amazon_domain": AMAZON_DOMAIN, "api_key": self.api_key,
        }, kind="search")
        return [self._parse_search_item(item)
                for item in (data.get("organic_results") or [])
                if isinstance(item, dict) and self._parse_search_item(item)]

    def brand_search(self, brand: str, page: int = 1) -> list[dict]:
        """Catalogue audit search: the brand's own search results, same
        shape as search_amazon. Counts against the SEARCH budget, like any
        other engine=amazon call — a full catalogue audit costs up to
        MAX_CATALOGUE_PAGES searches per candidate brand."""
        return self.search_amazon(brand, page=page)

    @staticmethod
    def _parse_search_item(item: dict) -> dict | None:
        asin = str(item.get("asin") or "").strip().upper()
        if len(asin) != 10:
            return None
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        return {
            "asin": asin, "title": title,
            "reviews": _coerce_reviews(item.get("reviews")),
            "rating": _coerce_rating(item.get("rating")),
            "price": str(item.get("extracted_price") or item.get("price") or ""),
            "url": (item.get("link_clean") or item.get("link")
                    or f"https://www.amazon.com/dp/{asin}"),
            "sponsored": bool(item.get("sponsored")),
        }

    # -- product -----------------------------------------------------------
    def product(self, asin: str) -> dict:
        """Single-product lookup. Returns brand/rating/reviews/seller info
        in the shape the runner consumes. Seller country is parsed only
        when the response actually carries it (rare); otherwise UNKNOWN."""
        data = self._get({
            "engine": "amazon_product", "asin": asin,
            "amazon_domain": AMAZON_DOMAIN, "api_key": self.api_key,
        }, kind="product")
        pr = data.get("product_results") or {}
        if not isinstance(pr, dict):
            raise SerpApiError("SerpApi product response has no product_results")
        brand = self._parse_brand(pr)
        seller = pr.get("seller") or {}
        if isinstance(seller, list):
            seller = seller[0] if seller and isinstance(seller[0], dict) else {}
        if not isinstance(seller, dict):
            seller = {}
        seller_name = str(seller.get("name") or "").strip()
        seller_id = str(seller.get("id") or seller.get("seller_id") or "").strip() or None
        country, country_conf = self._parse_seller_country(seller, seller_name)
        return {
            "asin": asin.upper(),
            "brand": brand,
            "title": str(pr.get("title") or "").strip(),
            "rating": _coerce_rating(pr.get("rating")),
            "reviews": _coerce_reviews(pr.get("reviews")),
            "price": str(pr.get("extracted_price") or pr.get("price") or ""),
            "seller_name": seller_name,
            "seller_id": seller_id,
            "seller_country": country,
            "country_confidence": country_conf,
        }

    @staticmethod
    def _parse_brand(pr: dict) -> str:
        # Form 1: product_results.brand, often "Brand: Aurora Peak".
        raw = pr.get("brand")
        if isinstance(raw, str) and raw.strip():
            return re.sub(r"^\s*brand:\s*", "", raw.strip(), flags=re.I)
        raw = pr.get("brand_name")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        # Form 2: item_specifications — dict or list of {name, value}.
        specs = pr.get("item_specifications")
        if isinstance(specs, dict):
            for key in ("brand", "Brand"):
                if isinstance(specs.get(key), str) and specs[key].strip():
                    return specs[key].strip()
        elif isinstance(specs, list):
            for entry in specs:
                if isinstance(entry, dict) and str(entry.get("name", "")).strip().lower() == "brand":
                    value = str(entry.get("value", "")).strip()
                    if value:
                        return value
        return ""

    @staticmethod
    def _parse_seller_country(seller: dict, seller_name: str) -> tuple[str, float]:
        """Seller country from whatever the seller block carries, via the
        same trap-safe detector as the direct path (a bare 'NY' or a
        'Central Ave' line must never imply a country). Usually nothing
        is present -> UNKNOWN, 0.0."""
        from backend.scoring import detect_seller_country
        location = seller.get("country") or seller.get("location") or seller.get("address")
        if isinstance(location, str) and location.strip():
            return detect_seller_country(seller_name, [location.strip()], location)[:2]
        return "UNKNOWN", 0.0
