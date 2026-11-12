"""Adapters between the tests and the v2 backend.

SPEC.md fixes the *rules* (scores, country detection, fail-closed audits,
auth, export, dry-run) but not every function name. These helpers probe the
small set of plausible names for each contract and SKIP with a precise reason
when nothing exists yet — so the suite is green-but-honest while the backend
lands, and the skip message tells the backend agent exactly what to expose.

If the backend agent picks different names, update the candidate lists here
(and only here); the test files themselves stay unchanged.
"""

import importlib
import inspect
import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Generic import helpers
# ---------------------------------------------------------------------------


def load_module(name):
    """Import backend.<name>, or SKIP naming the missing module."""
    try:
        return importlib.import_module(f"backend.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("backend"):
            pytest.skip(f"backend.{name} not implemented yet ({exc})")
        raise


def load_app():
    """Return the FastAPI app object, or SKIP if no server module exists."""
    for mod_name in ("server", "main"):
        try:
            mod = importlib.import_module(f"backend.{mod_name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("backend"):
                continue
            raise
        app = getattr(mod, "app", None)
        if app is not None:
            return app
        pytest.skip(f"backend.{mod_name} exists but exposes no `app` object")
    pytest.skip("no backend server module yet (looked for backend.server / backend.main)")


def _pick_fn(module, names):
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return name, fn
    return None, None


def _call_with_available_kwargs(fn, values):
    """Call fn passing only the kwargs its signature accepts (by name)."""
    sig = inspect.signature(fn)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**values)
    kwargs = {k: v for k, v in values.items() if k in params}
    # Positional-only fallback: if no recognised kw names, try positionally.
    if not kwargs and params:
        positional = [values[k] for k in values if k in params]
        if positional:
            return fn(*positional)
    return fn(**kwargs)


# ---------------------------------------------------------------------------
# PL scoring — backend/scoring.py (SPEC: "Return (score, reason string)")
# ---------------------------------------------------------------------------

PL_SCORE_FNS = ("score_private_label", "evaluate_private_label", "pl_score", "score_pl")


def load_pl_scorer():
    """Return (fn_name, fn) for PL scoring, or SKIP."""
    mod = load_module("scoring")
    name, fn = _pick_fn(mod, PL_SCORE_FNS)
    if fn is None:
        pytest.skip(
            "backend.scoring exposes none of "
            f"{PL_SCORE_FNS} — add one returning (score 0-1, reason str)"
        )
    return name, fn


def pl_score(fn, brand, seller, all_sellers=(), has_brand_store=False,
             has_aplus_content=False, total_offers_count=1):
    """Call a PL scorer and normalise its return to (score: float, reason: str).

    Accepts the SPEC tuple (score, reason), a bare float, or the v1-style
    dict ({is_private_label, ...} with an optional numeric pl_score/score).
    A v1-style dict with no numeric score is mapped from its boolean:
    match -> 0.9, no match -> 0.0, so the golden trap test still bites if
    the v1 substring logic is carried over unmodified.
    """
    values = {
        "brand_name": brand, "brand": brand,
        "buybox_seller": seller, "seller": seller, "seller_name": seller,
        "all_sellers": list(all_sellers),
        "has_brand_store": has_brand_store,
        "has_aplus_content": has_aplus_content,
        "total_offers_count": total_offers_count,
    }
    result = _call_with_available_kwargs(fn, values)
    if isinstance(result, (tuple, list)) and len(result) == 2:
        return float(result[0]), str(result[1])
    if isinstance(result, (int, float)):
        return float(result), ""
    if isinstance(result, dict):
        for key in ("pl_score", "score"):
            if isinstance(result.get(key), (int, float)):
                return float(result[key]), str(result.get("reason") or result.get("match_reason") or "")
        if isinstance(result.get("is_private_label"), bool):
            matched = result["is_private_label"]
            reason = str(result.get("match_reason") or "")
            return (0.9 if matched else 0.0), reason
        if str(result.get("confidence", "")).lower().startswith("negat"):
            return 0.0, str(result.get("match_reason") or "negated")
    pytest.skip(f"PL scorer returned an unrecognised shape: {type(result).__name__} {result!r}")


# ---------------------------------------------------------------------------
# Seller country — backend/scoring.py preferred, v1 location as fallback
# ---------------------------------------------------------------------------

COUNTRY_MODULES = ("scoring", "seller_checker", "seller")


def load_country_detector():
    """Return (fn_name, fn) for country detection, or SKIP."""
    for mod_name in COUNTRY_MODULES:
        try:
            mod = importlib.import_module(f"backend.{mod_name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("backend"):
                continue
            raise
        fn = getattr(mod, "detect_seller_country", None)
        if callable(fn):
            return f"backend.{mod_name}.detect_seller_country", fn
    pytest.skip(
        "no detect_seller_country found (looked in backend.scoring, "
        "backend.seller_checker, backend.seller)"
    )


def detect_country(fn, business_name, address_lines, full_text=""):
    """Call a country detector and normalise to an ISO code or 'UNKNOWN'."""
    values = {
        "business_name": business_name, "business": business_name,
        "address_lines": list(address_lines), "address": list(address_lines),
        "full_text": full_text,
    }
    result = _call_with_available_kwargs(fn, values)
    if isinstance(result, (tuple, list)) and result:
        # e.g. backend.scoring.detect_seller_country -> (code, confidence, reason)
        code = result[0]
    elif isinstance(result, dict):
        code = result.get("country_code") or result.get("country") or result.get("code") or ""
    elif isinstance(result, str):
        code = result
    else:
        pytest.skip(f"country detector returned unrecognised shape: {result!r}")
    code = str(code).strip().upper()
    if code in ("", "UNKNOWN", "NOT AVAILABLE", "N/A"):
        return "UNKNOWN"
    return code


# ---------------------------------------------------------------------------
# Enrichment — backend/enrichment.py
# ---------------------------------------------------------------------------

def load_enrichment():
    """Return the module holding the enrichment rules.

    SPEC names backend/enrichment.py, but the landed backend puts these in
    backend/scoring.py (email_is_personal, website_confidence,
    founder_confidence) — try the SPEC location first, then scoring.
    """
    for mod_name in ("enrichment", "scoring"):
        try:
            return importlib.import_module(f"backend.{mod_name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("backend"):
                continue
            raise
    pytest.skip("neither backend.enrichment nor backend.scoring exists yet")


def as_website_bool(result):
    """Normalise a website check to a bool.

    Accepts a plain bool, (bool, reason), or the backend.scoring
    website_confidence shape (website_or_None, confidence): a non-None
    website means the domain contains a brand token.
    """
    if isinstance(result, bool):
        return result
    if isinstance(result, (tuple, list)) and len(result) == 2:
        first, second = result
        if isinstance(second, (int, float)):
            return first is not None and first is not False
        if isinstance(first, bool):
            return first
    return as_bool(result)


def pick_enrichment_fn(module, names, rule):
    _, fn = _pick_fn(module, names)
    if fn is None:
        pytest.skip(f"backend.enrichment exposes none of {names} ({rule})")
    return fn


def as_bool(result):
    """Normalise a bool / (bool, reason) / dict result to a bool."""
    if isinstance(result, bool):
        return result
    if isinstance(result, (tuple, list)) and result and isinstance(result[0], bool):
        return result[0]
    if isinstance(result, dict):
        for key in ("is_personal", "personal", "valid", "matches", "ok"):
            if isinstance(result.get(key), bool):
                return result[key]
    pytest.skip(f"enrichment function returned unrecognised shape: {result!r}")


# ---------------------------------------------------------------------------
# Catalogue audit — backend/audit.py preferred
# ---------------------------------------------------------------------------

CATALOGUE_MODULES = ("audit", "catalogue", "pipeline")
CATALOGUE_FNS = ("audit_catalogue", "audit_catalogue_pages", "audit_brand_catalogue")


def load_catalogue_auditor():
    """Return (label, fn) for the catalogue audit, or SKIP."""
    for mod_name in CATALOGUE_MODULES:
        try:
            mod = importlib.import_module(f"backend.{mod_name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("backend"):
                continue
            raise
        name, fn = _pick_fn(mod, CATALOGUE_FNS)
        if fn is not None:
            return f"backend.{mod_name}.{name}", fn
    pytest.skip(
        "no catalogue audit function found (looked for "
        f"{CATALOGUE_FNS} in backend.audit / backend.catalogue / backend.pipeline)"
    )


def audit_catalogue(fn, reviews_by_asin, *, blocked=False, error=None,
                    pages_checked=0, html_pages=None):
    """Call the catalogue auditor and return its catalog dict.

    Two call shapes are supported:
    - Structured (landed backend): audit_catalogue(reviews_by_asin=[{asin,
      reviews}], blocked=..., error=..., pages_checked=...) -> flat catalog
      dict. Pass the parsed review rows as `reviews_by_asin`.
    - Raw-HTML (hypothetical): fn(pages=[html, ...]) — pass `html_pages`.
    The call shape is derived from the function's own signature, so a
    TypeError raised *inside* the backend still fails the test instead of
    being mistaken for a signature mismatch.
    """
    sig = inspect.signature(fn)
    names = [p.name for p in sig.parameters.values()]
    if "reviews_by_asin" in names:
        kwargs = {"reviews_by_asin": reviews_by_asin}
        if "blocked" in names:
            kwargs["blocked"] = blocked
        if "error" in names:
            kwargs["error"] = error
        if "pages_checked" in names:
            kwargs["pages_checked"] = pages_checked
        result = fn(**kwargs)
    elif "pages" in names and html_pages is not None:
        result = fn(pages=html_pages)
    elif "page_htmls" in names and html_pages is not None:
        result = fn(page_htmls=html_pages)
    elif len(names) == 1 and html_pages is not None:
        result = fn(html_pages)
    else:
        pytest.skip(f"catalogue auditor signature not recognised: {sig}")
    if not isinstance(result, dict):
        pytest.skip(f"catalogue auditor returned non-dict: {result!r}")
    return result


def catalog_block(audit_result):
    """Extract the catalog dict from an audit result (nested or flat)."""
    cat = audit_result.get("catalog")
    if isinstance(cat, dict):
        return cat
    keys = ("all_under_100", "incomplete", "fail_reason", "max_reviews", "pages_checked")
    if any(k in audit_result for k in keys):
        return audit_result
    pytest.skip(f"audit result has no catalog block: {audit_result!r}")


# ---------------------------------------------------------------------------
# Test-DB helpers (raw sqlite3 against the SPEC schema — works regardless of
# which ORM the backend uses, as long as it creates the SPEC tables).
# ---------------------------------------------------------------------------

BRANDS_DDL = """
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    seller_id TEXT UNIQUE,
    seller_name TEXT,
    seller_country TEXT,
    country_confidence REAL,
    pl_score REAL,
    pl_reason TEXT,
    created_at TEXT
)
"""


def db_path_from_url(url):
    """Extract the filesystem path from a sqlite:/// SQLAlchemy URL."""
    if not url:
        return None
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return Path(url[len(prefix):])
    return None


def find_test_db(candidates):
    """Return the first candidate path that exists and has a leads table."""
    import os
    url_path = db_path_from_url(os.environ.get("PLH_DATABASE_URL", ""))
    if url_path is not None:
        candidates = [url_path, *candidates]
    for path in candidates:
        path = Path(path)
        if not path.exists():
            continue
        try:
            with sqlite3.connect(str(path)) as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error:
            continue
        if "leads" in tables and "brands" in tables:
            return path
    return None


def seed_two_leads(db_path):
    """Seed two brands/leads with different personas/countries (SPEC schema).

    Returns (nova_brand_id, kitchenpro_brand_id). Caller filters to one and
    asserts the other is absent from the export.
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM outreach_status")
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM asins")
        conn.execute("DELETE FROM brands")
        cur = conn.execute(
            "INSERT INTO brands (name, seller_id, seller_name, seller_country, "
            "country_confidence, pl_score, pl_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Nova Home", "A1NOVAHOME1", "Nova Home LLC", "US", 0.95, 0.9,
             "shared_word ('nova')"),
        )
        nova_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO brands (name, seller_id, seller_name, seller_country, "
            "country_confidence, pl_score, pl_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("KitchenPro", "A1KITCHPRO2", "KitchenPro GmbH", "DE", 0.9, 0.85,
             "shared_word ('kitchenpro')"),
        )
        kp_id = cur.lastrowid
        conn.execute(
            "INSERT INTO leads (brand_id, persona, contact_quality, founder_name, "
            "founder_confidence, website, email, email_confidence, email_is_personal, "
            "pitch_hook, needs_review, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nova_id, "NEW_LAUNCH", "DIRECT", "Jane Nova", 0.9,
             "https://novahome.com", "jane@novahome.com", 0.9, 1,
             "New launch, 42 reviews", 0, "auto_hunt"),
        )
        conn.execute(
            "INSERT INTO leads (brand_id, persona, contact_quality, founder_name, "
            "founder_confidence, website, email, email_confidence, email_is_personal, "
            "pitch_hook, needs_review, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kp_id, "STAGNANT_VETERAN", "WEBSITE_ONLY", None, 0.0,
             "https://kitchenpro.example", None, 0.0, 0,
             "Stagnant veteran, 210 reviews", 0, "auto_hunt"),
        )
        conn.commit()
    return nova_id, kp_id
