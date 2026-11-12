"""FastAPI server — ALL endpoints in SPEC, prefix /api.

Auth: X-API-Token on ALL except /api/health. CORS only origins in
PLH_ALLOWED_ORIGINS (default http://localhost:5173,http://localhost:8000).
Rate limit: POST /api/runs and POST /api/audit max 10/min per token
(in-memory limiter — documented here; not a distributed limiter).
Max 1 live hunt at a time (in-process lock). Serves frontend/dist as
static files when it exists. Run with: uvicorn backend.server:app
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_

from backend import get_logger, setup_logging
from backend.db import (
    Asin, Brand, Job, Keyword, Lead, OutreachStatus, Run,
    get_session, init_db,
)
from backend.pipeline import audit_brand, create_job
from backend.runner import MAX_KEYWORDS_PER_RUN, run_hunt
from backend.scraper import extract_asin

setup_logging(os.environ.get("PLH_LOG_LEVEL", "INFO"))
log = get_logger(__name__)
VERSION = "2.0.0"

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "PLH_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8000").split(",") if o.strip()]
API_TOKEN = os.environ.get("PLH_API_TOKEN", "")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("server started", extra={"extra_fields": {"version": VERSION}})
    yield


app = FastAPI(title="Amazon PL Hunter v2", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["X-API-Token", "Content-Type"],
)

# --- rate limiting (in-memory, per token, 10/min on POST /api/runs, /api/audit) ---
_RATE: dict[str, deque] = defaultdict(deque)
_RATE_LIMIT = 10
_RATE_WINDOW = 60.0
_live_lock = threading.Lock()
_live_running = False


def _check_rate_limit(token: str, endpoint: str) -> None:
    now = time.monotonic()
    key = f"{token}:{endpoint}"
    q = _RATE[key]
    while q and now - q[0] > _RATE_WINDOW:
        q.popleft()
    if len(q) >= _RATE_LIMIT:
        raise HTTPException(429, "rate limit exceeded: max 10/min for this endpoint")
    q.append(now)


def require_token(request: Request) -> str:
    token = request.headers.get("X-API-Token", "")
    if not API_TOKEN:
        log.warning("PLH_API_TOKEN not set; auth disabled (dev only)")
        return token or "dev"
    if token != API_TOKEN:
        raise HTTPException(401, "missing or invalid X-API-Token")
    return token


# --- schemas ---------------------------------------------------------------
class RunCreate(BaseModel):
    mode: str = Field("dry", pattern="^(dry|live)$")
    max_keywords: int | None = Field(None, ge=1, le=MAX_KEYWORDS_PER_RUN)


class AuditCreate(BaseModel):
    brand: str | None = None
    asin: str | None = None


class CaptureCreate(BaseModel):
    asin: str | None = None
    url: str | None = None


class LeadPatch(BaseModel):
    status: str | None = Field(None, pattern="^(NEW|SHORTLISTED|CONTACTED|SKIP)$")
    notes: str | None = None


# --- helpers ---------------------------------------------------------------
def _lead_out(lead: Lead) -> dict:
    brand = lead.brand
    asin = lead.asin
    status_row = lead.outreach
    return {
        "id": lead.id, "brand": brand.name if brand else None,
        "seller_id": brand.seller_id if brand else None,
        "seller_country": brand.seller_country if brand else None,
        "asin": asin.asin if asin else None,
        "title": asin.title if asin else None,
        "reviews": asin.reviews if asin else 0,
        "rating": asin.rating if asin else None,
        "persona": lead.persona, "contact_quality": lead.contact_quality,
        "founder_name": lead.founder_name, "founder_confidence": lead.founder_confidence,
        "linkedin_url": lead.linkedin_url, "website": lead.website,
        "email": lead.email, "email_confidence": lead.email_confidence,
        "email_is_personal": lead.email_is_personal, "phone": lead.phone,
        "pitch_hook": lead.pitch_hook, "needs_review": lead.needs_review,
        "status": status_row.status if status_row else "NEW",
        "verified_at": lead.verified_at.isoformat() if lead.verified_at else None,
    }


def _apply_lead_filters(query, *, persona=None, contact_quality=None, country=None,
                        max_reviews=None, q=None, status=None, needs_review=None):
    if persona:
        query = query.filter(Lead.persona == persona)
    if needs_review is True:
        query = query.filter(Lead.needs_review.is_(True))
    if contact_quality:
        query = query.filter(Lead.contact_quality == contact_quality)
    if country:
        query = query.join(Brand).filter(Brand.seller_country == country)
    if max_reviews is not None:
        query = query.join(Asin, Lead.asin_id == Asin.id, isouter=True).filter(
            or_(Asin.reviews <= max_reviews, Asin.reviews.is_(None)))
    if q:
        like = f"%{q.lower()}%"
        query = query.join(Brand).filter(or_(
            func.lower(Brand.name).like(like),
            func.lower(Lead.founder_name).like(like)))
    if status:
        query = query.join(OutreachStatus).filter(OutreachStatus.status == status)
    return query


# --- endpoints -------------------------------------------------------------
@app.get("/api/health")
def health():
    try:
        session = get_session()
        session.execute(__import__("sqlalchemy").text("SELECT 1"))
        session.close()
        db_ok = True
    except Exception as exc:
        log.warning("health db check failed", extra={"extra_fields": {"error": str(exc)}})
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error", "version": VERSION}


@app.get("/api/stats", dependencies=[Depends(require_token)])
def stats():
    session = get_session()
    try:
        today = str(date.today())
        leads_total = session.query(Lead).count()
        leads_new_today = session.query(Lead).filter(
            func.date(Lead.created_at) == today).count()
        runs_7d = session.query(Run).filter(
            Run.started_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)).count()
        runs = session.query(Run).order_by(Run.id.desc()).limit(7).all()
        blocks = sum(r.captcha_blocks or 0 for r in runs)
        seen = sum(r.brands_seen or 0 for r in runs)
        return {
            "leads_total": leads_total, "leads_new_today": leads_new_today,
            "runs_last_7d": len(runs),
            "captcha_rate": (blocks / seen) if seen else 0.0,
        }
    finally:
        session.close()


@app.get("/api/leads", dependencies=[Depends(require_token)])
def list_leads(persona: str | None = None, contact_quality: str | None = None,
               country: str | None = None, max_reviews: int | None = None,
               q: str | None = None, status: str | None = None,
               needs_review: bool | None = None,
               sort: str = "newest", limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0)):
    session = get_session()
    try:
        query = session.query(Lead)
        query = _apply_lead_filters(query, persona=persona, contact_quality=contact_quality,
                                    country=country, max_reviews=max_reviews, q=q, status=status,
                                    needs_review=needs_review)
        total = query.count()
        if sort == "reviews_asc":
            query = query.join(Asin, Lead.asin_id == Asin.id, isouter=True).order_by(Asin.reviews.asc())
        elif sort == "reviews_desc":
            query = query.join(Asin, Lead.asin_id == Asin.id, isouter=True).order_by(Asin.reviews.desc())
        else:
            query = query.order_by(Lead.id.desc())
        items = [_lead_out(l) for l in query.offset(offset).limit(limit).all()]
        return {"total": total, "items": items}
    finally:
        session.close()


@app.patch("/api/leads/{lead_id}", dependencies=[Depends(require_token)])
def patch_lead(lead_id: int, body: LeadPatch):
    session = get_session()
    try:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(404, "lead not found")
        row = session.query(OutreachStatus).filter(OutreachStatus.lead_id == lead_id).first()
        if row is None:
            row = OutreachStatus(lead_id=lead_id, status="NEW")
            session.add(row)
        if body.status:
            row.status = body.status
        if body.notes is not None:
            row.notes = body.notes
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        return _lead_out(lead)
    finally:
        session.close()


@app.get("/api/leads/export", dependencies=[Depends(require_token)])
def export_leads(format: str = Query("csv", pattern="^(csv|xlsx)$"),
                 persona: str | None = None, contact_quality: str | None = None,
                 country: str | None = None, max_reviews: int | None = None,
                 q: str | None = None, status: str | None = None,
                 needs_review: bool | None = None):
    """EXACTLY the filtered rows (never the whole job)."""
    session = get_session()
    try:
        query = session.query(Lead)
        query = _apply_lead_filters(query, persona=persona, contact_quality=contact_quality,
                                    country=country, max_reviews=max_reviews, q=q, status=status,
                                    needs_review=needs_review)
        rows = [_lead_out(l) for l in query.order_by(Lead.id.desc()).all()]
    finally:
        session.close()
    fields = ["id", "brand", "seller_id", "seller_country", "asin", "title", "reviews",
              "rating", "persona", "contact_quality", "founder_name", "linkedin_url",
              "website", "email", "email_confidence", "email_is_personal", "phone",
              "pitch_hook", "needs_review", "status"]
    if format == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({f: r.get(f) for f in fields})
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=leads.csv"})
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(fields)
    for r in rows:
        ws.append([r.get(f) for f in fields])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(content=buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=leads.xlsx"})


@app.get("/api/runs", dependencies=[Depends(require_token)])
def list_runs(limit: int = Query(20, ge=1, le=100)):
    session = get_session()
    try:
        runs = session.query(Run).order_by(Run.id.desc()).limit(limit).all()
        return [{"id": r.id, "started_at": r.started_at.isoformat() if r.started_at else None,
                 "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                 "status": r.status, "keywords_used": json.loads(r.keywords_used or "[]"),
                 "brands_seen": r.brands_seen, "leads_new": r.leads_new,
                 "leads_total": r.leads_total, "captcha_blocks": r.captcha_blocks,
                 "errors": r.errors, "email_status": r.email_status,
                 "summary": json.loads(r.summary_json or "{}")} for r in runs]
    finally:
        session.close()


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_token)])
def get_run(run_id: int):
    session = get_session()
    try:
        r = session.get(Run, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return {"id": r.id, "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "status": r.status, "keywords_used": json.loads(r.keywords_used or "[]"),
                "brands_seen": r.brands_seen, "leads_new": r.leads_new,
                "leads_total": r.leads_total, "captcha_blocks": r.captcha_blocks,
                "errors": r.errors, "email_status": r.email_status,
                "summary": json.loads(r.summary_json or "{}")}
    finally:
        session.close()


@app.post("/api/runs", dependencies=[Depends(require_token)])
def create_run(body: RunCreate, request: Request):
    token = request.headers.get("X-API-Token", "")
    _check_rate_limit(token, "runs")
    if body.mode == "live" and os.environ.get("PLH_LIVE") != "1":
        raise HTTPException(409, "live mode requires PLH_LIVE=1 (set the env var / secret to enable)")
    global _live_running
    if body.mode == "live":
        if not _live_lock.acquire(blocking=False):
            raise HTTPException(409, "a live hunt is already running")
        _live_running = True
    try:
        # Run synchronously here (single-process deployment); the run row +
        # summary are written by run_hunt. For a production deployment this
        # should move to a background task — the DB-job design supports it.
        summary = run_hunt(mode=body.mode, max_keywords=body.max_keywords)
    finally:
        if body.mode == "live":
            _live_running = False
            _live_lock.release()
    return JSONResponse({"run_id": summary["run_id"]}, status_code=202)


@app.get("/api/keywords", dependencies=[Depends(require_token)])
def todays_keywords():
    session = get_session()
    try:
        today = str(date.today())
        rows = session.query(Keyword).filter(Keyword.run_date == today).order_by(Keyword.id).all()
        return {"run_date": today,
                "keywords": [{"keyword": r.keyword, "source": r.source, "used": r.used} for r in rows]}
    finally:
        session.close()


@app.post("/api/audit", dependencies=[Depends(require_token)])
def audit(body: AuditCreate, request: Request):
    token = request.headers.get("X-API-Token", "")
    _check_rate_limit(token, "audit")
    if not body.brand and not body.asin:
        raise HTTPException(400, "brand or asin required")
    session = get_session()
    try:
        return audit_brand(session, brand=body.brand, asin=body.asin)
    finally:
        session.close()


@app.post("/api/capture", dependencies=[Depends(require_token)])
def capture(body: CaptureCreate):
    asin = extract_asin(body.asin or "") or extract_asin(body.url or "")
    if not asin and not body.url:
        raise HTTPException(400, "asin or url required")
    session = get_session()
    try:
        job = create_job(session, run_id=None, kind="audit",
                         subject=asin or body.url or "unknown",
                         payload={"asin": asin, "url": body.url})
        return {"job_id": job.id, "deep_link": f"/audit?job={job.id}&asin={asin or ''}"}
    finally:
        session.close()


# Static frontend (only if the frontend agent has built it).
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
