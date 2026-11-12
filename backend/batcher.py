"""Lead batch emailing — the ONLY email PL Hunter v2 sends.

Retires the old "always send a run summary" rule (Jack, 2026-11-12):
- A run summary is ALWAYS written as local HTML to
  data/run_summaries/<run_id>.html (that, plus the dashboard /runs page,
  is how runs are monitored). Run summaries are never emailed.
- NO email is sent for a run that found 0 new leads.
- New LIVE leads accumulate unsent; once PLH_BATCH_MIN_LEADS (default
  100) unsent live leads exist, ONE email goes out with all of them in a
  single attached CSV, and exactly those leads are marked sent
  (leads.batch_sent_at). A failed send marks NOTHING; the next live run
  retries the same batch.
- Only real live leads count: source auto_hunt (direct live) and serpapi
  (SerpApi live). Dry-run / fixture leads (source dry_run) are NEVER
  counted or emailed — the dry path saves with source dry_run precisely
  so this filter can never mistake demo data for real leads.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
from datetime import date, datetime, timezone

from backend import get_logger
from backend.db import Lead, Run
from backend.mailer import brevo_configured, send_brevo_email

log = get_logger(__name__)

# Sources that count toward a batch: real live runs only.
LIVE_SOURCES = ("auto_hunt", "serpapi")

DEFAULT_BATCH_MIN_LEADS = 100

BATCH_CSV_COLUMNS = [
    "brand", "asin", "title", "reviews", "rating", "price",
    "seller_name", "seller_country", "persona", "pl_score",
    "email", "pitch_hook", "run_id",
]


def batch_threshold() -> int:
    """PLH_BATCH_MIN_LEADS (default 100); invalid values fall back to 100."""
    try:
        value = int(os.environ.get("PLH_BATCH_MIN_LEADS", "").strip() or DEFAULT_BATCH_MIN_LEADS)
    except ValueError:
        return DEFAULT_BATCH_MIN_LEADS
    return value if value > 0 else DEFAULT_BATCH_MIN_LEADS


def unsent_live_leads(session) -> list[Lead]:
    """Unsent leads from real live runs, oldest first (deterministic
    batch order). Dry-run / fixture leads are excluded by source."""
    return (session.query(Lead)
            .filter(Lead.source.in_(LIVE_SOURCES), Lead.batch_sent_at.is_(None))
            .order_by(Lead.id)
            .all())


def build_batch_csv(leads: list[Lead]) -> str:
    """One CSV row per lead, in BATCH_CSV_COLUMNS order. Values come
    straight from the DB rows (brand name is stored normalised)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(BATCH_CSV_COLUMNS)
    for lead in leads:
        brand = lead.brand
        asin = lead.asin
        writer.writerow([
            brand.name if brand else "",
            asin.asin if asin else "",
            asin.title if asin else "",
            asin.reviews if asin else 0,
            asin.rating if asin else "",
            asin.price if asin else "",
            brand.seller_name if brand else "",
            brand.seller_country if brand else "",
            lead.persona,
            brand.pl_score if brand else "",
            lead.email or "",
            lead.pitch_hook or "",
            lead.run_id if lead.run_id is not None else "",
        ])
    return out.getvalue()


def _batch_body(leads: list[Lead], runs: list[Run]) -> str:
    """Short HTML summary for the batch email: count, date range of the
    runs covered, and SerpApi searches used across those runs if known."""
    started = [r.started_at for r in runs if r.started_at]
    if started:
        date_range = f"{min(started).date().isoformat()} to {max(started).date().isoformat()}"
    else:
        created = [l.created_at for l in leads if l.created_at]
        date_range = (f"{min(created).date().isoformat()} to "
                      f"{max(created).date().isoformat()}") if created else "unknown"
    searches = 0
    serpapi_known = False
    for run in runs:
        try:
            summary = json.loads(run.summary_json or "{}")
        except (ValueError, TypeError):
            continue
        if summary.get("serpapi_mode"):
            serpapi_known = True
            searches += int(summary.get("serpapi_searches_used") or 0)
    lines = [
        f"<p>PL Hunter found <b>{len(leads)} new leads</b> "
        f"(runs {date_range}).</p>",
        "<p>All of them are in the attached CSV — one row per lead, with "
        "brand, ASIN, reviews, seller, persona, PL score, best contact "
        "email and pitch hook. Leads flagged needs_review need a hand-check "
        "before outreach.</p>",
    ]
    if serpapi_known:
        lines.append(f"<p>SerpApi searches used across these runs: {searches}.</p>")
    return ("<!doctype html><html><body><h1>PL Hunter: "
            f"{len(leads)} new leads</h1>" + "".join(lines) + "</body></html>")


def maybe_send_batch(session, *, new_leads_this_run: int = 0,
                     threshold: int | None = None) -> str:
    """Decide + send the lead batch after a live run. Returns the
    email_status for the run row:

    - "skipped_zero_leads": this run found 0 new leads and the unsent
      backlog is below the threshold — no email at all.
    - "batch_pending": leads were added (or are owed) but the unsent count
      is below the threshold, or Brevo is not configured yet — nothing
      sent, retried on a later live run.
    - "batch_sent": the batch email went out; exactly the leads in it are
      now marked batch_sent_at.
    - "batch_failed": the send was attempted and failed; NOTHING was
      marked sent, so the next live run retries the same batch.
    """
    limit = threshold if threshold is not None else batch_threshold()
    leads = unsent_live_leads(session)
    if len(leads) < limit:
        return "skipped_zero_leads" if new_leads_this_run == 0 else "batch_pending"
    if not brevo_configured():
        log.warning("batch ready but Brevo not configured; leaving unsent",
                    extra={"extra_fields": {"unsent": len(leads), "threshold": limit}})
        return "batch_pending"
    run_ids = sorted({l.run_id for l in leads if l.run_id is not None})
    runs = session.query(Run).filter(Run.id.in_(run_ids)).all() if run_ids else []
    filename = f"pl-hunter-leads-{date.today().isoformat()}.csv"
    csv_text = build_batch_csv(leads)
    attachment = {"content": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
                  "name": filename}
    subject = f"PL Hunter: {len(leads)} new leads"
    try:
        status = send_brevo_email(subject=subject,
                                  html_content=_batch_body(leads, runs),
                                  attachments=[attachment])
    except Exception as exc:
        # Failed send marks NOTHING — the next live run retries the batch.
        log.warning("batch send failed; nothing marked sent",
                    extra={"extra_fields": {"unsent": len(leads), "error": str(exc)}})
        return "batch_failed"
    now = datetime.now(timezone.utc)
    for lead in leads:
        lead.batch_sent_at = now
    session.commit()
    log.info("batch sent", extra={"extra_fields": {
        "leads": len(leads), "filename": filename, "transport": status,
        "run_ids": run_ids}})
    return "batch_sent"
