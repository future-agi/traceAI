import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from temporalio.worker import ActivityInboundInterceptor

from traceai_temporal import TemporalInterceptor
from traceai_temporal._content import MAX_BYTES, MAX_NODES, content_attributes, serialize


def test_deterministic_json_and_non_json_builtin_values():
    assert serialize({"b": 2, "a": [True, None, (1, 2)]}) == '{"a":[true,null,[1,2]],"b":2}'
    assert serialize({"a": 1, "b": 2}) == serialize({"b": 2, "a": 1})
    assert json.loads(serialize([b"raw-media", float("nan"), float("inf"), -float("inf"), 1 << 10000])) == [
        "bytes", "NaN", "Infinity", "-Infinity", "int",
    ]
    assert content_attributes("output", None) == {"output.value": "null", "output.mime_type": "application/json"}


def test_unknown_values_and_subclasses_never_invoke_application_hooks():
    calls = []

    class Meta(type):
        def __getattribute__(self, name):
            calls.append(name)
            raise AssertionError("metaclass hook")

        @property
        def __name__(self):
            raise AssertionError("metaclass property")

    class Dangerous(metaclass=Meta):
        def __str__(self):
            raise AssertionError("str")

        def __repr__(self):
            raise AssertionError("repr")

        def __json__(self):
            raise AssertionError("json")

        def __getattribute__(self, name):
            calls.append(name)
            raise AssertionError("getattribute")

    class Dict(dict):
        def items(self):
            raise AssertionError("items")

    class List(list):
        def __iter__(self):
            raise AssertionError("iter")

    class String(str):
        def __str__(self):
            raise AssertionError("str subclass")

    value = Dangerous()
    assert json.loads(serialize([value, Dict(secret="hidden"), List(["hidden"]), String("hidden")])) == [
        "Dangerous", "Dict", "List", "String",
    ]
    assert json.loads(serialize({value: value})) == {"Dangerous": "<omitted: unsupported key>"}
    assert "unmarked-secret" not in serialize({String("api_key"): "unmarked-secret"})
    assert calls == []


@pytest.mark.parametrize("equal", [False, True])
def test_unsupported_metaclass_equality_is_never_called(equal):
    calls = []

    class Meta(type):
        def __eq__(self, other):
            calls.append(other)
            return equal

    class Custom(metaclass=Meta):
        pass

    assert serialize([Custom()]) == '["Custom"]'
    assert calls == []


def test_oversized_deep_cyclic_and_branching_content_is_bounded():
    large = "sensitive-prefix" + "x" * MAX_BYTES
    assert "sensitive-prefix" not in serialize(large)
    assert "content limit" in serialize(large)
    assert len(serialize(["\x00" * 1000] * MAX_NODES).encode()) <= MAX_BYTES
    assert len(json.loads(serialize(list(range(MAX_NODES * 2))))) <= MAX_NODES + 1
    cyclic = []
    cyclic.append(cyclic)
    assert "content limit" in serialize(cyclic)
    branching = ["leaf"]
    for _ in range(40):
        branching = [branching, branching]
    assert len(serialize(branching).encode()) <= MAX_BYTES
    assert "content limit" in serialize(branching)


