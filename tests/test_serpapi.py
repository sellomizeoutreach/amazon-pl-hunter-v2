"""SerpApi tests — offline, fixtures modelled on the real SerpApi responses
(serpapi.com docs, verified 2026-11-12). No live calls: the client takes an
injectable request_fn, and the runner tests use a stub client. The network
guard in conftest.py would fail any test that touched the network anyway.

Covered: search parsing + sponsored skip, reviews<100 pre-filter (product
is never called for >=100-review or sponsored ASINs), brand parsing in
both forms ("Brand: X" and item_specifications), budget-cap enforcement,
UNKNOWN-country leads saved with needs_review, fail-closed catalogue on
brand-search failure, and a budget hit ending a run gracefully.
"""

import json

import pytest

from backend.serpapi_client import SerpApiBudgetError, SerpApiClient, SerpApiError

FAKE_KEY = "test-serpapi-key"

# --- Fixtures modelled on real SerpApi responses ---------------------------

SEARCH_JSON = {
    "search_metadata": {"status": "Success"},
    "search_parameters": {"engine": "amazon", "amazon_domain": "amazon.com"},
    "organic_results": [
        {"position": 1, "asin": "B0SPONSOR1", "title": "Sponsored Diffuser Pro",
         "rating": 4.8, "reviews": 45, "extracted_price": 29.99,
         "sponsored": True,
         "link_clean": "https://www.amazon.com/dp/B0SPONSOR1",
         "thumbnail": "https://example.com/t1.jpg"},
        {"position": 2, "asin": "B0BIGREV01", "title": "Mega Diffuser 500",
         "rating": 4.5, "reviews": 1240, "extracted_price": 19.99,
         "sponsored": False,
         "link_clean": "https://www.amazon.com/dp/B0BIGREV01"},
        {"position": 3, "asin": "B0GOOD0001",
         "title": "Aurora Peak Essential Oil Diffuser", "rating": 4.6,
         "reviews": 42, "extracted_price": 24.99, "sponsored": False,
         "link_clean": "https://www.amazon.com/dp/B0GOOD0001"},
        {"position": 4, "asin": "B0STRNG001", "title": "String Reviews Gadget",
         "rating": 4.0, "reviews": "1,234", "extracted_price": 9.99,
         "sponsored": False,
         "link_clean": "https://www.amazon.com/dp/B0STRNG001"},
        {"position": 5, "title": "No ASIN on this card", "rating": 5.0,
         "reviews": 3, "sponsored": False},
    ],
}

PRODUCT_JSON_WITH_SELLER = {
    "product_results": {
        "asin": "B0GOOD0001", "title": "Aurora Peak Essential Oil Diffuser",
        "brand": "Brand: Aurora Peak", "rating": 4.6, "reviews": 42,
        "extracted_price": 24.99,
        "seller": {"name": "Aurora Peak Goods", "id": "A1SELLER123",
                   "link": "https://www.amazon.com/sp?seller=A1SELLER123"},
    },
}

PRODUCT_JSON_NO_SELLER = {
    "product_results": {
        "asin": "B0FERN0001", "title": "Willow Fern Bamboo Organizer",
        "rating": 4.4, "reviews": 34, "extracted_price": 18.5,
        "item_specifications": [
            {"name": "Brand", "value": "Willow & Fern Co"},
            {"name": "Material", "value": "Bamboo"},
        ],
    },
}

BRAND_SEARCH_JSON = {
    "organic_results": [
        {"position": 1, "asin": "B0GOOD0001",
         "title": "Aurora Peak Essential Oil Diffuser", "rating": 4.6,
         "reviews": 42, "sponsored": False},
        {"position": 2, "asin": "B0GOOD0002",
         "title": "Aurora Peak Diffuser Refills", "rating": 4.3,
         "reviews": 17, "sponsored": False},
    ],
}


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        return self._payload


