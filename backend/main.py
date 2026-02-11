"""FastAPI server for the AI Debate application."""

import asyncio
import logging

import uvicorn
from fastapi import FastAPI, HTTPException

from config import settings
from models import (
    Conversation,
    ConversationStatus,
    CreateConversationRequest,
    PerformRequest,
)
from orchestrator import ConversationOrchestrator
from transcript import generate_transcript

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Zoom AI Debate", version="0.1.0")

# In-memory conversation store
_conversations: dict[str, Conversation] = {}
_orchestrators: dict[str, ConversationOrchestrator] = {}


@app.get("/api/health")
async def health():
    return {"status": "ok", "conversations": len(_conversations)}


@app.get("/api/conversations")
async def list_conversations():
    return [
        {"id": c.id, "topic": c.topic, "status": c.status}
        for c in _conversations.values()
    ]


@app.post("/api/conversations", status_code=201)
async def create_conversation(req: CreateConversationRequest):
    """Generate a conversation transcript using Claude."""
    conversation = Conversation(
        topic=req.topic,
        speakers=[req.speaker_a, req.speaker_b],
    )
    _conversations[conversation.id] = conversation

    try:
        transcript = await generate_transcript(
            topic=req.topic,
            num_turns=req.num_turns,
            speaker_a=req.speaker_a,
            speaker_b=req.speaker_b,
        )
        conversation.transcript = transcript
        conversation.status = ConversationStatus.READY
        logger.info(
            "Created conversation %s: %d lines on '%s'",
            conversation.id,
            len(transcript),
            req.topic,
        )
    except Exception as e:
        conversation.status = ConversationStatus.ERROR
        conversation.error = str(e)
        logger.error("Failed to generate transcript: %s", e)
        raise HTTPException(status_code=500, detail=f"Transcript generation failed: {e}")

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
