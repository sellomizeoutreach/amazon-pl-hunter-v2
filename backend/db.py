"""DB layer — single source of truth for schema (see SPEC.md).

SQLite file ``data/plhunter.db`` by default (``PLH_DATABASE_URL`` overrides,
e.g. ``sqlite:////abs/path/plhunter.db``). SQLAlchemy 2, ``init_db()`` creates
all tables plus a ``schema_version`` table (current version 2). Litestream
path for replication is documented in the coordinator-owned README.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

SCHEMA_VERSION = 3


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "plhunter.db"


def database_url() -> str:
    url = os.environ.get("PLH_DATABASE_URL", "").strip()
    if url:
        return url
    path = default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version = Column(Integer, primary_key=True)
    applied_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Run(Base):
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running")  # running/done/failed/partial
    keywords_used = Column(Text, default="[]")  # JSON list
    brands_seen = Column(Integer, default=0)
    leads_new = Column(Integer, default=0)
    leads_total = Column(Integer, default=0)
    captcha_blocks = Column(Integer, default=0)
    errors = Column(Integer, default=0)
    email_status = Column(String, default="pending")
    summary_json = Column(Text, default="{}")


class Keyword(Base):
    __tablename__ = "keywords"
    id = Column(Integer, primary_key=True)
    keyword = Column(String, nullable=False)
    source = Column(String, nullable=False)  # bsr/autocomplete/ddgs/seed/fallback
    run_date = Column(String, nullable=False)  # YYYY-MM-DD
    used = Column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("keyword", "run_date", name="uq_keyword_date"),)


class Brand(Base):
    __tablename__ = "brands"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)  # normalised
    seller_id = Column(String, nullable=True, unique=True)  # dedup key
    seller_name = Column(String, nullable=True)
    seller_country = Column(String, nullable=True)
    country_confidence = Column(Float, default=0.0)
    pl_score = Column(Float, default=0.0)
    pl_reason = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    leads = relationship("Lead", back_populates="brand")


class Asin(Base):
    __tablename__ = "asins"
    id = Column(Integer, primary_key=True)
    asin = Column(String, nullable=False, unique=True)
    brand_id = Column(Integer, ForeignKey("brands.id"), nullable=True)
    title = Column(String, nullable=True)
    reviews = Column(Integer, default=0)
    rating = Column(Float, nullable=True)
    price = Column(String, nullable=True)
    first_available = Column(String, nullable=True)
    bsr = Column(String, nullable=True)
    url = Column(String, nullable=True)
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True)
    brand_id = Column(Integer, ForeignKey("brands.id"), nullable=False)
    asin_id = Column(Integer, ForeignKey("asins.id"), nullable=True)
    persona = Column(String, default="UNKNOWN")  # NEW_LAUNCH/STAGNANT_VETERAN/DISTRESSED/UNKNOWN
    contact_quality = Column(String, default="UNCONTACTABLE")  # DIRECT/WEBSITE_ONLY/UNCONTACTABLE
    founder_name = Column(String, nullable=True)
    founder_confidence = Column(Float, default=0.0)
    linkedin_url = Column(String, nullable=True)
    linkedin_confidence = Column(Float, default=0.0)
    website = Column(String, nullable=True)
    email = Column(String, nullable=True)
    email_confidence = Column(Float, default=0.0)
    email_is_personal = Column(Boolean, default=False)
    phone = Column(String, nullable=True)
    pitch_hook = Column(Text, default="")
    needs_review = Column(Boolean, default=False)
    # auto_hunt (direct live) / serpapi (SerpApi live) / dry_run (never
    # emailed) / audit / extension
    source = Column(String, default="auto_hunt")
    # Hunt run that found the lead (loose ref to runs.id; NULL for older
    # rows). Used for the batch CSV "run it came from" column.
    run_id = Column(Integer, nullable=True)
    # Set when the lead went out in a batch CSV email; NULL = unsent.
    batch_sent_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    brand = relationship("Brand", back_populates="leads")
    asin = relationship("Asin")
    outreach = relationship("OutreachStatus", back_populates="lead", uselist=False)


class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    kind = Column(String, nullable=False)  # email/phone/linkedin/website
    value = Column(String, nullable=False)
    confidence = Column(Float, default=0.0)
    verified = Column(Boolean, default=False)
    source = Column(String, default="")


class OutreachStatus(Base):
    __tablename__ = "outreach_status"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), unique=True, nullable=False)
    status = Column(String, default="NEW")  # NEW/SHORTLISTED/CONTACTED/SKIP
    notes = Column(Text, default="")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    lead = relationship("Lead", back_populates="outreach")


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    kind = Column(String, nullable=False)  # keyword/brand/asin/enrich/audit
    subject = Column(String, nullable=False)
    stage = Column(String, default="search")  # search/pdp/seller/catalogue/enrich/score/save
    status = Column(String, default="pending")  # pending/running/done/failed
    payload_json = Column(Text, default="{}")
    result_json = Column(Text, default="{}")
    attempts = Column(Integer, default=0)
    error = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


_engine = None
_Session = None


def get_engine(url: str | None = None):
    global _engine, _Session
    if _engine is None:
        _engine = create_engine(url or database_url(), connect_args={"check_same_thread": False})
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session():
    get_engine()
    assert _Session is not None
    return _Session()


def _ensure_lead_columns(engine) -> None:
    """Additive migration for the leads table (schema v3).

    create_all() never adds columns to an existing table, so DBs created
    by v2 (schema v2) get batch_sent_at + run_id here. Existing rows are
    untouched (new columns are nullable, no backfill) — nothing is dropped
    or recreated.
    """
    import logging

    _log = logging.getLogger(__name__)
    from sqlalchemy import inspect as _inspect

    existing = {c["name"] for c in _inspect(engine).get_columns("leads")}
    stmts = []
    if "batch_sent_at" not in existing:
        stmts.append("ALTER TABLE leads ADD COLUMN batch_sent_at DATETIME")
    if "run_id" not in existing:
        stmts.append("ALTER TABLE leads ADD COLUMN run_id INTEGER")
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    _log.info("migrated leads table: %s", ", ".join(s.split("ADD COLUMN ")[1] for s in stmts))


def init_db(url: str | None = None):
    """Create all tables + schema_version row. Idempotent."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    _ensure_lead_columns(engine)
    with sessionmaker(bind=engine)() as session:
        row = session.get(SchemaVersion, SCHEMA_VERSION)
        if row is None:
            session.add(SchemaVersion(version=SCHEMA_VERSION))
            session.commit()
        # Clean up any older version rows (keep only current)
        session.execute(text("DELETE FROM schema_version WHERE version != :v"), {"v": SCHEMA_VERSION})
        session.commit()
    return engine


def reset_engine_for_tests() -> None:
    """Test helper: drop the cached engine so a new URL takes effect."""
    global _engine, _Session
    _engine = None
    _Session = None
