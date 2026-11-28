"""Opt-in, bounded knowledge-base enrichment without manufacturing retrieval spans.

ElevenLabs reports retrieval on an agent turn, not as an independently timed span.
The regular conversation JSON supplies chunk references; the knowledge-base API
supplies their *current* contents, not a historical snapshot. Preserve that provenance.
"""

from __future__ import annotations

import json
import logging
import math
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlencode

from .config import ForwarderConfig
from .map import AGENT_TURN_SPAN_NAME, _set_attr, _set_masked, attributes_to_dict, iter_spans
from .privacy import Capture, contains_signed_url, conversation_text, scrub_identity

logger = logging.getLogger("traceai_elevenlabs_agents")

MAX_DOCUMENTS = 16  # across the entire exported conversation, including cached copies
MAX_CONTENT_BYTES = 4096  # each document; cut on a UTF-8 boundary
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = 2.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never send xi-api-key to a redirect destination, even on the same host.
        return None


def _get_json(config: ForwarderConfig, path: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        config.elevenlabs_base_url.rstrip("/") + path,
        headers={"xi-api-key": config.elevenlabs_api_key, "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError("rag_http_error") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("rag_response_too_large")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("rag_response_not_object")
    return parsed


def _id(value: Any) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 256
        and value not in (".", "..") and not contains_signed_url(value)
    )


def _text(value: str, capture: Capture) -> Tuple[Optional[str], bool]:
    # Retrieved documents are untrusted free-form metadata. Apply the receiver's
    # always-on identity/stack/signed-URL policy, then the shared output mask.
    if re.search(r"https?://[^\s<>]+\?[^\s<>]+", value, re.IGNORECASE):
        return None, False
    value = scrub_identity(conversation_text(value))
    value = capture.mask("output.value", value)
    if not isinstance(value, str):
        return None, False
    encoded = value.encode("utf-8")
    return encoded[:MAX_CONTENT_BYTES].decode("utf-8", errors="ignore"), len(encoded) > MAX_CONTENT_BYTES


def enrich_rag(
    export_request: Dict[str, Any], *, original: Dict[str, Any], conversation_id: str, config: ForwarderConfig,
) -> None:
    """Enrich a mapped request in place, after the caller owns the conversation claim.

    Disabled by default. Neither conversation nor knowledge-base reads occur when
    either content side is hidden. Failures mark enrichment unavailable, never put
    API error bodies on spans, and do not prevent the native trace being forwarded.
    """
    capture = Capture.from_trace_config(config.trace_config)
    if not config.enrich_rag or not capture.everything:
        return
    output = {(s["traceId"], s["spanId"]): s for _, s in iter_spans(export_request)}
    candidates = []
    for _, span in iter_spans(original):
        attrs = attributes_to_dict(span.get("attributes"))
        if span.get("name") == AGENT_TURN_SPAN_NAME and isinstance(attrs.get("elevenlabs.rag.retrieval_query"), str):
            mapped = output[(span["traceId"].lower(), span["spanId"].lower())]
            candidates.append((mapped, attrs))
    if not candidates:
        return

    def status(span, value):
        _set_attr(span["attributes"], "elevenlabs.rag.enrichment.status", value)
        if value in ("unavailable", "partial"):
            # API exceptions can contain URLs, credentials or document text. Log only
            # the outcome, as the receiver does for other external failures.
            logger.warning("RAG enrichment %s: conversation_id=%s", value, conversation_id)

    try:
        conversation = _get_json(config, "/v1/convai/conversations/" + quote(conversation_id, safe=""))
        transcript = conversation.get("transcript")
        if conversation.get("conversation_id") != conversation_id or not isinstance(transcript, list):
            raise ValueError("rag_conversation_mismatch")
    except (OSError, ValueError, urllib.error.URLError):
        for span, _ in candidates:
            status(span, "unavailable")
        return

    cache = {}
    remaining = MAX_DOCUMENTS
    for span, attrs in candidates:
        index = attrs.get("elevenlabs.turn.index")
        # Never match by span order or timestamps (upstream uses whole seconds).
        turn = transcript[index] if type(index) is int and 0 <= index < len(transcript) else None
        info = turn.get("rag_retrieval_info") if isinstance(turn, dict) and turn.get("role") == "agent" else None
        if not (
            isinstance(info, dict)
            and info.get("retrieval_query") == attrs["elevenlabs.rag.retrieval_query"]
            and _id(info.get("embedding_model"))
            and info["embedding_model"] == attrs.get("elevenlabs.rag.embedding_model")
            and isinstance(info.get("chunks"), list)
        ):
            status(span, "unmatched")
            continue
        complete = True
        documents = 0
        limited = False
        for chunk in info["chunks"]:
            if remaining == 0:
                limited = True
                break
            remaining -= 1
            if not isinstance(chunk, dict) or not _id(chunk.get("document_id")) or not _id(chunk.get("chunk_id")):
                complete = False
                continue
            key = (chunk["document_id"], chunk["chunk_id"], info["embedding_model"])
            if key not in cache:
                path = "/v1/convai/knowledge-base/{}/chunk/{}?{}".format(
                    quote(key[0], safe=""), quote(key[1], safe=""), urlencode({"embedding_model": key[2]}),
                )
                try:
                    result = _get_json(config, path)
                    value = result.get("content") if result.get("id") == key[1] else None
                    cache[key] = _text(value, capture) if isinstance(value, str) else (None, False)
                except (OSError, ValueError, urllib.error.URLError):
                    cache[key] = (None, False)
            content, truncated = cache[key]
            if content is None:
                complete = False
                continue
            prefix = "retrieval.documents.{}.document.".format(documents)
            attributes = span["attributes"]
            _set_attr(attributes, prefix + "content", content)
            # IDs/distances describe the retrieved reference, not an LLM token count.
            _set_masked(attributes, capture, prefix + "id", scrub_identity(key[1]))
            metadata = {
                "document_id": scrub_identity(key[0]), "embedding_model": scrub_identity(key[2]),
                "source": "elevenlabs.knowledge_base_api", "snapshot": "current",
                "content_truncated": truncated,
            }
            distance = chunk.get("vector_distance")
            if type(distance) in (int, float) and math.isfinite(distance):
                metadata["vector_distance"] = distance
            _set_masked(attributes, capture, prefix + "metadata", json.dumps(metadata, ensure_ascii=False))
            documents += 1
        status(span, "limited" if limited else "complete" if complete else "partial")