def _request_fn_for(mapping):
    """Route fake requests by engine: mapping is {engine: payload-or-resp}."""
    def _fn(url, params=None, timeout=None):
        params = params or {}
        engine = params.get("engine")
        resp = mapping.get(engine)
        if isinstance(resp, _FakeResp):
            return resp
        if isinstance(resp, dict):
            return _FakeResp(resp)
        return _FakeResp({"error": f"no fixture for engine {engine}"}, status_code=400)
    return _fn


def _client(**kwargs):
    kwargs.setdefault("api_key", FAKE_KEY)
    return SerpApiClient(**kwargs)


# --- Client unit tests ------------------------------------------------------

def test_search_parses_and_flags_sponsored():
    # The parser keeps the sponsored flag on the item (so callers can see
    # it); the SKIP itself lives in the runner — see
    # test_reviews_prefilter_and_sponsored_skip.
    client = _client(request_fn=_request_fn_for({"amazon": SEARCH_JSON}))
    results = client.search_amazon("essential oil diffuser", page=1)
    asins = [r["asin"] for r in results]
    assert "B0SPONSOR1" in asins
    sponsored = next(r for r in results if r["asin"] == "B0SPONSOR1")
    assert sponsored["sponsored"] is True
    assert "B0BIGREV01" in asins and "B0GOOD0001" in asins
    assert len(results) == 4, "only the no-ASIN card is dropped by the parser"
    good = next(r for r in results if r["asin"] == "B0GOOD0001")
    assert good["title"] == "Aurora Peak Essential Oil Diffuser"
    assert good["reviews"] == 42 and good["rating"] == 4.6
    assert good["price"] == "24.99"
    assert good["sponsored"] is False
    stringy = next(r for r in results if r["asin"] == "B0STRNG001")
    assert stringy["reviews"] == 1234, "string reviews ('1,234') must coerce to int"


def test_product_brand_prefix_form():
    client = _client(request_fn=_request_fn_for(
        {"amazon_product": PRODUCT_JSON_WITH_SELLER}))
    detail = client.product("B0GOOD0001")
    assert detail["brand"] == "Aurora Peak", "'Brand: Aurora Peak' must parse to 'Aurora Peak'"
    assert detail["seller_name"] == "Aurora Peak Goods"
    assert detail["seller_id"] == "A1SELLER123"
    assert detail["seller_country"] == "UNKNOWN"
    assert detail["country_confidence"] == 0.0
    assert detail["reviews"] == 42 and detail["rating"] == 4.6


def test_product_brand_specifications_form():
    client = _client(request_fn=_request_fn_for(
        {"amazon_product": PRODUCT_JSON_NO_SELLER}))
    detail = client.product("B0FERN0001")
    assert detail["brand"] == "Willow & Fern Co"
    assert detail["seller_name"] == ""
    assert detail["seller_id"] is None
    assert detail["seller_country"] == "UNKNOWN"


def test_non200_and_error_field_raise_never_empty():
    client = _client(request_fn=_request_fn_for(
        {"amazon": _FakeResp({"error": "Invalid API key"}, status_code=401)}))
    with pytest.raises(SerpApiError, match="401"):
        client.search_amazon("diffuser")
    client2 = _client(request_fn=_request_fn_for(
        {"amazon": {"error": "Invalid API key"}}))
    with pytest.raises(SerpApiError, match="Invalid API key"):
        client2.search_amazon("diffuser")
    # A failed call still counts (it may still be billed) — the budget
    # must see it, and the caller must never get an empty list as if the
    # search had been clean.
    assert client.searches_used == 1
    assert client2.searches_used == 1


def test_search_budget_enforced():
    client = _client(max_searches=2,
                     request_fn=_request_fn_for({"amazon": SEARCH_JSON}))
    client.search_amazon("kw one")
    client.search_amazon("kw two")
    assert client.searches_used == 2
    with pytest.raises(SerpApiBudgetError):
        client.search_amazon("kw three")
    assert client.searches_used == 2, "a budget-blocked call must not count"


def test_product_budget_enforced():
    client = _client(max_products=1,
                     request_fn=_request_fn_for(
                         {"amazon_product": PRODUCT_JSON_WITH_SELLER}))
    client.product("B0GOOD0001")
    assert client.products_used == 1
    with pytest.raises(SerpApiBudgetError):
        client.product("B0FERN0001")


