from typing import Optional

from .config import _CONTENT_HIDE_FLAGS, TraceConfig


def allows_conversation_transcript(config: Optional[TraceConfig] = None) -> bool:
    """Allow a combined transcript only when text on both sides may be captured."""
    if config is None:
        config = TraceConfig()
    return not any(getattr(config, flag) for flag in _CONTENT_HIDE_FLAGS)
