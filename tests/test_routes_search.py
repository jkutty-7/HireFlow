"""
HTTP-level tests for routes/search.py.

Tests the FastAPI request/response layer:
  - POST /api/search   → 202 Accepted
  - auth enforcement   → 403 without valid key
  - GET  /api/search/{id}/status  → 200
  - GET  /api/search/{id}/results → 202 (running) / 200 (complete)

Pipeline background tasks are mocked so no actual LLM or API calls are made.
"""

import uuid
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from db.models import Search, Candidate as CandidateORM


# ─── POST /api/search ─────────────────────────────────────────────────────────

class TestStartSearch:
    async def test_returns_202_with_search_id(self, client):
        with patch("routes.search._run_pipeline", new=AsyncMock()):
            response = await client.post(
                "/api/search",
                json={"job_description": "Senior Python engineer with FastAPI skills."},
            )

        assert response.status_code == 202
        data = response.json()
        assert "search_id" in data
        assert data["status"] == "running"
        assert data["progress_pct"] == 0.0

    async def test_creates_search_row_in_db(self, client, db_session):
        from sqlalchemy import select

        with patch("routes.search._run_pipeline", new=AsyncMock()):
            response = await client.post(
                "/api/search",
                json={"job_description": "Backend engineer needed."},
            )

        assert response.status_code == 202
        search_id = uuid.UUID(response.json()["search_id"])

        result = await db_session.execute(
            select(Search).where(Search.id == search_id)
        )
        search = result.scalar_one_or_none()
        assert search is not None
        assert search.status == "running"
        assert "Backend engineer" in search.job_description

    async def test_missing_job_description_returns_422(self, client):
        response = await client.post("/api/search", json={})
        assert response.status_code == 422

    async def test_location_filter_accepted(self, client):
        with patch("routes.search._run_pipeline", new=AsyncMock()):
            response = await client.post(
                "/api/search",
                json={
                    "job_description": "Python developer.",
                    "location_filter": "Bangalore",
                },
            )
        assert response.status_code == 202

    async def test_max_candidates_accepted(self, client):
        with patch("routes.search._run_pipeline", new=AsyncMock()):
            response = await client.post(
                "/api/search",
                json={
                    "job_description": "React developer.",
                    "max_candidates": 10,
                },
            )
        assert response.status_code == 202


# ─── Auth enforcement (API key) ───────────────────────────────────────────────

class TestApiKeyAuth:
    async def test_no_key_allowed_in_dev_mode(self, client):
        """When settings.api_keys is empty (dev mode), all requests pass through."""
        with patch("routes.search._run_pipeline", new=AsyncMock()):
            response = await client.post(
                "/api/search",
                json={"job_description": "Test JD."},
            )
        # Dev mode (no API keys configured) → accepts request
        assert response.status_code == 202

    async def test_invalid_key_returns_403_when_keys_configured(self, client):
        """When at least one API key is configured, wrong keys are rejected."""
        with patch("auth.dependencies.settings") as mock_settings:
            mock_settings.api_keys = ["valid-key-abc123"]
            response = await client.post(
                "/api/search",
                headers={"X-API-Key": "wrong-key"},
                json={"job_description": "Test JD."},
            )
        assert response.status_code == 403

    async def test_valid_key_accepted(self, client):
        with patch("auth.dependencies.settings") as mock_settings:
            mock_settings.api_keys = ["valid-key-abc123"]
            with patch("routes.search._run_pipeline", new=AsyncMock()):
                response = await client.post(
                    "/api/search",
                    headers={"X-API-Key": "valid-key-abc123"},
                    json={"job_description": "Test JD."},
                )
        assert response.status_code == 202

    async def test_missing_key_header_returns_403_when_keys_configured(self, client):
        with patch("auth.dependencies.settings") as mock_settings:
            mock_settings.api_keys = ["some-key"]
            response = await client.post(
                "/api/search",
                json={"job_description": "Test JD."},
            )
        assert response.status_code == 403


# ─── GET /api/search/{id}/status ─────────────────────────────────────────────

class TestGetSearchStatus:
    async def test_returns_status_for_running_search(self, client, running_search):
        response = await client.get(f"/api/search/{running_search.id}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert "search_id" in data

    async def test_returns_404_for_unknown_id(self, client):
        unknown_id = uuid.uuid4()
        response = await client.get(f"/api/search/{unknown_id}/status")
        assert response.status_code == 404

    async def test_completed_search_shows_100_percent(self, client, db_session):
        from sqlalchemy import select

        search_id = uuid.uuid4()
        search = Search(
            id=search_id,
            job_description="Complete test",
            status="complete",
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(search)
        await db_session.commit()

        response = await client.get(f"/api/search/{search_id}/status")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert data["progress_pct"] == 100.0

    async def test_invalid_uuid_returns_422(self, client):
        response = await client.get("/api/search/not-a-uuid/status")
        assert response.status_code == 422


# ─── GET /api/search/{id}/results ────────────────────────────────────────────

class TestGetSearchResults:
    async def test_returns_202_while_running(self, client, running_search):
        response = await client.get(f"/api/search/{running_search.id}/results")
        assert response.status_code == 202

    async def test_returns_results_for_completed_search(self, client, db_session):
        search_id = uuid.uuid4()
        search = Search(
            id=search_id,
            job_description="Completed search",
            status="complete",
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(search)
        await db_session.commit()

        # Add a candidate
        candidate = CandidateORM(
            search_id=search_id,
            name="Test Candidate",
            title="Engineer",
            composite_score=75.0,
            skill_match_pct=80.0,
            seniority_fit="match",
            github_score=50.0,
            email_validity="verified",
            rank=1,
            rank_justification="Good match.",
            source="apollo",
        )
        db_session.add(candidate)
        await db_session.commit()

        response = await client.get(f"/api/search/{search_id}/results")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["name"] == "Test Candidate"
        assert data["candidates"][0]["rank"] == 1

    async def test_returns_404_for_unknown_search(self, client):
        response = await client.get(f"/api/search/{uuid.uuid4()}/results")
        assert response.status_code == 404

    async def test_failed_search_results_returns_response(self, client, db_session):
        search_id = uuid.uuid4()
        search = Search(
            id=search_id,
            job_description="Failed search",
            status="failed",
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(search)
        await db_session.commit()

        response = await client.get(f"/api/search/{search_id}/results")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["candidates"] == []


# ─── GET /health ──────────────────────────────────────────────────────────────

class TestHealth:
    async def test_health_endpoint_unauthenticated(self, client):
        """Health check must be accessible without an API key."""
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
