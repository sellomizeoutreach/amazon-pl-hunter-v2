"""Mailer — Brevo transactional API (transport only).

The only email v2 sends is the lead batch (see backend/batcher.py): one
email with all unsent live leads in a single attached CSV, once the batch
threshold is reached. Run summaries are ALWAYS written as local HTML to
data/run_summaries/<run_id>.html and are never emailed per-run.

Transport: Brevo REST via requests first (verified end-to-end 2026-11-12),
brevo-python SDK second (documented in the returned status string).
Never Gmail SMTP as primary. The API key is never logged.
"""
from __future__ import annotations

import html
import os
from pathlib import Path

from backend import get_logger

log = get_logger(__name__)


def render_summary_html(summary: dict) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in summary.items())
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>PL Hunter run {summary.get('run_id')}</title></head><body>"
            f"<h1>Amazon PL Hunter — run {summary.get('run_id')} ({summary.get('mode')})</h1>"
            f"<p>Status: {html.escape(str(summary.get('status')))}</p>"
            f"<table border='1'>{rows}</table></body></html>")


def _send_via_sdk(subject: str, html_content: str,
                  attachments: list[dict] | None = None) -> str:
    # brevo-python 5.x (pinned in requirements.txt): package is `brevo`,
    # not the old `brevo_python` (sib_api_v3_sdk-style) API. The old import
    # always failed here, so every send silently fell back to requests.
    # Note: the SDK builds its own httpx client, which chokes on some proxy
    # env vars, so pass a clean client; requests (primary) is unaffected.
    import httpx
    from brevo import Brevo
    client = Brevo(
        api_key=os.environ["BREVO_API_KEY"],
        httpx_client=httpx.Client(trust_env=False, timeout=15),
    )
    sender = os.environ.get("BREVO_SENDER", "")
    receiver = os.environ.get("BREVO_RECEIVER", "")
    kwargs: dict = {
        "sender": {"email": sender},
        "to": [{"email": receiver}],
        "subject": subject,
        "html_content": html_content,
    }
    if attachments:
        kwargs["attachment"] = attachments
    client.transactional_emails.send_transac_email(**kwargs)
    return "sent:sdk"


def _send_via_requests(subject: str, html_content: str,
                       attachments: list[dict] | None = None) -> str:
    import requests
    payload: dict = {
        "sender": {"email": os.environ.get("BREVO_SENDER", "")},
        "to": [{"email": os.environ.get("BREVO_RECEIVER", "")}],
        "subject": subject,
        "htmlContent": html_content,
    }
    if attachments:
        payload["attachment"] = attachments
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": os.environ["BREVO_API_KEY"], "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return "sent:requests"


def brevo_configured() -> bool:
    """True when a batch email could be attempted (key + sender + receiver)."""
    return bool(os.environ.get("BREVO_API_KEY", "").strip()
                and os.environ.get("BREVO_SENDER", "").strip()
                and os.environ.get("BREVO_RECEIVER", "").strip())


def send_brevo_email(*, subject: str, html_content: str,
                     attachments: list[dict] | None = None) -> str:
    """Send one Brevo transactional email.

    attachments: [{"content": <base64 str>, "name": <filename>}] (Brevo
    API shape). Returns "sent:requests" / "sent:sdk". Raises on failure —
    including when Brevo is not configured — so callers (the batcher) can
    leave everything unmarked and retry later. This function marks nothing.
    """
    if not brevo_configured():
        raise RuntimeError(
            "Brevo not configured (BREVO_API_KEY / BREVO_SENDER / BREVO_RECEIVER)")
    # Requests REST first: simple, dependency-light, verified end-to-end
    # against the live Brevo API (2026-11-12). SDK second as fallback.
    try:
        return _send_via_requests(subject, html_content, attachments)
    except Exception as exc:
        log.warning("brevo requests send failed; trying SDK",
                    extra={"extra_fields": {"error": str(exc)}})
    return _send_via_sdk(subject, html_content, attachments)


def write_summary_locally(summary: dict) -> Path:
    """Always write the run summary HTML to data/run_summaries/<run_id>.html.

    This is how runs are monitored (with the dashboard /runs page) — run
    summaries are never emailed per-run any more (see backend/batcher.py).
    """
    run_id = summary.get("run_id", "unknown")
    out_dir = Path(__file__).resolve().parent.parent / "data" / "run_summaries"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.html"
    path.write_text(render_summary_html(summary), encoding="utf-8")
    log.info("run summary written locally",
             extra={"extra_fields": {"run_id": run_id, "path": str(path)}})
    return path
