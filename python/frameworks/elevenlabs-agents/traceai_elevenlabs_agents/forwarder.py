"""The webhook decision table, independent of any HTTP server.

| Delivery | Response | Exported |
|---|---|---|
| forwarder paused (``FI_ELEVENLABS_PAUSED``) | 503 | nothing |
| signature missing, malformed, stale or wrong | 401 | nothing, body not parsed |
| body not JSON | 400 ``invalid_json`` | nothing |
| ``type`` other than ``post_call_transcription_otel`` | 200 ``ignored`` | nothing |
| no conversation id / no ``resourceSpans`` / malformed span ids | 400 + stable code | nothing |
| conversation already forwarded | 200 ``duplicate`` | nothing |
| same conversation being exported by another worker | 503 | nothing (ElevenLabs may retry) |
| mapper bug | 500 | nothing |
| collector refused or unreachable | 502 | nothing (ElevenLabs may retry) |
| collector accepted the batch | 200 ``forwarded`` | one trace |
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from . import store as store_mod
from .config import ForwarderConfig
from .exporter import ExportError, OTLPJsonExporter, traces_endpoint
from .map import MappingInputError, map_otlp_traces
from .store import ForwardedStore
from .verify import SignatureError, verify_signature

logger = logging.getLogger("traceai_elevenlabs_agents")

WEBHOOK_TYPE = "post_call_transcription_otel"

Result = Tuple[int, Dict[str, Any]]


class Forwarder:
    def __init__(
        self,
        config: ForwarderConfig,
        *,
        exporter: Optional[Any] = None,
        store: Optional[ForwardedStore] = None,
    ) -> None:
        self.config = config
        self.exporter = exporter or OTLPJsonExporter(
            traces_endpoint(config.fi_base_url), config.fi_api_key, config.fi_secret_key
        )
        self.store = store or ForwardedStore(config.state_path)

    # ------------------------------------------------------------------ webhook

    def handle_webhook(self, raw_body: bytes, signature_header: Optional[str]) -> Result:
        if self.config.paused:
            logger.info("delivery refused: forwarder paused")
            return 503, {"error": "paused"}
        try:
            verify_signature(
                raw_body,
                signature_header,
                self.config.webhook_secret,
                tolerance_secs=self.config.signature_tolerance_secs,
            )
        except SignatureError as error:
            logger.warning("delivery rejected: %s", error.reason)
            return 401, {"error": "invalid_signature"}
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("delivery rejected: invalid_json")
            return 400, {"error": "invalid_json"}
        if not isinstance(event, Mapping):
            return 400, {"error": "invalid_json"}
        event_type = event.get("type")
        if event_type != WEBHOOK_TYPE:
            logger.info("delivery ignored: type=%s", event_type if isinstance(event_type, str) else "?")
            return 200, {"status": "ignored"}
        data = event.get("data")
        if not isinstance(data, Mapping):
            return 400, {"error": "missing_data"}
        return self.forward(
            conversation_id=data.get("conversation_id"),
            agent_id=data.get("agent_id"),
            otlp_traces=data.get("otlp_traces"),
            source="post_call_webhook",
        )

    # ------------------------------------------------------------------ shared path

    def forward(
        self,
        *,
        conversation_id: Any,
        agent_id: Any,
        otlp_traces: Any,
        source: str,
        user_id: Optional[str] = None,
        force: bool = False,
    ) -> Result:
        conversation_id = conversation_id if isinstance(conversation_id, str) and conversation_id else None
        agent_id = agent_id if isinstance(agent_id, str) and agent_id else None
        try:
            export_request, summary = map_otlp_traces(
                otlp_traces,
                conversation_id=conversation_id,
                agent_id=agent_id,
                user_id=user_id,
                project_name=self.config.project_name,
                capture_content=self.config.capture_content,
            )
        except MappingInputError as error:
            logger.warning("delivery rejected: conversation_id=%s error=%s", conversation_id or "?", error.code)
            return 400, {"error": error.code}
        except Exception:  # noqa: BLE001 - a mapper bug must never half-export
            logger.error("mapper failed: conversation_id=%s", conversation_id or "?")
            return 500, {"error": "mapping_failed"}

        key = summary["conversation_id"]
        claim = self.store.claim(key, source, force=force)
        if claim == store_mod.DUPLICATE:
            logger.info("delivery acknowledged: conversation_id=%s already forwarded", key)
            return 200, {"status": "duplicate", "conversation_id": key}
        if claim == store_mod.IN_FLIGHT:
            logger.info("delivery deferred: conversation_id=%s export in flight", key)
            return 503, {"error": "in_flight", "conversation_id": key}
        try:
            self.exporter.export(export_request)
        except ExportError as error:
            self.store.release(key)
            logger.error("export failed: conversation_id=%s %s", key, error.detail)
            return 502, {"error": "export_failed", "conversation_id": key}
        except Exception:  # noqa: BLE001
            self.store.release(key)
            logger.error("export failed: conversation_id=%s unexpected error", key)
            return 502, {"error": "export_failed", "conversation_id": key}
        trace_id = summary["trace_ids"][0] if summary["trace_ids"] else None
        self.store.complete(key, trace_id)
        logger.info(
            "forwarded: conversation_id=%s source=%s spans=%d", key, source, summary["span_count"]
        )
        return 200, {
            "status": "forwarded",
            "conversation_id": key,
            "trace_id": trace_id,
            "spans": summary["span_count"],
        }
