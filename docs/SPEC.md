# Zoom AI Debate — Specification

## Overview

An API-driven application that synthesizes a conversation between two AI personas on any topic, then performs that conversation live in a Zoom meeting using distinct AI voices. Claude generates the dialogue transcript; OpenAI Realtime API synthesizes each line into spoken audio; two Zoom SDK workers join the meeting and speak their lines in turn.

## Data Flow

```
User Request (topic, meeting_url)
        │
        ▼
┌─────────────────────┐
│  Claude API          │  Generate structured transcript
│  (Anthropic)         │  Two speakers, back-and-forth dialogue
└─────────┬───────────┘
          │ JSON transcript
          ▼
┌─────────────────────┐
│  Orchestrator        │  Coordinates turn-taking
└──────┬───────┬──────┘
       │       │
       ▼       ▼
┌──────────┐ ┌──────────┐
│ Worker A │ │ Worker B │   Two Zoom SDK subprocesses
│ Voice 1  │ │ Voice 2  │   Each joins meeting independently
└────┬─────┘ └────┬─────┘
     │             │
     ▼             ▼
┌──────────────────────┐
│  OpenAI Realtime API │   TTS per line (different voices)
│  (2 connections)     │   Returns PCM16 audio
└──────────┬───────────┘
           │ audio chunks
           ▼
┌──────────────────────┐
│  Zoom Meeting        │   Both workers send audio via SDK
│  (participants hear) │   virtual microphone
└──────────────────────┘
```

## API Contracts

### POST /api/conversations
Create a new conversation transcript.

**Request:**
```json
{
  "topic": "Should AI be regulated?",
  "num_turns": 6,
  "speaker_a": {"name": "Alex", "voice": "alloy", "perspective": "pro-regulation"},
  "speaker_b": {"name": "Jordan", "voice": "echo", "perspective": "free-market"}
}
```

**Response (201):**
```json
{
  "id": "conv_abc123",
  "topic": "Should AI be regulated?",
  "status": "ready",
  "speakers": [
    {"name": "Alex", "voice": "alloy"},
    {"name": "Jordan", "voice": "echo"}
  ],
  "transcript": [
    {"speaker": "Alex", "text": "I believe we need thoughtful regulation..."},
    {"speaker": "Jordan", "text": "While I understand the concern..."}
  ],
  "created_at": "2026-02-10T12:00:00Z"
}
```

### POST /api/conversations/{id}/perform
Start performing the conversation in a Zoom meeting.

**Request:**
```json
{
  "meeting_url": "https://zoom.us/j/123456789?pwd=xxxx"
}
```

**Response (202):**
```json
{
  "id": "conv_abc123",
  "status": "performing",
  "workers": [
    {"name": "Alex", "status": "joining"},
    {"name": "Jordan", "status": "joining"}
  ]
}
```

### GET /api/conversations/{id}
Get conversation status and details.

**Response (200):**
```json
{
  "id": "conv_abc123",
  "topic": "Should AI be regulated?",
  "status": "performing",
  "progress": {
    "current_line": 3,
    "total_lines": 12,
    "current_speaker": "Jordan"
  },
  "workers": [
    {"name": "Alex", "status": "in_meeting", "voice": "alloy"},
    {"name": "Jordan", "status": "speaking", "voice": "echo"}
  ]
}
```

### POST /api/conversations/{id}/stop
Stop an in-progress performance. Workers leave the meeting.

**Response (200):**
```json
{
  "id": "conv_abc123",
  "status": "stopped"
}
```

### GET /api/conversations
List all conversations.

### GET /api/health
Health check.

## Conversation Statuses

- `generating` — Claude is creating the transcript
- `ready` — Transcript created, waiting to perform
- `joining` — Workers are joining the Zoom meeting
- `performing` — Conversation is being spoken in the meeting
- `completed` — All lines have been spoken
- `stopped` — Manually stopped
- `error` — Something went wrong

## Audio Pipeline

```
OpenAI Realtime API (per worker)
  ├── Voice A: "alloy" (24kHz PCM16)
  └── Voice B: "echo"  (24kHz PCM16)
         │
         ▼
    Resample 24kHz → 32kHz
         │
         ▼
    Zoom SDK AudioSource
    (virtual microphone, 20ms chunks)
         │
         ▼
    Meeting participants hear the voice
```

Each worker maintains its own OpenAI Realtime WebSocket connection configured with a different voice. Lines are spoken by sending text via `response.create` with instructions to speak the exact text.

## Zoom Worker Protocol

Adapted from bighead's `zoom_worker.py`. Each worker is a subprocess communicating via stdin/stdout JSON:

**Commands (parent → worker):**
- `{"action": "join", "meeting_id": "...", "password": "...", "name": "Alex"}`
- `{"action": "audio_chunk", "data": "<base64 PCM16 @ 32kHz>"}`
- `{"action": "leave"}`
- `{"action": "quit"}`

**Responses (worker → parent):**
- `{"action": "ready", "sdk_ready": true}`
- `{"action": "meeting_status", "status": "IN_MEETING"}`
- `{"action": "mic_start"}`
- `{"action": "leave", "success": true}`

## Orchestrator Logic

```
1. Parse meeting URL → meeting_id + password
2. Spawn Worker A subprocess → join meeting as speaker_a.name
3. Spawn Worker B subprocess → join meeting as speaker_b.name
4. Wait for both workers: status == IN_MEETING
5. Brief pause (2s) for meeting to settle
6. For each line in transcript:
   a. Determine which worker speaks (by speaker name)
   b. Connect to OpenAI Realtime API with that worker's voice
   c. Send text → receive audio chunks
   d. Forward audio chunks to the correct zoom_worker
   e. Wait for audio playback to complete
   f. Brief pause between lines (0.5-1s)
7. After all lines spoken, leave meeting
8. Cleanup: disconnect OpenAI, quit workers
```

## File Structure

```
zoom-ai-debate/
├── backend/
│   ├── main.py              # FastAPI server + API endpoints
│   ├── config.py            # Settings (env vars, defaults)
│   ├── models.py            # Pydantic models (Conversation, Speaker, Line)
│   ├── transcript.py        # Claude API transcript generator
│   ├── voice_synth.py       # OpenAI Realtime TTS wrapper
│   ├── zoom_worker.py       # Zoom SDK worker subprocess (from bighead)
│   ├── worker_manager.py    # Spawns/manages two workers
│   ├── orchestrator.py      # Coordinates the full performance
│   └── requirements.txt
├── tests/
│   ├── conftest.py
│   ├── test_transcript.py
│   ├── test_voice_synth.py
│   ├── test_orchestrator.py
│   └── test_api.py
├── docs/
│   └── SPEC.md
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Environment Variables

```
ANTHROPIC_API_KEY=       # Claude API for transcript generation
OPENAI_API_KEY=          # OpenAI Realtime API for voice synthesis
ZOOM_CLIENT_ID=          # Zoom SDK OAuth credentials
ZOOM_CLIENT_SECRET=      # Zoom SDK OAuth credentials
REDIS_URL=redis://localhost:6379
```

## Acceptance Criteria

1. POST /api/conversations with a topic returns a coherent, multi-turn dialogue transcript
2. POST /api/conversations/{id}/perform joins two bots to a Zoom meeting
3. Each bot speaks with a distinct OpenAI voice
4. Lines are spoken in order with natural pacing
5. GET /api/conversations/{id} shows real-time progress
6. POST /api/conversations/{id}/stop cleanly exits both bots
7. All business logic has unit tests
8. API endpoints have integration tests
