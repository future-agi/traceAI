"""One-shot backfill: ``GET /v1/convai/conversations/{id}?format=opentelemetry``.

Same mapper and same idempotency record as the webhook, so a conversation the
webhook already forwarded is not exported twice. ElevenLabs mints fresh span ids
on every export, so ``--force`` re-exports and duplicates the turn and tool spans
inside the existing trace; use it only for a conversation whose first export you
know did not land.

Needs an ElevenLabs API key with ``CONVAI_READ``. This is a pull command, never a
poll loop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple
from urllib.parse import quote, urlencode

from .forwarder import Forwarder

GET_PATH = "/v1/convai/conversations/{conversation_id}"


class BackfillError(Exception):
    pass


def fetch_conversation(conversation_id: str, api_key: str, base_url: str, timeout: float = 30.0) -> Dict[str, Any]:
    if not api_key:
        raise BackfillError("ELEVENLABS_API_KEY is required for backfill")
    url = base_url.rstrip("/") + GET_PATH.format(conversation_id=quote(conversation_id, safe="")) + "?" + urlencode(
        {"format": "opentelemetry"}
    )
    request = urllib.request.Request(url, headers={"xi-api-key": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise BackfillError("ElevenLabs returned HTTP {0}".format(error.code)) from None
    except (urllib.error.URLError, OSError) as error:
        raise BackfillError("ElevenLabs unreachable ({0})".format(type(getattr(error, "reason", error)).__name__)) from None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BackfillError("ElevenLabs returned a non-JSON body") from None
    if not isinstance(parsed, dict):
        raise BackfillError("ElevenLabs returned an unexpected body")
    return parsed


def backfill(forwarder: Forwarder, conversation_id: str, *, force: bool = False) -> Tuple[int, Dict[str, Any]]:
    config = forwarder.config
    conversation = fetch_conversation(conversation_id, config.elevenlabs_api_key, config.elevenlabs_base_url)
    if "otlp_traces" not in conversation:
        return 400, {"error": "missing_otlp_traces", "conversation_id": conversation_id}
    user_id = conversation.get("user_id")
    return forwarder.forward(
        conversation_id=conversation.get("conversation_id") or conversation_id,
        agent_id=conversation.get("agent_id"),
        otlp_traces=conversation.get("otlp_traces"),
        user_id=user_id if isinstance(user_id, str) and user_id else None,
        source="backfill",
        force=force,
    )
