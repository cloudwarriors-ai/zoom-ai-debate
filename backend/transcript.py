"""Claude-powered transcript generator for AI debates."""

import json
import logging

import anthropic

from config import settings
from models import Speaker, TranscriptLine

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a script writer for a live debate/conversation between two speakers.
Generate a natural, engaging dialogue where each speaker defends their perspective.

Rules:
- Alternate strictly between the two speakers, starting with Speaker A.
- Each line must be 1-3 sentences, suitable for spoken delivery (concise, punchy).
- The conversation should flow naturally: speakers react to each other, not just monologue.
- Include occasional rhetorical devices: questions, analogies, concessions then rebuttals.
- Avoid filler phrases like "That's a great point" repeatedly. Vary transitions.
- Stay on topic but let the conversation evolve organically.

Return ONLY a JSON array of objects with "speaker" and "text" keys.
No markdown fences, no commentary, just the raw JSON array.
Example format:
[{"speaker": "Alice", "text": "..."}, {"speaker": "Bob", "text": "..."}]
"""


def _build_user_prompt(
    topic: str, num_turns: int, speaker_a: Speaker, speaker_b: Speaker
) -> str:
    total_lines = num_turns * 2
    return (
        f"Topic: {topic}\n\n"
        f"Speaker A — {speaker_a.name}: {speaker_a.perspective}\n"
        f"Speaker B — {speaker_b.name}: {speaker_b.perspective}\n\n"
        f"Generate exactly {total_lines} lines of dialogue "
        f"({num_turns} lines per speaker, strictly alternating, "
        f"starting with {speaker_a.name})."
    )


def _parse_transcript_response(
    raw: str, speaker_a: Speaker, speaker_b: Speaker, expected_lines: int
) -> list[TranscriptLine]:
    """Parse Claude's JSON response into TranscriptLine objects.

    Validates speaker names and line count. Raises ValueError on
    malformed output so the caller can decide how to handle it.
    """
    # Strip markdown fences if Claude wraps them despite instructions
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()

    lines = json.loads(text)
    if not isinstance(lines, list):
        raise ValueError(f"Expected JSON array, got {type(lines).__name__}")

    valid_names = {speaker_a.name, speaker_b.name}
    result: list[TranscriptLine] = []

    for i, entry in enumerate(lines):
        if not isinstance(entry, dict):
            raise ValueError(f"Line {i}: expected object, got {type(entry).__name__}")
        speaker = entry.get("speaker", "")
        text_val = entry.get("text", "")
        if speaker not in valid_names:
            raise ValueError(
                f"Line {i}: unknown speaker '{speaker}', expected one of {valid_names}"
            )
        if not text_val:
            raise ValueError(f"Line {i}: empty text")
        result.append(TranscriptLine(speaker=speaker, text=text_val))

    if len(result) != expected_lines:
        logger.warning(
            "Expected %d lines, got %d. Using what we have.", expected_lines, len(result)
        )

    return result


async def generate_transcript(
    topic: str, num_turns: int, speaker_a: Speaker, speaker_b: Speaker
) -> list[TranscriptLine]:
    """Generate a debate transcript using Claude.

    Args:
        topic: The debate topic.
        num_turns: Number of lines per speaker.
        speaker_a: First speaker (goes first).
        speaker_b: Second speaker.

    Returns:
        Alternating list of TranscriptLine objects.

    Raises:
        anthropic.APIError: On API communication failure.
        ValueError: On unparseable response after retries.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    user_prompt = _build_user_prompt(topic, num_turns, speaker_a, speaker_b)
    expected_lines = num_turns * 2

    logger.info(
        "Generating transcript: topic=%r, turns=%d, speakers=%s/%s",
        topic, num_turns, speaker_a.name, speaker_b.name,
    )

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw_text = response.content[0].text
            logger.debug("Claude response (attempt %d): %s", attempt + 1, raw_text[:200])

            transcript = _parse_transcript_response(
                raw_text, speaker_a, speaker_b, expected_lines
            )
            logger.info("Transcript generated: %d lines", len(transcript))
            return transcript

        except (json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
            last_error = exc
            logger.warning("Parse error on attempt %d: %s", attempt + 1, exc)
            # Retry once with a nudge
            continue

        except anthropic.APIError:
            logger.exception("Anthropic API error during transcript generation")
            raise

    raise ValueError(f"Failed to parse transcript after 2 attempts: {last_error}")
