import json
import os
from typing import Any, Generator, Optional

import pytest
from fi_instrumentation.instrumentation._tracers import FITracer
from traceai_smolagents import SmolagentsInstrumentor
from fi_instrumentation.fi_types import (
    MessageAttributes,
    FiMimeTypeValues,
    FiSpanKindValues,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
)
from opentelemetry import trace as trace_api
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._importlib_metadata import entry_points
from smolagents import OpenAIServerModel, Tool
from smolagents.agents import (
    CodeAgent,  # type: ignore[import-untyped]
    ToolCallingAgent,
)
from smolagents.models import (
    ChatMessage,  # type: ignore[import-untyped]
    ChatMessageToolCall,
)

try:
    from smolagents.models import ChatMessageToolCallFunction as ChatMessageToolCallDefinition
except ImportError:  # The supported 1.12 SDK used the old name.
    from smolagents.models import ChatMessageToolCallDefinition


@pytest.fixture
def vcr_config():
    # A missing/mismatched cassette must fail, never contact a provider.
    return {"record_mode": "none"}


def _message(content):
    if hasattr(OpenAIServerModel, "generate"):
        return ChatMessage(role="user", content=content)
    return {"role": "user", "content": content}


def remove_all_vcr_request_headers(request: Any) -> Any:
    """
    Removes all request headers.

    Example:
    ```
    @pytest.mark.vcr(
        before_record_response=remove_all_vcr_request_headers
    )
    def test_openai() -> None:
        # make request to OpenAI
    """
    request.headers.clear()
    return request


def remove_all_vcr_response_headers(response: dict[str, Any]) -> dict[str, Any]:
    """
    Removes all response headers.

    Example:
    ```
    @pytest.mark.vcr(
        before_record_response=remove_all_vcr_response_headers
    )
    def test_openai() -> None:
        # make request to OpenAI
    """
    response["headers"] = {}
    return response


@pytest.fixture
def in_memory_span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(
    in_memory_span_exporter: InMemorySpanExporter,
) -> trace_api.TracerProvider:
    resource = Resource(attributes={})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    span_processor = SimpleSpanProcessor(span_exporter=in_memory_span_exporter)
    tracer_provider.add_span_processor(span_processor=span_processor)
    return tracer_provider


@pytest.fixture(autouse=True)
def instrument(
    tracer_provider: trace_api.TracerProvider,
    in_memory_span_exporter: InMemorySpanExporter,
) -> Generator[None, None, None]:
    import smolagents
    from smolagents import local_python_executor

    classes = {value for value in vars(smolagents).values() if isinstance(value, type)}
    method_names = {"run", "step", "_step_stream", "__call__", "generate"}
    originals = {cls: {name: value for name, value in vars(cls).items() if name in method_names}
                 for cls in classes}
    original_timeout = getattr(local_python_executor, "timeout", None)
    SmolagentsInstrumentor().instrument(tracer_provider=tracer_provider)
    try:
        yield
    finally:
        SmolagentsInstrumentor().uninstrument()
        for cls, methods in originals.items():
            assert {name for name in vars(cls) if name in method_names} == methods.keys()
            assert all(vars(cls)[name] is original for name, original in methods.items())
        assert getattr(local_python_executor, "timeout", None) is original_timeout
        in_memory_span_exporter.clear()


@pytest.fixture
def openai_api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    api_key = "sk-0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", api_key)
    return api_key


class TestInstrumentor:
    def test_entrypoint_for_opentelemetry_instrument(self) -> None:
        (instrumentor_entrypoint,) = entry_points(
            group="opentelemetry_instrumentor", name="smolagents"
        )
        instrumentor = instrumentor_entrypoint.load()()
        assert isinstance(instrumentor, SmolagentsInstrumentor)

    def test_FITracer(self) -> None:
        assert isinstance(SmolagentsInstrumentor()._tracer, FITracer)


