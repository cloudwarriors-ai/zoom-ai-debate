"""
Worker Manager

Manages zoom_worker subprocesses via asyncio. Each WorkerProcess wraps
an asyncio.subprocess and provides typed methods for sending commands
and reading JSON responses over the stdin/stdout protocol.
"""

import asyncio
import base64
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to the zoom_worker script
_WORKER_SCRIPT = str(Path(__file__).parent / "zoom_worker.py")


@dataclass
class WorkerProcess:
    """Wraps an asyncio subprocess running zoom_worker.py.

    Attributes:
        name: Human-readable name for this worker (e.g. "Alex").
        process: The underlying asyncio subprocess.
        state: Last known SDK/meeting state derived from JSON responses.
    """

    name: str
    process: asyncio.subprocess.Process
    state: dict = field(default_factory=lambda: {
        "sdk_ready": False,
        "in_meeting": False,
        "authenticated": False,
    })
    _reader_task: asyncio.Task | None = field(default=None, repr=False)
    _response_queue: asyncio.Queue = field(
        default_factory=asyncio.Queue, repr=False
    )
    _waiters: dict[str, list[asyncio.Future]] = field(
        default_factory=dict, repr=False
    )

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    async def send_command(self, cmd: dict) -> None:
        """Write a JSON command to the worker's stdin."""
        if self.process.stdin is None:
            raise RuntimeError(f"Worker {self.name}: stdin not available")
        line = json.dumps(cmd) + "\n"
        self.process.stdin.write(line.encode())
        await self.process.stdin.drain()

    async def _read_loop(self) -> None:
        """Continuously read stdout lines and dispatch JSON responses."""
        assert self.process.stdout is not None
        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                break
            line = raw.decode().strip()
            if not line.startswith("JSON:"):
                logger.debug("[%s stdout] %s", self.name, line)
                continue

            try:
                msg = json.loads(line[5:])
            except json.JSONDecodeError:
                logger.warning("[%s] bad JSON: %s", self.name, line[:200])
                continue

            action = msg.get("action", "")

            # Update internal state from interesting messages
            if action == "ready":
                self.state["sdk_ready"] = msg.get("sdk_ready", False)
            elif action == "auth":
                self.state["authenticated"] = msg.get("success", False)
            elif action == "meeting_status":
                status = msg.get("status", "")
                self.state["in_meeting"] = status == "IN_MEETING"
                self.state["meeting_status"] = status
            elif action == "status":
                self.state["authenticated"] = msg.get("authenticated", self.state["authenticated"])
                self.state["in_meeting"] = msg.get("in_meeting", self.state["in_meeting"])

            # Wake any futures waiting for this action
            if action in self._waiters:
                for fut in self._waiters.pop(action):
                    if not fut.done():
                        fut.set_result(msg)

            # Also put on the general queue for callers that poll
            try:
                self._response_queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

            # Log non-debug messages (skip high-frequency audio events)
            if action not in ("debug", "audio_received"):
                logger.info("[%s] %s", self.name, msg)

    async def wait_for(self, action: str, timeout: float = 30.0) -> dict:
        """Block until a response with the given action arrives."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._waiters.setdefault(action, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # Clean up the future
            if action in self._waiters:
                self._waiters[action] = [
                    f for f in self._waiters[action] if f is not fut
                ]
            raise

    @property
    def alive(self) -> bool:
        return self.process.returncode is None


class WorkerManager:
    """Spawns and manages zoom_worker subprocesses."""

    def __init__(self, env_overrides: dict[str, str] | None = None):
        self._env_overrides = env_overrides or {}
        self._workers: dict[str, WorkerProcess] = {}

    @property
    def workers(self) -> dict[str, WorkerProcess]:
        return dict(self._workers)

    @staticmethod
    async def _read_stderr(name: str, proc: asyncio.subprocess.Process) -> None:
        """Read and log stderr from a worker subprocess."""
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode().strip()
            if line:
                logger.warning("[%s stderr] %s", name, line)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def spawn_worker(self, name: str) -> WorkerProcess:
        """Start a new zoom_worker subprocess and begin reading its output.

        Args:
            name: Identifier for this worker (e.g. speaker name).

        Returns:
            A WorkerProcess ready to receive commands.
        """
        if name in self._workers and self._workers[name].alive:
            return self._workers[name]

        import os

        env = os.environ.copy()
        env.update(self._env_overrides)

        proc = await asyncio.create_subprocess_exec(
            sys.executable, _WORKER_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        wp = WorkerProcess(name=name, process=proc)
        wp._reader_task = asyncio.create_task(
            wp._read_loop(), name=f"worker-reader-{name}"
        )
        # Also read stderr for crash diagnostics
        asyncio.create_task(
            self._read_stderr(name, proc), name=f"worker-stderr-{name}"
        )

        self._workers[name] = wp
        logger.info("Spawned worker %r (pid=%s)", name, proc.pid)

        # Wait for the worker to signal readiness
        try:
            ready_msg = await wp.wait_for("ready", timeout=15.0)
            logger.info(
                "Worker %r ready: sdk_ready=%s stub=%s",
                name,
                ready_msg.get("sdk_ready"),
                ready_msg.get("stub", False),
            )
        except asyncio.TimeoutError:
            logger.warning("Worker %r did not send ready signal in time", name)

        return wp

    async def join_meeting(
        self,
        worker: WorkerProcess,
        meeting_id: str,
        password: str,
        display_name: str,
        timeout: float = 30.0,
    ) -> bool:
        """Send join command and wait for IN_MEETING status.

        Returns True if the worker reached IN_MEETING within timeout.
        """
        await worker.send_command({
            "action": "join",
            "meeting_id": meeting_id,
            "password": password,
            "name": display_name,
        })

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if worker.state.get("in_meeting"):
                return True
            try:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                msg = await worker.wait_for("meeting_status", timeout=min(remaining, 5.0))
                if msg.get("status") == "IN_MEETING":
                    return True
                if msg.get("status") in ("FAILED", "ENDED"):
                    logger.error("Worker %r join failed: %s", worker.name, msg)
                    return False
            except asyncio.TimeoutError:
                continue

        logger.error("Worker %r timed out waiting for IN_MEETING", worker.name)
        return False

    async def send_audio(self, worker: WorkerProcess, audio_data: bytes) -> None:
        """Base64-encode and send an audio chunk to the worker."""
        encoded = base64.b64encode(audio_data).decode("ascii")
        await worker.send_command({"action": "audio_chunk", "data": encoded})

    async def mute_audio(self, worker: WorkerProcess) -> None:
        await worker.send_command({"action": "mute_audio"})

    async def unmute_audio(self, worker: WorkerProcess) -> None:
        await worker.send_command({"action": "unmute_audio"})

    async def clear_audio(self, worker: WorkerProcess) -> None:
        await worker.send_command({"action": "clear_audio"})

    async def leave_meeting(self, worker: WorkerProcess) -> None:
        """Send leave command and wait for confirmation."""
        await worker.send_command({"action": "leave"})
        try:
            await worker.wait_for("leave", timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Worker %r did not confirm leave", worker.name)

    async def shutdown(self, worker: WorkerProcess) -> None:
        """Send quit command and terminate the subprocess."""
        try:
            await worker.send_command({"action": "quit"})
        except Exception:
            pass

        # Give the process a moment to exit cleanly
        try:
            await asyncio.wait_for(worker.process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            worker.process.kill()
            await worker.process.wait()

        if worker._reader_task and not worker._reader_task.done():
            worker._reader_task.cancel()

        self._workers.pop(worker.name, None)
        logger.info("Worker %r shut down", worker.name)

    async def shutdown_all(self) -> None:
        """Shut down every managed worker."""
        workers = list(self._workers.values())
        await asyncio.gather(
            *(self.shutdown(w) for w in workers),
            return_exceptions=True,
        )
