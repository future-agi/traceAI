"""T4-PY-CLOUD-006: ConverseStream through the client-creation installation path.

All provider operations and event streams are fakes; spans export only to memory.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import botocore
import pytest
from botocore.client import ClientCreator
from botocore.eventstream import EventStream
from opentelemetry import context as context_api, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import traceai_bedrock as bedrock
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    FiSpanKindValues,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolCallAttributes,
)


REQUEST = {
    "modelId": "meta.llama3-8b-instruct-v1:0",
    "system": [{"text": "Be concise."}],
    "messages": [{"role": "user", "content": [{"text": "hello"}]}],
    "inferenceConfig": {"maxTokens": 64, "temperature": 0},
}


class FakeEventStream(EventStream):
    """An EventStream-shaped, lazy source with observable iteration and close."""

    def __init__(self, events=(), error=None, close_error=None):
        self.events = iter(events)
        self.error = error
        self.close_error = close_error
        self.consumed = 0
        self.close_calls = 0
        self.active_spans = []
        self.initial_response = object()
        self.close_result = object()

    def __iter__(self):
        for event in self.events:
            self.active_spans.append(trace.get_current_span().get_span_context())
            self.consumed += 1
            yield event
        if self.error is not None:
            raise self.error

    def get_initial_response(self):
        return self.initial_response

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        return self.close_result


@pytest.fixture
def installed(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    pending = []

    def create_client(_creator, service_name, **kwargs):
        return pending.pop(0)

    # Exercise the actual wrap_function_wrapper installation without creating an
    # AWS session, resolving credentials, or constructing an HTTP transport.
    monkeypatch.setattr(ClientCreator, "create_client", create_client)
    monkeypatch.setattr(botocore, "__version__", "1.39.3")
    monkeypatch.setattr(bedrock, "Protect", None)
    instrumentor = bedrock.BedrockInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        skip_dep_check=True,
        config=TraceConfig(
            hide_inputs=False,
            hide_outputs=False,
            hide_input_messages=False,
            hide_output_messages=False,
            hide_input_text=False,
            hide_output_text=False,
            hide_llm_invocation_parameters=False,
        ),
    )

    def make(stream=None, error=None, service="bedrock-runtime", has_stream=True):
        if stream is None:
            stream = FakeEventStream()
        response = {"stream": stream, "ResponseMetadata": {"HTTPStatusCode": 200}}
        operation = Mock(return_value=response, side_effect=error)
        raw_client = SimpleNamespace(
            invoke_model=Mock(),
            invoke_model_with_response_stream=Mock(),
            converse=Mock(),
        )
        if has_stream:
            raw_client.converse_stream = operation
        pending.append(raw_client)
        client = object.__new__(ClientCreator).create_client(service_name=service)
        return SimpleNamespace(
            client=client, operation=operation, response=response, stream=stream
        )

    yield SimpleNamespace(make=make, exporter=exporter, provider=provider)
    instrumentor.uninstrument()
    provider.shutdown()


def test_converse_stream_installation_text_tools_and_trailing_usage(installed):
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hel"}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "lo"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockStart": {"contentBlockIndex": 1, "start": {
            "toolUse": {"toolUseId": "fake-tool-1", "name": "weather"}
        }}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {
            "toolUse": {"input": '{"city":'}
        }}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {
            "toolUse": {"input": '"Paris"}'}
        }}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 4, "totalTokens": 4}}},
    ]
    original_events = deepcopy(events)
    case = installed.make(FakeEventStream(events))
    request = deepcopy(REQUEST)
    parent_tracer = installed.provider.get_tracer(__name__)
    with parent_tracer.start_as_current_span("parent") as parent:
        response = case.client.converse_stream(**request)
        assert response is case.response
        assert case.client.converse_stream is not case.operation
        assert isinstance(response["stream"], EventStream)
        assert response["stream"].get_initial_response() is case.stream.initial_response
        assert case.stream.consumed == 0
        assert installed.exporter.get_finished_spans() == ()
        assert trace.get_current_span() is parent
        iterator = iter(response["stream"])
        # messageStop is not terminal: usage arrives in the following metadata.
        for event in events[:-1]:
            assert next(iterator) is event
            assert trace.get_current_span() is parent
        assert installed.exporter.get_finished_spans() == ()
        assert next(iterator) is events[-1]
        with pytest.raises(StopIteration):
            next(iterator)
        spans = installed.exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent.span_id == parent.get_span_context().span_id
        assert all(context.span_id == span.context.span_id for context in case.stream.active_spans)
        assert response["stream"].close() is case.stream.close_result
        assert len(installed.exporter.get_finished_spans()) == 1

    case.operation.assert_called_once_with(**REQUEST)
    assert request == REQUEST
    assert events == original_events
    assert span.name == "bedrock.converse_stream"
    assert span.status.status_code is StatusCode.OK
    attrs = span.attributes
    assert attrs[SpanAttributes.GEN_AI_SPAN_KIND] == FiSpanKindValues.LLM.value
    assert attrs[SpanAttributes.GEN_AI_PROVIDER_NAME] == FiLLMProviderValues.AWS.value
    assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == REQUEST["modelId"]
    assert attrs[SpanAttributes.INPUT_VALUE] == "hello"
    assert attrs[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}"] == "system"
    assert attrs[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.1.{MessageAttributes.MESSAGE_CONTENTS}.0.{MessageContentAttributes.MESSAGE_CONTENT_TEXT}"] == "hello"
    assert attrs[SpanAttributes.OUTPUT_VALUE] == "Hello"
    output = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0"
    assert attrs[f"{output}.{MessageAttributes.MESSAGE_ROLE}"] == "assistant"
    assert attrs[f"{output}.{MessageAttributes.MESSAGE_CONTENTS}.0.{MessageContentAttributes.MESSAGE_CONTENT_TEXT}"] == "Hello"
    tool = f"{output}.{MessageAttributes.MESSAGE_TOOL_CALLS}.0"
    assert attrs[f"{tool}.{ToolCallAttributes.TOOL_CALL_ID}"] == "fake-tool-1"
    assert attrs[f"{tool}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}"] == "weather"
    assert attrs[f"{tool}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}"] == '{"city":"Paris"}'
    assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 0
    assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 4
    assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 4


@pytest.mark.parametrize("phase", ["request", "iteration", "close"])
def test_converse_stream_errors_keep_original_exception_and_finish_once(installed, phase):
    error = RuntimeError("fake provider failure")
    stream = FakeEventStream(
        [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "partial"}}}],
        error=error if phase == "iteration" else None,
        close_error=error if phase == "close" else None,
    )
    case = installed.make(stream, error=error if phase == "request" else None)
    with pytest.raises(RuntimeError) as caught:
        response = case.client.converse_stream(**deepcopy(REQUEST))
        if phase == "iteration":
            list(response["stream"])
        elif phase == "close":
            response["stream"].close()
    assert caught.value is error
    spans = installed.exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert len([event for event in spans[0].events if event.name == "exception"]) == 1
    if phase == "iteration":
        assert spans[0].attributes[SpanAttributes.OUTPUT_VALUE] == "partial"
        response["stream"].close()
        assert len(installed.exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("consume_first", [False, True])
def test_converse_stream_early_close_preserves_close_and_partial_output(installed, consume_first):
    event = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "partial"}}}
    case = installed.make(FakeEventStream([event]))
    response = case.client.converse_stream(**deepcopy(REQUEST))
    if consume_first:
        assert next(iter(response["stream"])) is event
    assert response["stream"].close() is case.stream.close_result
    assert response["stream"].close() is case.stream.close_result
    assert case.stream.close_calls == 2
    assert case.stream.consumed == int(consume_first)
    spans = installed.exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.UNSET
    if consume_first:
        assert spans[0].attributes[SpanAttributes.OUTPUT_VALUE] == "partial"


def test_converse_stream_error_event_is_returned_unchanged(installed):
    event = {"modelStreamErrorException": {"message": "fake stream failure"}}
    case = installed.make(FakeEventStream([event]))
    stream = case.client.converse_stream(**deepcopy(REQUEST))["stream"]
    assert next(iter(stream)) is event
    stream.close()
    spans = installed.exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR


@pytest.mark.parametrize("during_creation", [False, True])
def test_converse_stream_suppression_is_passthrough(installed, during_creation):
    case = None if during_creation else installed.make()
    suppression = context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        if during_creation:
            case = installed.make()
        response = case.client.converse_stream(**deepcopy(REQUEST))
        assert response is case.response
        assert response["stream"] is case.stream
        list(response["stream"])
    finally:
        context_api.detach(suppression)
    assert installed.exporter.get_finished_spans() == ()


def test_converse_stream_absent_on_older_client_is_not_required(installed):
    case = installed.make(has_stream=False)
    assert not hasattr(case.client, "converse_stream")
    assert installed.exporter.get_finished_spans() == ()


def test_converse_stream_other_services_are_unchanged(installed):
    case = installed.make(service="s3")
    assert case.client.converse_stream is case.operation
    assert case.client.converse_stream(**deepcopy(REQUEST))["stream"] is case.stream
    assert installed.exporter.get_finished_spans() == ()


def test_converse_stream_invalid_request_reaches_provider(installed):
    error = ValueError("fake provider validation error")
    case = installed.make(error=error)
    with pytest.raises(ValueError) as caught:
        case.client.converse_stream(modelId=REQUEST["modelId"], messages=[])
    assert caught.value is error
    case.operation.assert_called_once_with(modelId=REQUEST["modelId"], messages=[])
    spans = installed.exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
