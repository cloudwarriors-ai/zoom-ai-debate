"""Smoke test to verify test runner works."""


def test_imports():
    """Verify core modules can be imported."""
    from models import Conversation, Speaker, TranscriptLine, ConversationStatus
    assert ConversationStatus.READY == "ready"


def test_config():
    """Verify config loads."""
    from config import settings
    assert settings.host == "0.0.0.0"
