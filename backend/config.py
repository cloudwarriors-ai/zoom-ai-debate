"""Application configuration from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings loaded from environment."""

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    zoom_client_id: str = os.getenv("ZOOM_CLIENT_ID", "")
    zoom_client_secret: str = os.getenv("ZOOM_CLIENT_SECRET", "")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    # OpenAI Realtime API
    openai_realtime_model: str = os.getenv(
        "OPENAI_REALTIME_MODEL", "gpt-realtime-2025-08-28"
    )
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"

    # Audio
    openai_sample_rate: int = 24000  # OpenAI Realtime outputs 24kHz
    zoom_sample_rate: int = 32000  # Zoom SDK expects 32kHz

    # Conversation defaults
    default_num_turns: int = 6
    pause_between_lines_sec: float = 1.0
    settle_after_join_sec: float = 3.0

    # Available OpenAI Realtime voices
    available_voices: list[str] = [
        "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"
    ]


settings = Settings()
