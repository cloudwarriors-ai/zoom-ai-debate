"""Integration tests for the FastAPI endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


SAMPLE_TRANSCRIPT = {
    "topic": "Should AI be regulated?",
    "speakers": [
        {"name": "Alex", "voice": "ash", "perspective": "pro"},
        {"name": "Jordan", "voice": "coral", "perspective": "con"},
    ],
    "transcript": [
        {"speaker": "Alex", "text": "We need oversight."},
        {"speaker": "Jordan", "text": "Innovation needs freedom."},
    ],
}


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestFormatEndpoint:
    @pytest.mark.asyncio
    async def test_format(self, client):
        resp = await client.get("/api/format")
        assert resp.status_code == 200
        data = resp.json()
        assert "schema" in data
        assert "example" in data
        assert "available_voices" in data


class TestConversationEndpoints:
    @pytest.mark.asyncio
    async def test_upload_conversation(self, client):
        resp = await client.post("/api/conversations", json=SAMPLE_TRANSCRIPT)
        assert resp.status_code == 201
        data = resp.json()
        assert data["topic"] == "Should AI be regulated?"
        assert data["status"] == "ready"
        assert len(data["transcript"]) == 2
        assert data["transcript"][0]["speaker"] == "Alex"

    @pytest.mark.asyncio
    async def test_upload_validates_speakers(self, client):
        bad = {
            **SAMPLE_TRANSCRIPT,
            "transcript": [{"speaker": "Unknown", "text": "Who am I?"}],
        }
        resp = await client.post("/api/conversations", json=bad)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_upload_requires_transcript(self, client):
        resp = await client.post("/api/conversations", json={
            "topic": "Test",
            "speakers": [{"name": "A", "voice": "alloy"}, {"name": "B", "voice": "echo"}],
            "transcript": [],
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_get_conversation(self, client):
        create_resp = await client.post("/api/conversations", json=SAMPLE_TRANSCRIPT)
        conv_id = create_resp.json()["id"]
        resp = await client.get(f"/api/conversations/{conv_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == conv_id

    @pytest.mark.asyncio
    async def test_get_nonexistent_conversation(self, client):
        resp = await client.get("/api/conversations/conv_doesnotexist")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_conversations(self, client):
        await client.post("/api/conversations", json=SAMPLE_TRANSCRIPT)
        resp = await client.get("/api/conversations")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_perform(self, client):
        create_resp = await client.post("/api/conversations", json=SAMPLE_TRANSCRIPT)
        conv_id = create_resp.json()["id"]

        with patch("main.ConversationOrchestrator") as mock_orch_cls:
            mock_orch = AsyncMock()
            mock_orch_cls.return_value = mock_orch

            resp = await client.post(
                f"/api/conversations/{conv_id}/perform",
                json={"meeting_url": "https://zoom.us/j/123?pwd=abc"},
            )

        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_stop_nonexistent(self, client):
        resp = await client.post("/api/conversations/conv_nope/stop")
        assert resp.status_code == 404
