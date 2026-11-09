"""Deferred real-exporter controls for generated exception.type value policy."""

import pytest
from fi_instrumentation.instrumentation._spans import FiSpan
from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan, StatusCode


EMAIL = "jane.doe@example.com"
SECOND_EMAIL = "case.owner@example.net"
IMAGE = "data:image/png;base64," + "QUJD" * 8
TIMESTAMP = 123456789
DETAIL_KEYS = {"exception.message", "exception.stacktrace"}


@pytest.fixture
def sdk():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield exporter, provider
    finally:
        provider.shutdown()


def _dynamic_error(module, qualname):
    cls = type("GeneratedError", (Exception,), {
        "__module__": module,
        "__qualname__": qualname,
    })
    return cls("ordinary failure")


def _qualified(module, qualname):
    return qualname if not module or module == "builtins" else module + "." + qualname


def _raised(error):
    try:
        raise error
    except Exception as caught:
        assert caught is error
        return caught


def _snapshot(error):
    return (
        type(error), error.args, error.__traceback__, error.__cause__,
        error.__context__, error.__suppress_context__, dict(error.__dict__),
    )


def _assert_unchanged(error, before):
    cls, args, tb, cause, context, suppressed, fields = before
    assert type(error) is cls
    assert error.args is args
    assert error.__traceback__ is tb
    assert error.__cause__ is cause
    assert error.__context__ is context
    assert error.__suppress_context__ is suppressed
    assert error.__dict__ == fields


def _event(span):
    assert len(span.events) == 1
    event = span.events[0]
    assert event.name == "exception"
    assert event.attributes
    assert event.timestamp == TIMESTAMP
    return event


def _record(provider, config, error, attributes=None, escaped=True, status=StatusCode.ERROR):
    span = FiSpan(provider.get_tracer("generated-type-policy").start_span("record"), config)
    span.record_exception(error, attributes=attributes, timestamp=TIMESTAMP, escaped=escaped)
    span.set_status(status)
    span.end()


def _native(provider, error, attributes=None, escaped=True):
    span = provider.get_tracer("native-type-control").start_span("native")
    span.record_exception(error, attributes=attributes, timestamp=TIMESTAMP, escaped=escaped)
    span.end()


def _explicit(provider, config, type_value, error_type=None):
    span = FiSpan(provider.get_tracer("explicit-type-control").start_span("explicit"), config)
    span.add_event("exception", {
        "exception.type": type_value,
        "exception.escaped": "True",
        "control": "populated",
    }, timestamp=TIMESTAMP)
    span.set_attribute("error.type", type_value if error_type is None else error_type)
    span.end()


