"""Maintained Open WebUI Pipe Function for real, individually timed model calls."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import weakref
from contextlib import nullcontext
from importlib.metadata import version
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from packaging.version import Version
from pydantic import BaseModel, Field, SecretStr, field_serializer
from starlette.responses import Response, StreamingResponse

from ._provider import Providers
from ._response import Observation

__version__ = "0.1.0"  # x-release-please-version
log = logging.getLogger(__name__)


def _check_host() -> None:
    installed = Version(version("open-webui"))
    if not Version("0.11.4") <= installed < Version("0.13"):
        raise RuntimeError("traceAI-openwebui requires Open WebUI >=0.11.4,<0.13")


def _secret() -> Any:
    return Field(
        default=SecretStr(""), json_schema_extra={"input": {"type": "password"}}
    )


async def _check_connection(request: Any, user: Any, model_id: str) -> None:
    from open_webui.routers.openai import get_all_models, get_openai_connection

    models = request.app.state.OPENAI_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.OPENAI_MODELS
    model = models.get(model_id)
    if model is None:
        raise ValueError(
            "The target model's OpenAI-compatible connection is unavailable"
        )
    # Use the host's resolver, including index/legacy URL configuration lookup.
    # Never cache this check: an administrator can change the connection's API.
    _, _, config = await get_openai_connection(model["urlIdx"])
    if config.get("api_type") == "responses":
        # The host discards status/error/incomplete_details when a Responses
        # endpoint returns JSON. Even stream=True can receive that lossy result.
        # Reject before inference/span creation rather than inventing success.
        raise ValueError(
            "Responses API connections are not supported; choose a Chat Completions connection"
        )


class Pipe:
    class Valves(BaseModel):
        target_model: str = Field(
            default="",
            description="Native Chat Completions model ID; not Responses API, Ollama, another Pipe or Arena.",
        )
        api_key: SecretStr = _secret()
        secret_key: SecretStr = _secret()
        endpoint: str = "https://api.futureagi.com"
        project: str = "open-webui"
        capture_content: bool = False
        max_chars: int = Field(default=32000, ge=1, le=131072)

        @field_serializer("api_key", "secret_key")
        def stored_secret(self, value: SecretStr) -> str:
            # Open WebUI persists valves through model_dump(). Repr stays masked.
            return value.get_secret_value()

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._providers = Providers()
        weakref.finalize(self, self._providers.close)

    async def pipe(
        self,
        body: dict[str, Any],
        __request__: Any,
        __user__: dict[str, Any],
        __metadata__: dict[str, Any] | None = None,
    ) -> Any:
        _check_host()
        from open_webui.models.users import Users
        from open_webui.utils.chat import generate_chat_completion

        valves = self.valves.model_copy(deep=True)
        target = __request__.app.state.MODELS.get(valves.target_model)
        # Recursive Pipes/Arena routes could produce nested spans for one request
        # or recurse indefinitely. Delegate only to a native provider entry.
        if (
            not target
            or target.get("pipe")
            or target.get("owned_by") == "arena"
            or target.get("info", {}).get("base_model_id")
            or valves.target_model == body.get("model")
        ):
            raise ValueError(
                "Choose a native provider model as target_model, not a workspace preset, Pipe or Arena"
            )
        # The native Ollama dispatcher discards in-band errors, invents [DONE]
        # at EOF and (on the floor host) invents missing usage as zero. Reject it
        # before dispatch instead of presenting converted data as real telemetry.
        if target.get("owned_by") == "ollama":
            raise ValueError(
                "Native Ollama connections are not supported; choose an OpenAI-compatible connection"
            )
        user = await Users.get_user_by_id(__user__.get("id"))
        if user is None:
            raise ValueError("Open WebUI user is unavailable")
        await _check_connection(__request__, user, valves.target_model)
        payload = copy.deepcopy(body)
        payload["model"] = valves.target_model
        if __metadata__ is not None:
            payload["metadata"] = dict(__metadata__)
        metadata = __metadata__ or {}
        temporary = str(metadata.get("chat_id", "")).startswith(
            ("temporary:", "local:")
        )
        capture = valves.capture_content and not temporary
        span = None
        release = lambda: None
        try:
            if (
                valves.api_key.get_secret_value()
                and valves.secret_key.get_secret_value()
            ):
                provider, release = self._providers.acquire(
                    (
                        valves.endpoint,
                        valves.project,
                        valves.api_key.get_secret_value(),
                        valves.secret_key.get_secret_value(),
                    )
                )
                span = provider.get_tracer("traceai_openwebui", __version__).start_span(
                    "openwebui.model",
                    kind=SpanKind.CLIENT,
                    attributes={
                        "fi.span.kind": "LLM",
                        "gen_ai.operation.name": "chat",
                        "gen_ai.request.model": valves.target_model,
                        "llm.model_name": valves.target_model,
                        "openwebui.tracing.scope": "model_call",
                    },
                )
        except Exception:  # noqa: BLE001 - telemetry fails open without logging private payloads
            release()
            release = lambda: None
            log.warning("Open WebUI tracing unavailable; model execution continues")
        observation = (
            Observation(span, capture, valves.max_chars) if span is not None else None
        )
        if span is not None and capture:
            messages = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in payload.get("messages", [])
                if isinstance(m, dict)
            ]
            span.set_attribute(
                "input.value",
                json.dumps(messages, ensure_ascii=False)[: valves.max_chars],
            )
            span.set_attribute("input.mime_type", "application/json")

        def active():
            return (
                trace.use_span(
                    span,
                    end_on_exit=False,
                    record_exception=False,
                    set_status_on_exception=False,
                )
                if span is not None
                else nullcontext()
            )

        watched_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()

        def observe(method: str, *args: Any) -> None:
            if observation is not None:
                try:
                    getattr(observation, method)(*args)
                except Exception:  # noqa: BLE001 - telemetry fails open without logging private payloads
                    log.warning("Open WebUI response telemetry unavailable")

        def finish(outcome: str) -> None:
            nonlocal span
            try:
                if span is not None:
                    # Host cleanup may abandon our stream under shield before the
                    # cancelled consumer finishes. A pending cancel request is
                    # already known; do not overwrite it with "incomplete" on GC.
                    if outcome == "incomplete" and any(
                        task.cancelling() or task.cancelled() for task in watched_tasks
                    ):
                        outcome = "cancelled"
                    observe("finish")
                    if observation is not None and observation.failed:
                        outcome = "error"
                    span.set_attribute("openwebui.outcome", outcome)
                    if outcome != "completed":
                        span.set_status(
                            Status(
                                StatusCode.ERROR,
                                {
                                    "error": "model request failed",
                                    "cancelled": "model request cancelled",
                                    "incomplete": "model stream ended without completion",
                                }[outcome],
                            )
                        )
                    span.end()
                    span = None
            finally:
                for task in watched_tasks:
                    task.remove_done_callback(task_done)
                watched_tasks.clear()
                release()

        def task_done(task: asyncio.Task) -> None:
            # A host may cancel while handling a chunk, outside our __anext__.
            if task.cancelled():
                finish("cancelled")

        def watch_task() -> None:
            task = asyncio.current_task()
            if span is not None and task is not None and task not in watched_tasks:
                watched_tasks.add(task)
                task.add_done_callback(task_done)

        watch_task()
        try:
            with active():
                # Preserve upstream access checks. Never pass bypass_filter=True.
                response = await generate_chat_completion(
                    __request__,
                    payload,
                    user,
                    bypass_system_prompt=getattr(
                        __request__.state, "bypass_system_prompt", False
                    ),
                )
        except asyncio.CancelledError:
            finish("cancelled")
            raise
        except BaseException:
            finish("error")
            raise

        if not isinstance(response, StreamingResponse):
            data = response
            if isinstance(response, Response):
                try:
                    data = json.loads(response.body)
                except (ValueError, TypeError):
                    data = None
            invalid = not isinstance(data, dict)
            http_error = isinstance(response, Response) and response.status_code >= 400
            try:
                observe("payload", data)
            finally:
                finish("error" if invalid or http_error else "completed")
            # The native router normally returns decoded JSON, not a Response.
            # Validate both forms even with tracing disabled: the Pipe dispatcher
            # would otherwise fabricate an empty completion or treat a raw string
            # as generated model content.
            if http_error and (invalid or not data.get("error")):
                return {
                    "error": {
                        "message": "model request failed",
                        "code": response.status_code,
                    }
                }
            if invalid:
                return {"error": "model returned an invalid non-streaming response"}
            return data

        if observation is not None and response.status_code >= 400:
            observation.failed = True

        async def traced_stream():
            watch_task()
            outcome = "incomplete"
            try:
                iterator = response.body_iterator.__aiter__()
                while True:
                    try:
                        # Do not leave the span attached across a yield: unrelated
                        # tool/retrieval work must not become a child of this call.
                        with active():
                            chunk = await iterator.__anext__()
                    except StopAsyncIteration:
                        if observation is not None:
                            observe("finish")
                            outcome = (
                                "completed" if observation.terminal else "incomplete"
                            )
                        else:
                            outcome = "completed"
                        break
                    observe("chunk", chunk)
                    yield chunk
            except (asyncio.CancelledError, GeneratorExit):
                outcome = "cancelled"
                raise
            except BaseException:
                outcome = "error"
                raise
            finally:
                try:
                    close = getattr(response.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                    # Open WebUI's Pipe dispatcher consumes only body_iterator;
                    # it does not run the returned response's background task.
                    if response.background is not None:
                        await response.background()
                finally:
                    finish(outcome)

        # Preserve the original stream exactly, including errors and [DONE].
        stream = traced_stream()
        # Includes a consumer that abandons the returned response before starting
        # iteration. This is incomplete observation, never claimed as success.
        weakref.finalize(stream, finish, "incomplete")
        return StreamingResponse(
            stream,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    def flush(self, timeout_millis: int = 10000) -> bool:
        return self._providers.flush(timeout_millis)

    def close(self) -> None:
        self._providers.close()


__all__ = ["Pipe"]
