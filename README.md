# Zoom AI Debate

Two AI bots join a Zoom meeting and have a spoken conversation using distinct voices. You provide a transcript, the system synthesizes speech via OpenAI's Realtime API, and two Zoom SDK bot workers perform the dialogue live in the meeting.

## How It Works

1. **Upload a transcript** — `POST /api/conversations` with a topic, two speakers (each with a voice), and transcript lines
2. **Perform in a meeting** — `POST /api/conversations/{id}/perform` with a Zoom meeting URL
3. Two bot workers join the meeting, unmute, and speak the transcript line-by-line using OpenAI Realtime TTS
4. Workers leave when the conversation is complete

## Architecture

```
External LLM (generates transcript)
        |
        v
  REST API (FastAPI)
        |
        v
  Orchestrator
   /         \
Worker A    Worker B     (Zoom SDK subprocess per speaker)
   |            |
Voice Synth  Voice Synth  (OpenAI Realtime API WebSocket)
   |            |
   v            v
     Zoom Meeting        (live audio via virtual mic)
```

- **Voice synthesis**: OpenAI Realtime API (PCM16 24kHz) resampled to 32kHz for Zoom
- **Zoom SDK**: `zoom-meeting-sdk` Python bindings, virtual mic via `setExternalAudioSource`
- **Audio routing**: PulseAudio null sink in Docker container
- **Platform**: x86_64 Linux only (Ubuntu 22.04) — runs under QEMU on ARM Macs

## Required Environment Variables

Create a `.env` file in the project root:

```env
# Zoom SDK credentials (required)
ZOOM_CLIENT_ID=your_zoom_client_id
ZOOM_CLIENT_SECRET=your_zoom_client_secret

# OpenAI API key (required — for Realtime TTS)
OPENAI_API_KEY=sk-your-openai-api-key

# OpenAI Realtime model (optional, default shown)
OPENAI_REALTIME_MODEL=gpt-realtime-2025-08-28

# Redis URL (optional, default shown)
REDIS_URL=redis://localhost:6379
```

### Getting Credentials

- **Zoom SDK**: Create a [Meeting SDK app](https://marketplace.zoom.us/develop/create) in the Zoom Marketplace. You need the Client ID and Client Secret.
- **OpenAI**: Get an API key from [platform.openai.com](https://platform.openai.com). Must have access to the Realtime API.

## Quick Start

```bash
# Clone and configure
git clone https://github.com/cloudwarriors-ai/zoom-ai-debate.git
cd zoom-ai-debate
cp .env.example .env
# Edit .env with your credentials

# Build and run
docker compose up -d

# Check health
curl http://localhost:8000/api/health

# Get the expected transcript format
curl http://localhost:8000/api/format
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/format` | Returns expected transcript schema + example |
| `POST` | `/api/conversations` | Upload a transcript |
| `GET` | `/api/conversations/{id}` | Get conversation status |
| `POST` | `/api/conversations/{id}/perform` | Start performing in a Zoom meeting |
| `POST` | `/api/conversations/{id}/stop` | Stop a running performance |

### Upload a Transcript

```bash
curl -X POST http://localhost:8000/api/conversations \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "AI regulation",
    "speakers": [
      {"name": "Alex", "voice": "ash"},
      {"name": "Jordan", "voice": "coral"}
    ],
    "transcript": [
      {"speaker": "Alex", "text": "We need guardrails for AI development."},
      {"speaker": "Jordan", "text": "But over-regulation will stifle innovation."}
    ]
  }'
```

### Perform in a Meeting

```bash
curl -X POST http://localhost:8000/api/conversations/{id}/perform \
  -H "Content-Type: application/json" \
  -d '{"meeting_url": "https://zoom.us/j/123456789?pwd=abc123"}'
```

## Available Voices

OpenAI Realtime API voices: `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`

## Development

```bash
# Install dependencies locally (macOS — runs in stub mode, no real Zoom)
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run API locally (stub mode on macOS)
cd backend && uvicorn main:app --reload
```

## Docker Notes

- The container runs as `linux/amd64` (required by Zoom SDK)
- On ARM Macs, Docker uses QEMU emulation automatically
- PulseAudio and Xvfb are set up in the container for headless audio/display
- The Zoom SDK virtual mic sends 20ms PCM16 chunks at 32kHz
