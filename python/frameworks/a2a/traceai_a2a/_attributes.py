"""
Attribute extractors for A2A Protocol objects.

These functions extract span attributes from A2A SDK model objects
(Task, Message, Artifact, AgentCard) in a safe, exception-guarded way —
following the same pattern as other traceAI attribute extractors.
"""

import json
import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

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

try:
    # Optional — used to surface FI canonical input.value / output.value keys
    # so the Future AGI dashboard's Input/Output panels populate.
    from fi_instrumentation.fi_types import SpanAttributes as _FISpanAttributes

    _INPUT_VALUE_KEY = _FISpanAttributes.INPUT_VALUE
    _OUTPUT_VALUE_KEY = _FISpanAttributes.OUTPUT_VALUE
except Exception:  # pragma: no cover - fallback to raw OI keys
    _INPUT_VALUE_KEY = "input.value"
    _OUTPUT_VALUE_KEY = "output.value"

logger = logging.getLogger(__name__)

AttributeYield = Iterator[Tuple[str, Any]]


def _part_text(part: Any) -> Optional[str]:
    """Pull text out of a Part across v0 (`.text` / dict) and v1 (protobuf)."""
    try:
        # v0 / v1 both expose `.text` directly for text parts.
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            return text
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
    except Exception:
        pass
    return None


def extract_parts_text(parts: Any) -> str:
    """Join the text content of a sequence of Parts into one string."""
    if not parts:
        return ""
    fragments: List[str] = []
    try:
        for part in parts:
            text = _part_text(part)
            if text:
                fragments.append(text)
    except Exception:
        logger.debug("Failed to extract text from parts", exc_info=True)
    return "".join(fragments)


def extract_message_text(message: Any) -> str:
    """Join the text content of a Message's parts."""
    if message is None:
        return ""
    try:
        parts = getattr(message, "parts", None)
        if parts is None and isinstance(message, dict):
            parts = message.get("parts")
        return extract_parts_text(parts)
    except Exception:
        logger.debug("Failed to extract message text", exc_info=True)
        return ""


def extract_artifact_text(artifact: Any) -> str:
    """Join the text content of an Artifact's parts."""
    if artifact is None:
        return ""
    try:
        parts = getattr(artifact, "parts", None)
        if parts is None and isinstance(artifact, dict):
            parts = artifact.get("parts")
        return extract_parts_text(parts)
    except Exception:
        logger.debug("Failed to extract artifact text", exc_info=True)
        return ""


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

    try:
        # Surface the final artifact text as output.value so the dashboard's
        # Output panel populates on non-streaming v0 calls.
        artifacts = getattr(task, "artifacts", None)
        if artifacts is None and isinstance(task, dict):
            artifacts = task.get("artifacts")
        if artifacts:
            output_text = "".join(extract_artifact_text(a) for a in artifacts)
            if output_text:
                yield _OUTPUT_VALUE_KEY, output_text
    except Exception:
        logger.debug("Failed to extract task output text", exc_info=True)


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

    In a2a-sdk 0.x ``Part`` carried a ``type`` discriminator. In 1.x the
    type information is encoded by which field on the protobuf ``Part`` is
    populated — ``text``, ``raw``, ``url``, ``data``, ``filename``,
    ``media_type``. We probe the v0 discriminator first; if absent we
    infer the type from the populated v1 field.
    """
    try:
        parts = getattr(artifact, "parts", None) or (
            artifact.get("parts") if isinstance(artifact, dict) else None
        )
        if not parts:
            return None
        first_part = parts[0]
        # v0 discriminator
        part_type = getattr(first_part, "type", None) or (
            first_part.get("type") if isinstance(first_part, dict) else None
        )
        if part_type:
            return _enum_or_str(part_type)
        # v1 protobuf Part — infer from populated oneof-ish field.
        for field_name, label in (
            ("text", "text"),
            ("raw", "file"),
            ("url", "file"),
            ("filename", "file"),
            ("data", "data"),
        ):
            value = getattr(first_part, field_name, None)
            if value:
                return label
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
            # Surface the user-message text as input.value so the Future AGI
            # dashboard's Input panel shows the prompt instead of being blank.
            input_text = extract_message_text(message)
            if input_text:
                yield _INPUT_VALUE_KEY, input_text
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


def get_send_message_request_attributes(
    request: Any, streaming: bool
) -> AttributeYield:
    """Extract attributes from an a2a-sdk 1.x ``SendMessageRequest`` protobuf.

    ``Client.send_message(request: SendMessageRequest, *, context=None)``.
    The request carries a single ``Message`` whose ``task_id`` / ``role`` /
    ``parts`` we surface. Falls back gracefully if the request is missing
    or doesn't look like a SendMessageRequest.
    """
    yield A2A_STREAMING, streaming

    if request is None:
        return

    try:
        message = getattr(request, "message", None)
        if message is None:
            return
        task_id = getattr(message, "task_id", None)
        if task_id:
            yield A2A_TASK_ID, str(task_id)
        yield from get_message_attributes(message)
        input_text = extract_message_text(message)
        if input_text:
            yield _INPUT_VALUE_KEY, input_text
    except Exception:
        logger.debug(
            "Failed to extract attributes from SendMessageRequest", exc_info=True
        )


def _enum_or_str(value: Any) -> str:
    """Convert an Enum (or any object with .value) to str, else str() directly."""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)
