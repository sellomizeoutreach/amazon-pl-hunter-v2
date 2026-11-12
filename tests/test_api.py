"""API tests — auth, health, filtered export, dry-run hunt, CORS.

SPEC (backend/server.py, prefix /api; token auth on ALL except /api/health):
- Header `X-API-Token: <PLH_API_TOKEN>`.
- GET /api/health -> {status, db, version} (public).
- GET /api/leads/export?format=csv&<same filters as /api/leads> returns
  EXACTLY the filtered rows (never the whole job) — the v1 extension bug
  was exporting unfiltered rows.
- POST /api/runs {mode: dry|live} -> 202 {run_id}; dry-run uses fixtures,
  writes a real run row + summary, zero network.
- CORS: only origins in PLH_ALLOWED_ORIGINS.

All tests SKIP while no backend server module exists. Seeding is done with
raw sqlite3 against the SPEC schema so it works regardless of the backend's
ORM, as long as the backend creates the SPEC tables at startup.
"""

import time
from pathlib import Path

import pytest

from tests.helpers import find_test_db, seed_two_leads


def _resolve_test_db(api_client):
    """Find the test DB file the app actually created (SPEC tables present).

    The backend binds its engine to PLH_DATABASE_URL (see backend/db.py);
    find_test_db() reads that variable first, with the fixture's hinted
    path as fallback. The real project data/plhunter.db is DELIBERATELY
    not a candidate — tests must never seed the real DB.
    """
    _, hinted_path = api_client
    candidates = [hinted_path, Path("data/plhunter.db")]  # temp cwd only
    return find_test_db(candidates)


def _seed_or_skip(api_client):
    db_path = _resolve_test_db(api_client)
    if db_path is None:
        pytest.skip(
            "could not find a test DB with SPEC tables (brands/leads) — "
            "backend either did not run init_db() at startup or does not "
            "honour PLH_DATABASE_URL; refusing to seed blindly"
        )
    seed_two_leads(db_path)
    return db_path


class TestAuth:
    def test_missing_token_rejected_on_leads(self, api_client):
        client, _ = api_client
        resp = client.get("/api/leads")
        assert resp.status_code in (401, 403), (
            f"GET /api/leads without a token returned {resp.status_code} — "
            "auth is required on all routes except /api/health")

    def test_wrong_token_rejected_on_leads(self, api_client):
        client, _ = api_client
        resp = client.get("/api/leads", headers={"X-API-Token": "wrong-token"})
        assert resp.status_code in (401, 403)

    def test_missing_token_rejected_on_runs(self, api_client):
        client, _ = api_client
        resp = client.post("/api/runs", json={"mode": "dry"})
        assert resp.status_code in (401, 403)

    def test_health_is_public(self, api_client):
        client, _ = api_client
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") in ("ok", "healthy", "up")
        assert "db" in body and "version" in body


class TestCors:
    def test_unlisted_origin_gets_no_cors_access(self, api_client):
        client, _ = api_client
        resp = client.get("/api/health", headers={"Origin": "https://evil.example.test"})
        allowed = resp.headers.get("access-control-allow-origin")
        assert allowed != "https://evil.example.test", (
            "unlisted origin was granted CORS access")

    def test_listed_origin_not_granted_to_other_origins(self, api_client):
        """If CORS headers are emitted at all, they must only ever echo a
        listed origin — never reflect an arbitrary Origin header."""
        client, _ = api_client
        for origin in ("https://attacker.example", "http://localhost:9999"):
            resp = client.get("/api/health", headers={"Origin": origin})
            allowed = resp.headers.get("access-control-allow-origin")
            assert allowed not in (origin, "*"), (
                f"Origin {origin} was reflected/granted: {allowed!r}")


class TestExport:
    def test_export_returns_only_filtered_rows(self, api_client, authed_headers):
        """Seed two leads (NEW_LAUNCH/US vs STAGNANT_VETERAN/DE), filter to
        one persona, assert the other brand is ABSENT from the CSV."""
        client, _ = api_client
        _seed_or_skip(api_client)
        resp = client.get("/api/leads/export?format=csv&persona=NEW_LAUNCH",
                          headers=authed_headers)
        assert resp.status_code == 200
        csv_text = resp.text
        assert "Nova Home" in csv_text, "filtered lead missing from export"
        assert "KitchenPro" not in csv_text, (
            "export leaked an unfiltered row (v1 bug: export ignored filters)")
        lines = [ln for ln in csv_text.splitlines() if ln.strip()]
        assert len(lines) == 2, f"expected header + 1 data row, got {len(lines)} lines"

    def test_export_country_filter(self, api_client, authed_headers):
        client, _ = api_client
        _seed_or_skip(api_client)
        resp = client.get("/api/leads/export?format=csv&country=DE",
                          headers=authed_headers)
        assert resp.status_code == 200
        assert "KitchenPro" in resp.text
        assert "Nova Home" not in resp.text

    def test_leads_list_filter_matches_export(self, api_client, authed_headers):
        client, _ = api_client
        _seed_or_skip(api_client)
        resp = client.get("/api/leads?persona=NEW_LAUNCH", headers=authed_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert "Nova Home" in str(body["items"][0])

    def test_needs_review_filter(self, api_client, authed_headers):
        """Flag one seeded lead as needs_review; the list AND the export
        must return exactly that lead and nothing else."""
        import sqlite3

        client, _ = api_client
        db_path = _seed_or_skip(api_client)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE leads SET needs_review = 1 WHERE brand_id IN "
            "(SELECT id FROM brands WHERE name = 'KitchenPro')"
        )
        conn.commit()
        conn.close()
        resp = client.get("/api/leads?needs_review=true", headers=authed_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert "KitchenPro" in str(body["items"][0])
        assert "Nova Home" not in str(body["items"])
        exp = client.get("/api/leads/export?format=csv&needs_review=true",
                         headers=authed_headers)
        assert exp.status_code == 200
        assert "KitchenPro" in exp.text
        assert "Nova Home" not in exp.text


class TestDryRun:
    def test_dry_run_completes_and_writes_run_row_and_summary(
            self, api_client, authed_headers):
        client, _ = api_client
        resp = client.post("/api/runs", json={"mode": "dry"}, headers=authed_headers)
        assert resp.status_code == 202, f"POST /api/runs dry returned {resp.status_code}: {resp.text}"
        run_id = resp.json().get("run_id")
        assert run_id, f"202 response has no run_id: {resp.json()}"

        # Poll until the run leaves `running` (dry-run uses fixtures: fast).
        final = None
        for _ in range(60):
            got = client.get(f"/api/runs/{run_id}", headers=authed_headers)
            assert got.status_code == 200, f"GET /api/runs/{run_id} -> {got.status_code}"
            final = got.json()
            if final.get("status") != "running":
                break
            time.sleep(0.5)
        assert final is not None
        assert final.get("status") in ("done", "partial"), (
            f"dry run ended {final.get('status')}: {final}")
        assert any(k in final for k in ("summary_json", "summary", "leads_new")), (
            f"run row has no summary: {final}")

        # Run history lists it too.
        listed = client.get("/api/runs", headers=authed_headers)
        assert listed.status_code == 200
        body = listed.json()
        items = body if isinstance(body, list) else body.get("items", body.get("runs", []))
        assert any(str(run_id) in str(item) for item in items)

    def test_live_run_without_live_flag_is_409(self, api_client, authed_headers):
        client, _ = api_client
        resp = client.post("/api/runs", json={"mode": "live"}, headers=authed_headers)
        assert resp.status_code == 409, (
            f"live hunt without PLH_LIVE=1 must be 409, got {resp.status_code}")
