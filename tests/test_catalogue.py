"""Catalogue audit tests — fail-closed behaviour and pagination.

SPEC audit shape:
  catalog: {total_products, max_reviews, all_under_100, pages_checked,
            incomplete: bool, fail_reason: str|null}
  FAIL-CLOSED: blocked/CAPTCHA/error fetch -> all_under_100=false,
  incomplete=true, fail_reason set, persona=UNKNOWN, needs_review=true.
  Never pass on failure.

The audit also must check beyond page 1: the page-2 fixture hides a
312-review SKU (B0CNHOME99) that a page-1-only audit would miss — the exact
v1 failure mode from the audit (catalogue audit "checks page 1 only").

Two levels are tested against the landed backend:
- audit_catalogue (backend/scoring.py): pure function over parsed review
  rows — unit level.
- audit_brand (backend/pipeline.py): full SPEC audit shape, with a stub
  scraper that serves fixture HTML / raises BlockedError, so the
  fail-closed path is exercised end-to-end offline (no network).

Fixture-shape tests run now (backend-independent); backend-backed tests SKIP
while the auditor does not exist — see tests/helpers.py.
"""

import os
import tempfile

import pytest
from bs4 import BeautifulSoup

from conftest import fixture_html
from tests.helpers import audit_catalogue, catalog_block, load_catalogue_auditor


def _review_rows(*fixture_names):
    """Parse fixture catalogue pages into [{asin, reviews}] review rows."""
    rows = []
    for name in fixture_names:
        soup = BeautifulSoup(fixture_html(name), "html.parser")
        for item in soup.select(".catalog-item[data-asin]"):
            rows.append({
                "asin": item["data-asin"],
                "reviews": int(item.select_one(".review-count").get_text(strip=True)),
            })
    return rows


def _review_counts(html):
    soup = BeautifulSoup(html, "html.parser")
    return [int(el.get_text(strip=True)) for el in soup.select(".review-count")]


# --- Fixture-shape checks (run now, no backend) ---------------------------

def test_captcha_fixture_is_detectable_as_block():
    html = fixture_html("captcha.html").lower()
    markers = ("robot-check", "validatecaptcha", "not a robot")
    assert any(m in html for m in markers)
    assert "data-asin" not in html


def test_page1_only_would_wrongly_pass():
    """Documents the trap: page 1 alone looks like a pass (max 87)."""
    assert max(_review_counts(fixture_html("catalogue_page1.html"))) == 87


def test_page2_holds_the_high_review_sku():
    assert max(_review_counts(fixture_html("catalogue_page2.html"))) == 312


def test_fixture_review_rows_parse():
    rows = _review_rows("catalogue_page1.html", "catalogue_page2.html")
    assert [(r["asin"], r["reviews"]) for r in rows] == [
        ("B0CNHOME01", 42), ("B0CNHOME18", 87), ("B0CNHOME22", 64),
        ("B0CNHOME99", 312), ("B0CNHOME31", 18),
    ]


# --- Backend-backed audit_catalogue tests ----------------------------------

