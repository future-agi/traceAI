"""Pytest configuration and fixtures for Ollama instrumentation tests."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest


# Create mock modules before any imports
def setup_mocks():
    """Set up all required module mocks."""

    # Helper to create a module-like object
    def create_mock_module(name):
        mod = ModuleType(name)
        return mod

    # Create a proper SpanAttributes class that returns string values
    class MockSpanAttributes:
        GEN_AI_PROVIDER_NAME = "llm.system"
        GEN_AI_PROVIDER_NAME = "llm.provider"
        GEN_AI_REQUEST_MODEL = "llm.model"
        GEN_AI_REQUEST_MODEL = "llm.model"
        LLM_RESPONSE_MODEL = "llm.response_model"
        GEN_AI_REQUEST_PARAMETERS = "llm.invocation_parameters"
        GEN_AI_USAGE_INPUT_TOKENS = "llm.token_count.prompt"
        GEN_AI_USAGE_OUTPUT_TOKENS = "llm.token_count.completion"
        GEN_AI_USAGE_TOTAL_TOKENS = "llm.token_count.total"
        GEN_AI_SPAN_KIND = "fi.span.kind"
        INPUT_VALUE = "input.value"
        OUTPUT_VALUE = "output.value"
        EMBEDDING_MODEL_NAME = "embedding.model"
        LLM_SYSTEM_PROMPT = "llm.system_prompt"
        INPUT_MIME_TYPE = "input.mime_type"
        OUTPUT_MIME_TYPE = "output.mime_type"
        GEN_AI_INPUT_MESSAGES = "llm.input_messages"
        GEN_AI_OUTPUT_MESSAGES = "llm.output_messages"
        EMBEDDING_EMBEDDINGS = "embedding.embeddings"
        OUTPUT_VALUE = "raw.output"
        RERANKER_MODEL_NAME = "reranker.model"
        RERANKER_QUERY = "reranker.query"
        RERANKER_TOP_K = "reranker.top_k"

    class MockMessageAttributes:
        MESSAGE_ROLE = "message.role"
        MESSAGE_CONTENT = "message.content"
        MESSAGE_TOOL_CALLS = "message.tool_calls"

    class MockEmbeddingAttributes:
        EMBEDDING_VECTOR = "embedding.vector"

    # Create mock for fi_instrumentation.fi_types
    mock_fi_types = MagicMock()
    mock_fi_types.SpanAttributes = MockSpanAttributes

    # Create proper enum-like objects for FiSpanKindValues
    class MockEnum:
        def __init__(self, value):
            self.value = value

    class MockFiSpanKindValues:
        LLM = MockEnum("LLM")
        EMBEDDING = MockEnum("EMBEDDING")
        RERANKER = MockEnum("RERANKER")

    class MockFiMimeTypeValues:
        JSON = MockEnum("application/json")
        TEXT = MockEnum("text/plain")

    mock_fi_types.FiSpanKindValues = MockFiSpanKindValues
    mock_fi_types.FiMimeTypeValues = MockFiMimeTypeValues

    mock_fi_types.MessageAttributes = MockMessageAttributes
    mock_fi_types.EmbeddingAttributes = MockEmbeddingAttributes

    # Mock fi_instrumentation base
    import json
    mock_fi_instrumentation = create_mock_module("fi_instrumentation")
    mock_fi_instrumentation.FITracer = MagicMock()
    mock_fi_instrumentation.TraceConfig = MagicMock()
    mock_fi_instrumentation.safe_json_dumps = lambda x: json.dumps(x) if x else "{}"
    mock_fi_instrumentation.get_attributes_from_context = MagicMock(return_value={})
    mock_fi_instrumentation.fi_types = mock_fi_types

    # Mock fi_instrumentation.instrumentation submodule
    mock_fi_instr_instr = create_mock_module("fi_instrumentation.instrumentation")
    mock_fi_instr_protect = create_mock_module("fi_instrumentation.instrumentation._protect_wrapper")
    mock_fi_instr_protect.GuardrailProtectWrapper = MagicMock()

    # Mock fi.evals
    mock_fi = create_mock_module("fi")
    mock_fi_evals = create_mock_module("fi.evals")
    mock_fi_evals.Protect = None

    # Register fi mocks
    sys.modules["fi_instrumentation"] = mock_fi_instrumentation
    sys.modules["fi_instrumentation.fi_types"] = mock_fi_types
    sys.modules["fi_instrumentation.instrumentation"] = mock_fi_instr_instr
    sys.modules["fi_instrumentation.instrumentation._protect_wrapper"] = mock_fi_instr_protect
    sys.modules["fi"] = mock_fi
    sys.modules["fi.evals"] = mock_fi_evals

    # Mock opentelemetry - comprehensive mock
    mock_otel = create_mock_module("opentelemetry")
    mock_trace = create_mock_module("opentelemetry.trace")
    mock_trace.get_tracer_provider = MagicMock(return_value=MagicMock())
    mock_trace.get_tracer = MagicMock(return_value=MagicMock())
    mock_trace.INVALID_SPAN = MagicMock()
    mock_trace.Span = MagicMock()
    mock_trace.Tracer = MagicMock()
    mock_trace.Status = MagicMock()
    mock_trace.StatusCode = MagicMock()
    mock_trace.StatusCode.OK = "OK"
    mock_trace.StatusCode.ERROR = "ERROR"
    mock_trace.SpanKind = MagicMock()
    mock_trace.SpanKind.CLIENT = "CLIENT"
    mock_trace.SpanKind.SERVER = "SERVER"

    mock_context = create_mock_module("opentelemetry.context")
    mock_util = create_mock_module("opentelemetry.util")
    mock_util_types = create_mock_module("opentelemetry.util.types")
    mock_util_types.AttributeValue = type
    mock_util_types.Attributes = dict

    mock_otel_instr = create_mock_module("opentelemetry.instrumentation")
    mock_otel_instr_instrumentor = create_mock_module("opentelemetry.instrumentation.instrumentor")

    class MockBaseInstrumentor:
        def instrumentation_dependencies(self):
            return []

        def instrument(self, **kwargs):
            self._instrument(**kwargs)

        def _instrument(self, **kwargs):
            pass

        def _uninstrument(self, **kwargs):
            pass

    mock_otel_instr_instrumentor.BaseInstrumentor = MockBaseInstrumentor

    # Register opentelemetry mocks
    sys.modules["opentelemetry"] = mock_otel
    sys.modules["opentelemetry.trace"] = mock_trace
    sys.modules["opentelemetry.context"] = mock_context
    sys.modules["opentelemetry.util"] = mock_util
    sys.modules["opentelemetry.util.types"] = mock_util_types
    sys.modules["opentelemetry.instrumentation"] = mock_otel_instr
    sys.modules["opentelemetry.instrumentation.instrumentor"] = mock_otel_instr_instrumentor

    # Mock wrapt
    mock_wrapt = create_mock_module("wrapt")
    mock_wrapt.wrap_function_wrapper = MagicMock()
    sys.modules["wrapt"] = mock_wrapt


# Set up mocks before tests run
setup_mocks()


@pytest.fixture
def mock_ollama_client():
    """Create a mock Ollama client."""
    mock_client = MagicMock()

    # Mock chat response
    mock_client.chat.return_value = {
        "message": {"role": "assistant", "content": "Hello!"},
        "prompt_eval_count": 10,
        "eval_count": 5,
        "total_duration": 1000000000,
    }

    # Mock generate response
    mock_client.generate.return_value = {
        "response": "Generated text",
        "prompt_eval_count": 5,
        "eval_count": 10,
    }

    # Mock embed response
    mock_client.embed.return_value = {
        "embedding": [0.1, 0.2, 0.3, 0.4, 0.5],
    }

    return mock_client
