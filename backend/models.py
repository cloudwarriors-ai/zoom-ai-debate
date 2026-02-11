"""Data models for the AI debate application."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ConversationStatus(str, Enum):
    GENERATING = "generating"
    READY = "ready"
    JOINING = "joining"
    PERFORMING = "performing"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


class Speaker(BaseModel):
    name: str = "Speaker"
    voice: str = "alloy"
    perspective: str = ""


class TranscriptLine(BaseModel):
    speaker: str
    text: str


class WorkerStatus(BaseModel):
    name: str
    voice: str
    status: str = "idle"


class ConversationProgress(BaseModel):
    current_line: int = 0
    total_lines: int = 0
    current_speaker: str = ""


class CreateConversationRequest(BaseModel):
    topic: str
    num_turns: int = Field(default=6, ge=1, le=50)
    speaker_a: Speaker = Field(default_factory=lambda: Speaker(name="Alex", voice="alloy"))
    speaker_b: Speaker = Field(default_factory=lambda: Speaker(name="Jordan", voice="echo"))


class PerformRequest(BaseModel):
    meeting_url: str


class Conversation(BaseModel):
    id: str = Field(default_factory=lambda: f"conv_{uuid.uuid4().hex[:12]}")
    topic: str
    status: ConversationStatus = ConversationStatus.GENERATING
    speakers: list[Speaker] = Field(default_factory=list)
    transcript: list[TranscriptLine] = Field(default_factory=list)
    progress: ConversationProgress = Field(default_factory=ConversationProgress)
    workers: list[WorkerStatus] = Field(default_factory=list)
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def parse_meeting_url(url: str) -> tuple[str, str]:
    """Extract meeting_id and password from a Zoom meeting URL.

    Handles formats:
        https://zoom.us/j/12345678901?pwd=abcdef
        https://company.zoom.us/j/12345678901?pwd=abcdef
    """
    import re
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(url)
    path = parsed.path

    # Extract meeting ID from path
    match = re.search(r"/j/(\d+)", path)
    if not match:
        raise ValueError(f"Could not extract meeting ID from URL: {url}")

    meeting_id = match.group(1)

    # Extract password from query params
    params = parse_qs(parsed.query)
    password = params.get("pwd", [""])[0]

    return meeting_id, password
