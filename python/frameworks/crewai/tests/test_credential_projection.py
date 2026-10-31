"""Security regressions use synthetic values only and never contact a provider."""

import json
from unittest.mock import MagicMock

from traceai_crewai._wrappers import (
    _safe_data,
    _task_input,
    _get_input_value,
    _ExecuteCoreWrapper,
)

SENTINEL = "FAKE_CREDENTIAL_ONLY_6c3f"


class ExplosiveRuntime:
    def __repr__(self):
        raise AssertionError("runtime repr evaluated")

    def to_dict(self):
        raise AssertionError("runtime to_dict evaluated")

    def dict(self):
        raise AssertionError("runtime dict evaluated")

    def model_dump(self):
        raise AssertionError("runtime model_dump evaluated")


def test_opaque_objects_are_never_evaluated_even_when_nested():
    assert _safe_data({"value": [ExplosiveRuntime(), "user text"]}) == {
        "value": ["[omitted runtime object]", "user text"]
    }


def test_task_projection_supports_positional_and_keywords_without_agent_dump():
    def execute(agent=None, context=None, tools=None):
        pass

    task = MagicMock(description="task text", expected_output="expected text")
    positional = json.loads(
        _task_input(
            execute,
            task,
            (ExplosiveRuntime(), "context text", [ExplosiveRuntime()]),
            {},
        )
    )
    keywords = json.loads(
        _task_input(
            execute,
            task,
            (),
            {
                "agent": ExplosiveRuntime(),
                "context": "context text",
                "tools": [ExplosiveRuntime()],
            },
        )
    )
    assert (
        positional
        == keywords
        == {
            "description": "task text",
            "expected_output": "expected text",
            "context": "context text",
        }
    )


def test_generic_input_never_serializes_runtime_objects():
    def execute(arg, **kwargs):
        pass

    payload = _get_input_value(execute, ExplosiveRuntime(), extra=ExplosiveRuntime())
    assert SENTINEL not in payload
    assert json.loads(payload) == {}


def test_exception_does_not_enter_span_and_same_error_propagates():
    tracer = MagicMock()
    span = tracer.start_as_current_span.return_value.__enter__.return_value
    task = MagicMock(
        description="task", expected_output="result", key="task-key", id="task-id"
    )
    error = RuntimeError(SENTINEL)

    def execute(agent=None, context=None, tools=None):
        raise error

    try:
        _ExecuteCoreWrapper(tracer)(execute, task, (), {"context": "context"})
    except RuntimeError as actual:
        assert actual is error
    else:
        raise AssertionError("application error was swallowed")
    assert not span.record_exception.called
    assert SENTINEL not in str(span.mock_calls)
