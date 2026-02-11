"""Transcript generator for AI debates.

Supports multiple backends:
- Claude CLI (default for local dev — no API key needed)
- Anthropic API (set ANTHROPIC_API_KEY)
- OpenAI API (set OPENAI_API_KEY + LLM_BACKEND=openai)
- Any OpenAI-compatible API (set LLM_BASE_URL + LLM_API_KEY)
"""

import asyncio
import json
import logging
import os
import shutil

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
    """Parse LLM JSON response into TranscriptLine objects."""
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


# ---------- Backend: Claude CLI ----------

CLAUDE_CLI_TIMEOUT_SEC = 120


def _find_claude_cli() -> str:
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("claude CLI not found on PATH")
    return path


async def _run_claude_cli(system_prompt: str, user_prompt: str) -> str:
    claude_bin = _find_claude_cli()
    cmd = [
        claude_bin,
        "--print",
        "--system-prompt", system_prompt,
        "--model", "sonnet",
        "--output-format", "text",
        "--allowedTools", "",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=user_prompt.encode()),
            timeout=CLAUDE_CLI_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"claude CLI timed out after {CLAUDE_CLI_TIMEOUT_SEC}s")

    if proc.returncode != 0:
        err_msg = stderr.decode().strip()
        raise RuntimeError(f"claude CLI exited with code {proc.returncode}: {err_msg}")

    return stdout.decode()


# ---------- Backend: Anthropic API ----------

async def _run_anthropic_api(system_prompt: str, user_prompt: str) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = await client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text


# ---------- Backend: OpenAI-compatible API ----------

async def _run_openai_api(system_prompt: str, user_prompt: str) -> str:
    import openai

    kwargs = {}
    base_url = os.getenv("LLM_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url

    client = openai.AsyncOpenAI(
        api_key=os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY")),
        **kwargs,
    )
    response = await client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4096,
    )
    return response.choices[0].message.content


# ---------- Backend selection ----------

def _get_backend():
    """Pick the LLM backend based on environment."""
    backend = os.getenv("LLM_BACKEND", "").lower()

    if backend == "anthropic":
        return _run_anthropic_api
    if backend == "openai":
        return _run_openai_api

    # Auto-detect from env vars
    if os.getenv("ANTHROPIC_API_KEY"):
        return _run_anthropic_api
    if os.getenv("LLM_BASE_URL"):
        return _run_openai_api

    # Default: Claude CLI
    return _run_claude_cli


# ---------- Public API ----------

async def generate_transcript(
    topic: str, num_turns: int, speaker_a: Speaker, speaker_b: Speaker
) -> list[TranscriptLine]:
    """Generate a debate transcript using the configured LLM backend.

    Backend selection (in order):
    1. LLM_BACKEND env var (explicit: "anthropic", "openai", "cli")
    2. ANTHROPIC_API_KEY set → Anthropic API
    3. LLM_BASE_URL set → OpenAI-compatible API
    4. Default → Claude CLI
    """
    run_llm = _get_backend()
    backend_name = run_llm.__name__
    user_prompt = _build_user_prompt(topic, num_turns, speaker_a, speaker_b)
    expected_lines = num_turns * 2

    logger.info(
        "Generating transcript via %s: topic=%r, turns=%d, speakers=%s/%s",
        backend_name, topic, num_turns, speaker_a.name, speaker_b.name,
    )

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw_text = await run_llm(SYSTEM_PROMPT, user_prompt)
            logger.debug("LLM response (attempt %d): %s", attempt + 1, raw_text[:200])

            transcript = _parse_transcript_response(
                raw_text, speaker_a, speaker_b, expected_lines
            )
            logger.info("Transcript generated: %d lines", len(transcript))
            return transcript

        except (json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
            last_error = exc
            logger.warning("Parse error on attempt %d: %s", attempt + 1, exc)
            continue

    raise ValueError(f"Failed to parse transcript after 2 attempts: {last_error}")
