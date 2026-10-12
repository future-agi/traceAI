"""
title: Future AGI
author: Future AGI
version: 0.3.0
license: Apache-2.0
description: Send Open WebUI chat turns to Future AGI.
requirements: opentelemetry-exporter-otlp-proto-http,certifi
"""

# Standalone on purpose. Open WebUI installs the frontmatter requirements
# into its own environment. The OTel imports are deliberately top-level: when
# Open WebUI cannot install the requirements, saving the function fails rather
# than silently making every hook a no-op.

import hashlib
import hmac
import json
import logging
import threading
import time
import weakref
from typing import Any

import certifi
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field, SecretStr, field_serializer

logger = logging.getLogger(__name__)

_COLLECTOR_PATH = "/tracer/v1/traces"
_STARTED_KEY = "_futureagi_turn_started_ns"
_STREAM_FAILURE_KEY = "_futureagi_stream_failure"
_TEMPORARY_PREFIXES = ("temporary:", "local:")
_DEFAULT_EMAIL_HMAC_KEY = b"future-agi-openwebui-email-hash-v1"

# Open WebUI enables native OTel telemetry in the same process. Keep this
# filter's queue independent from that process-wide configuration: the
# explicit values below prevent malformed or pathological OTEL_BSP_* values
# from changing provider creation or making the filter fail closed.
_BSP_MAX_QUEUE_SIZE = 2048
_BSP_SCHEDULE_DELAY_MILLIS = 5000
_BSP_MAX_EXPORT_BATCH_SIZE = 512
_BSP_EXPORT_TIMEOUT_MILLIS = 30000


def collector_endpoint(endpoint: str) -> str:
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith(_COLLECTOR_PATH):
        return trimmed
    return f"{trimmed}{_COLLECTOR_PATH}"


def email_hash(email: str, salt: str = "") -> str:
    """Return a stable pseudonymous HMAC of a normalised email address."""
    normalised = email.strip().lower().encode("utf-8")
    key = salt.encode("utf-8") if salt else _DEFAULT_EMAIL_HMAC_KEY
    return hmac.new(key, normalised, hashlib.sha256).hexdigest()


def _secret(description: str) -> Any:
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
    output = message.get("output") or []
    tool_loop = isinstance(output, list) and any(
        isinstance(item, dict)
        and (
            item.get("type") in {"function_call", "function_call_output"}
            or "function_call" in item
        )
        for item in output
    )
    if tool_loop:
        prompt = usage.get("input_tokens")
        completion = usage.get("output_tokens")
    else:
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
    result: dict[str, int] = {}
    if isinstance(prompt, int):
        result["prompt"] = prompt
    if isinstance(completion, int):
        result["completion"] = completion
    if "prompt" in result and "completion" in result:
        result["total"] = result["prompt"] + result["completion"]
    return result


def _truncate(value: str, max_chars: int) -> str:
    return value if max_chars <= 0 else value[:max_chars]


