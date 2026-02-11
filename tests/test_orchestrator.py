"""Tests for the conversation orchestrator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import Conversation, ConversationStatus, Speaker, TranscriptLine
from orchestrator import ConversationOrchestrator


def _make_conversation(lines: list[tuple[str, str]] | None = None) -> Conversation:
    if lines is None:
        lines = [
            ("Alex", "First point from Alex."),
            ("Jordan", "Response from Jordan."),
            ("Alex", "Follow-up from Alex."),
            ("Jordan", "Final word from Jordan."),
        ]
    return Conversation(
        topic="Test debate",
        speakers=[
            Speaker(name="Alex", voice="alloy"),
            Speaker(name="Jordan", voice="echo"),
        ],
        transcript=[TranscriptLine(speaker=s, text=t) for s, t in lines],
    )


class TestOrchestratorLifecycle:
    @pytest.mark.asyncio
    async def test_status_transitions(self):
        """Verify the orchestrator moves through expected statuses."""
        conv = _make_conversation()

        mock_worker = MagicMock()
        mock_synth = AsyncMock()
        # Return small audio (100 samples = ~3ms at 24kHz)
        mock_synth.synthesize = AsyncMock(return_value=b"\x00" * 200)
        mock_synth.connect = AsyncMock()
        mock_synth.disconnect = AsyncMock()

        mock_mgr = AsyncMock()
        mock_mgr.spawn_worker = AsyncMock(return_value=mock_worker)
        mock_mgr.join_meeting = AsyncMock(return_value=True)
        mock_mgr.send_audio = AsyncMock()
        mock_mgr.leave_meeting = AsyncMock()
        mock_mgr.shutdown = AsyncMock()

        orch = ConversationOrchestrator(conv)
        orch.worker_mgr = mock_mgr

        with patch("orchestrator.VoiceSynthesizer", return_value=mock_synth):
            with patch("orchestrator.settings") as mock_settings:
                mock_settings.openai_api_key = "test-key"
                mock_settings.openai_sample_rate = 24000
                mock_settings.zoom_sample_rate = 32000
                mock_settings.settle_after_join_sec = 0
                mock_settings.pause_between_lines_sec = 0

                await orch.start("https://zoom.us/j/123?pwd=abc")
                # Wait for the orchestrator task to complete
                await orch._task

        assert conv.status == ConversationStatus.COMPLETED
        assert conv.progress.total_lines == 4
        # Verify both workers were spawned
        assert mock_mgr.spawn_worker.call_count == 2
        # Verify synth was called for each line
        assert mock_synth.synthesize.call_count == 4

    @pytest.mark.asyncio
    async def test_stop_during_performance(self):
        """Verify stop() interrupts the performance."""
        conv = _make_conversation([("Alex", "Line.")] * 20)

        mock_synth = AsyncMock()

        async def slow_synthesize(text):
            await asyncio.sleep(0.5)
            return b"\x00" * 200

        mock_synth.synthesize = slow_synthesize
        mock_synth.connect = AsyncMock()
        mock_synth.disconnect = AsyncMock()

        mock_mgr = AsyncMock()
        mock_mgr.spawn_worker = AsyncMock(return_value=MagicMock())
        mock_mgr.join_meeting = AsyncMock(return_value=True)
        mock_mgr.send_audio = AsyncMock()
        mock_mgr.leave_meeting = AsyncMock()
        mock_mgr.shutdown = AsyncMock()

        orch = ConversationOrchestrator(conv)
        orch.worker_mgr = mock_mgr

        with patch("orchestrator.VoiceSynthesizer", return_value=mock_synth):
            with patch("orchestrator.settings") as mock_settings:
                mock_settings.openai_api_key = "test-key"
                mock_settings.openai_sample_rate = 24000
                mock_settings.zoom_sample_rate = 32000
                mock_settings.settle_after_join_sec = 0
                mock_settings.pause_between_lines_sec = 0

                await orch.start("https://zoom.us/j/123?pwd=abc")
                await asyncio.sleep(0.2)
                await orch.stop()

        assert conv.status == ConversationStatus.STOPPED