def _assert_shape(result, hide, escaped=True):
    event = _event(result)
    assert event.attributes["exception.escaped"] == str(escaped)
    if hide:
        assert DETAIL_KEYS.isdisjoint(event.attributes)
        assert result.status.description is None
    else:
        assert event.attributes["exception.message"] == "ordinary failure"
        assert event.attributes["exception.stacktrace"]
    assert result.status.status_code is StatusCode.ERROR
    return event


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("metadata", ["module", "qualname", "both"])
def test_generated_pii_type_matches_explicit_policy(sdk, hide, pii, metadata):
    exporter, provider = sdk
    module = EMAIL if metadata in {"module", "both"} else "safe_module"
    qualname = SECOND_EMAIL if metadata in {"qualname", "both"} else "TenantError"
    raw_type = _qualified(module, qualname)
    tokens = [token for token in (EMAIL, SECOND_EMAIL) if token in raw_type]
    assert tokens
    original = _raised(_dynamic_error(module, qualname))
    before = _snapshot(original)
    config = TraceConfig(pii_redaction=pii, hide_error_details=hide, base64_image_max_length=4096)
    _native(provider, original)
    _explicit(provider, config, raw_type)
    _record(provider, config, original)
    raw, control, result = exporter.get_finished_spans()
    assert _event(raw).attributes["exception.type"] == raw_type
    event = _assert_shape(result, hide)
    expected = _event(control).attributes["exception.type"]
    assert isinstance(expected, str) and expected
    assert event.attributes["exception.type"] == expected
    assert result.attributes["error.type"] == control.attributes["error.type"]
    for token in tokens:
        assert token in raw.to_json()
        assert (token in result.to_json()) is (not pii)
    if not pii:
        assert event.attributes["exception.type"] == raw_type
    else:
        assert event.attributes["exception.type"] != raw_type
    _assert_unchanged(original, before)


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("boundary", ["below", "at", "above"])
def test_generated_image_type_honors_cap_boundary(sdk, hide, pii, boundary):
    exporter, provider = sdk
    limit = len(IMAGE) + {"below": 1, "at": 0, "above": -1}[boundary]
    original = _dynamic_error("builtins", IMAGE)
    before = _snapshot(original)
    config = TraceConfig(pii_redaction=pii, hide_error_details=hide, base64_image_max_length=limit)
    _native(provider, original)
    _explicit(provider, config, IMAGE)
    _record(provider, config, original)
    raw, control, result = exporter.get_finished_spans()
    assert _event(raw).attributes["exception.type"] == IMAGE
    assert IMAGE in raw.to_json()
    event = _assert_shape(result, hide)
    expected = _event(control).attributes["exception.type"]
    assert isinstance(expected, str) and expected
    assert event.attributes["exception.type"] == expected
    assert result.attributes["error.type"] == control.attributes["error.type"]
    if boundary == "above":
        assert event.attributes["exception.type"] != IMAGE
        assert IMAGE not in result.to_json()
    else:
        assert event.attributes["exception.type"] == IMAGE
        assert result.attributes["error.type"] == IMAGE
        assert IMAGE in result.to_json()
    _assert_unchanged(original, before)


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
def test_generated_type_combines_image_cap_and_pii(sdk, hide, pii):
    exporter, provider = sdk
    raw_type = EMAIL + " " + IMAGE
    original = _dynamic_error("builtins", raw_type)
    before = _snapshot(original)
    config = TraceConfig(
        pii_redaction=pii, hide_error_details=hide, base64_image_max_length=len(IMAGE) - 1,
    )
    _native(provider, original)
    _explicit(provider, config, raw_type)
    _record(provider, config, original)
    raw, control, result = exporter.get_finished_spans()
    assert _event(raw).attributes["exception.type"] == raw_type
    assert EMAIL in raw.to_json() and IMAGE in raw.to_json()
    event = _assert_shape(result, hide)
    expected = _event(control).attributes["exception.type"]
    assert isinstance(expected, str) and expected
    assert event.attributes["exception.type"] == expected
    assert result.attributes["error.type"] == control.attributes["error.type"]
    assert IMAGE not in result.to_json()
    assert (EMAIL in result.to_json()) is (not pii)
    _assert_unchanged(original, before)


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("override", ["ordinary", "pii", "image", "empty"])
def test_caller_type_override_wins_without_renaming_span_error(sdk, hide, pii, override):
    exporter, provider = sdk
    value = {"ordinary": "caller.Override", "pii": SECOND_EMAIL, "image": IMAGE, "empty": ""}[override]
    original = _dynamic_error(EMAIL, "OriginalError")
    raw_type = EMAIL + ".OriginalError"
    before = _snapshot(original)
    attributes = {
        "exception.type": value,
        "exception.escaped": "False",
        "exception.message": "caller message",
        "exception.stacktrace": "caller stack",
        "extra": "keep me",
        "attempt": 7,
        "retryable": True,
    }
    original_attributes = dict(attributes)
    config = TraceConfig(
        pii_redaction=pii, hide_error_details=hide, base64_image_max_length=len(IMAGE) - 1,
    )
    _explicit(provider, config, value, error_type=raw_type)
    _record(provider, config, original, attributes=attributes, escaped=True)
    control, result = exporter.get_finished_spans()
    event = _event(result)
    assert event.attributes["exception.type"] == _event(control).attributes["exception.type"]
    assert event.attributes["exception.escaped"] == "False"
    assert event.attributes["extra"] == "keep me"
    assert event.attributes["attempt"] == 7
    assert event.attributes["retryable"] is True
    assert result.attributes["error.type"] == control.attributes["error.type"]
    assert result.status.status_code is StatusCode.ERROR
    if override in {"ordinary", "empty"} or (override == "pii" and not pii):
        assert event.attributes["exception.type"] == value
    if override == "ordinary":
        assert result.attributes["error.type"] != "caller.Override"
    if override == "image":
        assert event.attributes["exception.type"]
        assert IMAGE not in result.to_json()
    if pii:
        assert EMAIL not in result.to_json()
        assert SECOND_EMAIL not in result.to_json()
    if hide:
        assert DETAIL_KEYS.isdisjoint(event.attributes)
        assert result.status.description is None
    else:
        assert event.attributes["exception.message"] == "caller message"
        assert event.attributes["exception.stacktrace"] == "caller stack"
    assert attributes == original_attributes
    _assert_unchanged(original, before)