# --- Runner tests (stub client, temp DB) ------------------------------------

class _StubClient:
    """Duck-typed stand-in for SerpApiClient: no network, full control."""

    def __init__(self, search_results, products, brand_results,
                 fail_brand_search=False, budget_on_search_at=None):
        self.search_results = search_results
        self.products = products
        self.brand_results = brand_results
        self.fail_brand_search = fail_brand_search
        self.budget_on_search_at = budget_on_search_at
        self.searches_used = 0
        self.products_used = 0
        self.product_calls: list[str] = []

    def search_amazon(self, keyword, page=1):
        self.searches_used += 1
        if (self.budget_on_search_at is not None
                and self.searches_used >= self.budget_on_search_at):
            raise SerpApiBudgetError(
                f"SerpApi search budget hit: {self.searches_used} searches used")
        return self.search_results

    def brand_search(self, brand, page=1):
        self.searches_used += 1
        if self.fail_brand_search:
            raise SerpApiError("brand search failed")
        return self.brand_results

    def product(self, asin):
        self.products_used += 1
        self.product_calls.append(asin)
        return self.products[asin]


def _serpapi_session(tmp_path):
    import importlib
    db = importlib.import_module("backend.db")
    db_path = tmp_path / "serpapi_test.db"
    url = f"sqlite:///{db_path}"
    if hasattr(db, "reset_engine_for_tests"):
        db.reset_engine_for_tests()
    db.init_db(url)
    return db.get_session()


def _run_row(session):
    from backend.db import Run
    run = Run(status="running", keywords_used="[]")
    session.add(run)
    session.commit()
    return run


def _good_stub(**kwargs):
    return _StubClient(
        search_results=[
            {"asin": "B0SPONSOR1", "title": "Sponsored Diffuser Pro",
             "reviews": 45, "rating": 4.8, "price": "29.99", "sponsored": True,
             "url": "https://www.amazon.com/dp/B0SPONSOR1"},
            {"asin": "B0BIGREV01", "title": "Mega Diffuser 500",
             "reviews": 1240, "rating": 4.5, "price": "19.99",
             "sponsored": False, "url": "https://www.amazon.com/dp/B0BIGREV01"},
            {"asin": "B0GOOD0001", "title": "Aurora Peak Essential Oil Diffuser",
             "reviews": 42, "rating": 4.6, "price": "24.99",
             "sponsored": False, "url": "https://www.amazon.com/dp/B0GOOD0001"},
        ],
        products={"B0GOOD0001": {
            "asin": "B0GOOD0001", "brand": "Aurora Peak",
            "title": "Aurora Peak Essential Oil Diffuser",
            "rating": 4.6, "reviews": 42, "price": "24.99",
            "seller_name": "Aurora Peak Goods", "seller_id": "A1SELLER123",
            "seller_country": "UNKNOWN", "country_confidence": 0.0}},
        brand_results=[],
        **kwargs)


def test_reviews_prefilter_and_sponsored_skip(tmp_path):
    from backend.runner import _serpapi_hunt
    session = _serpapi_session(tmp_path)
    try:
        stub = _good_stub()
        result = _serpapi_hunt(session, _run_row(session), ["diffuser"], client=stub)
    finally:
        session.close()
    # product() is called ONLY for the sub-100, non-sponsored ASIN — never
    # for the sponsored card or the 1,240-review one.
    assert stub.product_calls == ["B0GOOD0001", "B0GOOD0001"], (
        f"product called for {stub.product_calls} — pre-filter failed "
        "(same ASIN twice = page 1 + page 2, which is expected)")
    assert result["leads_new"] == 1, "the good ASIN becomes exactly one lead (dedup on page 2)"


