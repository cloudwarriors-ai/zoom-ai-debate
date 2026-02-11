"""OpenAI Realtime API voice synthesizer.

Converts text to PCM16 audio via WebSocket connection to OpenAI's
Realtime API. Each call to synthesize() sends text and collects
the streamed audio chunks into a complete PCM16 byte buffer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import TracebackType
from typing import Optional

import numpy as np
import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

SYNTHESIS_TIMEOUT_SEC = 30
CONNECT_TIMEOUT_SEC = 10


class VoiceSynthError(Exception):
    """Raised when voice synthesis fails."""


class VoiceSynthesizer:
    """Text-to-speech via OpenAI Realtime API WebSocket.

    Usage::

        async with VoiceSynthesizer(api_key="sk-...") as synth:
            pcm_bytes = await synth.synthesize("Hello world")
    """

    REALTIME_API_URL = "wss://api.openai.com/v1/realtime"
    MODEL = "gpt-4o-realtime-preview-2024-12-17"

    def __init__(
        self,
        api_key: str,
        voice: str = "alloy",
        *,
        model: str | None = None,
        url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.voice = voice
        self.model = model or self.MODEL
        self.url = url or self.REALTIME_API_URL
        self._ws: Optional[ClientConnection] = None
        self._connected = False

    # -- Lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Open WebSocket and configure the realtime session."""
        if self._connected and self._ws is not None:
            return

        ws_url = f"{self.url}?model={self.model}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(ws_url, additional_headers=headers),
                timeout=CONNECT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as exc:
            raise VoiceSynthError("Timed out connecting to OpenAI Realtime API") from exc
        except Exception as exc:
            raise VoiceSynthError(f"Failed to connect to OpenAI Realtime API: {exc}") from exc

        # Wait for session.created
        raw = await self._ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "session.created":
            raise VoiceSynthError(f"Unexpected initial message: {msg.get('type')}")

        logger.info("Connected to OpenAI Realtime API, session=%s", msg.get("session", {}).get("id"))

        # Configure session for text-in, audio-out
        await self._send({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self.voice,
                "output_audio_format": "pcm16",
                "input_audio_format": "pcm16",
                "turn_detection": None,
            },
        })

        # Wait for session.updated confirmation
        raw = await self._ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "session.updated":
            raise VoiceSynthError(f"Expected session.updated, got: {msg.get('type')}")

        self._connected = True
        logger.info("Session configured: voice=%s, format=pcm16", self.voice)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass  # Best-effort close
            finally:
                self._ws = None
                self._connected = False

    async def __aenter__(self) -> VoiceSynthesizer:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    # -- Synthesis --------------------------------------------------------

    async def synthesize(self, text: str) -> bytes:
        """Convert text to PCM16 audio bytes (24kHz mono, 16-bit LE).

        Sends text to the Realtime API, collects all audio delta chunks,
        and returns the concatenated PCM16 byte buffer.

        Raises VoiceSynthError on failure or timeout.
        """
        if not self._connected or self._ws is None:
            raise VoiceSynthError("Not connected. Call connect() first.")

        logger.debug("Synthesizing %d chars: %.50s...", len(text), text)

        # Request speech generation
        await self._send({
            "type": "response.create",
            "response": {
                "modalities": ["audio", "text"],
                "instructions": f"Say exactly the following text naturally: {text}",
            },
        })

        # Collect audio chunks until response.done
        audio_chunks: list[bytes] = []
        done_event = asyncio.Event()
        error_msg: str | None = None

        try:
            deadline = asyncio.get_event_loop().time() + SYNTHESIS_TIMEOUT_SEC
            while not done_event.is_set():
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                if msg_type == "response.audio.delta":
                    chunk = base64.b64decode(msg["delta"])
                    audio_chunks.append(chunk)

                elif msg_type == "response.done":
                    done_event.set()

                elif msg_type == "response.audio.done":
                    pass

                elif msg_type == "response.text.delta":
                    pass

                elif msg_type == "response.text.done":
                    pass

                elif msg_type in (
                    "response.created",
                    "response.output_item.added",
                    "response.output_item.done",
                    "response.content_part.added",
                    "response.content_part.done",
                    "rate_limits.updated",
                ):
                    pass

                elif msg_type == "error":
                    error_detail = msg.get("error", {})
                    error_msg = error_detail.get("message", str(error_detail))
                    done_event.set()

                else:
                    logger.debug("Unhandled message type: %s", msg_type)

        except (TimeoutError, asyncio.TimeoutError):
            raise VoiceSynthError(
                f"Synthesis timed out after {SYNTHESIS_TIMEOUT_SEC}s for text: {text[:80]}"
            )
        except websockets.exceptions.ConnectionClosed as exc:
            self._connected = False
            self._ws = None
            raise VoiceSynthError(f"WebSocket closed during synthesis: {exc}") from exc

        if error_msg:
            raise VoiceSynthError(f"OpenAI Realtime API error: {error_msg}")

        if not audio_chunks:
            raise VoiceSynthError("No audio data received from OpenAI Realtime API")

        result = b"".join(audio_chunks)
        logger.info(
            "Synthesized %d bytes of PCM16 audio (%.1fs at 24kHz)",
            len(result),
            len(result) / (24000 * 2),  # 2 bytes per sample
        )
        return result

    # -- Internal ---------------------------------------------------------

    async def _send(self, message: dict) -> None:
        """Send a JSON message over the WebSocket."""
        if self._ws is None:
            raise VoiceSynthError("WebSocket not connected")
        await self._ws.send(json.dumps(message))


def resample_audio(audio_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample PCM16 audio from one sample rate to another.

    Uses linear interpolation via numpy. Input and output are raw
    PCM16 bytes (16-bit signed little-endian, mono).

    Args:
        audio_bytes: Raw PCM16 audio data.
        from_rate: Source sample rate in Hz (e.g. 24000).
        to_rate: Target sample rate in Hz (e.g. 32000).

    Returns:
        Resampled PCM16 audio bytes.
    """
    if from_rate == to_rate:
        return audio_bytes

    if len(audio_bytes) < 2:
        return audio_bytes

    # Decode PCM16 (signed 16-bit little-endian)
    samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float64)
    num_samples = len(samples)

    # Calculate new length
    new_num_samples = int(num_samples * to_rate / from_rate)
    if new_num_samples == 0:
        return b""

    # Linear interpolation
    old_indices = np.linspace(0, num_samples - 1, new_num_samples)
    resampled = np.interp(old_indices, np.arange(num_samples), samples)

    # Clip and convert back to int16
    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
    return resampled.tobytes()