def _ordinary_error(kind):
    if kind == "builtin":
        return _raised(ValueError("ordinary failure")), "ValueError"
    if kind == "custom":
        return _raised(_dynamic_error("sample_app.errors", "Outer.NestedError")), "sample_app.errors.Outer.NestedError"
    try:
        raise ValueError("ordinary cause")
    except ValueError as cause:
        try:
            raise RuntimeError("ordinary failure") from cause
        except RuntimeError as original:
            return original, "RuntimeError"


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("escaped", [False, True], ids=["handled", "escaped"])
@pytest.mark.parametrize("kind", ["builtin", "custom", "chained"])
def test_ordinary_types_keep_native_shape_and_identity(sdk, hide, pii, escaped, kind):
    exporter, provider = sdk
    original, expected_type = _ordinary_error(kind)
    before = _snapshot(original)
    attributes = {"extra": "ordinary caller", "attempt": 3}
    config = TraceConfig(pii_redaction=pii, hide_error_details=hide, base64_image_max_length=4096)
    _native(provider, original, attributes=attributes, escaped=escaped)
    _record(provider, config, original, attributes=attributes, escaped=escaped)
    raw, result = exporter.get_finished_spans()
    raw_event = _event(raw)
    event = _assert_shape(result, hide, escaped)
    expected = {
        key: value for key, value in raw_event.attributes.items()
        if not (hide and key in DETAIL_KEYS)
    }
    assert dict(event.attributes) == expected
    assert list(event.attributes) == list(expected)
    assert event.attributes["exception.type"] == expected_type
    assert result.attributes["error.type"] == expected_type
    if not hide:
        assert "Traceback (most recent call last)" in event.attributes["exception.stacktrace"]
        if kind == "chained":
            assert "ordinary cause" in event.attributes["exception.stacktrace"]
    assert attributes == {"extra": "ordinary caller", "attempt": 3}
    _assert_unchanged(original, before)


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
def test_context_manager_preserves_propagated_exception(sdk, hide):
    exporter, provider = sdk
    original = _dynamic_error(EMAIL, "ContextError")
    original_args = original.args
    config = TraceConfig(pii_redaction=True, hide_error_details=hide, base64_image_max_length=4096)
    span = FiSpan(provider.get_tracer("context-type-policy").start_span("context"), config)
    caught_original = False
    try:
        with span:
            try:
                raise original
            except Exception:
                original_tb = original.__traceback__
                raise
    except Exception as caught:
        caught_original = True
        assert caught is original
        assert caught.__traceback__ is original_tb
    assert caught_original is True
    assert original.args is original_args
    (result,) = exporter.get_finished_spans()
    assert len(result.events) == 1
    event = result.events[0]
    assert event.name == "exception"
    assert event.attributes["exception.type"]
    assert event.attributes["exception.escaped"] == "True"
    assert result.status.status_code is StatusCode.ERROR
    assert result.attributes["error.type"]
    assert EMAIL not in result.to_json()
    if hide:
        assert DETAIL_KEYS.isdisjoint(event.attributes)
        assert result.status.description is None
    else:
        assert event.attributes["exception.message"] == "ordinary failure"
        assert event.attributes["exception.stacktrace"]


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
def test_recovered_exception_does_not_synthesize_span_error_type(sdk, hide):
    exporter, provider = sdk
    original = _dynamic_error(EMAIL, "RecoveredError")
    before = _snapshot(original)
    config = TraceConfig(pii_redaction=True, hide_error_details=hide, base64_image_max_length=4096)
    _record(provider, config, original, escaped=False, status=StatusCode.OK)
    (result,) = exporter.get_finished_spans()
    event = _event(result)
    assert event.attributes["exception.type"]
    assert event.attributes["exception.escaped"] == "False"
    assert result.status.status_code is StatusCode.OK
    assert "error.type" not in result.attributes
    assert EMAIL not in result.to_json()
    _assert_unchanged(original, before)


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
def test_preexisting_span_error_type_is_not_overwritten(sdk, hide):
    exporter, provider = sdk
    original = _dynamic_error(EMAIL, "ProviderError")
    before = _snapshot(original)
    config = TraceConfig(pii_redaction=True, hide_error_details=hide, base64_image_max_length=4096)
    span = FiSpan(provider.get_tracer("preexisting-error-control").start_span("existing"), config)
    span.set_attribute("error.type", "provider.PinnedError")
    span.record_exception(original, timestamp=TIMESTAMP, escaped=True)
    span.set_status(StatusCode.ERROR)
    span.end()
    (result,) = exporter.get_finished_spans()
    _assert_shape(result, hide)
    assert result.attributes["error.type"] == "provider.PinnedError"
    assert EMAIL not in result.to_json()
    _assert_unchanged(original, before)


def test_non_recording_span_skips_exception_processing():
    class UnformattableError(Exception):
        def __str__(self):
            raise AssertionError("non-recording spans must not format exceptions")

    original = UnformattableError("ordinary failure")
    before = _snapshot(original)
    span = FiSpan(NonRecordingSpan(INVALID_SPAN_CONTEXT), TraceConfig(pii_redaction=True))
    assert span.record_exception(original, timestamp=TIMESTAMP, escaped=True) is None
    assert span.is_recording() is False
    _assert_unchanged(original, before)