class TestCatalogueAudit:
    @pytest.fixture()
    def auditor(self):
        return load_catalogue_auditor()

    def test_captcha_fails_closed(self, auditor):
        _, fn = auditor
        cat = catalog_block(audit_catalogue(
            fn, None, blocked=True, error="robot-check: validateCaptcha",
            pages_checked=0))
        assert cat["all_under_100"] is False, "CAPTCHA audit must not pass"
        assert cat["incomplete"] is True
        assert cat.get("fail_reason"), "fail_reason must be set on a blocked fetch"

    def test_page1_only_passes(self, auditor):
        _, fn = auditor
        cat = catalog_block(audit_catalogue(
            fn, _review_rows("catalogue_page1.html"), pages_checked=1))
        assert cat["all_under_100"] is True
        assert cat["incomplete"] is False
        assert cat.get("fail_reason") in (None, "")
        assert cat["max_reviews"] == 87
        assert cat["pages_checked"] == 1

    def test_page2_high_review_sku_is_caught(self, auditor):
        """The 312-review SKU on page 2 must fail the audit — this is the
        regression test for v1's page-1-only catalogue check."""
        _, fn = auditor
        cat = catalog_block(audit_catalogue(
            fn, _review_rows("catalogue_page1.html", "catalogue_page2.html"),
            pages_checked=2))
        assert cat["all_under_100"] is False
        assert cat["max_reviews"] == 312
        assert cat["pages_checked"] == 2
        assert cat["total_products"] == 5

    def test_empty_catalogue_is_incomplete_not_a_pass(self, auditor):
        _, fn = auditor
        cat = catalog_block(audit_catalogue(fn, [], pages_checked=0))
        assert cat["all_under_100"] is False
        assert cat["incomplete"] is True
        assert cat.get("fail_reason")


# --- Full-shape audit_brand tests (stub scraper, offline) ------------------

def _fixture_scraper(mode="pages"):
    """Stub scraper (real Scraper subclass) serving fixture HTML for
    catalogue pages, or raising BlockedError — exercises audit_brand's
    fail-closed path offline. Real parse_search / catalogue_url are kept."""
    try:
        from backend.scraper import BlockedError, Scraper as _RealScraper
    except ModuleNotFoundError as exc:
        pytest.skip(f"backend.scraper not implemented yet ({exc})")

    class _Stub(_RealScraper):
        def __init__(self):
            super().__init__(delay_min=0, delay_max=0)
            self.mode = mode

        def fetch(self, url):
            if self.mode == "blocked":
                raise BlockedError("robot-check: validateCaptcha")
            if "page=1" in url:
                return fixture_html("catalogue_page1.html")
            if "page=2" in url:
                return fixture_html("catalogue_page2.html")
            return "<html><body></body></html>"

    return _Stub()


def _audit_session(tmp_path):
    """A backend.db session bound to a throwaway DB, or SKIP."""
    import importlib
    try:
        db = importlib.import_module("backend.db")
        pipeline = importlib.import_module("backend.pipeline")
    except ModuleNotFoundError as exc:
        pytest.skip(f"audit_brand backend modules not implemented yet ({exc})")
    db_path = tmp_path / "audit_test.db"
    url = f"sqlite:///{db_path}"
    if hasattr(db, "reset_engine_for_tests"):
        db.reset_engine_for_tests()
    db.init_db(url)
    return db.get_session(), pipeline


class TestAuditBrand:
    def test_blocked_catalogue_fails_closed_full_shape(self, tmp_path):
        """CAPTCHA on the catalogue fetch -> full SPEC fail-closed result."""
        session, pipeline = _audit_session(tmp_path)
        try:
            result = pipeline.audit_brand(
                session, brand="Nova Home", scraper=_fixture_scraper(mode="blocked"))
        finally:
            session.close()
        cat = result["catalog"]
        assert cat["all_under_100"] is False
        assert cat["incomplete"] is True
        assert cat.get("fail_reason"), "fail_reason must be set"
        assert result["persona"] == "UNKNOWN"
        assert result["needs_review"] is True

    def test_page2_sku_caught_in_full_audit(self, tmp_path):
        """End-to-end pagination proof: the 312-review page-2 SKU fails the
        brand audit even though page 1 alone passes."""
        session, pipeline = _audit_session(tmp_path)
        try:
            result = pipeline.audit_brand(
                session, brand="Nova Home", scraper=_fixture_scraper(mode="pages"))
        finally:
            session.close()
        cat = result["catalog"]
        assert cat["all_under_100"] is False
        assert cat["max_reviews"] == 312
        assert cat["total_products"] == 5
        assert cat["pages_checked"] >= 2
        assert any("312" in p for p in result["listing_problems"])
