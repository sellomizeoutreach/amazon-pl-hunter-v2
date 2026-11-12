"""Fixture integrity tests — backend-independent.

These prove the saved HTML fixtures actually contain the data the golden
tests rely on (review counts, ASINs, seller info, CAPTCHA markers). They run
green right now, before any backend code exists, and they catch a fixture
that was silently edited into uselessness.
"""

from bs4 import BeautifulSoup

from conftest import fixture_html


def _soup(name):
    return BeautifulSoup(fixture_html(name), "html.parser")


def _review_counts(soup):
    return [int(el.get_text(strip=True)) for el in soup.select(".review-count")]


def test_search_fixture_has_three_products_with_review_data():
    soup = _soup("search_results.html")
    items = soup.select(".s-result-item[data-asin]")
    assert len(items) == 3
    asins = [it["data-asin"] for it in items]
    assert asins == ["B0CNHOME01", "B0CINNOVA2", "B0CKPRO003"]
    assert _review_counts(soup) == [42, 87, 210]
    brands = [el.get_text(strip=True) for el in soup.select(".brand-name")]
    assert brands == ["Nova Home", "Innovatech", "KitchenPro"]
    sellers = [el.get_text(strip=True) for el in soup.select(".sold-by")]
    assert sellers[0] == "Sold by Nova Home LLC"
    assert sellers[1] == "Sold by Innovatech Kitchen Co"


def test_pdp_fixture_is_the_nova_home_healthy_listing():
    soup = _soup("pdp.html")
    assert soup.select_one("#productTitle").get_text(strip=True).startswith(
        "Nova Home Bamboo Cutting Board")
    assert "Nova Home" in soup.select_one("#bylineInfo").get_text()
    assert "42 ratings" in soup.select_one("#acrCustomerReviewText").get_text()
    assert "4.6 out of 5 stars" in soup.select_one("#acrPopover").get_text()
    assert soup.select_one("#add-to-cart-button") is not None
    assert "In Stock" in soup.select_one("#availability").get_text()
    assert soup.select_one("#aplus") is not None
    assert len(soup.select("#feature-bullets ul li")) == 5
    detail = soup.select_one("#detailBulletsWrapper_feature_div").get_text()
    assert "March 12, 2025" in detail
    assert soup.select_one("#merchant-info a")["href"] == "/sp?seller=A1NOVAHOME1"


def test_seller_fixture_is_a_us_seller():
    soup = _soup("seller_page.html")
    text = soup.get_text(separator=" ")
    assert "Nova Home LLC" in text
    assert "456 Oak Street" in text
    assert "Austin, TX 78704" in text
    assert "United States" in text


def test_catalogue_page1_fixture_all_under_100():
    counts = _review_counts(_soup("catalogue_page1.html"))
    assert counts == [42, 87, 64]
    assert max(counts) < 100


def test_catalogue_page2_fixture_has_high_review_sku():
    soup = _soup("catalogue_page2.html")
    counts = _review_counts(soup)
    assert counts == [312, 18]
    item = soup.select_one('.catalog-item[data-asin="B0CNHOME99"]')
    assert item is not None
    assert "Butcher Block" in item.get_text()
    assert "312" in item.select_one(".review-count").get_text()


def test_captcha_fixture_has_robot_check_markers_and_no_products():
    html = fixture_html("captcha.html")
    lowered = html.lower()
    assert "validatecaptcha" in lowered
    assert "not a robot" in lowered
    assert "robot-check" in lowered
    soup = BeautifulSoup(html, "html.parser")
    assert soup.select(".s-result-item") == []
    assert soup.select(".catalog-item") == []
    assert "data-asin" not in html


def test_fixtures_are_readable_by_the_real_parser():
    """The backend's own Scraper must parse the fixtures into the exact
    rows the golden tests rely on — a fixture the parser cannot read proves
    nothing. Skips while backend/scraper.py does not exist."""
    import pytest as _pytest

    try:
        from backend.scraper import Scraper
    except ModuleNotFoundError as exc:
        _pytest.skip(f"backend.scraper not implemented yet ({exc})")

    search = Scraper.parse_search(fixture_html("search_results.html"))
    assert [(i["asin"], i["reviews"]) for i in search] == [
        ("B0CNHOME01", 42), ("B0CINNOVA2", 87), ("B0CKPRO003", 210)]
    p1 = Scraper.parse_search(fixture_html("catalogue_page1.html"))
    assert [i["reviews"] for i in p1] == [42, 87, 64]
    p2 = Scraper.parse_search(fixture_html("catalogue_page2.html"))
    assert [i["reviews"] for i in p2] == [312, 18]
    assert Scraper.parse_search(fixture_html("captcha.html")) == []

    pdp = Scraper.parse_pdp(fixture_html("pdp.html"), "B0CNHOME01")
    assert pdp["brand"] == "Nova Home"
    assert pdp["reviews"] == 42
    assert pdp["seller_id"] == "A1NOVAHOME1"
    assert pdp["buybox_seller"] == "Nova Home LLC"
    assert pdp["has_aplus"] is True

    seller = Scraper.parse_seller_page(fixture_html("seller_page.html"))
    assert seller["business_name"] == "Nova Home LLC"
    assert seller["seller_country"] == "US"
    assert seller["country_confidence"] >= 0.9
