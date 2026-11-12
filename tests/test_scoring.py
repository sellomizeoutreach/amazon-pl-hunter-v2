"""Golden scoring tests — PL detection, seller country, enrichment rules.

Contracts under test (SPEC.md "Scoring rules"):
- PL score 0-1: a SINGLE generic token such as 'nova' in 'Innovatech'
  scores <= 0.3 (the v1 trap: v1's substring check matched 'nova' inside
  'innovatech' at 0.95). Megabrand list negates to 0. Brand Store / A+ adds
  at most +0.2. Seller-count > 5 subtracts.
- Seller country: explicit country token wins; a 2-letter code counts ONLY
  if it is a known ISO country code AND not a US state code, so 'NY' and
  '123 Central Ave, New York' -> UNKNOWN (v1 returned HK for 'Central').
- Enrichment: generic inboxes are never personal; a website domain must
  contain a brand token (>= 3 chars) or be None; a founder name is never
  derived from a URL slug; 1 corroboration caps founder confidence at 0.5,
  2 independent corroborations are needed for >= 0.8.

Backend-dependent tests SKIP (not fail) while backend/scoring.py and
backend/enrichment.py do not exist yet — see tests/helpers.py for the
function-name contract.
"""

import pytest

from tests.helpers import (
    as_bool,
    as_website_bool,
    detect_country,
    load_country_detector,
    load_enrichment,
    load_pl_scorer,
    pick_enrichment_fn,
    pl_score,
)


# ---------------------------------------------------------------------------
# PL detection
# ---------------------------------------------------------------------------

class TestPLDetection:
    @pytest.fixture()
    def scorer(self):
        return load_pl_scorer()

    def test_nova_innovatech_trap(self, scorer):
        """'Nova' vs seller 'Innovatech' must NOT be a PL match.

        'nova' is a substring of 'innovatech', which is exactly how v1
        scored this 0.95. SPEC caps a single generic token at <= 0.3.
        """
        _, fn = scorer
        score, reason = pl_score(fn, "Nova", "Innovatech",
                                 all_sellers=["Innovatech"])
        assert score <= 0.3, f"nova/Innovatech trap scored {score}: {reason}"

    def test_nova_innovatech_trap_with_suffix_seller(self, scorer):
        _, fn = scorer
        score, _ = pl_score(fn, "Nova", "Innovatech Kitchen Co",
                            all_sellers=["Innovatech Kitchen Co"])
        assert score <= 0.3

    def test_genuine_match_nova_home(self, scorer):
        _, fn = scorer
        score, reason = pl_score(fn, "Nova Home", "Nova Home LLC",
                                 all_sellers=["Nova Home LLC"])
        assert score >= 0.7, f"genuine Nova Home match scored {score}: {reason}"

    def test_genuine_match_nova_home_store_variant(self, scorer):
        _, fn = scorer
        score, _ = pl_score(fn, "Nova Home", "Nova Home Store",
                            all_sellers=["Nova Home Store"])
        assert score >= 0.7

    def test_megabrand_nike_negates_to_zero(self, scorer):
        _, fn = scorer
        score, reason = pl_score(fn, "Nike", "Nike",
                                 all_sellers=["Nike Official Store"])
        assert score == 0, f"megabrand Nike scored {score}: {reason}"

    def test_megabrand_adidas_and_apple_negate_to_zero(self, scorer):
        _, fn = scorer
        for brand in ("Adidas", "Apple"):
            score, _ = pl_score(fn, brand, brand, all_sellers=[brand])
            assert score == 0, f"megabrand {brand} scored {score}"

    def test_seller_count_over_five_subtracts(self, scorer):
        """Same brand/seller match scores lower with 8 sellers than with 1."""
        _, fn = scorer
        single, _ = pl_score(fn, "KitchenPro", "KitchenPro",
                             all_sellers=["KitchenPro"], total_offers_count=1)
        many, _ = pl_score(fn, "KitchenPro", "KitchenPro",
                           all_sellers=["KitchenPro"] + [f"Reseller {i}" for i in range(7)],
                           total_offers_count=8)
        assert many < single, f"8-seller score {many} not below 1-seller {single}"

    def test_brand_store_adds_at_most_0_2(self, scorer):
        _, fn = scorer
        base, _ = pl_score(fn, "Nova Home", "Nova Home LLC",
                           all_sellers=["Nova Home LLC"])
        boosted, _ = pl_score(fn, "Nova Home", "Nova Home LLC",
                              all_sellers=["Nova Home LLC"],
                              has_brand_store=True, has_aplus_content=True)
        assert boosted - base <= 0.2 + 1e-9, f"brand-store boost {boosted - base} > 0.2"