class TestModels:
    @pytest.mark.vcr(
        decode_compressed_response=True,
        before_record_request=remove_all_vcr_request_headers,
        before_record_response=remove_all_vcr_response_headers,
    )
    def test_openai_server_model_has_expected_attributes(
        self,
        openai_api_key: str,
        in_memory_span_exporter: InMemorySpanExporter,
    ) -> None:
        model = OpenAIServerModel(
            model_id="gpt-4o",
            api_key=os.environ["OPENAI_API_KEY"],
            api_base="https://api.openai.com/v1",
        )
        input_message_content = (
            "Who won the World Cup in 2018? Answer in one word with no punctuation."
        )
        output_message = model(
            messages=[
                _message(input_message_content)
            ]
        )
        output_message_content = output_message.content
        assert output_message_content == "France"

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == f"{type(model).__name__}.{'generate' if hasattr(model, 'generate') else '__call__'}"
        assert span.status.is_ok
        attributes = dict(span.attributes or {})
        assert attributes.pop(GEN_AI_SPAN_KIND) == LLM
        assert attributes.pop(INPUT_MIME_TYPE) == JSON
        assert isinstance(input_value := attributes.pop(INPUT_VALUE), str)
        input_data = json.loads(input_value)
        assert "messages" in input_data
        assert attributes.pop(OUTPUT_MIME_TYPE) == JSON
        assert isinstance(output_value := attributes.pop(OUTPUT_VALUE), str)
        assert isinstance(json.loads(output_value), dict)
        assert attributes.pop(GEN_AI_REQUEST_MODEL) == "gpt-4o"
        assert isinstance(inv_params := attributes.pop(GEN_AI_REQUEST_PARAMETERS), str)
        assert json.loads(inv_params) == {}
        assert attributes.pop(f"{GEN_AI_INPUT_MESSAGES}.0.{MESSAGE_ROLE}") == "user"
        assert (
            attributes.pop(f"{GEN_AI_INPUT_MESSAGES}.0.{MESSAGE_CONTENT}")
            == input_message_content
        )
        assert isinstance(attributes.pop(GEN_AI_USAGE_INPUT_TOKENS), int)
        assert isinstance(attributes.pop(GEN_AI_USAGE_OUTPUT_TOKENS), int)
        assert isinstance(attributes.pop(GEN_AI_USAGE_TOTAL_TOKENS), int)
        assert attributes.pop(f"{GEN_AI_OUTPUT_MESSAGES}.0.{MESSAGE_ROLE}") == "assistant"
        assert (
            attributes.pop(f"{GEN_AI_OUTPUT_MESSAGES}.0.{MESSAGE_CONTENT}")
            == output_message_content
        )
        assert not attributes

    @pytest.mark.vcr(
        decode_compressed_response=True,
        before_record_request=remove_all_vcr_request_headers,
        before_record_response=remove_all_vcr_response_headers,
    )
    def test_openai_server_model_with_tool_has_expected_attributes(
        self,
        openai_api_key: str,
        in_memory_span_exporter: InMemorySpanExporter,
        tracer_provider: trace_api.TracerProvider,
    ) -> None:
        model = OpenAIServerModel(
            model_id="gpt-4o",
            api_key=os.environ["OPENAI_API_KEY"],
            api_base="https://api.openai.com/v1",
        )
        input_message_content = "What is the weather in Paris?"

        class GetWeatherTool(Tool):  # type: ignore[misc]
            name = "get_weather"
            description = "Get the weather for a given city"
            inputs = {
                "location": {
                    "type": "string",
                    "description": "The city to get the weather for",
                }
            }
            output_type = "string"

            def forward(self, location: str) -> str:
                return "sunny"

        generate = model.generate if hasattr(model, "generate") else model
        output_message = generate(
            messages=[
                _message(input_message_content)
            ],
            tools_to_call_from=[GetWeatherTool()],
        )
        output_message_content = output_message.content
        assert output_message_content is None
        tool_calls = output_message.tool_calls
        assert len(tool_calls) == 1
        assert isinstance(tool_call := tool_calls[0], ChatMessageToolCall)
        assert tool_call.function.name == "get_weather"
        assert (json.loads(tool_call.function.arguments) if isinstance(tool_call.function.arguments, str)
                else tool_call.function.arguments) == {"location": "Paris"}

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == f"{type(model).__name__}.{'generate' if hasattr(model, 'generate') else '__call__'}"
        assert span.status.is_ok
        attributes = dict(span.attributes or {})
        assert attributes.pop(GEN_AI_SPAN_KIND) == LLM
        assert attributes.pop(INPUT_MIME_TYPE) == JSON
        assert isinstance(input_value := attributes.pop(INPUT_VALUE), str)
        input_data = json.loads(input_value)
        assert "messages" in input_data
        assert attributes.pop(OUTPUT_MIME_TYPE) == JSON
        assert isinstance(output_value := attributes.pop(OUTPUT_VALUE), str)
        assert isinstance(json.loads(output_value), dict)
        assert attributes.pop(GEN_AI_REQUEST_MODEL) == "gpt-4o"
        assert isinstance(inv_params := attributes.pop(GEN_AI_REQUEST_PARAMETERS), str)
        assert json.loads(inv_params) == {}
        assert attributes.pop(f"{GEN_AI_INPUT_MESSAGES}.0.{MESSAGE_ROLE}") == "user"
        assert (
            attributes.pop(f"{GEN_AI_INPUT_MESSAGES}.0.{MESSAGE_CONTENT}")
            == input_message_content
        )
        assert isinstance(
            tool_json_schema := attributes.pop(f"{GEN_AI_TOOL_DEFINITIONS}.0.{TOOL_JSON_SCHEMA}"), str
        )
        assert json.loads(tool_json_schema) == {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather for a given city",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city to get the weather for",
                        },
                    },
                    "required": ["location"],
                },
            },
        }
        assert isinstance(attributes.pop(GEN_AI_USAGE_INPUT_TOKENS), int)
        assert isinstance(attributes.pop(GEN_AI_USAGE_OUTPUT_TOKENS), int)
        assert isinstance(attributes.pop(GEN_AI_USAGE_TOTAL_TOKENS), int)
        assert attributes.pop(f"{GEN_AI_OUTPUT_MESSAGES}.0.{MESSAGE_ROLE}") == "assistant"
        assert (
            attributes.pop(
                f"{GEN_AI_OUTPUT_MESSAGES}.0.{MESSAGE_TOOL_CALLS}.0.{TOOL_CALL_ID}"
            )
            == tool_call.id
        )
        assert (
            attributes.pop(
                f"{GEN_AI_OUTPUT_MESSAGES}.0.{MESSAGE_TOOL_CALLS}.0.{TOOL_CALL_FUNCTION_NAME}"
            )
            == "get_weather"
        )
        assert isinstance(
            tool_call_arguments_json := attributes.pop(
                f"{GEN_AI_OUTPUT_MESSAGES}.0.{MESSAGE_TOOL_CALLS}.0.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}"
            ),
            str,
        )
        assert json.loads(tool_call_arguments_json) == {"location": "Paris"}
        assert not attributes


