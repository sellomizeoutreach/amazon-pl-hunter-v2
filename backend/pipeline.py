"""Pipeline — DB jobs, persisted stages, fail-closed audit/enrichment.

One brand/ASIN = one job. Stages persisted after each stage
(search -> pdp -> seller -> catalogue -> enrich -> score -> save).
A killed run resumes pending/running jobs on the next run (stale running
jobs are re-queued by runner.py). No bare `except Exception: pass` —
every failure is logged with context and recorded on the job/run.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from backend import get_logger
from backend.db import Asin, Brand, Contact, Job, Lead, OutreachStatus
from backend.scoring import (
    audit_catalogue, classify_persona, email_is_personal, founder_confidence,
    is_non_cn, listing_problems, pitch_hook, pl_score, verify_email_mx,
    website_confidence,
)
from backend.scraper import BlockedError, Scraper, extract_asin

log = get_logger(__name__)

STAGES = ["search", "pdp", "seller", "catalogue", "enrich", "score", "save"]
MAX_CATALOGUE_PAGES = 5


def _now():
    return datetime.now(timezone.utc)


def create_job(session, *, run_id: int | None, kind: str, subject: str,
               payload: dict | None = None) -> Job:
    job = Job(run_id=run_id, kind=kind, subject=subject,
              payload_json=json.dumps(payload or {}), status="pending", stage="search")
    session.add(job)
    session.commit()
    return job


def set_stage(session, job: Job, stage: str, status: str | None = None,
              result: dict | None = None, error: str = "") -> None:
    job.stage = stage
    if status:
        job.status = status
    if result is not None:
        job.result_json = json.dumps(result)
    if error:
        job.error = error
    job.updated_at = _now()
    session.commit()


def requeue_stale_jobs(session, run_id: int | None = None) -> int:
    """Stale `running` jobs (previous run killed) -> pending, attempts+1."""
    q = session.query(Job).filter(Job.status == "running")
    if run_id is not None:
        q = q.filter(Job.run_id == run_id)
    count = 0
    for job in q.all():
        job.status = "pending"
        job.attempts += 1
        job.updated_at = _now()
        count += 1
    session.commit()
    if count:
        log.info("re-queued stale running jobs", extra={"extra_fields": {"count": count}})
    return count


def normalise_brand(name: str) -> str:
    return " ".join(name.strip().lower().split())


def save_lead(session, *, brand_name: str, seller_id: str | None, seller_name: str,
              seller_country: str, country_confidence: float, score: float,
              reason: str, asin_data: dict, persona: str, contacts: dict,
              pitch: str, needs_review: bool, source: str = "auto_hunt",
              run_id: int | None = None) -> Lead | None:
    """Persist brand/asin/lead/contacts; dedup on seller_id (SPEC dedup key).
    Returns None if the brand was already saved (dedup hit)."""
    norm = normalise_brand(brand_name)
    existing = None
    if seller_id:
        existing = session.query(Brand).filter(Brand.seller_id == seller_id).first()
    if existing is None:
        existing = session.query(Brand).filter(Brand.name == norm).first()
    if existing is not None:
        log.info("dedup: brand already saved", extra={"extra_fields": {"brand": norm, "seller_id": seller_id}})
        return None
    brand = Brand(name=norm, seller_id=seller_id, seller_name=seller_name,
                  seller_country=seller_country, country_confidence=country_confidence,
                  pl_score=score, pl_reason=reason)
    session.add(brand)
    session.flush()
    asin_row = None
    asin = asin_data.get("asin")
    if asin:
        asin_row = session.query(Asin).filter(Asin.asin == asin).first()
        if asin_row is None:
            asin_row = Asin(asin=asin, brand_id=brand.id, title=asin_data.get("title"),
                            reviews=asin_data.get("reviews", 0), rating=asin_data.get("rating"),
                            price=asin_data.get("price"), first_available=asin_data.get("first_available"),
                            bsr=asin_data.get("bsr"), url=asin_data.get("url"))
            session.add(asin_row)
            session.flush()
    lead = Lead(
        brand_id=brand.id, asin_id=asin_row.id if asin_row else None,
        persona=persona, contact_quality=contacts.get("contact_quality", "UNCONTACTABLE"),
        founder_name=contacts.get("founder_name"), founder_confidence=contacts.get("founder_confidence", 0.0),
        linkedin_url=contacts.get("linkedin_url"), linkedin_confidence=contacts.get("linkedin_confidence", 0.0),
        website=contacts.get("website"), email=contacts.get("email"),
        email_confidence=contacts.get("email_confidence", 0.0),
        email_is_personal=contacts.get("email_is_personal", False),
        phone=contacts.get("phone"), pitch_hook=pitch, needs_review=needs_review,
        source=source, run_id=run_id, verified_at=_now(),
    )
    session.add(lead)
    session.flush()
    for kind, value, conf in contacts.get("contact_rows", []):
        if value:
            session.add(Contact(lead_id=lead.id, kind=kind, value=value,
                                confidence=conf, verified=conf >= 0.8, source=source))
    session.add(OutreachStatus(lead_id=lead.id, status="NEW"))
    session.commit()
    return lead


def enrich_brand(brand_name: str, *, website: str | None = None,
                 email: str | None = None, founder_name: str | None = None,
                 founder_sources: int = 0, linkedin_url: str | None = None,
                 phone: str | None = None) -> dict:
    """Verified enrichment only. Website must contain a brand token;
    founder needs 2 corroborations for >=0.8; NEVER derive a name from a
    URL slug (callers must not pass slug-derived names). Generic inboxes
    are never personal. Email MX checked; DNS down -> conf <=0.4 + review."""
    site, site_conf = website_confidence(brand_name, website)
    f_conf = founder_confidence(founder_sources)
    founder = founder_name if founder_sources >= 1 else None
    email_conf, dns_ok, mx_reason = (0.0, True, "no_email")
    if email:
        email_conf, dns_ok, mx_reason = verify_email_mx(email)
    needs_review = False
    if email and not dns_ok:
        needs_review = True
    contact_quality = "UNCONTACTABLE"
    if founder and f_conf >= 0.5:
        contact_quality = "DIRECT"
    elif site or email:
        contact_quality = "WEBSITE_ONLY"
    rows = []
    if site:
        rows.append(("website", site, site_conf))
    if email:
        rows.append(("email", email, email_conf))
    if linkedin_url:
        rows.append(("linkedin", linkedin_url, 0.5 if founder else 0.3))
    if phone:
        rows.append(("phone", phone, 0.5))
    return {
        "website": site, "website_confidence": site_conf,
        "founder_name": founder, "founder_confidence": f_conf,
        "linkedin_url": linkedin_url if founder else None,
        "linkedin_confidence": 0.5 if (linkedin_url and founder) else 0.0,
        "email": email, "email_confidence": email_conf,
        "email_is_personal": email_is_personal(email),
        "phone": phone, "contact_quality": contact_quality,
        "needs_review": needs_review, "mx_reason": mx_reason,
        "contact_rows": rows,
    }


def audit_brand(session, *, brand: str | None = None, asin: str | None = None,
                scraper: Scraper | None = None, run_id: int | None = None) -> dict:
    """On-demand brand audit (SPEC audit result shape). Fail-closed: any
    catalogue fetch block/CAPTCHA/error -> incomplete, persona UNKNOWN,
    needs_review=True. Never pass on failure."""
    scraper = scraper or Scraper()
    asin_val = extract_asin(asin) if asin else None
    job = create_job(session, run_id=run_id, kind="audit",
                     subject=brand or asin_val or "unknown",
                     payload={"brand": brand, "asin": asin_val})
    set_stage(session, job, "pdp", "running")
    result: dict = {"brand": brand, "asin": asin_val, "seller_id": None,
                    "seller_country": "UNKNOWN", "country_confidence": 0.0,
                    "pl_score": 0.0, "pl_reason": "audit_failed",
                    "catalog": audit_catalogue(None, blocked=True, error="audit_not_run"),
                    "persona": "UNKNOWN", "listing_problems": [],
                    "pitch_hook": "", "contacts": {}, "needs_review": True}
    try:
        if not asin_val and not brand:
            raise ValueError("brand or asin required")
        # PDP stage
        pdp = None
        if asin_val:
            html = scraper.fetch(scraper.pdp_url(asin_val))
            pdp = scraper.parse_pdp(html, asin_val)
            brand = brand or pdp.get("brand") or None
            result["asin"] = asin_val
        if not brand:
            raise ValueError("brand could not be determined from ASIN")
        result["brand"] = brand
        set_stage(session, job, "seller", "running", {"brand": brand})
        # Seller stage
        seller_country, country_conf = "UNKNOWN", 0.0
        seller_id = pdp.get("seller_id") if pdp else None
        if seller_id:
            try:
                html = scraper.fetch(scraper.seller_url(seller_id))
                info = scraper.parse_seller_page(html)
                seller_country, country_conf = info["seller_country"], info["country_confidence"]
            except BlockedError as exc:
                log.warning("seller page blocked", extra={"extra_fields": {"seller_id": seller_id, "error": str(exc)}})
                raise
            except Exception as exc:
                log.warning("seller page failed", extra={"extra_fields": {"seller_id": seller_id, "error": str(exc)}})
        result["seller_id"] = seller_id
        result["seller_country"] = seller_country
        result["country_confidence"] = country_conf
        # Catalogue stage — up to MAX_CATALOGUE_PAGES, fail closed.
        set_stage(session, job, "catalogue", "running")
        catalog_items: list[dict] = []
        pages_checked = 0
        if pdp:
            catalog_items.append({"asin": asin_val, "reviews": pdp.get("reviews", 0)})
        for page in range(1, MAX_CATALOGUE_PAGES + 1):
            html = scraper.fetch(scraper.catalogue_url(brand, page))
            pages_checked = page
            items = scraper.parse_search(html)
            if not items:
                break
            catalog_items.extend({"asin": i["asin"], "reviews": i["reviews"]} for i in items)
        catalog = audit_catalogue(catalog_items, pages_checked=pages_checked)
        result["catalog"] = catalog
        # Score + persona
        set_stage(session, job, "score", "running")
        health = (pdp or {}).get("health", {}) if pdp else {}
        score, reason = pl_score(
            brand, buybox_seller=(pdp or {}).get("buybox_seller", ""),
            has_brand_store=(pdp or {}).get("has_brand_store", False),
            has_aplus=(pdp or {}).get("has_aplus", False),
            seller_count=1)
        result["pl_score"] = score
        result["pl_reason"] = reason
        problems = listing_problems(health)
        result["listing_problems"] = problems
        if not catalog["all_under_100"]:
            problems = [*problems, f"Catalogue max reviews {catalog['max_reviews']} (>= 100)"]
            result["listing_problems"] = problems
        persona = classify_persona((pdp or {}).get("age_months"), (pdp or {}).get("reviews", 0),
                                   problems, (pdp or {}).get("has_aplus", False))
        result["persona"] = persona
        result["pitch_hook"] = pitch_hook(brand, persona, asin_val or "", (pdp or {}).get("reviews", 0), problems)
        contacts = enrich_brand(brand)
        result["contacts"] = {k: v for k, v in contacts.items() if k != "contact_rows"}
        result["needs_review"] = bool(contacts.get("needs_review") or not is_non_cn(seller_country))
        set_stage(session, job, "save", "done", result)
        return result
    except BlockedError as exc:
        result["catalog"] = audit_catalogue(None, blocked=True, error=str(exc))
        result["persona"] = "UNKNOWN"
        result["needs_review"] = True
        result["pl_reason"] = f"blocked: {exc}"
        set_stage(session, job, "catalogue", "failed", result, error=str(exc))
        return result
    except Exception as exc:
        log.warning("audit failed", extra={"extra_fields": {"brand": brand, "asin": asin_val, "error": str(exc)}})
        result["catalog"] = audit_catalogue(None, blocked=True, error=str(exc))
        result["persona"] = "UNKNOWN"
        result["needs_review"] = True
        result["pl_reason"] = f"error: {exc}"
        set_stage(session, job, "save", "failed", result, error=str(exc))
        return result