def turn_attributes(
    body: dict[str, Any], user: dict[str, Any] | None, valves: "Filter.Valves"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build root and child attributes from Open WebUI's outlet payload."""
    user = user or {}
    messages = body.get("messages") or []
    reply = (
        messages[-1]
        if messages
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "assistant"
        else {}
    )
    prompt_messages = messages[:-1] if reply else messages
    chat_id = body.get("chat_id") or ""
    redact = valves.redact or chat_id.startswith(_TEMPORARY_PREFIXES)

    common: dict[str, Any] = {}
    if user.get("id"):
        common["user.id"] = user["id"]
    if chat_id:
        common["session.id"] = chat_id
        common["gen_ai.conversation.id"] = chat_id

    root = dict(common, **{"fi.span.kind": "CHAIN"})
    if valves.include_email_hash and user.get("email"):
        root["user.email_hash"] = email_hash(
            user["email"], valves.email_hash_salt.get_secret_value()
        )

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

    if not redact:
        last_user = next(
            (
                m
                for m in reversed(prompt_messages)
                if isinstance(m, dict) and m.get("role") == "user"
            ),
            None,
        )
        if last_user is not None:
            root["input.value"] = _truncate(
                _text(last_user.get("content")), valves.max_chars
            )
        prompt_json = json.dumps(
            [
                {"role": m.get("role"), "content": _text(m.get("content"))}
                for m in prompt_messages
                if isinstance(m, dict)
            ],
            ensure_ascii=False,
        )
        llm["input.value"] = _truncate(prompt_json, valves.max_chars)
        llm["input.mime_type"] = "application/json"
        if reply:
            output = _truncate(_text(reply.get("content")), valves.max_chars)
            root["output.value"] = llm["output.value"] = output
    return root, llm


class _ProviderCleanup:
    """Own providers until the filter finalizer has shut them down."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: list[Any] = []
        self._shutdown_ids: set[int] = set()

    def add(self, provider: Any) -> None:
        with self._lock:
            self._providers.append(provider)

    def shutdown(self, provider: Any) -> None:
        provider_id = id(provider)
        with self._lock:
            if provider_id in self._shutdown_ids or not any(
                candidate is provider for candidate in self._providers
            ):
                return
            self._shutdown_ids.add(provider_id)
        try:
            provider.shutdown()
        except Exception:
            with self._lock:
                self._shutdown_ids.remove(provider_id)
            raise
        with self._lock:
            self._shutdown_ids.remove(provider_id)
            self._providers = [
                candidate for candidate in self._providers if candidate is not provider
            ]

    def shutdown_all(self) -> None:
        with self._lock:
            providers = tuple(self._providers)
        for provider in providers:
            self.shutdown(provider)


def _shutdown_provider_cleanup(cleanup: _ProviderCleanup) -> None:
    cleanup.shutdown_all()


_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})


def _is_failure_status(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _FAILURE_STATUSES


def _decode_stream_event(value: Any) -> Any:
    """Decode a host stream line without changing the value returned to Open WebUI."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8", "replace")
        except UnicodeError:
            return value
    if not isinstance(value, str):
        return value
    payload = value.strip()
    if payload.startswith("data:"):
        payload = payload[5:].strip()
    if not payload or payload == "[DONE]":
        return value
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError):
        return value
    return decoded if isinstance(decoded, dict) else value


def _has_provider_failure_signal(value: Any) -> bool:
    """Recognise explicit host/provider failure markers without guessing from content."""
    value = _decode_stream_event(value)
    if not isinstance(value, dict):
        return False
    if value.get("type") == "response.failed":
        return True
    if value.get("failed") is True or value.get("response_failed") is True:
        return True
    if _is_failure_status(value.get("status")):
        return True
    if value.get("error"):
        return True

    response = value.get("response")
    if isinstance(response, dict):
        if response.get("type") == "response.failed":
            return True
        if _is_failure_status(response.get("status")) or response.get("error"):
            return True

    for key in ("data", "event", "metadata"):
        nested = value.get(key)
        if isinstance(nested, str) and nested.strip() == "response.failed":
            return True
        if _has_provider_failure_signal(nested):
            return True

    messages = value.get("messages")
    if isinstance(messages, list) and messages:
        reply = messages[-1]
        if isinstance(reply, dict) and reply.get("role") == "assistant":
            return _has_provider_failure_signal(reply)
    return False


def _is_empty_failed_reply(body: dict[str, Any]) -> bool:
    messages = body.get("messages") or []
    if not messages or not isinstance(messages[-1], dict):
        return True
    reply = messages[-1]
    return (
        not _text(reply.get("content"))
        and not _usage(reply)
        and not reply.get("output")
    )


