"""Conversation orchestrator — coordinates the full performance lifecycle.

Spawns two Zoom workers, connects voice synthesizers, and drives
the transcript line-by-line through the meeting.
"""

import asyncio
import logging
from typing import Optional

from config import settings
from models import (
    Conversation,
    ConversationStatus,
    TranscriptLine,
    WorkerStatus,
    parse_meeting_url,
)
from voice_synth import VoiceSynthesizer, resample_audio
from worker_manager import WorkerManager, WorkerProcess

logger = logging.getLogger(__name__)


class ConversationOrchestrator:
    """Drives a conversation performance in a Zoom meeting."""

    def __init__(self, conversation: Conversation):
        self.conversation = conversation
        self.worker_mgr = WorkerManager()
        self._workers: dict[str, WorkerProcess] = {}
        self._synths: dict[str, VoiceSynthesizer] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def start(self, meeting_url: str):
        """Begin performing the conversation in the given meeting."""
        self.conversation.status = ConversationStatus.JOINING
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(meeting_url))

    async def stop(self):
        """Stop the performance and clean up."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._cleanup()
        self.conversation.status = ConversationStatus.STOPPED

    async def _run(self, meeting_url: str):
        """Main performance loop."""
        try:
            meeting_id, password = parse_meeting_url(meeting_url)

            # Spawn and join workers
            await self._join_workers(meeting_id, password)

            if self._stop_event.is_set():
                return

            # Connect voice synthesizers
            await self._connect_synths()

            if self._stop_event.is_set():
                return

            # Brief pause for meeting to settle
            await asyncio.sleep(settings.settle_after_join_sec)

            # Perform the conversation
            self.conversation.status = ConversationStatus.PERFORMING
            await self._perform_transcript()

            # Done
            self.conversation.status = ConversationStatus.COMPLETED
            logger.info(
                "Conversation %s completed — all %d lines spoken",
                self.conversation.id,
                len(self.conversation.transcript),
            )

        except asyncio.CancelledError:
            logger.info("Conversation %s cancelled", self.conversation.id)
            raise
        except Exception as e:
            logger.error("Conversation %s failed: %s", self.conversation.id, e)
            self.conversation.status = ConversationStatus.ERROR
            self.conversation.error = str(e)
        finally:
            await self._cleanup()

    async def _join_workers(self, meeting_id: str, password: str):
        """Spawn two workers and join them to the meeting.

        Uses parallel joins with retry. If a worker crashes (QEMU segfault),
        the surviving worker continues and the crashed one is retried.
        """
        max_retries = 3

        for speaker in self.conversation.speakers:
            ws = WorkerStatus(
                name=speaker.name, voice=speaker.voice, status="joining"
            )
            self.conversation.workers.append(ws)

        # Spawn all workers
        for speaker in self.conversation.speakers:
            worker = await self.worker_mgr.spawn_worker(speaker.name)
            self._workers[speaker.name] = worker

        # Join all in parallel
        join_tasks = []
        for speaker in self.conversation.speakers:
            worker = self._workers[speaker.name]
            join_tasks.append(
                self.worker_mgr.join_meeting(
                    worker, meeting_id, password, speaker.name
                )
            )
        await asyncio.gather(*join_tasks)

        # Wait a bit then check who survived and unmute them
        await asyncio.sleep(3.0)

        for speaker in self.conversation.speakers:
            worker = self._workers[speaker.name]
            if not worker.alive:
                # Worker crashed — retry
                logger.warning("Worker %r crashed after join, retrying", speaker.name)
                for attempt in range(1, max_retries + 1):
                    await asyncio.sleep(3.0)
                    worker = await self.worker_mgr.spawn_worker(speaker.name)
                    self._workers[speaker.name] = worker
                    joined = await self.worker_mgr.join_meeting(
                        worker, meeting_id, password, speaker.name
                    )
                    if not joined:
                        continue
                    await asyncio.sleep(3.0)
                    if worker.alive:
                        logger.info("Worker %r retry %d succeeded", speaker.name, attempt)
                        break
                    logger.warning("Worker %r retry %d crashed", speaker.name, attempt)
                else:
                    raise RuntimeError(
                        f"Worker {speaker.name} crashed {max_retries} times"
                    )

            await self.worker_mgr.unmute_audio(worker)

        # Update statuses
        for ws in self.conversation.workers:
            ws.status = "in_meeting"

        logger.info(
            "Both workers joined meeting %s", meeting_id
        )

    async def _connect_synths(self):
        """Connect a voice synthesizer per speaker."""
        for speaker in self.conversation.speakers:
            synth = VoiceSynthesizer(
                api_key=settings.openai_api_key,
                voice=speaker.voice,
                model=settings.openai_realtime_model,
            )
            await synth.connect()
            self._synths[speaker.name] = synth

        logger.info("Voice synthesizers connected for %d speakers", len(self._synths))

    async def _perform_transcript(self):
        """Speak each line through the correct worker."""
        lines = self.conversation.transcript
        total = len(lines)
        self.conversation.progress.total_lines = total

        for i, line in enumerate(lines):
            if self._stop_event.is_set():
                break

            self.conversation.progress.current_line = i + 1
            self.conversation.progress.current_speaker = line.speaker

            # Update worker status
            for ws in self.conversation.workers:
                ws.status = "speaking" if ws.name == line.speaker else "waiting"

            logger.info(
                "[%d/%d] %s: %s",
                i + 1,
                total,
                line.speaker,
                line.text[:80],
            )

            # Synthesize audio
            synth = self._synths.get(line.speaker)
            if not synth:
                logger.warning("No synthesizer for speaker %s, skipping", line.speaker)
                continue

            audio_24k = await synth.synthesize(line.text)

            if not audio_24k:
                logger.warning("No audio generated for line %d, skipping", i + 1)
                continue

            # Resample 24kHz → 32kHz for Zoom
            audio_32k = resample_audio(
                audio_24k, settings.openai_sample_rate, settings.zoom_sample_rate
            )

            # Send audio to the correct worker
            worker = self._workers.get(line.speaker)
            if worker:
                await self.worker_mgr.send_audio(worker, audio_32k)

                # Wait for audio to play (estimate based on audio length)
                duration_sec = len(audio_32k) / (settings.zoom_sample_rate * 2)  # 16-bit = 2 bytes/sample
                await asyncio.sleep(duration_sec + 0.3)  # audio duration + small buffer

            # Pause between lines
            if i < total - 1 and not self._stop_event.is_set():
                await asyncio.sleep(settings.pause_between_lines_sec)

        # Reset worker statuses
        for ws in self.conversation.workers:
            ws.status = "idle"

    async def _cleanup(self):
        """Disconnect synths and shutdown workers."""
        # Disconnect voice synthesizers
        for name, synth in self._synths.items():
            try:
                await synth.disconnect()
            except Exception as e:
                logger.warning("Error disconnecting synth %s: %s", name, e)
        self._synths.clear()

        # Leave meetings and shutdown workers
        for name, worker in self._workers.items():
            try:
                await self.worker_mgr.leave_meeting(worker)
                await self.worker_mgr.shutdown(worker)
            except Exception as e:
                logger.warning("Error shutting down worker %s: %s", name, e)
        self._workers.clear()

        # Update worker statuses
        for ws in self.conversation.workers:
            ws.status = "disconnected"
