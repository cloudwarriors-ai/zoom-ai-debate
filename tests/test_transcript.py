"""Tests for the Claude transcript generator."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import Speaker, TranscriptLine
from transcript import _build_user_prompt, _parse_transcript_response, generate_transcript


class TestBuildUserPrompt:
    def test_includes_topic(self):
        prompt = _build_user_prompt(
            "AI regulation",
            4,
            Speaker(name="Alice", perspective="pro"),
            Speaker(name="Bob", perspective="con"),
        )
        assert "AI regulation" in prompt

    def test_includes_speakers(self):
        prompt = _build_user_prompt(
            "Topic",
            3,
            Speaker(name="Alice", perspective="pro-regulation"),
            Speaker(name="Bob", perspective="free-market"),
        )
        assert "Alice" in prompt
        assert "Bob" in prompt
        assert "pro-regulation" in prompt
        assert "free-market" in prompt

    def test_line_count(self):
        prompt = _build_user_prompt(
            "Topic",
            5,
            Speaker(name="A"),
            Speaker(name="B"),
        )
        assert "10 lines" in prompt


class TestParseTranscriptResponse:
    def _speakers(self):
        return Speaker(name="Alice"), Speaker(name="Bob")

    def test_valid_json(self):
        a, b = self._speakers()
        raw = json.dumps([
            {"speaker": "Alice", "text": "First point."},
            {"speaker": "Bob", "text": "Counterpoint."},
        ])
        result = _parse_transcript_response(raw, a, b, 2)
        assert len(result) == 2
        assert result[0].speaker == "Alice"
        assert result[1].speaker == "Bob"

    def test_strips_markdown_fences(self):
        a, b = self._speakers()
        raw = '```json\n[{"speaker": "Alice", "text": "Hi."}, {"speaker": "Bob", "text": "Hello."}]\n```'
        result = _parse_transcript_response(raw, a, b, 2)
        assert len(result) == 2

    def test_rejects_unknown_speaker(self):
        a, b = self._speakers()
        raw = json.dumps([{"speaker": "Charlie", "text": "Who am I?"}])
        with pytest.raises(ValueError, match="unknown speaker"):
            _parse_transcript_response(raw, a, b, 1)

    def test_rejects_empty_text(self):
        a, b = self._speakers()
        raw = json.dumps([{"speaker": "Alice", "text": ""}])
        with pytest.raises(ValueError, match="empty text"):
            _parse_transcript_response(raw, a, b, 1)

    def test_warns_on_wrong_count(self, caplog):
        a, b = self._speakers()
        raw = json.dumps([{"speaker": "Alice", "text": "Only one."}])
        import logging
        with caplog.at_level(logging.WARNING):
            result = _parse_transcript_response(raw, a, b, 4)
        assert len(result) == 1


class TestGenerateTranscript:
    @pytest.mark.asyncio
    async def test_calls_claude_cli_and_parses(self):
        cli_output = json.dumps([
            {"speaker": "Alex", "text": "I think AI needs regulation."},
            {"speaker": "Jordan", "text": "But innovation requires freedom."},
            {"speaker": "Alex", "text": "Freedom without guardrails is dangerous."},
            {"speaker": "Jordan", "text": "Guardrails shouldn't strangle progress."},
        ])

        with patch("transcript._run_claude_cli", new_callable=AsyncMock, return_value=cli_output) as mock_cli:
            result = await generate_transcript(
                topic="AI regulation",
                num_turns=2,
                speaker_a=Speaker(name="Alex", perspective="pro"),
                speaker_b=Speaker(name="Jordan", perspective="con"),
            )

        assert len(result) == 4
        assert result[0].speaker == "Alex"
        assert result[1].speaker == "Jordan"
        mock_cli.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_on_parse_error(self):
        with patch(
            "transcript._run_claude_cli",
            new_callable=AsyncMock,
            side_effect=["not json", json.dumps([
                {"speaker": "A", "text": "Line 1."},
                {"speaker": "B", "text": "Line 2."},
            ])],
        ) as mock_cli:
            result = await generate_transcript(
                topic="Test",
                num_turns=1,
                speaker_a=Speaker(name="A"),
                speaker_b=Speaker(name="B"),
            )

        assert len(result) == 2
        assert mock_cli.call_count == 2
