"""Dependency-bound controls, NOT asserted to pass by this source-only packet.

The stateful test is a release blocker: the existing preflight/native fast path
can format again after inspecting four earlier strings. No dependency behavior
is mocked, patched, skipped or xfailed to make that case pass.
"""
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig

EMAIL = "jane.doe@example.com"


class CustomTextError(Exception):
    def __str__(self):
        return "custom failure " + self.args[0]


class StatefulTextError(Exception):
    def __init__(self):
        super().__init__("stateful")
        self.calls = 0

    def __str__(self):
        self.calls += 1
        return "plain failure" if self.calls <= 4 else "account " + EMAIL


@pytest.fixture
def sdk(clean_env):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


def exception_event(span):
    events = [event for event in span.events if event.name == "exception"]
    assert len(events) == 1
    return events[0]


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("text", ["ordinary", EMAIL], ids=["unchanged", "redacted"])
def test_custom_native_shape_timestamp_escaped_and_overrides(sdk, pii, text):
    exporter, provider = sdk
    native = provider.get_tracer("native")
    fi = FITracer(provider.get_tracer("fi"), config=TraceConfig(pii_redaction=pii, hide_error_details=False))
    error = CustomTextError(text)
    attributes = {"extra": "caller " + text, "exception.message": "override " + text}
    before = dict(attributes)
    for tracer in (native, fi):
        span = tracer.start_span("case")
        span.record_exception(error, attributes=attributes, timestamp=123456789, escaped=True)
        span.end()
    raw, result = exporter.get_finished_spans()
    raw_event, result_event = exception_event(raw), exception_event(result)
    expected = dict(raw_event.attributes)
    if pii:
        expected = {key: value.replace(EMAIL, "<EMAIL_ADDRESS>") if isinstance(value, str) else value
                    for key, value in expected.items()}
    assert dict(result_event.attributes) == expected
    assert result_event.timestamp == raw_event.timestamp == 123456789
    assert attributes == before
    assert error.args == (text,)


def test_stateful_native_fast_path_never_exports_uninspected_text(sdk):
    exporter, provider = sdk
    tracer = FITracer(provider.get_tracer("fi"), config=TraceConfig(pii_redaction=True, hide_error_details=False))
    error = StatefulTextError()
    span = tracer.start_span("case")
    span.record_exception(error, timestamp=123456789)
    span.end()
    (result,) = exporter.get_finished_spans()
    event = exception_event(result)
    assert EMAIL not in json.dumps(dict(event.attributes))
    assert event.timestamp == 123456789
    assert error.args == ("stateful",)


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
def test_application_exception_identity_and_one_event(sdk, pii):
    exporter, provider = sdk
    tracer = FITracer(provider.get_tracer("fi"), config=TraceConfig(pii_redaction=pii, hide_error_details=False))
    original = CustomTextError(EMAIL)
    with pytest.raises(CustomTextError) as caught:
        with tracer.start_as_current_span("case"):
            raise original
    assert caught.value is original
    (result,) = exporter.get_finished_spans()
    event = exception_event(result)
    expected = "<EMAIL_ADDRESS>" if pii else EMAIL
    assert event.attributes["exception.message"] == "custom failure " + expected
    assert result.status.description == "custom failure " + expected
    assert original.args == (EMAIL,)


# Additive controls for the final-formatting boundary. Expected values below
# use literal replacements, never TraceConfig or the implementation under test.
NATIVE_CAP = 64
NATIVE_IMAGE = "data:image/png;base64," + "QUJD" * 40
NATIVE_SMALL_IMAGE = "data:image/png;base64,QUJD"
NATIVE_MARK = "__REDACTED__"


class AlternatingNativeTextError(Exception):
    """Every adjacent pair of renderings has one ordinary and one sensitive value."""

    def __init__(self, text, first_sensitive):
        super().__init__(text)
        self.next_sensitive = first_sensitive
        self.rendered = []

    def __str__(self):
        value = "sensitive " + self.args[0] + " end" if self.next_sensitive else "ordinary failure"
        self.next_sensitive = not self.next_sensitive
        self.rendered.append(value)
        return value