def test_unknown_country_lead_saved_with_needs_review(tmp_path):
    from backend.db import Lead
    from backend.runner import _serpapi_hunt
    session = _serpapi_session(tmp_path)
    try:
        stub = _good_stub()
        result = _serpapi_hunt(session, _run_row(session), ["diffuser"], client=stub)
        lead = session.query(Lead).one()
        # Read relationship attributes BEFORE closing the session.
        values = (lead.brand.name, lead.brand.seller_country,
                  lead.brand.country_confidence, lead.needs_review,
                  lead.source, lead.brand.seller_id)
    finally:
        session.close()
    assert result["leads_new"] == 1
    assert values[0] == "aurora peak"
    assert values[1] == "UNKNOWN"
    assert values[2] == 0.0
    assert values[3] is True, "UNKNOWN-country leads must be flagged for hand-check"
    assert values[4] == "serpapi"
    assert values[5] == "A1SELLER123"


def test_no_seller_block_lead_saved_with_needs_review(tmp_path):
    """Jack's call 2026-11-12: a SerpApi product with no seller block is
    saved as needs_review (PL status unverified) instead of dropped."""
    from backend.db import Lead
    from backend.runner import _serpapi_hunt
    session = _serpapi_session(tmp_path)
    try:
        stub = _good_stub()
        stub.products = {"B0GOOD0001": {
            "asin": "B0GOOD0001", "brand": "Aurora Peak",
            "title": "Aurora Peak Essential Oil Diffuser",
            "rating": 4.6, "reviews": 42, "price": "24.99",
            "seller_name": "", "seller_id": None,
            "seller_country": "UNKNOWN", "country_confidence": 0.0}}
        result = _serpapi_hunt(session, _run_row(session), ["diffuser"], client=stub)
        lead = session.query(Lead).one()
        values = (lead.brand.name, lead.needs_review, lead.source)
    finally:
        session.close()
    assert result["leads_new"] == 1
    assert values == ("aurora peak", True, "serpapi")


def test_fail_closed_catalogue_on_brand_search_failure(tmp_path):
    from backend.db import Lead
    from backend.runner import _serpapi_hunt
    session = _serpapi_session(tmp_path)
    try:
        stub = _good_stub(fail_brand_search=True)
        result = _serpapi_hunt(session, _run_row(session), ["diffuser"], client=stub)
        count = session.query(Lead).count()
    finally:
        session.close()
    assert result["leads_new"] == 0
    assert count == 0, "a failed brand search must mean no lead (fail closed)"


def test_budget_hit_ends_run_gracefully(tmp_path, monkeypatch):
    """A budget hit mid-run: status done (not failed/crash), budget flag +
    searches-used in the summary, and the run still writes its local HTML
    summary (email is governed by the batch rule, not by this test)."""
    from pathlib import Path

    import backend.runner as runner_mod
    from backend.runner import run_hunt

    monkeypatch.setenv("PLH_LIVE", "1")
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    monkeypatch.setattr(runner_mod, "generate_keywords",
                        lambda *a, **k: [("kw one", "seed"), ("kw two", "seed")])
    # Redirect the local summary write into tmp_path: the real
    # data/run_summaries/ holds earlier real runs and must not be touched.
    written: list[Path] = []

    def _fake_write(summary):
        path = tmp_path / f"run_{summary['run_id']}.html"
        path.write_text(f"run {summary['run_id']}", encoding="utf-8")
        written.append(path)
        return path

    monkeypatch.setattr(runner_mod, "write_summary_locally", _fake_write)
    session = _serpapi_session(tmp_path)
    stub = _StubClient(search_results=[], products={}, brand_results=[],
                       budget_on_search_at=2)
    summary = None
    try:
        summary = run_hunt(mode="live", max_keywords=2, session=session,
                           serpapi_client=stub)
    finally:
        session.close()
    assert summary["status"] == "done"
    assert summary["serpapi_mode"] is True
    assert summary["serpapi_budget_hit"] is True
    assert summary["serpapi_searches_used"] == 2
    assert summary["serpapi_note"] and "budget" in summary["serpapi_note"].lower()
    assert summary["email_status"] == "skipped_zero_leads"
    assert len(written) == 1 and written[0].exists(), (
        "the run must still write its local summary")
