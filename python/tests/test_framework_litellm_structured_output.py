"""T3-LITELLM-002: request telemetry must not reject valid completion arguments."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from pydantic import BaseModel

# Import LiteLLM without fetching its remote model-cost map. All calls are mocked.
with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
    from traceai_litellm import LiteLLMInstrumentor, SpanAttributes


class Answer(BaseModel):
    value: int


class Unprintable:
    def __str__(self):
        raise ValueError("cannot render telemetry")


@pytest.fixture(
    params=[
        pytest.param(("completion", False), id="sync"),
        pytest.param(("acompletion", False), id="async"),
        pytest.param(("completion", True), id="sync-stream"),
        pytest.param(("acompletion", True), id="async-stream"),
        pytest.param(("completion_with_retries", False), id="sync-retries"),
        pytest.param(("acompletion_with_retries", False), id="async-retries"),
    ]
)
def completion_case(request, monkeypatch):
    function_name, streaming = request.param
    span = MagicMock(spec=trace_api.Span)
    tracer = MagicMock()
    tracer.start_as_current_span.return_value.__enter__.return_value = span
    instrumentor = LiteLLMInstrumentor()
    monkeypatch.setattr(instrumentor, "_tracer", tracer, raising=False)

    response = {"choices": [{"delta": {"content": '{"value":1}'}}]}

    async def async_chunks():
        yield response

    is_async = function_name.startswith("a")
    original = AsyncMock() if is_async else Mock()
    original.return_value = (
        (async_chunks() if is_async else iter([response])) if streaming else response
    )
    monkeypatch.setattr(instrumentor, "original_litellm_funcs", {function_name: original})
    token = context_api.attach(
        context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)
    )
    try:
        yield SimpleNamespace(
            wrapper=getattr(instrumentor, f"_{function_name}_wrapper"),
            is_async=is_async,
            streaming=streaming,
            original=original,
            response=response,
            span=span,
            span_context=tracer.start_as_current_span.return_value,
            kwargs={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Return value 1"}],
                "stream": streaming,
                "temperature": 0.2,
            },
        )
    finally:
        context_api.detach(token)


def _call_completion(case):
    if case.is_async:
        async def call():
            result = await case.wrapper(**case.kwargs)
            return [chunk async for chunk in result] if case.streaming else result

        return asyncio.run(call())
    result = case.wrapper(**case.kwargs)
    return list(result) if case.streaming else result


def _assert_forwarded_once(case):
    case.original.assert_called_once_with(**case.kwargs)
    if case.is_async:
        case.original.assert_awaited_once_with(**case.kwargs)
    assert case.original.call_args.kwargs["messages"] is case.kwargs["messages"]


def _assert_success(case):
    result = _call_completion(case)
    if case.streaming:
        assert len(result) == 1
        assert result[0] is case.response
    else:
        assert result is case.response
    _assert_forwarded_once(case)
    case.span.record_exception.assert_not_called()


def _request_parameters(case):
    attributes = dict(call.args for call in case.span.set_attribute.call_args_list)
    return json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS])


@pytest.mark.parametrize("parameter", ["response_format", "timeout"])
def test_supported_non_json_parameters_preserve_completion(completion_case, parameter):
    case = completion_case
    value = Answer if parameter == "response_format" else httpx.Timeout(10.0)
    case.kwargs[parameter] = value

    _assert_success(case)

    # Serialization must operate on telemetry copies, not the provider's arguments.
    assert case.original.call_args.kwargs[parameter] is value
    parameters = _request_parameters(case)
    # Opaque/structured provider options are deliberately excluded by the
    # credential-safe inference allowlist, but still reach LiteLLM unchanged.
    assert parameter not in parameters
    assert parameters["temperature"] == 0.2
    assert "model" not in parameters
    assert "messages" not in parameters


def test_json_response_format_is_unchanged(completion_case):
    case = completion_case
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "Answer", "schema": Answer.model_json_schema()},
    }
    case.kwargs["response_format"] = response_format

    _assert_success(case)

    assert case.original.call_args.kwargs["response_format"] is response_format
    assert "response_format" not in _request_parameters(case)


@pytest.mark.parametrize("failure", ["unprintable", "circular", "message-extraction"])
def test_request_attribute_failures_do_not_block_completion(
    completion_case, monkeypatch, failure
):
    case = completion_case
    if failure == "unprintable":
        case.kwargs["metadata"] = Unprintable()
    elif failure == "circular":
        circular = {}
        circular["self"] = circular
        case.kwargs["metadata"] = circular
    else:
        monkeypatch.setattr(
            "traceai_litellm._process_messages",
            Mock(side_effect=ValueError("cannot extract telemetry")),
        )

    _assert_success(case)


def test_application_error_is_preserved(completion_case):
    case = completion_case
    case.kwargs["response_format"] = Answer
    error = RuntimeError("provider failure")
    case.original.side_effect = error

    with pytest.raises(RuntimeError, match="provider failure") as raised:
        _call_completion(case)

    assert raised.value is error
    _assert_forwarded_once(case)
    if case.streaming:
        # The real span context records the exception exactly once. A mock
        # context must receive the same exception rather than record it twice.
        case.span_context.__exit__.assert_called_once()
        exc_type, exc_value, traceback = case.span_context.__exit__.call_args.args
        assert exc_type is type(error)
        assert exc_value is error
        assert traceback is not None
        case.span.record_exception.assert_not_called()
    else:
        case.span.record_exception.assert_called_once_with(error)