def native_expected_text(value, pii):
    value = value.replace(NATIVE_IMAGE, NATIVE_MARK)
    return value.replace(EMAIL, "<EMAIL_ADDRESS>") if pii else value


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("first_sensitive", [False, True], ids=["ordinary-first", "sensitive-first"])
@pytest.mark.parametrize("content", ["email", "image", "both"])
def test_each_materialized_stateful_field_receives_policy(sdk, pii, first_sensitive, content):
    exporter, provider = sdk
    text = {
        "email": "account " + EMAIL,
        "image": "image " + NATIVE_IMAGE,
        "both": "account " + EMAIL + " image " + NATIVE_IMAGE,
    }[content]
    fi = FITracer(provider.get_tracer("fi"), config=TraceConfig(
        pii_redaction=pii, hide_error_details=False, base64_image_max_length=NATIVE_CAP,
    ))
    original = AlternatingNativeTextError(text, first_sensitive)
    raw_error = AlternatingNativeTextError(text, first_sensitive)
    for tracer, error in ((provider.get_tracer("native"), raw_error), (fi, original)):
        span = tracer.start_span("case")
        span.record_exception(error, timestamp=123456789, escaped=True)
        span.set_status(StatusCode.ERROR)
        span.end()
    raw, result = exporter.get_finished_spans()
    raw_event, event = exception_event(raw), exception_event(result)
    # Real native output is the sensitivity control: this data reaches a sink
    # without FI, regardless of how many bookkeeping renderings FI performs.
    raw_text = raw_event.attributes["exception.message"] + raw_event.attributes["exception.stacktrace"]
    assert text in raw_text
    ordinary = "ordinary failure"
    sensitive = "sensitive " + native_expected_text(text, pii) + " end"
    assert event.attributes["exception.message"] in (ordinary, sensitive)
    assert event.attributes["exception.stacktrace"].endswith((ordinary + "\n", sensitive + "\n"))
    final_text = event.attributes["exception.message"] + event.attributes["exception.stacktrace"]
    assert sensitive in final_text
    assert ordinary in final_text
    assert NATIVE_IMAGE not in final_text
    if pii:
        assert EMAIL not in final_text
    elif content in ("email", "both"):
        assert EMAIL in final_text
    if content in ("image", "both"):
        assert NATIVE_MARK in final_text
    assert set(event.attributes) == {
        "exception.type", "exception.message", "exception.stacktrace", "exception.escaped",
    }
    assert event.attributes["exception.type"] == f"{__name__}.AlternatingNativeTextError"
    assert result.attributes["error.type"] == event.attributes["exception.type"]
    assert event.attributes["exception.escaped"] == "True"
    assert event.timestamp == raw_event.timestamp == 123456789
    assert len(result.events) == len(raw.events) == 1
    assert original.args == (text,)
    assert original.__traceback__ is None


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("oversized", [False, True], ids=["small-image", "oversized-image"])
def test_final_caller_overrides_preserve_metadata_and_apply_policy(sdk, pii, hide, oversized):
    exporter, provider = sdk
    image = NATIVE_IMAGE if oversized else NATIVE_SMALL_IMAGE
    text = "account " + EMAIL + " image " + image + " end"
    attributes = {
        "exception.type": "caller.Override",
        "exception.message": "override message " + text,
        "exception.stacktrace": "override stack " + text,
        "exception.escaped": "False",
        "error.message": "override error " + text,
        "provider.exception.message": "provider " + text,
        "extra": "caller " + text,
        "attempt": 7,
        "retryable": True,
    }
    before = dict(attributes)
    original = ValueError("generated " + text)
    fi = FITracer(provider.get_tracer("fi"), config=TraceConfig(
        pii_redaction=pii, hide_error_details=hide, base64_image_max_length=NATIVE_CAP,
    ))
    for tracer in (provider.get_tracer("native"), fi):
        span = tracer.start_span("case")
        span.record_exception(original, attributes=attributes, timestamp=123456789, escaped=True)
        span.set_status(StatusCode.ERROR)
        span.end()
    raw, result = exporter.get_finished_spans()
    raw_event, event = exception_event(raw), exception_event(result)
    assert dict(raw_event.attributes) == before
    detail_keys = {"exception.message", "exception.stacktrace", "error.message", "provider.exception.message"}
    expected = {
        key: native_expected_text(value, pii) if isinstance(value, str) else value
        for key, value in raw_event.attributes.items()
        if not (hide and key in detail_keys)
    }
    assert dict(event.attributes) == expected
    assert event.timestamp == raw_event.timestamp == 123456789
    assert event.attributes["exception.type"] == "caller.Override"
    assert event.attributes["exception.escaped"] == "False"
    # Event overrides must not rename the original error used for status association.
    assert result.attributes["error.type"] == "ValueError"
    assert result.status.status_code is StatusCode.ERROR
    assert len(result.events) == len(raw.events) == 1
    assert attributes == before
    assert original.args == ("generated " + text,)
    assert original.__traceback__ is None
    assert "generated " not in json.dumps(dict(event.attributes))
    if hide:
        assert detail_keys.isdisjoint(event.attributes)
        assert result.status.description is None
    if oversized:
        assert NATIVE_IMAGE not in json.dumps(dict(event.attributes))
        assert NATIVE_MARK in event.attributes["extra"]
    else:
        assert NATIVE_SMALL_IMAGE in event.attributes["extra"]
    if pii:
        assert EMAIL not in json.dumps(dict(event.attributes))
    else:
        assert EMAIL in event.attributes["extra"]


