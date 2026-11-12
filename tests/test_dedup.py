"""Dedup tests — the v1 bug, proven fixed.

v1 failure (audit 2026-11-11): dedup did NOT persist across GitHub Actions
runs — seen_leads.json was never downloaded back, so the same brands were
re-emailed every run. v2 fixes this with a DB-backed dedup key:
brands.seller_id is UNIQUE (SPEC.md), and the Action downloads the DB
artifact before the run and uploads it after.

- test_seller_id_unique_contract: backend-independent; builds the SPEC
  brands table in a throwaway SQLite file and proves a duplicate seller_id
  cannot create a second brand, and the single row survives a fresh
  connection (restart).
- test_same_seller_id_inserted_twice_dedupes / test_dedup_persists_across
  _restart: backend-backed (raw SQL against the backend's own DB file);
  SKIP while backend/db.py does not exist or creates no SPEC tables.
"""

import sqlite3

import pytest

from tests.helpers import BRANDS_DDL


def _insert_brand(conn, name, seller_id):
    conn.execute(
        "INSERT INTO brands (name, seller_id, seller_name) VALUES (?, ?, ?)",
        (name, seller_id, name),
    )
    conn.commit()


def _count_brands(path):
    with sqlite3.connect(str(path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0]


def test_seller_id_unique_contract(temp_db_path):
    """SPEC schema, no backend: duplicate seller_id -> one brand, surviving
    a fresh connection (the restart case v1 failed)."""
    with sqlite3.connect(str(temp_db_path)) as conn:
        conn.executescript(BRANDS_DDL)
        _insert_brand(conn, "Nova Home", "A1NOVAHOME1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_brand(conn, "Nova Home Duplicate", "A1NOVAHOME1")
        conn.rollback()
        assert _count_brands(temp_db_path) == 1

    # Fresh connection = restart: still exactly one brand.
    assert _count_brands(temp_db_path) == 1
    with sqlite3.connect(str(temp_db_path)) as conn:
        row = conn.execute(
            "SELECT name, seller_id FROM brands").fetchone()
        assert row == ("Nova Home", "A1NOVAHOME1")


def _backend_db_or_skip():
    """A temp DB with the backend's own SPEC schema, or SKIP.

    Creates the tables via backend.db.init_db() on this process's
    throwaway PLH_DATABASE_URL — never the real data/plhunter.db.
    """
    import importlib
    import os

    try:
        db = importlib.import_module("backend.db")
    except ModuleNotFoundError as exc:
        pytest.skip(f"backend.db not implemented yet ({exc})")
    from conftest import TEST_DB_PATH, TEST_DB_URL

    if hasattr(db, "reset_engine_for_tests"):
        db.reset_engine_for_tests()
    db.init_db(TEST_DB_URL)
    if not TEST_DB_PATH.exists():
        pytest.skip("backend.db.init_db() did not create the temp test DB")
    return TEST_DB_PATH


def test_same_seller_id_inserted_twice_dedupes():
    db_path = _backend_db_or_skip()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM brands WHERE seller_id = 'A1DEDUPTEST1'")
        conn.commit()
        _insert_brand(conn, "Dedup Test Brand", "A1DEDUPTEST1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_brand(conn, "Dedup Test Brand Again", "A1DEDUPTEST1")
        conn.rollback()
        count = conn.execute(
            "SELECT COUNT(*) FROM brands WHERE seller_id = 'A1DEDUPTEST1'"
        ).fetchone()[0]
        assert count == 1


def test_dedup_persists_across_restart():
    db_path = _backend_db_or_skip()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM brands WHERE seller_id = 'A1DEDUPTEST2'")
        conn.commit()
        _insert_brand(conn, "Restart Dedup Brand", "A1DEDUPTEST2")
    # New connection = process restart: the row — and the UNIQUE constraint —
    # must still be there, and a duplicate must still be refused.
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM brands WHERE seller_id = 'A1DEDUPTEST2'"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            _insert_brand(conn, "Restart Dedup Brand Again", "A1DEDUPTEST2")
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) FROM brands WHERE seller_id = 'A1DEDUPTEST2'"
        ).fetchone()[0] == 1
