"""Lead batch email tests — the 2026-11-12 rule (supersedes "always send a
run summary"):

- 0 new leads -> no Brevo call, email_status skipped_zero_leads.
- 99 unsent live leads -> no send.
- 100 unsent -> exactly one send whose CSV has all 100 rows, and all 100
  are then marked sent (a second check sends nothing).
- Failed Brevo send -> nothing marked sent; the next attempt retries.
- Dry-run / fixture leads (source dry_run) are NEVER counted toward a
  batch, even in large numbers.
- The v3 migration adds batch_sent_at + run_id to an old-schema leads
  table without losing rows.

Offline throughout: Brevo is a recording fake, SerpApi is a stub client,
and every DB is a throwaway temp file.
"""

import base64
import csv
import io

import pytest

from backend.batcher import (
    BATCH_CSV_COLUMNS,
    build_batch_csv,
    maybe_send_batch,
    unsent_live_leads,
)


def _batch_session(tmp_path, name="batch_test.db"):
    import importlib
    db = importlib.import_module("backend.db")
    db_path = tmp_path / name
    url = f"sqlite:///{db_path}"
    if hasattr(db, "reset_engine_for_tests"):
        db.reset_engine_for_tests()
    db.init_db(url)
    return db.get_session()


def _seed_lead(session, i, source="auto_hunt", run_id=1):
    """One deterministic live (or dry-run) lead via the real save_lead."""
    from backend.pipeline import save_lead
    return save_lead(
        session, brand_name=f"Batch Brand {i}", seller_id=f"A9BATCH{i:04d}",
        seller_name=f"Batch Brand {i}", seller_country="US",
        country_confidence=0.9, score=0.9, reason="test",
        asin_data={"asin": f"B0T{i:07d}", "title": f"Batch Product {i}",
                   "reviews": i % 90, "rating": 4.5, "price": "$19.99",
                   "url": f"https://www.amazon.com/dp/B0T{i:07d}"},
        persona="NEW_LAUNCH", contacts={}, pitch="Test pitch hook",
        needs_review=False, source=source, run_id=run_id)


def _seed_leads(session, n, **kwargs):
    return [_seed_lead(session, i, **kwargs) for i in range(n)]


class _FakeBrevo:
    """Recording stand-in for backend.mailer.send_brevo_email."""

    def __init__(self, fail=False):
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, *, subject, html_content, attachments=None):
        if self.fail:
            raise RuntimeError("brevo down")
        self.calls.append({"subject": subject, "html": html_content,
                           "attachments": attachments or []})
        return "sent:requests"


