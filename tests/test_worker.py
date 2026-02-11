"""Tests for the zoom worker subprocess and worker manager."""

import asyncio
import json

import pytest

from worker_manager import WorkerManager


class TestWorkerManager:
    @pytest.mark.asyncio
    async def test_spawn_and_ready(self):
        """Spawn a stub worker and verify it signals ready."""
        mgr = WorkerManager()
        worker = await mgr.spawn_worker("test-bot")

        assert worker.name == "test-bot"
        assert worker.state["sdk_ready"] is True
        assert worker.alive

        await mgr.shutdown(worker)
        assert not worker.alive

    @pytest.mark.asyncio
    async def test_join_meeting_stub(self):
        """Join in stub mode and verify IN_MEETING status."""
        mgr = WorkerManager()
        worker = await mgr.spawn_worker("joiner")

        success = await mgr.join_meeting(worker, "123456789", "pwd", "Test Bot")
        assert success
        assert worker.state["in_meeting"] is True

        await mgr.shutdown(worker)

    @pytest.mark.asyncio
    async def test_send_audio(self):
        """Send audio to stub worker without error."""
        mgr = WorkerManager()
        worker = await mgr.spawn_worker("audio-test")
        await mgr.join_meeting(worker, "123", "", "Bot")

        audio_data = b"\x00" * 6400  # 100ms at 32kHz 16-bit
        await mgr.send_audio(worker, audio_data)
        # Give the worker a moment to process
        await asyncio.sleep(0.1)

        await mgr.shutdown(worker)

    @pytest.mark.asyncio
    async def test_leave_meeting(self):
        """Leave meeting in stub mode."""
        mgr = WorkerManager()
        worker = await mgr.spawn_worker("leaver")
        await mgr.join_meeting(worker, "123", "", "Bot")

        await mgr.leave_meeting(worker)
        # State should reflect leaving
        await asyncio.sleep(0.2)

        await mgr.shutdown(worker)

    @pytest.mark.asyncio
    async def test_shutdown_all(self):
        """Spawn multiple workers and shut them all down."""
        mgr = WorkerManager()
        w1 = await mgr.spawn_worker("bot-1")
        w2 = await mgr.spawn_worker("bot-2")

        assert len(mgr.workers) == 2
        await mgr.shutdown_all()
        assert len(mgr.workers) == 0
