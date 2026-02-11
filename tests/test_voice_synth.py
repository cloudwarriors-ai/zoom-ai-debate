"""Tests for the OpenAI Realtime voice synthesizer."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from voice_synth import VoiceSynthesizer, VoiceSynthError, resample_audio


class TestResampleAudio:
    def test_same_rate_passthrough(self):
        data = np.zeros(100, dtype=np.int16).tobytes()
        assert resample_audio(data, 24000, 24000) == data

    def test_upsample(self):
        samples_in = np.zeros(24000, dtype=np.int16)
        data_in = samples_in.tobytes()
        data_out = resample_audio(data_in, 24000, 32000)
        # 32000 samples * 2 bytes = 64000
        assert len(data_out) == 64000

    def test_downsample(self):
        samples_in = np.zeros(32000, dtype=np.int16)
        data_in = samples_in.tobytes()
        data_out = resample_audio(data_in, 32000, 24000)
        assert len(data_out) == 48000  # 24000 * 2

    def test_preserves_signal(self):
        # Create a simple ramp signal
        samples = np.arange(0, 1000, dtype=np.int16)
        data_in = samples.tobytes()
        data_out = resample_audio(data_in, 24000, 32000)
        out_samples = np.frombuffer(data_out, dtype=np.int16)
        # Output should still be monotonically increasing
        assert np.all(np.diff(out_samples) >= 0)

    def test_empty_input(self):
        assert resample_audio(b"", 24000, 32000) == b""

    def test_single_sample(self):
        data = np.array([1000], dtype=np.int16).tobytes()
        result = resample_audio(data, 24000, 32000)
        # With single sample, interpolation gives 1 output sample
        assert len(result) == 2  # 1 sample * 2 bytes


class TestVoiceSynthesizer:
    def test_init_defaults(self):
        synth = VoiceSynthesizer(api_key="test-key")
        assert synth.voice == "alloy"
        assert not synth._connected

    def test_init_custom_voice(self):
        synth = VoiceSynthesizer(api_key="test-key", voice="echo")
        assert synth.voice == "echo"

    @pytest.mark.asyncio
    async def test_synthesize_not_connected_raises(self):
        synth = VoiceSynthesizer(api_key="test-key")
        with pytest.raises(VoiceSynthError, match="Not connected"):
            await synth.synthesize("hello")

    @pytest.mark.asyncio
    async def test_synthesize_collects_audio(self):
        """Mock the WebSocket to simulate a synthesis flow."""
        synth = VoiceSynthesizer(api_key="test-key")

        # Simulate audio data
        audio_chunk = np.zeros(480, dtype=np.int16).tobytes()
        audio_b64 = base64.b64encode(audio_chunk).decode()

        messages = [
            json.dumps({"type": "session.created", "session": {"id": "sess_123"}}),
            json.dumps({"type": "session.updated"}),
            # After connect, synthesize will send response.create and read:
            json.dumps({"type": "response.created"}),
            json.dumps({"type": "response.output_item.added"}),
            json.dumps({"type": "response.content_part.added"}),
            json.dumps({"type": "response.audio.delta", "delta": audio_b64}),
            json.dumps({"type": "response.audio.delta", "delta": audio_b64}),
            json.dumps({"type": "response.audio.done"}),
            json.dumps({"type": "response.output_item.done"}),
            json.dumps({"type": "response.content_part.done"}),
            json.dumps({"type": "response.done"}),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()

        mock_connect = AsyncMock(return_value=mock_ws)
        with patch("voice_synth.websockets.connect", mock_connect):
            await synth.connect()
            assert synth._connected

            result = await synth.synthesize("Test speech")

            # Should have collected 2 audio chunks
            assert len(result) == len(audio_chunk) * 2
            await synth.disconnect()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=[
            json.dumps({"type": "session.created", "session": {"id": "s1"}}),
            json.dumps({"type": "session.updated"}),
        ])
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()

        mock_connect = AsyncMock(return_value=mock_ws)
        with patch("voice_synth.websockets.connect", mock_connect):
            async with VoiceSynthesizer(api_key="test") as synth:
                assert synth._connected
            # After exiting, should be disconnected
            assert not synth._connected