class TestRun:
    def test_multiagents(self, in_memory_span_exporter) -> None:
        class FakeModelMultiagentsManagerAgent:
            def generate(
                self,
                messages: list[dict[str, Any]],
                stop_sequences: Optional[list[str]] = None,
                grammar: Optional[Any] = None,
                tools_to_call_from: Optional[list[Tool]] = None,
                **kwargs,
            ) -> Any:
                if tools_to_call_from is not None:
                    if "Report on the current US president" not in str(messages):
                        return ChatMessage(
                            role="assistant",
                            content="",
                            tool_calls=[
                                ChatMessageToolCall(
                                    id="call_0",
                                    type="function",
                                    function=ChatMessageToolCallDefinition(
                                        name="search_agent",
                                        arguments={"task": "Who is the current US president?", "additional_args": {}},
                                    ),
                                )
                            ],
                        )
                    else:
                        assert "Report on the current US president" in str(messages)
                        return ChatMessage(
                            role="assistant",
                            content="",
                            tool_calls=[
                                ChatMessageToolCall(
                                    id="call_0",
                                    type="function",
                                    function=ChatMessageToolCallDefinition(
                                        name="final_answer", arguments={"answer": "Final report."}
                                    ),
                                )
                            ],
                        )
                else:
                    if "Report on the current US president" not in str(messages):
                        return ChatMessage(
                            role="assistant",
                            content="""
Thought: Let's call our search agent.
Code:
```py
result = search_agent("Who is the current US president?")
```<end_code>
""",
                        )
                    else:
                        assert "Report on the current US president" in str(messages)
                        return ChatMessage(
                            role="assistant",
                            content="""
Thought: Let's return the report.
Code:
```py
final_answer("Final report.")
```<end_code>
""",
                        )

        FakeModelMultiagentsManagerAgent.__call__ = FakeModelMultiagentsManagerAgent.generate
        manager_model = FakeModelMultiagentsManagerAgent()

        class FakeModelMultiagentsManagedAgent:
            def generate(
                self,
                messages: list[dict[str, Any]],
                tools_to_call_from: Optional[list[Tool]] = None,
                stop_sequences: Optional[list[str]] = None,
                grammar: Optional[Any] = None,
                **kwargs,
            ) -> Any:
                return ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ChatMessageToolCall(
                            id="call_0",
                            type="function",
                            function=ChatMessageToolCallDefinition(
                                name="final_answer",
                                arguments={"answer": "Report on the current US president"},
                            ),
                        )
                    ],
                )

        FakeModelMultiagentsManagedAgent.__call__ = FakeModelMultiagentsManagedAgent.generate
        managed_model = FakeModelMultiagentsManagedAgent()

        web_agent = ToolCallingAgent(
            tools=[],
            model=managed_model,
            max_steps=10,
            name="search_agent",
            description=(
                "Runs web searches for you. Give it your request as an argument. "
                "Make the request as detailed as needed, you can ask for thorough reports"
            ),
        )

        manager_code_agent = CodeAgent(
            tools=[],
            model=manager_model,
            managed_agents=[web_agent],
            additional_authorized_imports=["time"],
        )

        report = manager_code_agent.run("Fake question.")
        assert report == "Final report."
        spans = in_memory_span_exporter.get_finished_spans()
        roots = [span for span in spans if span.parent is None]
        assert len(roots) == 1, [s.name for s in roots]
        root, = roots
        assert root.attributes[GEN_AI_SPAN_KIND] == AGENT
        assert any(span.name.startswith("Step ") for span in spans)
        assert any(span.name == "ToolCallingAgent.run" and span.parent is not None for span in spans)
        assert all(span.status.is_ok for span in spans)
        in_memory_span_exporter.clear()

        manager_toolcalling_agent = ToolCallingAgent(
            tools=[],
            model=manager_model,
            managed_agents=[web_agent],
        )

        report = manager_toolcalling_agent.run("Fake question.")
        assert report == "Final report."
        spans = in_memory_span_exporter.get_finished_spans()
        roots = [span for span in spans if span.parent is None]
        assert len(roots) == 1, [s.name for s in roots]
        root, = roots
        assert root.attributes[GEN_AI_SPAN_KIND] == AGENT
        assert any(span.name.startswith("Step ") for span in spans)
        assert any(span.name == "ToolCallingAgent.run" and span.parent is not None for span in spans)
        assert all(span.status.is_ok for span in spans)
        in_memory_span_exporter.clear()


