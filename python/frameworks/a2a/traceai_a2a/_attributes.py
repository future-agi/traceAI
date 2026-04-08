"""
Attribute extractors for A2A Protocol objects.

These functions extract span attributes from A2A SDK model objects
(Task, Message, Artifact, AgentCard) in a safe, exception-guarded way —
following the same pattern as other traceAI attribute extractors.
"""

import json
import logging
from typing import Any, Dict, Iterator, Optional, Tuple

from traceai_a2a._semantic_conventions import (
    A2A_AGENT_CARD_NAME,
    A2A_AGENT_CARD_VERSION,
    A2A_AGENT_URL,
    A2A_ARTIFACT_TYPE,
    A2A_MESSAGE_PARTS_COUNT,
    A2A_MESSAGE_ROLE,
    A2A_PUSH_NOTIFICATION_URL,
    A2A_STREAMING,
    A2A_TASK_ID,
    A2A_TASK_STATE,
)

logger = logging.getLogger(__name__)

AttributeYield = Iterator[Tuple[str, Any]]


def get_task_attributes(task: Any) -> AttributeYield:
    """
    Extract span attributes from an A2A Task object (or task dict payload).

    Handles both A2A SDK model objects (with .id, .status attributes) and
    raw dict payloads (passed before a task object is returned).
    """
    try:
        if hasattr(task, "id") and task.id:
            yield A2A_TASK_ID, str(task.id)
        elif isinstance(task, dict) and task.get("id"):
            yield A2A_TASK_ID, str(task["id"])
    except Exception:
        logger.debug("Failed to extract task ID", exc_info=True)

    try:
        # Handle A2A SDK TaskStatus object
        status = getattr(task, "status", None)
        if status is not None:
            state = getattr(status, "state", None)
            if state is not None:
                # state may be an Enum or a plain string
                yield A2A_TASK_STATE, _enum_or_str(state)
        elif isinstance(task, dict):
            state = task.get("status", {}).get("state")
            if state:
                yield A2A_TASK_STATE, str(state)
    except Exception:
        logger.debug("Failed to extract task state", exc_info=True)


def get_message_attributes(message: Any) -> AttributeYield:
    """
    Extract span attributes from an A2A Message object or dict.
    """
    try:
        role = getattr(message, "role", None) or (
            message.get("role") if isinstance(message, dict) else None
        )
        if role:
            yield A2A_MESSAGE_ROLE, _enum_or_str(role)
    except Exception:
        logger.debug("Failed to extract message role", exc_info=True)

    try:
        parts = getattr(message, "parts", None) or (
            message.get("parts") if isinstance(message, dict) else None
        )
        if parts is not None:
            yield A2A_MESSAGE_PARTS_COUNT, len(parts)
    except Exception:
        logger.debug("Failed to extract message parts count", exc_info=True)


def get_agent_card_attributes(agent_card: Any) -> AttributeYield:
    """
    Extract span attributes from an A2A AgentCard object (from /.well-known/agent.json).
    """
    try:
        name = getattr(agent_card, "name", None) or (
            agent_card.get("name") if isinstance(agent_card, dict) else None
        )
        if name:
            yield A2A_AGENT_CARD_NAME, str(name)
    except Exception:
        logger.debug("Failed to extract agent card name", exc_info=True)

    try:
        version = getattr(agent_card, "version", None) or (
            agent_card.get("version") if isinstance(agent_card, dict) else None
        )
        if version:
            yield A2A_AGENT_CARD_VERSION, str(version)
    except Exception:
        logger.debug("Failed to extract agent card version", exc_info=True)


def get_artifact_type(artifact: Any) -> Optional[str]:
    """
    Determine the type of an A2A artifact.
    Returns one of: 'text', 'file', 'data', or None.
    """
    try:
        parts = getattr(artifact, "parts", None) or (
            artifact.get("parts") if isinstance(artifact, dict) else None
        )
        if not parts:
            return None
        first_part = parts[0]
        part_type = getattr(first_part, "type", None) or (
            first_part.get("type") if isinstance(first_part, dict) else None
        )
        return _enum_or_str(part_type) if part_type else None
    except Exception:
        logger.debug("Failed to determine artifact type", exc_info=True)
        return None


def get_send_task_payload_attributes(payload: Dict[str, Any], streaming: bool) -> AttributeYield:
    """
    Extract attributes from the send_task/send_task_streaming payload dict.
    This runs before the task is submitted, so we extract what we can from
    the request body.
    """
    yield A2A_STREAMING, streaming

    try:
        # Extract session_id as task ID if present (pre-submission)
        session_id = payload.get("sessionId") or payload.get("session_id")
        if session_id:
            yield A2A_TASK_ID, str(session_id)
    except Exception:
        logger.debug("Failed to extract session_id from payload", exc_info=True)

    try:
        message = payload.get("message", {})
        if message:
            yield from get_message_attributes(message)
    except Exception:
        logger.debug("Failed to extract message attributes from payload", exc_info=True)

    try:
        push_notification = payload.get("pushNotification") or payload.get("push_notification")
        if isinstance(push_notification, dict):
            url = push_notification.get("url")
            if url:
                yield A2A_PUSH_NOTIFICATION_URL, str(url)
    except Exception:
        logger.debug("Failed to extract push notification URL", exc_info=True)


def _enum_or_str(value: Any) -> str:
    """Convert an Enum (or any object with .value) to str, else str() directly."""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)