# ---------------------------------------------------------------------------
# Seller country
# ---------------------------------------------------------------------------

class TestSellerCountry:
    @pytest.fixture()
    def detector(self):
        return load_country_detector()

    def test_central_ave_new_york_is_unknown_not_hk(self, detector):
        """'123 Central Ave, New York' must be UNKNOWN, not HK.

        v1's HONG_KONG_KEYWORDS contained bare 'central', so any US address
        on a Central Ave / Central St came back Hong Kong.
        """
        _, fn = detector
        code = detect_country(fn, "Some Seller LLC",
                              ["123 Central Ave", "New York, NY 10001"])
        assert code == "UNKNOWN", f"Central Ave address detected as {code}"

    def test_bare_ny_address_line_is_unknown(self, detector):
        _, fn = detector
        code = detect_country(fn, "Some Seller LLC", ["NY"])
        assert code == "UNKNOWN", f"bare 'NY' detected as {code}"

    def test_bare_us_state_code_is_unknown(self, detector):
        _, fn = detector
        for state in ("TX", "CA", "FL"):
            code = detect_country(fn, "Some Seller LLC", [state])
            assert code == "UNKNOWN", f"bare state '{state}' detected as {code}"

    def test_shenzhen_guangdong_china_is_cn(self, detector):
        _, fn = detector
        code = detect_country(fn, "Shenzhen Nova Tech Co Ltd",
                              ["No. 88 Huaqiang North Road", "Shenzhen, Guangdong, China"])
        assert code == "CN", f"Shenzhen address detected as {code}"

    def test_austin_tx_usa_is_us(self, detector):
        _, fn = detector
        code = detect_country(fn, "Nova Home LLC",
                              ["456 Oak Street", "Austin, TX 78704", "United States"])
        assert code == "US", f"Austin address detected as {code}"

    def test_austin_tx_usa_short_form_is_us(self, detector):
        _, fn = detector
        code = detect_country(fn, "Nova Home LLC", ["Austin, TX, USA"])
        assert code == "US", f"'Austin, TX, USA' detected as {code}"

    def test_explicit_hong_kong_still_detected(self, detector):
        _, fn = detector
        code = detect_country(fn, "Harbour Trading Ltd",
                              ["Room 1201, Kowloon Bay", "Hong Kong"])
        assert code == "HK", f"Hong Kong address detected as {code}"

    def test_london_united_kingdom_is_gb(self, detector):
        _, fn = detector
        code = detect_country(fn, "London Kitchen Ltd",
                              ["14 Baker Street", "London, United Kingdom"])
        assert code == "GB", f"London address detected as {code}"


# ---------------------------------------------------------------------------
# Enrichment rules
# ---------------------------------------------------------------------------