class TestTools:
    def test_tool_invocation_returning_string_has_expected_attributes(
        self, in_memory_span_exporter: InMemorySpanExporter
    ) -> None:
        class GetWeatherTool(Tool):  # type: ignore[misc]
            name = "get_weather"
            description = "Get the weather for a given city"
            inputs = {
                "location": {
                    "type": "string",
                    "description": "The city to get the weather for",
                }
            }
            output_type = "string"

            def forward(self, location: str) -> str:
                return "sunny"

        weather_tool = GetWeatherTool()

        result = weather_tool("Paris")
        assert result == "sunny"

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        attributes = dict(span.attributes or {})

        assert attributes.pop(GEN_AI_SPAN_KIND) == TOOL
        assert isinstance(input_value := attributes.pop(INPUT_VALUE), str)
        assert json.loads(input_value) == {
            "args": ["Paris"],
        }
        assert attributes.pop(OUTPUT_VALUE) == "sunny"
        assert attributes.pop(OUTPUT_MIME_TYPE) == TEXT
        assert attributes.pop(GEN_AI_TOOL_NAME) == "get_weather"
        assert attributes.pop(GEN_AI_TOOL_DESCRIPTION) == "Get the weather for a given city"
        assert isinstance(tool_parameters := attributes.pop(TOOL_PARAMETERS), str)
        assert json.loads(tool_parameters) == {
            "location": {
                "type": "string",
                "description": "The city to get the weather for",
            },
        }
        assert not attributes

    def test_tool_invocation_returning_dict_has_expected_attributes(
        self, in_memory_span_exporter: InMemorySpanExporter
    ) -> None:
        class GetWeatherTool(Tool):  # type: ignore[misc]
            name = "get_weather"
            description = "Get detailed weather information for a given city"
            inputs = {
                "location": {
                    "type": "string",
                    "description": "The city to get the weather for",
                }
            }
            output_type = "object"

            def forward(self, location: str) -> dict[str, Any]:
                return {"condition": "sunny", "temperature": 25, "humidity": 60}

        weather_tool = GetWeatherTool()

        result = weather_tool("Paris")
        assert result == {"condition": "sunny", "temperature": 25, "humidity": 60}

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        attributes = dict(span.attributes or {})

        assert attributes.pop(GEN_AI_SPAN_KIND) == TOOL
        assert isinstance(input_value := attributes.pop(INPUT_VALUE), str)
        assert json.loads(input_value) == {
            "args": ["Paris"],
        }
        assert isinstance(output_value := attributes.pop(OUTPUT_VALUE), str)
        assert json.loads(output_value) == {
            "condition": "sunny",
            "temperature": 25,
            "humidity": 60,
        }
        assert attributes.pop(OUTPUT_MIME_TYPE) == JSON
        assert attributes.pop(GEN_AI_TOOL_NAME) == "get_weather"
        assert (
            attributes.pop(GEN_AI_TOOL_DESCRIPTION)
            == "Get detailed weather information for a given city"
        )
        assert isinstance(tool_parameters := attributes.pop(TOOL_PARAMETERS), str)
        assert json.loads(tool_parameters) == {
            "location": {
                "type": "string",
                "description": "The city to get the weather for",
            },
        }
        assert not attributes


