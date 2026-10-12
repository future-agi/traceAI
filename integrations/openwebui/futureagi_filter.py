"""
title: Future AGI
author: Future AGI
version: 0.2.0
license: Apache-2.0
description: Send Open WebUI chat turns to Future AGI.
requirements: opentelemetry-api,opentelemetry-sdk,opentelemetry-exporter-otlp-proto-http
"""

# Standalone on purpose. Open WebUI installs the frontmatter requirements
# into its own environment, and importing traceAI there would pull in
# instrumentors this filter does not use. Open WebUI reads that frontmatter
# only when the file's first line is the opening triple quote by itself.

import hashlib
import json
import logging
import threading
import time
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_serializer

logger = logging.getLogger(__name__)

_COLLECTOR_PATH = "/tracer/v1/traces"
# Open WebUI passes the same __metadata__ dict to inlet and outlet of one
# request; the turn's start time rides on it, so the filter keeps no
# per-turn state (and nothing piles up when a failed turn gets no outlet).
_STARTED_KEY = "_futureagi_turn_started_ns"


def collector_endpoint(endpoint: str) -> str:
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith(_COLLECTOR_PATH):
        return trimmed
    return f"{trimmed}{_COLLECTOR_PATH}"


def email_hash(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()


def _secret(description: str) -> Any:
    # "input": {"type": "password"} is what Open WebUI's valves form reads to
    # mask a field.
    return Field(
        default=SecretStr(""),
        description=description,
        json_schema_extra={"input": {"type": "password"}},
    )


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _usage(message: dict[str, Any]) -> dict[str, int]:
    usage = message.get("usage") or (message.get("info") or {}).get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    out = {}
    if isinstance(prompt, int):
        out["prompt"] = prompt
    if isinstance(completion, int):
        out["completion"] = completion
    # Not total_tokens: Open WebUI 0.11.4 adds total_tokens, input_tokens and
    # output_tokens up over the stream's chunks, so a provider that repeats
    # usage in its last two chunks doubles them. prompt_tokens and
    # completion_tokens keep the provider's values, so they are read first.
    if "prompt" in out and "completion" in out:
        out["total"] = out["prompt"] + out["completion"]
    return out


def turn_attributes(
    body: dict[str, Any], user: dict[str, Any] | None, valves: "Filter.Valves"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attributes of the turn's root span and LLM span, from an Open WebUI outlet body.

    Never includes a raw email or a credential; no message text when ``redact`` is on.
    """
    user = user or {}
    messages = body.get("messages") or []
    reply = messages[-1] if messages and messages[-1].get("role") == "assistant" else {}
    prompt_messages = messages[:-1] if reply else messages

    common: dict[str, Any] = {}
    if user.get("id"):
        common["user.id"] = user["id"]
    chat_id = body.get("chat_id")
    if chat_id:
        common["session.id"] = chat_id
        common["gen_ai.conversation.id"] = chat_id

    root = dict(common, **{"fi.span.kind": "CHAIN"})
    if valves.include_email_hash and user.get("email"):
        root["user.email_hash"] = email_hash(user["email"])

    llm = dict(common, **{"fi.span.kind": "LLM", "gen_ai.operation.name": "chat"})
    model = body.get("model")
    if model:
        llm["gen_ai.request.model"] = model
        llm["llm.model_name"] = model
    usage = _usage(reply)
    if not usage:
        llm["gen_ai.usage.unavailable"] = True
    if "prompt" in usage:
        llm["gen_ai.usage.input_tokens"] = usage["prompt"]
        llm["llm.token_count.prompt"] = usage["prompt"]
    if "completion" in usage:
        llm["gen_ai.usage.output_tokens"] = usage["completion"]
        llm["llm.token_count.completion"] = usage["completion"]
    if "total" in usage:
        llm["llm.token_count.total"] = usage["total"]

    if not valves.redact:
        last_user = next(
            (m for m in reversed(prompt_messages) if m.get("role") == "user"), None
        )
        if last_user is not None:
            root["input.value"] = _text(last_user.get("content"))
        llm["input.value"] = json.dumps(
            [
                {"role": m.get("role"), "content": _text(m.get("content"))}
                for m in prompt_messages
            ]
        )
        llm["input.mime_type"] = "application/json"
        if reply:
            root["output.value"] = llm["output.value"] = _text(reply.get("content"))
    return root, llm


class Filter:
    class Valves(BaseModel):
        api_key: SecretStr = _secret("Future AGI API key, sent as X-Api-Key.")
        secret_key: SecretStr = _secret("Future AGI secret key, sent as X-Secret-Key.")
        endpoint: str = Field(
            default="https://api.futureagi.com",
            description="Future AGI origin; /tracer/v1/traces is added.",
        )
        project: str = Field(
            default="open-webui", description="Future AGI project name."
        )
        redact: bool = Field(
            default=False, description="Leave prompts and replies out of the spans."
        )
        include_email_hash: bool = Field(
            default=False,
            description="Add a SHA-256 of the user's email. The email itself is never sent.",
        )

        # Open WebUI stores valves with model_dump(); a SecretStr would be
        # dumped as "**********" and the key lost.
        @field_serializer("api_key", "secret_key")
        def _stored_value(self, value: SecretStr) -> str:
            return value.get_secret_value()

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._lock = threading.Lock()
        self._provider: Any = None
        self._provider_key: tuple | None = None

    def inlet(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            self._start_turn(
                __metadata__ if __metadata__ is not None else body.get("metadata")
            )
        except Exception:
            logger.warning("Future AGI inlet failed", exc_info=True)
        return body

    def outlet(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            self._record_turn(body, __user__, __metadata__ or {})
        except Exception:
            logger.warning("Future AGI outlet failed", exc_info=True)
        return body

    def flush(self, timeout_millis: int = 10000) -> None:
        if self._provider is not None:
            self._provider.force_flush(timeout_millis)

    def _start_turn(self, metadata: dict[str, Any] | None) -> None:
        if isinstance(metadata, dict):
            metadata[_STARTED_KEY] = time.time_ns()

    def _record_turn(
        self,
        body: dict[str, Any],
        user: dict[str, Any] | None,
        metadata: dict[str, Any],
    ) -> None:
        started = metadata.pop(_STARTED_KEY, None)
        valves = self.valves
        if (
            not valves.api_key.get_secret_value()
            or not valves.secret_key.get_secret_value()
        ):
            return
        from opentelemetry import trace

        ended = time.time_ns()
        started = started or ended
        root_attributes, llm_attributes = turn_attributes(body, user, valves)
        tracer = self._provider_for(valves).get_tracer("openwebui-futureagi")
        # Spans only end here; the batch processor exports them on its own
        # thread, so the chat never waits for Future AGI.
        root = tracer.start_span(
            "openwebui.chat", start_time=started, attributes=root_attributes
        )
        llm = tracer.start_span(
            "openwebui.llm",
            context=trace.set_span_in_context(root),
            start_time=started,
            attributes=llm_attributes,
        )
        llm.end(end_time=ended)
        root.end(end_time=ended)

    def _provider_for(self, valves: "Filter.Valves") -> Any:
        key = (
            collector_endpoint(valves.endpoint),
            valves.project,
            valves.api_key.get_secret_value(),
            valves.secret_key.get_secret_value(),
        )
        with self._lock:
            if self._provider is not None and self._provider_key == key:
                return self._provider
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            old = self._provider
            provider = TracerProvider(
                resource=Resource.create(
                    {"project_name": valves.project, "project_type": "observe"}
                )
            )
            # Bounded queue: when Future AGI is unreachable, spans beyond it are dropped.
            provider.add_span_processor(
                BatchSpanProcessor(self._make_exporter(valves), max_queue_size=2048)
            )
            self._provider, self._provider_key = provider, key
        if old is not None:
            threading.Thread(target=old.shutdown, daemon=True).start()
        return provider

    def _make_exporter(self, valves: "Filter.Valves") -> Any:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(
            endpoint=collector_endpoint(valves.endpoint),
            headers={
                "X-Api-Key": valves.api_key.get_secret_value(),
                "X-Secret-Key": valves.secret_key.get_secret_value(),
            },
        )