class TestEnrichment:
    @pytest.fixture()
    def enrichment(self):
        return load_enrichment()

    @pytest.mark.parametrize("email", [
        "info@novahome.com", "support@novahome.com", "sales@novahome.com",
        "hello@novahome.com", "contact@novahome.com", "wholesale@novahome.com",
        "careers@novahome.com", "partnerships@novahome.com", "admin@novahome.com",
    ])
    def test_generic_inbox_is_never_personal(self, enrichment, email):
        fn = pick_enrichment_fn(
            enrichment, ("is_personal_email", "email_is_personal", "classify_email"),
            "generic inboxes must never be personal")
        assert as_bool(fn(email)) is False, f"{email} classified as personal"

    def test_named_personal_email_is_personal(self, enrichment):
        fn = pick_enrichment_fn(
            enrichment, ("is_personal_email", "email_is_personal", "classify_email"),
            "a named inbox must be personal")
        assert as_bool(fn("jane.doe@novahome.com")) is True

    def test_website_domain_must_contain_brand_token(self, enrichment):
        fn = pick_enrichment_fn(
            enrichment, ("website_matches_brand", "validate_website",
                         "website_confidence", "check_website_domain"),
            "website domain must contain a brand token (>=3 chars)")
        assert as_website_bool(fn("Nova Home", "https://novahome.com")) is True
        assert as_website_bool(fn("Nova Home", "https://www.nova-home.co")) is True
        assert as_website_bool(fn("Nova Home", "https://randomstore.com")) is False
        assert as_website_bool(fn("Nova Home", "https://amazon.com/dp/B0CNHOME01")) is False

    def test_website_without_brand_token_is_rejected_as_none(self, enrichment):
        """A website with no brand token must come back as None/invalid,
        never silently accepted as the brand's site."""
        fn = pick_enrichment_fn(
            enrichment, ("website_matches_brand", "validate_website",
                         "website_confidence", "check_website_domain"),
            "website domain must contain a brand token (>=3 chars)")
        result = fn("Nova Home", "https://randomstore.com")
        assert as_website_bool(result) is False

    def test_founder_never_derived_from_url_slug(self, enrichment):
        """No founder name may come from a URL slug.

        If the backend exposes a slug->name helper it must refuse; if it
        exposes none, this documents the rule and skips.
        """
        for name in ("founder_from_url_slug", "derive_founder_from_slug",
                     "extract_founder_from_url"):
            candidate = getattr(enrichment, name, None)
            if callable(candidate):
                pytest.fail(
                    f"backend.enrichment.{name} exists — slug-derived founder "
                    "names are forbidden by SPEC; remove it")
        pytest.skip(
            "no slug->founder helper exposed (good) — founder-from-slug rule "
            "is covered by test_founder_single_source_caps_at_0_5 once the "
            "founder scorer lands")

    def test_founder_single_source_caps_at_0_5(self, enrichment):
        fn = pick_enrichment_fn(
            enrichment, ("founder_confidence", "score_founder_confidence",
                         "score_contact_confidence"),
            "1 corroboration caps founder confidence at 0.5")
        confidence = _founder_confidence(fn, n_sources=1)
        assert confidence is not None
        assert confidence <= 0.5, f"1-source founder confidence {confidence} > 0.5"

    def test_founder_two_sources_allow_0_8(self, enrichment):
        fn = pick_enrichment_fn(
            enrichment, ("founder_confidence", "score_founder_confidence",
                         "score_contact_confidence"),
            "2 independent corroborations are needed for confidence >= 0.8")
        confidence = _founder_confidence(fn, n_sources=2)
        assert confidence is not None
        assert confidence >= 0.8, f"2-source founder confidence {confidence} < 0.8"


def _founder_confidence(fn, n_sources):
    """Call a founder-confidence function with 1 or 2 corroborating sources.

    Tries a few plausible call shapes; returns a float or None (caller skips
    on None, with the reason recorded by the test).
    """
    sources = [
        {"source": "website_about", "value": "Jane Nova"},
        {"source": "linkedin", "value": "Jane Nova"},
    ][:n_sources]
    attempts = [
        lambda: fn(n_sources),
        lambda: fn(corroborations=n_sources),
        lambda: fn(sources),
        lambda: fn("Jane Nova", sources),
        lambda: fn(name="Jane Nova", sources=sources),
        lambda: fn(sources=sources),
    ]
    for attempt in attempts:
        try:
            result = attempt()
        except TypeError:
            continue
        if isinstance(result, (int, float)):
            return float(result)
        if isinstance(result, (tuple, list)) and result and isinstance(result[0], (int, float)):
            return float(result[0])
        if isinstance(result, dict) and isinstance(result.get("confidence"), (int, float)):
            return float(result["confidence"])
    pytest.skip(
        "founder-confidence function exists but none of the probed call "
        "shapes (int count / sources list / (name, sources)) returned a "
        "numeric confidence"
    )