def _remember_stream_failure(event: Any, metadata: dict[str, Any] | None) -> None:
    """Persist a failure marker on the request object shared with outlet."""
    if isinstance(metadata, dict) and _has_provider_failure_signal(event):
        metadata[_STREAM_FAILURE_KEY] = True


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
            default=True,
            description="Leave prompts and replies out. Set false to see them in Observe.",
        )
        include_email_hash: bool = Field(
            default=False, description="Add a pseudonymous HMAC of the user's email."
        )
        email_hash_salt: SecretStr = _secret(
            "Optional HMAC salt for the pseudonymous email hash."
        )
        max_chars: int = Field(
            default=32000,
            ge=0,
            description="Maximum input or output characters per span; 0 means unlimited.",
        )
        priority: int = Field(
            default=0, description="Open WebUI filter priority; lower runs first."
        )

        @field_serializer("api_key", "secret_key", "email_hash_salt")
        def _stored_value(self, value: SecretStr) -> str:
            return value.get_secret_value()

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._lock = threading.Lock()
        self._provider: Any = None
        self._provider_key: str | None = None
        self._provider_cleanup = _ProviderCleanup()
        self._provider_finalizer = weakref.finalize(
            self, _shutdown_provider_cleanup, self._provider_cleanup
        )

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
        except Exception as error:  # noqa: BLE001
            _log_hook_error("inlet", error)
        return body

    def stream(
        self,
        event: Any,
        __metadata__: dict[str, Any] | None = None,
    ) -> Any:
        """Remember provider errors from decoded events or raw host stream lines.

        Open WebUI normally passes a decoded dict here. A small host adapter may
        also forward a non-SSE JSON line (or bytes) from its plain-JSON error
        branch; those values are inspected without changing the value returned
        to Open WebUI.
        """
        try:
            _remember_stream_failure(event, __metadata__)
        except Exception as error:  # noqa: BLE001
            _log_hook_error("stream", error)
        return event

    def outlet(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            self._record_turn(body, __user__, __metadata__ or {})
        except Exception as error:  # noqa: BLE001
            _log_hook_error("outlet", error)
        return body

    def flush(self, timeout_millis: int = 10000) -> None:
        if self._provider is not None:
            self._provider.force_flush(timeout_millis)

    def _start_turn(self, metadata: dict[str, Any] | None) -> None:
        if isinstance(metadata, dict):
            # Metadata is owned by Open WebUI and can be reused by a retry. Clear
            # any marker left by an earlier turn before starting this one.
            metadata.pop(_STREAM_FAILURE_KEY, None)
            metadata[_STARTED_KEY] = time.time_ns()

    def _record_turn(
        self,
        body: dict[str, Any],
        user: dict[str, Any] | None,
        metadata: dict[str, Any],
    ) -> None:
        started = metadata.pop(_STARTED_KEY, None)
        stream_failed = metadata.pop(_STREAM_FAILURE_KEY, False)
        if started is None:
            return
        valves = self.valves
        if (
            not valves.api_key.get_secret_value()
            or not valves.secret_key.get_secret_value()
        ):
            return
        ended = time.time_ns()
        root_attributes, llm_attributes = turn_attributes(body, user, valves)
        provider = self._provider_for(valves)
        tracer = provider.get_tracer("openwebui-futureagi")
        root = tracer.start_span(
            "openwebui.chat",
            context=context.Context(),
            start_time=started,
            attributes=root_attributes,
        )
        llm = tracer.start_span(
            "openwebui.llm",
            context=trace.set_span_in_context(root),
            start_time=started,
            attributes=llm_attributes,
        )
        if (
            stream_failed
            or _has_provider_failure_signal(body)
            or _has_provider_failure_signal(metadata)
            or _is_empty_failed_reply(body)
        ):
            status = Status(StatusCode.ERROR, "provider returned a failed response")
            llm.set_status(status)
            root.set_status(status)
        llm.end(end_time=ended)
        root.end(end_time=ended)

    def _provider_for(self, valves: "Filter.Valves") -> Any:
        key_material = "\0".join(
            (
                collector_endpoint(valves.endpoint),
                valves.project,
                valves.api_key.get_secret_value(),
                valves.secret_key.get_secret_value(),
            )
        )
        key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        with self._lock:
            if self._provider is not None and self._provider_key == key:
                return self._provider
            exporter = self._make_exporter(valves)
            provider = TracerProvider(
                sampler=ALWAYS_ON,
                resource=Resource(
                    {"project_name": valves.project, "project_type": "observe"}
                ),
                shutdown_on_exit=False,
            )
            provider.add_span_processor(
                BatchSpanProcessor(
                    exporter,
                    max_queue_size=_BSP_MAX_QUEUE_SIZE,
                    schedule_delay_millis=_BSP_SCHEDULE_DELAY_MILLIS,
                    max_export_batch_size=_BSP_MAX_EXPORT_BATCH_SIZE,
                    export_timeout_millis=_BSP_EXPORT_TIMEOUT_MILLIS,
                )
            )
            self._provider_cleanup.add(provider)
            old = self._provider
            self._provider, self._provider_key = provider, key
        if old is not None:
            threading.Thread(
                target=self._provider_cleanup.shutdown,
                args=(old,),
                daemon=True,
            ).start()
        return provider

    def _make_exporter(self, valves: "Filter.Valves") -> Any:
        return OTLPSpanExporter(
            endpoint=collector_endpoint(valves.endpoint),
            certificate_file=certifi.where(),
            headers={
                "X-Api-Key": valves.api_key.get_secret_value(),
                "X-Secret-Key": valves.secret_key.get_secret_value(),
            },
        )


def _log_hook_error(hook: str, error: Exception) -> None:
    logger.warning("Future AGI %s failed: %s", hook, type(error).__name__)
    logger.debug(
        "Future AGI %s failure details",
        hook,
        exc_info=(type(error), error, error.__traceback__),
    )
