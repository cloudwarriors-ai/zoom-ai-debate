"""Integration tests for the FastAPI endpoints."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from models import Speaker, TranscriptLine


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestConversationEndpoints:
    @pytest.mark.asyncio
    async def test_create_conversation(self, client):
        mock_transcript = [
            TranscriptLine(speaker="Alex", text="Point one."),
            TranscriptLine(speaker="Jordan", text="Counter one."),
        ]

        with patch("main.generate_transcript", new_callable=AsyncMock, return_value=mock_transcript):
            resp = await client.post("/api/conversations", json={
                "topic": "Test topic",
                "num_turns": 1,
                "speaker_a": {"name": "Alex", "voice": "alloy", "perspective": "pro"},
                "speaker_b": {"name": "Jordan", "voice": "echo", "perspective": "con"},
            })

        assert resp.status_code == 201
        data = resp.json()
        assert data["topic"] == "Test topic"
        assert data["status"] == "ready"
        assert len(data["transcript"]) == 2
        assert data["transcript"][0]["speaker"] == "Alex"

    @pytest.mark.asyncio
    async def test_create_conversation_defaults(self, client):
        mock_transcript = [
            TranscriptLine(speaker="Alex", text="Hello."),
            TranscriptLine(speaker="Jordan", text="Hi."),
        ]

        with patch("main.generate_transcript", new_callable=AsyncMock, return_value=mock_transcript):
            resp = await client.post("/api/conversations", json={
                "topic": "Default speakers test",
            })

        assert resp.status_code == 201
        data = resp.json()
        assert data["speakers"][0]["name"] == "Alex"
        assert data["speakers"][1]["name"] == "Jordan"

    @pytest.mark.asyncio
    async def test_get_conversation(self, client):
        mock_transcript = [TranscriptLine(speaker="A", text="Hi.")]

        with patch("main.generate_transcript", new_callable=AsyncMock, return_value=mock_transcript):
            create_resp = await client.post("/api/conversations", json={"topic": "Test"})

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
        mock_transcript = [TranscriptLine(speaker="A", text="Hi.")]

        with patch("main.generate_transcript", new_callable=AsyncMock, return_value=mock_transcript):
            await client.post("/api/conversations", json={"topic": "List test"})

        resp = await client.get("/api/conversations")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(c["topic"] == "List test" for c in data)

    @pytest.mark.asyncio
    async def test_perform_requires_ready_state(self, client):
        mock_transcript = [TranscriptLine(speaker="A", text="Hi.")]

        with patch("main.generate_transcript", new_callable=AsyncMock, return_value=mock_transcript):
            create_resp = await client.post("/api/conversations", json={"topic": "Perform test"})

        conv_id = create_resp.json()["id"]

        # Mock the orchestrator to avoid actual Zoom connection
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