def test_text_budget_never_detaches_credential_value_from_its_key():
    # Leave enough text budget for an eight-character value but not its longer
    # credential key. Renaming only that key would bypass S1 structural scrubbing.
    available = MAX_BYTES - MAX_NODES * 64
    filler = "x" * ((available - 8 - 2 - 60) // 6)
    captured = serialize({"a": filler, "authorization": "secret12"})
    assert "secret12" not in captured
    assert "content limit" in captured


@pytest.mark.parametrize("source", ["config", "environment"])
@pytest.mark.parametrize("side", ["input", "output"])
async def test_client_and_activity_content_hide_controls(tracing, monkeypatch, source, side):
    provider, exporter = tracing
    if source == "environment":
        monkeypatch.setenv("FI_HIDE_" + side.upper() + "S", "true")
        config = TraceConfig()
    else:
        config = TraceConfig(**{"hide_" + side + "s": True})
    root = TemporalInterceptor(provider, config)
    client_next = Mock()
    sentinel = object()
    client_next.start_workflow = AsyncMock(return_value=sentinel)
    request = SimpleNamespace(workflow="Unit", id="unit", headers={}, args=["private-input"])
    assert await root.intercept_client(client_next).start_workflow(request) is sentinel
    info = SimpleNamespace(activity_type="echo", workflow_id="unit", workflow_run_id="run",
                           activity_id="1", attempt=2, is_local=False)
    activity_next = Mock(spec=ActivityInboundInterceptor)
    activity_next.execute_activity = AsyncMock(return_value="private-output")
    with patch("traceai_temporal._interceptor.activity.info", return_value=info):
        result = await root.intercept_activity(activity_next).execute_activity(request)
    assert result == "private-output"
    for span in exporter.get_finished_spans():
        attrs = span.attributes
        if side + ".value" in attrs:
            assert "private-" + side not in attrs[side + ".value"]
            assert side + ".mime_type" not in attrs
    activity_span = exporter.get_finished_spans()[-1]
    visible = "output" if side == "input" else "input"
    assert "private-" + visible in activity_span.attributes[visible + ".value"]


async def test_content_capture_errors_and_activity_failure_preserve_application_identity(tracing):
    provider, exporter = tracing
    root = TemporalInterceptor(provider)
    failure = ValueError("raw private stack content")
    next = Mock(spec=ActivityInboundInterceptor)
    next.execute_activity = AsyncMock(side_effect=failure)
    info = SimpleNamespace(activity_type="failure", workflow_id="unit", workflow_run_id="run",
                           activity_id="1", attempt=1, is_local=False)
    with patch("traceai_temporal._interceptor.activity.info", return_value=info):
        with pytest.raises(ValueError) as caught:
            await root.intercept_activity(next).execute_activity(SimpleNamespace(headers={}, args=["input"]))
    assert caught.value is failure
    span = exporter.get_finished_spans()[-1]
    assert span.status.status_code is trace.StatusCode.ERROR
    assert span.attributes["error.type"] == "ValueError"
    assert "output.value" not in span.attributes
    assert span.events == ()
    assert "raw private" not in (span.status.description or "")
    next.execute_activity = AsyncMock(return_value=failure)
    with (
        patch("traceai_temporal._interceptor.activity.info", return_value=info),
        patch("traceai_temporal._interceptor.content_attributes", side_effect=RuntimeError("telemetry")),
    ):
        assert await root.intercept_activity(next).execute_activity(SimpleNamespace(headers={}, args=[])) is failure
    next.execute_activity.assert_awaited_once()


@pytest.mark.parametrize("flag,side", [
    ("hide_input_text", "input"), ("hide_input_messages", "input"), ("hide_input_images", "input"),
    ("hide_output_text", "output"), ("hide_output_messages", "output"),
])
def test_input_output_attribute_families_use_core_hide_controls(tracing, flag, side):
    from traceai_temporal._interceptor import _set_content

    provider, exporter = tracing
    root = TemporalInterceptor(provider, TraceConfig(**{flag: True}))
    with root._span("families", trace.SpanKind.SERVER, {}) as span:
        _set_content(span, "input", ["private-input"])
        _set_content(span, "output", "private-output")
    attrs = exporter.get_finished_spans()[0].attributes
    assert "private-" + side not in attrs[side + ".value"]
    visible = "output" if side == "input" else "input"
    assert "private-" + visible in attrs[visible + ".value"]


def test_captured_content_reaches_final_sanitizer(tracing):
    from traceai_temporal._interceptor import _set_content

    provider, exporter = tracing
    value = {"api_key": "synthetic-field-credential", "url": "https://example.invalid/?signature=signed-secret",
             "input_audio": {"data": "QUJDRA==", "format": "wav"},
             "media": "data:audio/wav;base64,QUJDRA=="}
    root = TemporalInterceptor(provider)
    with root._span("privacy", trace.SpanKind.SERVER, {}) as span:
        _set_content(span, "input", [value])
        _set_content(span, "output", value)
    for side in ("input", "output"):
        captured = exporter.get_finished_spans()[0].attributes[side + ".value"]
        assert "synthetic-field-credential" not in captured
        assert "signed-secret" not in captured
        assert "QUJDRA==" not in captured
        assert "<redacted>" in captured
