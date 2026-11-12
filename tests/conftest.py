"""Shared pytest config for Amazon PL Hunter v2.

- Puts the project root on sys.path so `import backend...` works.
- Provides the test API token, a temp SQLite DB (via the backend's real
  variable PLH_DATABASE_URL — see backend/db.py), fixture helpers, and a
  network guard (no test may touch the network — offline suite).
- Backend-dependent fixtures SKIP (never fail) when the backend module they
  need does not exist yet, with a clear reason naming the missing module.
  See tests/helpers.py for the adapter contract the backend must satisfy.
"""

import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_API_TOKEN = "test-token-plhunter-v2"

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Temp DB used by API/dedup/catalogue tests. Set BEFORE any backend import
# so the backend's engine (created lazily on first use) binds to the
# throwaway file. backend/db.py honours PLH_DATABASE_URL, a SQLAlchemy URL
# (e.g. sqlite:////abs/path.db) — that is the ONLY db-path variable the
# backend reads; PLH_DB_PATH / PLHUNTER_DB_PATH are not honoured.
_TEST_TMPDIR = Path(tempfile.mkdtemp(prefix="plhunter-tests-"))
TEST_DB_PATH = _TEST_TMPDIR / "plhunter_test.db"
TEST_DB_URL = f"sqlite:///{TEST_DB_PATH}"
os.environ.setdefault("PLH_API_TOKEN", TEST_API_TOKEN)
os.environ.setdefault("PLH_DATABASE_URL", TEST_DB_URL)
os.environ.setdefault("PLH_LIVE", "0")


def fixture_html(name: str) -> str:
    """Read a saved HTML fixture by filename."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_db_url() -> str:
    """The SQLAlchemy URL of this process's throwaway test DB."""
    return os.environ.get("PLH_DATABASE_URL", TEST_DB_URL)


@pytest.fixture()
def temp_db_path(tmp_path):
    """A fresh, empty SQLite file path for one test."""
    return tmp_path / "plhunter.db"


@pytest.fixture()
def test_token():
    return TEST_API_TOKEN


@pytest.fixture()
def authed_headers(test_token):
    return {"X-API-Token": test_token}


@pytest.fixture()
def fixture_dir():
    return FIXTURES_DIR


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail loudly if any test tries real network I/O. Suite must run offline."""

    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests (offline suite)")

    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture(scope="module")
def api_client(tmp_path_factory):
    """FastAPI TestClient for the v2 backend, or SKIP if the app is absent.

    Module-scoped (the app is imported once). Runs with cwd pointed at a
    temp dir and PLH_DATABASE_URL pointed at a throwaway file, so a test
    run can never write to the real data/plhunter.db. The cached engine is
    reset first (backend.db.reset_engine_for_tests) so the temp URL takes
    effect even if another test already created an engine. Uses manual
    os.chdir/os.environ (not monkeypatch, which is function-scoped) with
    restore on teardown.
    """
    from tests.helpers import load_app

    workdir = tmp_path_factory.mktemp("api-cwd")
    db_path = workdir / "plhunter_test.db"
    db_url = f"sqlite:///{db_path}"
    old_cwd = os.getcwd()
    env_keys = ("PLH_API_TOKEN", "PLH_DATABASE_URL", "PLH_LIVE")
    old_env = {k: os.environ.get(k) for k in env_keys}
    os.chdir(workdir)
    os.environ["PLH_API_TOKEN"] = TEST_API_TOKEN
    os.environ["PLH_DATABASE_URL"] = db_url
    os.environ["PLH_LIVE"] = "0"
    try:
        try:
            from backend.db import init_db, reset_engine_for_tests
            reset_engine_for_tests()
        except ImportError:
            pass
        app = load_app()
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover
            pytest.skip("fastapi/httpx not installed — pip install -r tests/requirements-test.txt")
        # Create the SPEC tables explicitly: TestClient only runs the app's
        # startup event inside a context manager, so init_db() may never
        # fire for plain client.get() calls.
        try:
            from backend.db import init_db as _init_db
            _init_db(db_url)
        except ImportError:
            pass
        yield TestClient(app), db_path
    finally:
        os.chdir(old_cwd)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