def native_parity_error(kind):
    try:
        if kind == "builtin":
            raise ValueError("ordinary failure")
        if kind == "custom":
            raise CustomTextError("ordinary")
        try:
            raise KeyError("ordinary cause")
        except KeyError as cause:
            raise RuntimeError("ordinary outer") from cause
    except Exception as error:
        return error


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("escaped", [False, True], ids=["handled", "escaped"])
@pytest.mark.parametrize("kind", ["builtin", "custom", "chained"])
def test_ordinary_raised_exception_matches_real_native_event(sdk, pii, escaped, kind):
    exporter, provider = sdk
    error = native_parity_error(kind)
    before_args, before_tb, before_cause = error.args, error.__traceback__, error.__cause__
    attributes = {"extra": "ordinary caller", "attempt": 3}
    fi = FITracer(provider.get_tracer("fi"), config=TraceConfig(
        pii_redaction=pii, hide_error_details=False, base64_image_max_length=NATIVE_CAP,
    ))
    for tracer in (provider.get_tracer("native"), fi):
        span = tracer.start_span("case")
        span.record_exception(error, attributes=attributes, timestamp=123456789, escaped=escaped)
        span.set_status(StatusCode.ERROR)
        span.end()
    raw, result = exporter.get_finished_spans()
    raw_event, event = exception_event(raw), exception_event(result)
    assert dict(event.attributes) == dict(raw_event.attributes)
    assert list(event.attributes) == list(raw_event.attributes)
    assert event.timestamp == raw_event.timestamp == 123456789
    assert event.attributes["exception.escaped"] == str(escaped)
    assert "Traceback (most recent call last)" in event.attributes["exception.stacktrace"]
    if kind == "chained":
        assert "ordinary cause" in event.attributes["exception.stacktrace"]
        assert "ordinary outer" in event.attributes["exception.stacktrace"]
    assert result.attributes["error.type"] == raw_event.attributes["exception.type"]
    assert len(result.events) == len(raw.events) == 1
    assert error.args == before_args
    assert error.__traceback__ is before_tb
    assert error.__cause__ is before_cause
    assert attributes == {"extra": "ordinary caller", "attempt": 3}


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
@pytest.mark.parametrize("entry", ["current", "span"])
def test_stateful_context_preserves_original_exception_and_single_event(sdk, pii, hide, entry):
    exporter, provider = sdk
    tracer = FITracer(provider.get_tracer("fi"), config=TraceConfig(
        pii_redaction=pii, hide_error_details=hide, base64_image_max_length=NATIVE_CAP,
    ))
    text = "account " + EMAIL + " image " + NATIVE_IMAGE + " end"
    original = AlternatingNativeTextError(text, first_sensitive=True)
    cause = ValueError(text)
    original.__cause__ = cause
    context = tracer.start_as_current_span("case") if entry == "current" else tracer.start_span("case")
    with pytest.raises(AlternatingNativeTextError) as caught:
        with context as span:
            assert type(span).__name__ == "FiSpan"
            raise original
    assert caught.value is original
    assert original.args == (text,)
    assert original.__cause__ is cause
    assert cause.args == (text,)
    (result,) = exporter.get_finished_spans()
    event = exception_event(result)
    assert len(result.events) == 1
    assert result.status.status_code is StatusCode.ERROR
    assert result.attributes["error.type"] == f"{__name__}.AlternatingNativeTextError"
    assert event.attributes["exception.type"] == result.attributes["error.type"]
    exported = json.dumps({
        "event": dict(event.attributes), "status": result.status.description,
        "attributes": dict(result.attributes),
    })
    assert NATIVE_IMAGE not in exported
    if pii or hide:
        assert EMAIL not in exported
    else:
        assert EMAIL in event.attributes["exception.stacktrace"]
    if hide:
        assert set(event.attributes) == {"exception.type", "exception.escaped"}
        assert result.status.description is None
    else:
        assert NATIVE_MARK in event.attributes["exception.stacktrace"]
        assert "Traceback (most recent call last)" in event.attributes["exception.stacktrace"]
    if entry == "span":
        assert event.attributes["exception.escaped"] == "True"


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("hide", [False, True], ids=["visible", "hidden"])
def test_sensitive_cause_stack_is_checked_even_when_message_is_ordinary(sdk, pii, hide):
    exporter, provider = sdk
    text = "account " + EMAIL + " image " + NATIVE_IMAGE + " end"
    try:
        try:
            raise ValueError(text)
        except ValueError as cause:
            raise RuntimeError("ordinary outer") from cause
    except RuntimeError as caught:
        error = caught
    fi = FITracer(provider.get_tracer("fi"), config=TraceConfig(
        pii_redaction=pii, hide_error_details=hide, base64_image_max_length=NATIVE_CAP,
    ))
    for tracer in (provider.get_tracer("native"), fi):
        span = tracer.start_span("case")
        span.record_exception(error, timestamp=123456789)
        span.end()
    raw, result = exporter.get_finished_spans()
    raw_event, event = exception_event(raw), exception_event(result)
    assert raw_event.attributes["exception.message"] == "ordinary outer"
    assert EMAIL in raw_event.attributes["exception.stacktrace"]
    assert NATIVE_IMAGE in raw_event.attributes["exception.stacktrace"]
    expected = {
        key: native_expected_text(value, pii) if isinstance(value, str) else value
        for key, value in raw_event.attributes.items()
        if not (hide and key in {"exception.message", "exception.stacktrace"})
    }
    assert dict(event.attributes) == expected
    assert event.timestamp == raw_event.timestamp == 123456789
    assert len(result.events) == len(raw.events) == 1
    assert error.args == ("ordinary outer",)
    assert error.__cause__.args == (text,)


def test_default_exception_timestamp_and_escaped_use_sdk_semantics(sdk):
    exporter, provider = sdk
    tracer = FITracer(provider.get_tracer("fi"), config=TraceConfig(
        pii_redaction=True, hide_error_details=False,
    ))
    span = tracer.start_span("case")
    span.record_exception(ValueError("ordinary failure"))
    span.end()
    (result,) = exporter.get_finished_spans()
    event = exception_event(result)
    assert result.start_time <= event.timestamp <= result.end_time
    assert event.attributes["exception.escaped"] == "False"
    assert event.attributes["exception.message"] == "ordinary failure"
    assert "error.type" not in result.attributes
    assert len(result.events) == 1


def test_ended_span_does_not_format_or_emit_an_exception(sdk):
    exporter, provider = sdk
    tracer = FITracer(provider.get_tracer("fi"), config=TraceConfig(
        pii_redaction=True, hide_error_details=False,
    ))
    span = tracer.start_span("case")
    span.end()
    error = AlternatingNativeTextError(EMAIL, first_sensitive=True)
    span.record_exception(error)
    (result,) = exporter.get_finished_spans()
    assert not result.events
    assert error.rendered == []
    assert error.args == (EMAIL,)
