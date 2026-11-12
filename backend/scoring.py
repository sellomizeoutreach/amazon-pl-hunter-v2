"""Scoring — shared by backend + tests (SPEC frozen rules).

PL score, seller-country detection (NY / Central Ave traps -> UNKNOWN),
enrichment confidence, persona classification, catalogue fail-closed audit.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from backend import get_logger

log = get_logger(__name__)

# --- Megabrands (from v1 pl_detector.py, trimmed to the negation list) ---
MEGABRANDS = {
    "l'oreal", "loreal", "maybelline", "cerave", "neutrogena", "olay", "dove",
    "nivea", "garnier", "pantene", "colgate", "crest", "gillette", "vaseline",
    "aveeno", "cetaphil", "revlon", "covergirl", "nyx", "clinique",
    "estee lauder", "johnson & johnson", "old spice", "axe", "suave",
    "apple", "samsung", "sony", "bose", "jbl", "logitech", "canon", "nikon",
    "hp", "dell", "lenovo", "asus", "microsoft", "google", "philips",
    "panasonic", "nike", "adidas", "puma", "reebok", "levis", "calvin klein",
    "tommy hilfiger", "ralph lauren", "hanes", "crocs", "skechers",
    "dyson", "shark", "ninja", "keurig", "nespresso", "cuisinart",
    "kitchenaid", "instant pot", "stanley", "yeti",
    "amazon basics", "amazonbasics", "amazon essentials", "solimo",
}

STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "for", "in", "to", "with", "by",
    "on", "at", "from", "llc", "inc", "ltd", "limited", "corp", "co",
    "company", "gmbh", "direct", "official", "store", "shop", "retail",
    "online", "global", "international", "group", "enterprises", "solutions",
    "products", "brand", "brands", "us", "usa", "uk", "de", "ca", "fr",
    "tech", "home", "life", "care", "beauty", "fashion",
}

GENERIC_INBOXES = {
    "info@", "support@", "sales@", "hello@", "contact@", "wholesale@",
    "careers@", "partnerships@", "admin@",
}

# US state codes — a bare 2-letter code that is also a state is NEVER a country.
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

# ISO 3166-1 alpha-2 codes (subset incl. all plausible seller countries).
ISO_CODES = {
    "US", "CN", "HK", "GB", "DE", "CA", "FR", "IT", "ES", "IN", "JP", "AU",
    "MX", "NL", "PL", "SE", "SG", "TR", "AE", "VN", "TW", "KR", "AT", "BE",
    "CH", "DK", "FI", "IE", "NO", "NZ", "PT", "CZ", "HU", "GR", "IL", "TH",
    "MY", "ID", "PH", "BR", "CL", "CO", "ZA", "NG", "KE", "EG", "SA", "QA",
    "KW", "BH", "OM", "JO", "LB", "PK", "BD", "LK", "NP", "MM", "KH", "LA",
    "UA", "RO", "BG", "HR", "RS", "SK", "SI", "LT", "LV", "EE", "IS",
}

COUNTRY_NAMES = {
    "united states": "US", "usa": "US", "u.s.a.": "US", "america": "US",
    "china": "CN", "people's republic of china": "CN", "prc": "CN",
    "hong kong": "HK", "hongkong": "HK",
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "germany": "DE", "deutschland": "DE",
    "canada": "CA", "france": "FR", "italy": "IT", "italia": "IT",
    "spain": "ES", "espana": "ES", "australia": "AU", "india": "IN",
    "japan": "JP", "mexico": "MX", "netherlands": "NL", "holland": "NL",
    "poland": "PL", "sweden": "SE", "singapore": "SG", "turkey": "TR",
    "turkiye": "TR", "united arab emirates": "AE", "uae": "AE",
    "vietnam": "VN", "taiwan": "TW", "south korea": "KR", "korea": "KR",
}

CHINESE_CITIES = {
    "shenzhen", "guangzhou", "dongguan", "foshan", "hangzhou", "yiwu",
    "ningbo", "wenzhou", "xiamen", "fuzhou", "suzhou", "nanjing", "wuxi",
    "qingdao", "jinan", "zhengzhou", "chengdu", "wuhan", "changsha",
    "beijing", "shanghai", "tianjin", "chongqing", "shantou", "huizhou",
    "nanchang", "hefei", "kunming", "xian", "taiyuan", "shijiazhuang",
}
CHINESE_PROVINCES = {
    "guangdong", "zhejiang", "fujian", "jiangsu", "shandong", "henan",
    "sichuan", "hunan", "hubei", "hebei", "anhui", "jiangxi", "shaanxi",
    "shanxi", "liaoning", "jilin", "heilongjiang", "guizhou", "yunnan",
    "guangxi", "hainan", "xinjiang", "qinghai", "gansu",
}


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    norm = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode().lower()
    return [t for t in re.findall(r"[a-z0-9]{3,}", norm) if t not in STOP_WORDS]


# Legal/corporate suffixes carry no identity — excluded from name matching.
LEGAL_SUFFIXES = {
    "llc", "inc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh",
}

# Common descriptive words: sharing ONLY one of these proves nothing
# (e.g. brand "Home Essentials" vs seller "Home Goods" sharing just "home").
# They still count as part of a multi-token overlap that also has a
# distinctive token (e.g. "Nova Home" vs "Nova Home LLC" — nova + home).
GENERIC_WORDS = {
    "home", "life", "care", "beauty", "fashion", "store", "shop", "retail",
    "online", "global", "international", "group", "enterprises", "solutions",
    "products", "product", "brand", "brands", "tech", "technology", "goods",
    "direct", "official", "trade", "trading", "the", "and", "for", "with",
}


def _raw_tokens(text: str) -> list[str]:
    """Whole-word tokens for name matching: everything >=3 chars except
    legal suffixes. Generic descriptive words are KEPT here — the caller
    decides how much they count — so a two-token overlap like nova+home
    is not collapsed to a single token by stop-word filtering."""
    if not text:
        return []
    norm = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode().lower()
    return [t for t in re.findall(r"[a-z0-9]{3,}", norm) if t not in LEGAL_SUFFIXES]


def is_megabrand(brand_name: str) -> bool:
    if not brand_name:
        return False
    norm = re.sub(r"[^a-z0-9\s]", "", brand_name.lower().strip())
    for mb in MEGABRANDS:
        clean = re.sub(r"[^a-z0-9\s]", "", mb)
        if norm == clean or norm.startswith(clean + " ") or norm.endswith(" " + clean):
            return True
    return False


def name_match_score(brand_name: str, seller_name: str) -> tuple[float, str]:
    """Token-overlap name match.

    Full-token overlap vs substring trap are different things:
    - 'nova' as its own word in both "Nova Home" and "Nova Home LLC" is a
      real match (0.9; v1 scored a shared word 0.90). A two-token overlap
      with at least one distinctive token (nova + home) is likewise 0.9.
    - 'nova' inside 'Innovatech' is NOT a full-token overlap — it is a
      substring accident and scores <= 0.3.
    - A single overlap that is ONLY a generic word ('home', 'beauty', ...)
      scores 0.3; two purely generic tokens score 0.5 (weak).
    """
    if not brand_name or not seller_name:
        return 0.0, "missing_data"
    brand_words = _raw_tokens(brand_name)
    seller_words = _raw_tokens(seller_name)
    clean_brand = re.sub(r"[^a-z0-9]", "", brand_name.lower())
    clean_seller = re.sub(r"[^a-z0-9]", "", seller_name.lower())
    if clean_brand and clean_brand == clean_seller:
        return 1.0, "exact_match"
    overlap = set(brand_words) & set(seller_words)
    if overlap:
        distinctive = [w for w in overlap if w not in GENERIC_WORDS]
        if distinctive:
            return 0.9, f"shared_word ({sorted(distinctive)[0]})"
        if len(overlap) >= 2:
            return 0.5, f"generic_words_only ({','.join(sorted(overlap))})"
        return 0.3, f"single_generic_token ({sorted(overlap)[0]})"
    # Substring trap: 'nova' in 'innovatech' — brand token is a substring of a
    # longer seller token (or vice versa) with no full-token overlap.
    for bw in brand_words:
        for sw in seller_words:
            if bw != sw and (bw in sw or sw in bw):
                return 0.3, f"substring_only ({bw}~{sw})"
    for bw in brand_words:
        for sw in seller_words:
            if len(bw) >= 5 and len(sw) >= 5:
                sim = SequenceMatcher(None, bw, sw).ratio()
                if sim >= 0.85:
                    return 0.85, f"fuzzy_word ({bw}~{sw})"
    return 0.0, "no_word_resemblance"


def pl_score(brand_name: str, buybox_seller: str = "", all_sellers: list[str] | None = None,
             has_brand_store: bool = False, has_aplus: bool = False,
             seller_count: int = 1) -> tuple[float, str]:
    """PL score 0-1. Megabrand -> 0. Brand Store / A+ adds at most +0.2.
    Seller count > 5 subtracts. Returns (score, reason)."""
    all_sellers = all_sellers or []
    if is_megabrand(brand_name):
        return 0.0, f"negated: megabrand ({brand_name})"
    if (buybox_seller or "").strip().lower() in ("amazon", "amazon.com"):
        return 0.0, "negated: sold by Amazon (1P)"
    best, reason = 0.0, "no_word_resemblance"
    for seller in [buybox_seller, *all_sellers]:
        if not seller:
            continue
        s, r = name_match_score(brand_name, seller)
        if s > best:
            best, reason = s, r
    score = best
    if has_brand_store or has_aplus:
        score = min(1.0, score + 0.2)
        reason += " +brand_store_or_aplus(+0.2)"
    count = max(seller_count, len({s.lower() for s in all_sellers if s}) + (1 if buybox_seller else 0))
    if count > 5:
        score = max(0.0, score - 0.3)
        reason += f" -many_sellers({count}, -0.3)"
    return round(min(1.0, max(0.0, score)), 3), reason


def detect_seller_country(business_name: str = "", address_lines: list[str] | None = None,
                          full_text: str = "") -> tuple[str, float, str]:
    """Returns (country_code_or_UNKNOWN, confidence, reason).

    Rules (SPEC): explicit country token wins; a 2-letter code counts ONLY if
    it is a known ISO code AND not a US state code — so 'NY' and
    '123 Central Ave, New York' -> UNKNOWN. 'Central' is also a Hong Kong
    district name, so it must never imply HK on its own.
    """
    address_lines = address_lines or []
    sample = f"{business_name} {' '.join(address_lines)} {full_text}".lower()

    if re.search(r"[\u4e00-\u9fff]", sample):
        return "CN", 0.95, "chinese_characters"
    # Explicit country names (word-boundary, longest first).
    for name in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", sample):
            return COUNTRY_NAMES[name], 0.95, f"explicit_country:{name}"
    # Chinese city/province tokens.
    for city in CHINESE_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", sample):
            return "CN", 0.9, f"chinese_city:{city}"
    for prov in CHINESE_PROVINCES:
        if re.search(rf"\b{re.escape(prov)}\b", sample):
            return "CN", 0.9, f"chinese_province:{prov}"
    # Hong Kong: only on explicit 'hong kong' / 'kowloon' etc. — 'central'
    # alone is a street/district word in many cities and proves nothing.
    if re.search(r"\b(hong kong|hongkong|kowloon|tsim sha tsui|wan chai)\b", sample):
        return "HK", 0.9, "hong_kong_marker"
    # Last address line as a bare 2-letter code.
    last = (address_lines[-1].strip().upper() if address_lines else "")
    if re.fullmatch(r"[A-Z]{2}", last):
        if last in US_STATE_CODES:
            return "UNKNOWN", 0.0, f"us_state_code:{last}"
        if last in ISO_CODES:
            return last, 0.8, f"iso_code:{last}"
        return "UNKNOWN", 0.0, f"unknown_code:{last}"
    # A bare 2-letter token anywhere is NOT enough (NY trap) — require the
    # explicit-name checks above to have fired first.
    return "UNKNOWN", 0.0, "no_country_evidence"


def is_non_cn(country: str) -> bool:
    """UNKNOWN fails closed for the NON_CN filter: excluded from auto flow."""
    return (country or "UNKNOWN").upper() not in ("CN", "HK", "UNKNOWN", "")


# --- Enrichment confidence -------------------------------------------------

def website_confidence(brand_name: str, website: str | None) -> tuple[str | None, float]:
    """Website domain MUST contain a brand token (>=3 chars) or website=None."""
    if not website:
        return None, 0.0
    domain = re.sub(r"^https?://(www\.)?", "", website.strip().lower()).split("/")[0]
    tokens = [t for t in _tokens(brand_name) if len(t) >= 3]
    if any(t in domain for t in tokens):
        return website, 0.9
    log.warning("website domain does not contain a brand token; discarding", extra={"extra_fields": {"website": website, "brand": brand_name}})
    return None, 0.0


def founder_confidence(corroborations: int) -> float:
    """2 independent corroborations -> >=0.8; 1 source caps at 0.5."""
    if corroborations >= 2:
        return 0.85
    if corroborations == 1:
        return 0.5
    return 0.0


def email_is_personal(email: str | None) -> bool:
    if not email:
        return False
    local = email.split("@")[0].lower()
    if any(email.lower().startswith(p) for p in GENERIC_INBOXES):
        return False
    # firstname.lastname / firstname_lastname / firstname+initial patterns
    return bool(re.match(r"^[a-z]+[._-][a-z]+", local)) or bool(re.match(r"^[a-z]{3,}$", local) and "." not in local and local not in {"info", "support", "sales", "hello", "contact", "admin"})


def verify_email_mx(email: str) -> tuple[float, bool, str]:
    """Syntax + MX check via email-validator + dnspython.

    If DNS is unavailable, confidence <= 0.4 and needs_review=True — never
    claim verified. Returns (confidence, dns_available, reason).
    """
    if not email:
        return 0.0, False, "no_email"
    try:
        from email_validator import EmailNotValidError, validate_email
        try:
            info = validate_email(email, check_deliverability=True)
            return 0.9, True, f"mx_ok:{info.domain}"
        except EmailNotValidError as exc:
            return 0.0, True, f"invalid:{exc}"
    except Exception as exc:  # DNS resolver down / no network — fail closed, logged
        log.warning("email MX check unavailable; capping confidence", extra={"extra_fields": {"email": email, "error": str(exc)}})
        return 0.4, False, f"dns_unavailable:{exc}"


# --- Persona / catalogue ----------------------------------------------------

def classify_persona(age_months: int | None, reviews: int, health_issues: list[str],
                     has_aplus: bool) -> str:
    text = " ".join(health_issues).lower()
    if any(k in text for k in ("frequently returned", "suppressed", "critical low rating")):
        return "DISTRESSED"
    if age_months is not None and age_months <= 12:
        return "NEW_LAUNCH"
    if age_months is not None and age_months <= 18 and reviews < 50:
        return "NEW_LAUNCH"
    if age_months is not None and age_months >= 24 and reviews < 200:
        return "STAGNANT_VETERAN"
    if age_months is not None and age_months >= 36 and reviews < 350:
        return "STAGNANT_VETERAN"
    if age_months is None and reviews < 40 and not has_aplus:
        return "NEW_LAUNCH"
    return "UNKNOWN"


def audit_catalogue(reviews_by_asin: list[dict] | None, *, blocked: bool = False,
                    error: str | None = None, pages_checked: int = 0) -> dict:
    """Fail-closed catalogue audit.

    reviews_by_asin: [{asin, reviews}]. If blocked/error -> all_under_100=False,
    incomplete=True, persona-relevant failure. Never pass on failure.
    """
    if blocked or error:
        return {
            "total_products": 0, "max_reviews": 0, "all_under_100": False,
            "pages_checked": pages_checked, "incomplete": True,
            "fail_reason": error or "catalogue_fetch_blocked",
        }
    if not reviews_by_asin:
        return {
            "total_products": 0, "max_reviews": 0, "all_under_100": False,
            "pages_checked": pages_checked, "incomplete": True,
            "fail_reason": "no_catalogue_products_found",
        }
    max_rev = max(r.get("reviews", 0) for r in reviews_by_asin)
    return {
        "total_products": len(reviews_by_asin), "max_reviews": max_rev,
        "all_under_100": max_rev < 100, "pages_checked": pages_checked,
        "incomplete": False, "fail_reason": None,
    }


def listing_problems(health: dict) -> list[str]:
    problems = []
    if health.get("is_suppressed"):
        problems.append("Listing suppressed / currently unavailable")
    if health.get("is_buybox_suppressed"):
        problems.append("Buy Box suppressed (no Add to Cart)")
    if health.get("has_frequently_returned_badge"):
        problems.append("Frequently Returned Item badge active")
    rating = health.get("rating") or 0
    if rating and rating < 3.8:
        problems.append(f"Low rating ({rating} stars, below 3.8)")
    if not health.get("has_aplus"):
        problems.append("Missing A+ content")
    if not health.get("has_video"):
        problems.append("Missing product video")
    if (health.get("bullet_count") or 0) < 4:
        problems.append(f"Only {health.get('bullet_count') or 0} bullet points (recommended 5)")
    return problems


def pitch_hook(brand_name: str, persona: str, asin: str, reviews: int,
               problems: list[str], founder_first: str = "there") -> str:
    issue = "; ".join(problems) if problems else "listing gaps we spotted"
    if persona == "DISTRESSED":
        return (f"Hi {founder_first}, Amazon flagged {brand_name} (ASIN {asin}) — {issue}. "
                "We fix suppressed listings and return-rate problems for private-label brands.")
    if persona == "NEW_LAUNCH":
        return (f"Hi {founder_first}, congrats on launching {brand_name} on Amazon — "
                f"{reviews} reviews on ASIN {asin} so far. We run 90-day launch sprints for new PL brands.")
    if persona == "STAGNANT_VETERAN":
        return (f"Hi {founder_first}, {brand_name} has been on Amazon a while but is sitting at "
                f"{reviews} reviews on ASIN {asin} with {issue}. We turn stalled catalogues around.")
    return (f"Hi {founder_first}, we came across {brand_name} (ASIN {asin}) — {issue}. "
            "We help private-label brands grow on Amazon.")