# message attributes
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = (
    MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
)
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_NAME = MessageAttributes.MESSAGE_NAME
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
MESSAGE_TOOL_CALLS = MessageAttributes.MESSAGE_TOOL_CALLS

# mime types
JSON = FiMimeTypeValues.JSON.value
TEXT = FiMimeTypeValues.TEXT.value

# span kinds
AGENT = FiSpanKindValues.AGENT.value
CHAIN = FiSpanKindValues.CHAIN.value
LLM = FiSpanKindValues.LLM.value
TOOL = FiSpanKindValues.TOOL.value

# span attributes
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
GEN_AI_INPUT_MESSAGES = SpanAttributes.GEN_AI_INPUT_MESSAGES
GEN_AI_REQUEST_PARAMETERS = SpanAttributes.GEN_AI_REQUEST_PARAMETERS
GEN_AI_REQUEST_MODEL = SpanAttributes.GEN_AI_REQUEST_MODEL
GEN_AI_OUTPUT_MESSAGES = SpanAttributes.GEN_AI_OUTPUT_MESSAGES
GEN_AI_PROMPTS = SpanAttributes.GEN_AI_PROMPTS
GEN_AI_USAGE_OUTPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
GEN_AI_USAGE_INPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS
GEN_AI_USAGE_TOTAL_TOKENS = SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS
GEN_AI_TOOL_DEFINITIONS = SpanAttributes.GEN_AI_TOOL_DEFINITIONS
GEN_AI_SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
GEN_AI_TOOL_DESCRIPTION = SpanAttributes.GEN_AI_TOOL_DESCRIPTION
GEN_AI_TOOL_NAME = SpanAttributes.GEN_AI_TOOL_NAME
TOOL_PARAMETERS = SpanAttributes.TOOL_PARAMETERS

# tool attributes
TOOL_JSON_SCHEMA = ToolAttributes.TOOL_JSON_SCHEMA

# tool call attributes
TOOL_CALL_FUNCTION_ARGUMENTS_JSON = ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON
TOOL_CALL_FUNCTION_NAME = ToolCallAttributes.TOOL_CALL_FUNCTION_NAME
TOOL_CALL_ID = ToolCallAttributes.TOOL_CALL_ID


@pytest.mark.parametrize("raises", [False, True])
def test_timeout_wrapper_preserves_context_result_and_exception(raises):
    from concurrent.futures import ThreadPoolExecutor
    from opentelemetry import context
    from traceai_smolagents._wrappers import _TimeoutWrapper

    marker = object()
    failure = ValueError("offline timeout worker failure")

    def timeout(seconds):
        def decorate(function):
            def invoke(*args, **kwargs):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    return executor.submit(function, *args, **kwargs).result(timeout=seconds)
            return invoke
        return decorate

    def work(value):
        assert context.get_value("test-parent") is marker
        if raises:
            raise failure
        return value

    wrapped = _TimeoutWrapper()(timeout, None, (5,), {})(work)
    token = context.attach(context.set_value("test-parent", marker))
    try:
        if raises:
            with pytest.raises(ValueError) as caught:
                wrapped(42)
            assert caught.value is failure
        else:
            assert wrapped(42) == 42
        assert context.get_value("test-parent") is marker
    finally:
        context.detach(token)
    assert context.get_value("test-parent") is None
