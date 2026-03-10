"""Pytest fixtures for traceai-cerebras tests."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from typing import Any, Dict, List, Optional


class MockMessage:
    """Mock Cerebras message."""

    def __init__(self, role: str = "assistant", content: str = "Test response"):
        self.role = role
        self.content = content


class MockDelta:
    """Mock streaming delta."""

    def __init__(self, content: Optional[str] = None, role: Optional[str] = None):
        self.content = content
        self.role = role


class MockChoice:
    """Mock completion choice."""

    def __init__(
        self,
        message: Optional[MockMessage] = None,
        delta: Optional[MockDelta] = None,
        finish_reason: str = "stop",
        index: int = 0,
    ):
        self.message = message or MockMessage()
        self.delta = delta
        self.finish_reason = finish_reason
        self.index = index


class MockUsage:
    """Mock token usage."""

    def __init__(
        self,
        prompt_tokens: int = 10,
        completion_tokens: int = 20,
        total_tokens: int = 30,
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class MockTimeInfo:
    """Mock Cerebras time_info."""

    def __init__(
        self,
        queue_time: float = 0.001,
        prompt_time: float = 0.010,
        completion_time: float = 0.050,
        total_time: float = 0.061,
    ):
        self.queue_time = queue_time
        self.prompt_time = prompt_time
        self.completion_time = completion_time
        self.total_time = total_time


class MockChatCompletion:
    """Mock Cerebras chat completion response."""

    def __init__(
        self,
        id: str = "chatcmpl-123",
        model: str = "llama3.1-8b",
        choices: Optional[List[MockChoice]] = None,
        usage: Optional[MockUsage] = None,
        time_info: Optional[MockTimeInfo] = None,
    ):
        self.id = id
        self.object = "chat.completion"
        self.model = model
        self.choices = choices or [MockChoice()]
        self.usage = usage or MockUsage()
        self.time_info = time_info or MockTimeInfo()
        self.created = 1234567890

    def model_dump(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "object": self.object,
            "model": self.model,
            "choices": [
                {
                    "index": c.index,
                    "message": {"role": c.message.role, "content": c.message.content},
                    "finish_reason": c.finish_reason,
                }
                for c in self.choices
            ],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "time_info": {
                "queue_time": self.time_info.queue_time,
                "prompt_time": self.time_info.prompt_time,
                "completion_time": self.time_info.completion_time,
                "total_time": self.time_info.total_time,
            },
        }


class MockStreamChunk:
    """Mock streaming chunk."""

    def __init__(
        self,
        id: str = "chatcmpl-123",
        model: str = "llama3.1-8b",
        delta_content: Optional[str] = None,
        delta_role: Optional[str] = None,
        finish_reason: Optional[str] = None,
        usage: Optional[MockUsage] = None,
        time_info: Optional[MockTimeInfo] = None,
    ):
        self.id = id
        self.model = model
        self.choices = [
            MockChoice(
                delta=MockDelta(content=delta_content, role=delta_role),
                finish_reason=finish_reason or "",
            )
        ]
        self.usage = usage
        self.time_info = time_info

    def model_dump(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "model": self.model,
            "choices": [
                {
                    "delta": {
                        "content": self.choices[0].delta.content,
                        "role": self.choices[0].delta.role,
                    },
                    "finish_reason": self.choices[0].finish_reason,
                }
            ],
        }
        if self.usage:
            result["usage"] = {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            }
        if self.time_info:
            result["time_info"] = {
                "queue_time": self.time_info.queue_time,
                "prompt_time": self.time_info.prompt_time,
                "completion_time": self.time_info.completion_time,
                "total_time": self.time_info.total_time,
            }
        return result


class MockTracerProvider:
    """Mock OpenTelemetry TracerProvider."""

    def __init__(self):
        self.tracers = {}

    def get_tracer(self, name: str, version: str = None):
        if name not in self.tracers:
            self.tracers[name] = MockTracer(name)
        return self.tracers[name]


class MockTracer:
    """Mock OpenTelemetry Tracer."""

    def __init__(self, name: str):
        self.name = name
        self.spans = []

    def start_span(self, name: str, **kwargs):
        span = MockSpan(name)
        span.set_attributes(kwargs.get("attributes", {}))
        self.spans.append(span)
        return span

    def start_as_current_span(self, name: str, **kwargs):
        return MockSpanContext(MockSpan(name))


class MockSpan:
    """Mock OpenTelemetry Span."""

    def __init__(self, name: str):
        self.name = name
        self.attributes = {}
        self.events = []
        self.status = None
        self._ended = False

    def set_attribute(self, key: str, value: Any):
        self.attributes[key] = value

    def set_attributes(self, attributes: Dict[str, Any]):
        self.attributes.update(attributes)

    def add_event(self, name: str, attributes: Dict[str, Any] = None):
        self.events.append({"name": name, "attributes": attributes or {}})

    def set_status(self, status):
        self.status = status

    def record_exception(self, exception):
        self.events.append({"name": "exception", "exception": exception})

    def end(self):
        self._ended = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.end()


class MockSpanContext:
    """Mock span context manager."""

    def __init__(self, span: MockSpan):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, *args):
        self.span.end()


@pytest.fixture
def mock_completion():
    """Create a mock chat completion response."""
    return MockChatCompletion()


@pytest.fixture
def mock_stream_chunks():
    """Create mock streaming chunks."""
    return [
        MockStreamChunk(delta_role="assistant"),
        MockStreamChunk(delta_content="Hello"),
        MockStreamChunk(delta_content=" there"),
        MockStreamChunk(delta_content="!"),
        MockStreamChunk(
            finish_reason="stop",
            usage=MockUsage(),
            time_info=MockTimeInfo(),
        ),
    ]


@pytest.fixture
def mock_tracer_provider():
    """Create a mock tracer provider."""
    return MockTracerProvider()


@pytest.fixture
def mock_tracer():
    """Create a mock tracer."""
    return MockTracer("test-tracer")