@pytest.fixture()
def _brevo_env(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("BREVO_SENDER", "sender@example.com")
    monkeypatch.setenv("BREVO_RECEIVER", "receiver@example.com")
    monkeypatch.setenv("PLH_BATCH_MIN_LEADS", "100")


def _patch_brevo(monkeypatch, fake):
    monkeypatch.setattr("backend.batcher.send_brevo_email", fake)


# --- Threshold behaviour ----------------------------------------------------

def test_99_unsent_no_send(tmp_path, monkeypatch, _brevo_env):
    from backend.db import Lead
    session = _batch_session(tmp_path)
    try:
        _seed_leads(session, 99)
        fake = _FakeBrevo()
        _patch_brevo(monkeypatch, fake)
        assert maybe_send_batch(session, new_leads_this_run=99) == "batch_pending"
        assert fake.calls == [], "below 100 unsent leads, no email at all"
        assert session.query(Lead).filter(Lead.batch_sent_at.isnot(None)).count() == 0
    finally:
        session.close()


def test_100_unsent_one_send_all_marked(tmp_path, monkeypatch, _brevo_env):
    from backend.db import Lead
    session = _batch_session(tmp_path)
    try:
        _seed_leads(session, 100)
        fake = _FakeBrevo()
        _patch_brevo(monkeypatch, fake)
        assert maybe_send_batch(session, new_leads_this_run=100) == "batch_sent"
        assert len(fake.calls) == 1, "exactly one batch email for 100 leads"
        call = fake.calls[0]
        assert call["subject"] == "PL Hunter: 100 new leads"
        assert len(call["attachments"]) == 1
        assert call["attachments"][0]["name"].endswith(".csv")
        csv_text = base64.b64decode(call["attachments"][0]["content"]).decode("utf-8")
        rows = list(csv.reader(io.StringIO(csv_text)))
        assert rows[0] == BATCH_CSV_COLUMNS
        assert len(rows) == 101, "header + all 100 leads, none dropped or doubled"
        assert rows[1][0] == "batch brand 0"
        assert rows[1][1] == "B0T0000000"
        assert rows[1][12] == "1", "run_id column carries the run the lead came from"
        assert all(r[10] == "" for r in rows[1:]), "no contact email on these seeds"
        assert session.query(Lead).filter(Lead.batch_sent_at.is_(None)).count() == 0
        assert session.query(Lead).filter(Lead.batch_sent_at.isnot(None)).count() == 100
        # Second check: nothing left unsent, so nothing more is sent.
        assert maybe_send_batch(session, new_leads_this_run=0) == "skipped_zero_leads"
        assert len(fake.calls) == 1
    finally:
        session.close()


def test_failed_send_marks_nothing_then_retries(tmp_path, monkeypatch, _brevo_env):
    from backend.db import Lead
    session = _batch_session(tmp_path)
    try:
        _seed_leads(session, 100)
        failing = _FakeBrevo(fail=True)
        _patch_brevo(monkeypatch, failing)
        assert maybe_send_batch(session, new_leads_this_run=100) == "batch_failed"
        assert failing.calls == []
        assert session.query(Lead).filter(Lead.batch_sent_at.isnot(None)).count() == 0
        working = _FakeBrevo()
        _patch_brevo(monkeypatch, working)
        assert maybe_send_batch(session, new_leads_this_run=0) == "batch_sent"
        assert len(working.calls) == 1, "the next attempt retries the same batch"
        assert session.query(Lead).filter(Lead.batch_sent_at.is_(None)).count() == 0
    finally:
        session.close()


def test_dry_run_leads_never_counted(tmp_path, monkeypatch, _brevo_env):
    session = _batch_session(tmp_path)
    try:
        _seed_leads(session, 150, source="dry_run")
        fake = _FakeBrevo()
        _patch_brevo(monkeypatch, fake)
        assert unsent_live_leads(session) == []
        assert maybe_send_batch(session, new_leads_this_run=150) == "batch_pending"
        assert fake.calls == [], "150 dry-run leads must never trigger a batch email"
    finally:
        session.close()


# --- 0-new-lead runs and dry runs (end to end through run_hunt) -------------

class _EmptyStubClient:
    searches_used = 0
    products_used = 0

    def search_amazon(self, keyword, page=1):
        return []

    def brand_search(self, brand, page=1):
        return []

    def product(self, asin):
        raise AssertionError("product must never be called with no results")


def test_zero_new_leads_no_brevo_call(tmp_path, monkeypatch, _brevo_env):
    import backend.runner as runner_mod
    from backend.db import Lead
    from backend.runner import run_hunt

    monkeypatch.setenv("PLH_LIVE", "1")
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    monkeypatch.setattr(runner_mod, "generate_keywords",
                        lambda *a, **k: [("kw one", "seed")])
    written: list = []
    monkeypatch.setattr(
        runner_mod, "write_summary_locally",
        lambda summary: written.append(summary["run_id"]) or None)
    fake = _FakeBrevo()
    _patch_brevo(monkeypatch, fake)
    session = _batch_session(tmp_path)
    try:
        summary = run_hunt(mode="live", max_keywords=1, session=session,
                           serpapi_client=_EmptyStubClient())
    finally:
        session.close()
    assert summary["leads_new"] == 0
    assert summary["email_status"] == "skipped_zero_leads"
    assert fake.calls == [], "a 0-new-lead run must make no Brevo call at all"
    assert len(written) == 1, "the local HTML summary is still written"
    assert summary["run_id"] in written


def test_dry_run_leads_are_dry_run_source_and_never_batched(
        tmp_path, monkeypatch, _brevo_env):
    import backend.runner as runner_mod
    from backend.db import Lead
    from backend.runner import run_hunt

    fake = _FakeBrevo()
    _patch_brevo(monkeypatch, fake)
    # Keep the local summary out of the real data/ dir (it holds real runs).
    monkeypatch.setattr(runner_mod, "write_summary_locally", lambda summary: None)
    session = _batch_session(tmp_path)
    try:
        summary = run_hunt(mode="dry", session=session)
        assert summary["leads_new"] >= 0
        assert summary["email_status"] in ("skipped_zero_leads", "skipped_dry_run")
        dry_leads = session.query(Lead).filter(Lead.source == "dry_run").all()
        assert len(dry_leads) == summary["leads_new"]
        assert unsent_live_leads(session) == []
        assert maybe_send_batch(session, new_leads_this_run=summary["leads_new"]) in (
            "skipped_zero_leads", "batch_pending")
        assert fake.calls == []
    finally:
        session.close()


# --- Migration ----------------------------------------------------------------

OLD_LEADS_DDL = """
CREATE TABLE leads (
    id INTEGER PRIMARY KEY,
    brand_id INTEGER NOT NULL,
    asin_id INTEGER,
    persona TEXT,
    contact_quality TEXT,
    founder_name TEXT,
    founder_confidence REAL,
    linkedin_url TEXT,
    linkedin_confidence REAL,
    website TEXT,
    email TEXT,
    email_confidence REAL,
    email_is_personal BOOLEAN,
    phone TEXT,
    pitch_hook TEXT,
    needs_review BOOLEAN,
    source TEXT,
    verified_at DATETIME,
    created_at DATETIME
)
"""


def test_migration_adds_columns_and_preserves_rows(tmp_path):
    import sqlite3

    import backend.db as db

    db_path = tmp_path / "old_schema.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(OLD_LEADS_DDL)
        conn.execute(
            "INSERT INTO leads (id, brand_id, persona, source) VALUES (1, 1, 'NEW_LAUNCH', 'auto_hunt')")
        conn.commit()
    if hasattr(db, "reset_engine_for_tests"):
        db.reset_engine_for_tests()
    db.init_db(f"sqlite:///{db_path}")
    with sqlite3.connect(str(db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
        assert "batch_sent_at" in cols and "run_id" in cols
        row = conn.execute(
            "SELECT id, persona, source FROM leads WHERE id = 1").fetchone()
    assert row == (1, "NEW_LAUNCH", "auto_hunt"), "the old row must survive the migration"
    session = db.get_session()
    try:
        lead = session.query(db.Lead).filter(db.Lead.id == 1).one()
        assert lead.batch_sent_at is None
        assert lead.run_id is None
    finally:
        session.close()
