"""FastAPI server for the AI Debate application."""

import logging

import uvicorn
from fastapi import FastAPI, HTTPException

from config import settings
from models import (
    Conversation,
    ConversationStatus,
    PerformRequest,
    UploadTranscriptRequest,
)
from orchestrator import ConversationOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Zoom AI Debate", version="0.2.0")

_conversations: dict[str, Conversation] = {}
_orchestrators: dict[str, ConversationOrchestrator] = {}

TRANSCRIPT_FORMAT = {
    "description": "Upload a debate transcript for two speakers to perform in a Zoom meeting.",
    "schema": {
        "topic": "string — the debate topic",
        "speakers": [
            {"name": "string — display name in meeting", "voice": "string — OpenAI voice id", "perspective": "string — optional, their stance"},
        ],
        "transcript": [
            {"speaker": "string — must match a speaker name", "text": "string — the line to speak"},
        ],
    },
    "available_voices": settings.available_voices,
    "example": {
        "topic": "Should AI be regulated?",
        "speakers": [
            {"name": "Alex", "voice": "ash", "perspective": "pro-regulation"},
            {"name": "Jordan", "voice": "coral", "perspective": "industry self-regulation"},
        ],
        "transcript": [
            {"speaker": "Alex", "text": "We need government oversight of AI systems."},
            {"speaker": "Jordan", "text": "Innovation requires freedom, not bureaucracy."},
            {"speaker": "Alex", "text": "Freedom without guardrails is dangerous."},
            {"speaker": "Jordan", "text": "Bad regulation is worse than no regulation."},
        ],
    },
}


@app.get("/api/health")
async def health():
    return {"status": "ok", "conversations": len(_conversations)}


@app.get("/api/format")
async def get_format():
    """Return the expected transcript format so any LLM can generate it."""
    return TRANSCRIPT_FORMAT


@app.get("/api/conversations")
async def list_conversations():
    return [
        {"id": c.id, "topic": c.topic, "status": c.status}
        for c in _conversations.values()
    ]


@app.post("/api/conversations", status_code=201)
async def create_conversation(req: UploadTranscriptRequest):
    """Upload a pre-generated transcript."""
    # Validate speaker names in transcript match declared speakers
    valid_names = {s.name for s in req.speakers}
    for i, line in enumerate(req.transcript):
        if line.speaker not in valid_names:
            raise HTTPException(
                status_code=422,
                detail=f"Line {i}: speaker '{line.speaker}' not in {valid_names}",
            )

    conversation = Conversation(
        topic=req.topic,
        speakers=req.speakers,
        transcript=req.transcript,
        status=ConversationStatus.READY,
    )
    _conversations[conversation.id] = conversation

    logger.info(
        "Uploaded conversation %s: %d lines on '%s'",
        conversation.id,
        len(req.transcript),
        req.topic,
    )
    return conversation


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    conversation = _conversations.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.post("/api/conversations/{conversation_id}/perform", status_code=202)
async def perform_conversation(conversation_id: str, req: PerformRequest):
    """Start performing the conversation in a Zoom meeting."""
    conversation = _conversations.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation.status not in (ConversationStatus.READY, ConversationStatus.STOPPED):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot perform conversation in '{conversation.status}' state",
        )

    orchestrator = ConversationOrchestrator(conversation)
    _orchestrators[conversation_id] = orchestrator

    await orchestrator.start(req.meeting_url)
    logger.info("Started performance of %s in %s", conversation_id, req.meeting_url)

    return conversation


@app.post("/api/conversations/{conversation_id}/stop")
async def stop_conversation(conversation_id: str):
    """Stop an in-progress performance."""
    conversation = _conversations.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    orchestrator = _orchestrators.get(conversation_id)
    if orchestrator:
        await orchestrator.stop()
        del _orchestrators[conversation_id]
        logger.info("Stopped performance of %s", conversation_id)

    return conversation


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
