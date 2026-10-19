"""Error details on spans: TraceConfig.hide_error_details and the FiSpan wrapper.

Uses a real SDK TracerProvider with an in-memory exporter, so the assertions are
on the spans that would actually be exported.
"""

import os
from unittest.mock import patch

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from fi_instrumentation.fi_types import FiSpanKindValues
from fi_instrumentation.instrumentation._errors import (
    clean_error_description,
    get_error_type,
    is_error_detail_key,
)
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import (
    FI_HIDE_ERROR_DETAILS,
    TraceConfig,
)

CONTENT_FLAGS = [
    ("hide_inputs", "FI_HIDE_INPUTS"),
    ("hide_outputs", "FI_HIDE_OUTPUTS"),
    ("hide_input_messages", "FI_HIDE_INPUT_MESSAGES"),
    ("hide_output_messages", "FI_HIDE_OUTPUT_MESSAGES"),
    ("hide_input_text", "FI_HIDE_INPUT_TEXT"),
    ("hide_output_text", "FI_HIDE_OUTPUT_TEXT"),
]

SECRET = "my prompt is: hunter2"


class ProviderError(Exception):
    """Shaped like openai.APIStatusError: `.code` and `.type` from the body."""

    def __init__(self, message, code=None, type=None):
        super().__init__(message)
        self.code = code
        self.type = type


class BotoLikeError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class AnthropicLikeError(Exception):
    def __init__(self, message, error_type):
        super().__init__(message)
        self.body = {"type": "error", "error": {"type": error_type, "message": message}}


class GoogleLikeError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.code = 404
        self.status = "NOT_FOUND"


def _raise(exc):
    try:
        raise exc
    except BaseException as caught:
        return caught


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def make_tracer(exporter, clean_env):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _make(**config):
        return FITracer(provider.get_tracer(__name__), config=TraceConfig(**config))

    return _make


def _only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    return spans[0]


def _exception_events(span):
    return [e for e in span.events if e.name == "exception"]


# --------------------------------------------------------------------------- #
# TraceConfig derivation
# --------------------------------------------------------------------------- #
class TestHideErrorDetailsDerivation:
    def test_default_is_off(self, clean_env):
        assert TraceConfig().hide_error_details is False

    @pytest.mark.parametrize("flag,_env", CONTENT_FLAGS)
    def test_each_content_flag_implies_it(self, clean_env, flag, _env):
        assert TraceConfig(**{flag: True}).hide_error_details is True

    @pytest.mark.parametrize("_flag,env", CONTENT_FLAGS)
    def test_each_content_env_var_implies_it(self, clean_env, _flag, env):
        with patch.dict(os.environ, {env: "true"}):
            assert TraceConfig().hide_error_details is True

    @pytest.mark.parametrize(
        "flag",
        ["hide_input_images", "hide_embedding_vectors", "hide_llm_invocation_parameters"],
    )
    def test_non_content_flags_do_not_imply_it(self, clean_env, flag):
        assert TraceConfig(**{flag: True}).hide_error_details is False

    @pytest.mark.parametrize("flag,_env", CONTENT_FLAGS)
    def test_explicit_false_overrides_derivation(self, clean_env, flag, _env):
        config = TraceConfig(**{flag: True}, hide_error_details=False)
        assert config.hide_error_details is False

    def test_explicit_true_without_other_flags(self, clean_env):
        assert TraceConfig(hide_error_details=True).hide_error_details is True

    def test_env_false_overrides_derivation(self, clean_env):
        with patch.dict(os.environ, {"FI_HIDE_INPUTS": "true", FI_HIDE_ERROR_DETAILS: "false"}):
            assert TraceConfig().hide_error_details is False

    def test_env_true(self, clean_env):
        with patch.dict(os.environ, {FI_HIDE_ERROR_DETAILS: "true"}):
            assert TraceConfig().hide_error_details is True

    def test_constructor_wins_over_env(self, clean_env):
        with patch.dict(os.environ, {FI_HIDE_ERROR_DETAILS: "true"}):
            assert TraceConfig(hide_error_details=False).hide_error_details is False

    def test_invalid_env_falls_back_to_derived(self, clean_env):
        with patch.dict(os.environ, {"FI_HIDE_OUTPUTS": "true", FI_HIDE_ERROR_DETAILS: "maybe"}):
            assert TraceConfig().hide_error_details is True
        with patch.dict(os.environ, {FI_HIDE_ERROR_DETAILS: "maybe"}):
            assert TraceConfig().hide_error_details is False

    def test_type_is_checked(self, clean_env):
        with pytest.raises(TypeError):
            TraceConfig(hide_error_details="yes")

    @pytest.mark.parametrize(
        "key",
        [
            "exception.message",
            "exception.stacktrace",
            "error.message",
            "autogen.error.message",
            "autogen.tool.error_message",
        ],
    )
    def test_mask_drops_error_detail_keys(self, clean_env, key):
        assert TraceConfig(hide_error_details=True).mask(key, SECRET) is None
        assert TraceConfig().mask(key, SECRET) == SECRET

    def test_mask_keeps_error_type(self, clean_env):
        config = TraceConfig(hide_error_details=True)
        assert config.mask("error.type", "model_not_found") == "model_not_found"
        assert config.mask("exception.type", "ValueError") == "ValueError"


# --------------------------------------------------------------------------- #
# error.type and description helpers
# --------------------------------------------------------------------------- #
class TestErrorType:
    @pytest.mark.parametrize(
        "exc,expected",
        [
            (ProviderError("x", code="model_not_found"), "model_not_found"),
            (ProviderError("x", code=None, type="invalid_request_error"), "invalid_request_error"),
            (BotoLikeError("x", "ValidationException"), "ValidationException"),
            (AnthropicLikeError("x", "not_found_error"), "not_found_error"),
            (GoogleLikeError("x"), "NOT_FOUND"),
            (ValueError("x"), "ValueError"),
            (ProviderError("x"), f"{__name__}.ProviderError"),
        ],
    )
    def test_error_type(self, exc, expected):
        assert get_error_type(exc) == expected

    def test_free_text_code_is_not_used(self):
        exc = ProviderError("x", code=f"bad request: {SECRET}")
        assert get_error_type(exc) == f"{__name__}.ProviderError"

    def test_builtin_exit_code_is_not_used(self):
        assert get_error_type(SystemExit("leaving")) == "SystemExit"

    def test_detail_keys(self):
        assert is_error_detail_key("error.message")
        assert not is_error_detail_key("error.type")
        assert not is_error_detail_key("exception.type")
        assert not is_error_detail_key("gen_ai.input.messages")


class TestCleanErrorDescription:
    def test_type_prefix_is_removed(self):
        exc = ValueError("boom")
        assert clean_error_description("ValueError: boom", [exc]) == "boom"

    def test_qualified_type_prefix_is_removed(self):
        exc = ProviderError("boom")
        desc = f"{__name__}.ProviderError: boom"
        assert clean_error_description(desc, [exc]) == "boom"

    def test_message_kept(self):
        assert clean_error_description("boom", [ValueError("boom")]) == "boom"

    def test_empty_message(self):
        assert clean_error_description("ValueError: ", [ValueError()]) is None

    def test_unrelated_description_kept(self):
        assert clean_error_description("Task failed", [ValueError("boom")]) == "Task failed"

    def test_traceback_never_kept(self):
        desc = "ValueError('boom')Traceback (most recent call last):\n  File \"/private/tmp/x.py\""
        assert clean_error_description(desc, [ValueError("boom")]) == "boom"
        assert clean_error_description(desc, []) == "ValueError('boom')"


# --------------------------------------------------------------------------- #
# FiSpan wrapper paths
# --------------------------------------------------------------------------- #
class TestRecordException:
    def test_default_keeps_details_and_sets_error_type(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.record_exception(_raise(ProviderError(SECRET, code="model_not_found")))
        span.end()
        exported = _only_span(exporter)
        assert exported.attributes["error.type"] == "model_not_found"
        (event,) = _exception_events(exported)
        assert event.attributes["exception.message"] == SECRET
        assert "Traceback" in event.attributes["exception.stacktrace"]
        assert event.attributes["exception.type"] == f"{__name__}.ProviderError"

    def test_hidden_keeps_only_type_and_escaped(self, make_tracer, exporter):
        span = make_tracer(hide_error_details=True).start_span("op")
        span.record_exception(
            _raise(ValueError(SECRET)),
            attributes={"exception.message": SECRET, "custom": "kept"},
            escaped=True,
        )
        span.end()
        exported = _only_span(exporter)
        (event,) = _exception_events(exported)
        assert dict(event.attributes) == {
            "exception.type": "ValueError",
            "exception.escaped": "True",
            "custom": "kept",
        }
        assert exported.attributes["error.type"] == "ValueError"

    def test_instrumentor_error_type_is_not_overwritten(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.set_attribute("error.type", "rate_limited")
        span.record_exception(ValueError("x"))
        span.end()
        assert _only_span(exporter).attributes["error.type"] == "rate_limited"

    def test_first_exception_wins(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.record_exception(KeyError("a"))
        span.record_exception(ValueError("b"))
        span.end()
        assert _only_span(exporter).attributes["error.type"] == "KeyError"


class TestSetStatus:
    def test_type_prefix_reduced_to_message(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        try:
            raise ValueError(SECRET)
        except ValueError as exc:
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
        span.end()
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.ERROR
        assert exported.status.description == SECRET
        assert exported.attributes["error.type"] == "ValueError"

    def test_order_independent_after_record_exception(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        exc = _raise(ValueError("boom"))
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, "ValueError: boom"))
        span.end()
        assert _only_span(exporter).status.description == "boom"

    def test_code_and_description_form(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.set_status(StatusCode.ERROR, "upstream returned 500")
        span.end()
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.ERROR
        assert exported.status.description == "upstream returned 500"

    def test_traceback_is_cut(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.set_status(
            Status(
                StatusCode.ERROR,
                "ValueError('x')Traceback (most recent call last):\n  File \"/private/tmp/a.py\"",
            )
        )
        span.end()
        assert _only_span(exporter).status.description == "ValueError('x')"

    def test_hidden_has_no_description(self, make_tracer, exporter):
        span = make_tracer(hide_inputs=True).start_span("op")
        try:
            raise ValueError(SECRET)
        except ValueError as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.end()
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.ERROR
        assert exported.status.description is None
        assert exported.attributes["error.type"] == "ValueError"

    def test_hidden_code_and_description_form(self, make_tracer, exporter):
        span = make_tracer(hide_error_details=True).start_span("op")
        span.set_status(StatusCode.ERROR, SECRET)
        span.end()
        assert _only_span(exporter).status.description is None

    def test_ok_passes_through(self, make_tracer, exporter):
        span = make_tracer(hide_error_details=True).start_span("op")
        span.set_status(StatusCode.OK)
        span.end()
        assert _only_span(exporter).status.status_code is StatusCode.OK


class TestAddEvent:
    def test_hidden_exception_event_drops_message_and_stacktrace(self, make_tracer, exporter):
        span = make_tracer(hide_error_details=True).start_span("op")
        span.add_event(
            "exception",
            {
                "exception.type": "KeyboardInterrupt",
                "exception.message": SECRET,
                "exception.stacktrace": "Traceback ...",
                "exception.escaped": False,
            },
        )
        span.end()
        exported = _only_span(exporter)
        (event,) = _exception_events(exported)
        assert dict(event.attributes) == {
            "exception.type": "KeyboardInterrupt",
            "exception.escaped": False,
        }
        assert exported.attributes["error.type"] == "KeyboardInterrupt"

    def test_default_exception_event_unchanged(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.add_event("exception", {"exception.type": "X", "exception.message": "m"})
        span.end()
        (event,) = _exception_events(_only_span(exporter))
        assert event.attributes["exception.message"] == "m"

    def test_other_events_pass_through(self, make_tracer, exporter):
        span = make_tracer(hide_error_details=True).start_span("op")
        span.add_event("First Token Stream Event")
        span.add_event("interrupt", {"langgraph.interrupt": True})
        span.end()
        exported = _only_span(exporter)
        assert [e.name for e in exported.events] == ["First Token Stream Event", "interrupt"]
        assert "error.type" not in exported.attributes


class TestSetAttribute:
    def test_hidden_error_attributes_dropped(self, make_tracer, exporter):
        span = make_tracer(hide_output_text=True).start_span("op")
        span.set_attribute("error.message", SECRET)
        span.set_attributes({"exception.message": SECRET, "exception.stacktrace": "tb"})
        span.set_attribute("error.type", "ValueError")
        span.end()
        attributes = dict(_only_span(exporter).attributes)
        assert attributes == {"error.type": "ValueError"}

    def test_default_error_attributes_kept(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.set_attribute("error.message", "m")
        span.end()
        assert _only_span(exporter).attributes["error.message"] == "m"


class TestUseSpanAutoRecording:
    """The OTel API's use_span builds "Type: msg" and calls record_exception."""

    def test_default(self, make_tracer, exporter):
        tracer = make_tracer()
        with pytest.raises(ValueError):
            with tracer.start_as_current_span("op", fi_span_kind=FiSpanKindValues.LLM):
                raise ValueError(SECRET)
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.ERROR
        assert exported.status.description == SECRET
        assert "Traceback" not in exported.status.description
        assert exported.attributes["error.type"] == "ValueError"
        (event,) = _exception_events(exported)
        assert event.attributes["exception.message"] == SECRET
        assert "Traceback" in event.attributes["exception.stacktrace"]

    def test_hidden(self, make_tracer, exporter):
        tracer = make_tracer(hide_inputs=True)
        with pytest.raises(ProviderError):
            with tracer.start_as_current_span("op", fi_span_kind=FiSpanKindValues.LLM):
                raise ProviderError(SECRET, code="model_not_found")
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.ERROR
        assert exported.status.description is None
        assert exported.attributes["error.type"] == "model_not_found"
        (event,) = _exception_events(exported)
        assert set(event.attributes) == {"exception.type", "exception.escaped"}

    def test_trace_api_use_span_with_fi_span(self, make_tracer, exporter):
        span = make_tracer(hide_error_details=True).start_span("op")
        with pytest.raises(ValueError):
            with trace_api.use_span(span, end_on_exit=True):
                raise ValueError(SECRET)
        exported = _only_span(exporter)
        assert exported.status.description is None
        (event,) = _exception_events(exported)
        assert "exception.message" not in event.attributes

    def test_span_as_context_manager(self, make_tracer, exporter):
        """`with tracer.start_span(...) as span` used to yield the raw SDK span."""
        tracer = make_tracer(hide_error_details=True)
        with pytest.raises(ValueError):
            with tracer.start_span("op", fi_span_kind=FiSpanKindValues.TOOL) as span:
                assert type(span).__name__ == "FiSpan"
                raise ValueError(SECRET)
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.ERROR
        assert exported.status.description is None
        assert exported.attributes["gen_ai.span.kind"] == "TOOL"
        (event,) = _exception_events(exported)
        assert event.attributes["exception.escaped"] == "True"
        assert "exception.message" not in event.attributes

    def test_span_as_context_manager_default(self, make_tracer, exporter):
        with pytest.raises(KeyError):
            with make_tracer().start_span("op"):
                raise KeyError("k")
        exported = _only_span(exporter)
        assert exported.status.description == "'k'"
        assert exported.attributes["error.type"] == "KeyError"

    def test_decorator_path(self, make_tracer, exporter):
        tracer = make_tracer(hide_error_details=True)

        @tracer.chain
        def fails(x):
            raise RuntimeError(f"{SECRET} {x}")

        with pytest.raises(RuntimeError):
            fails("y")
        exported = _only_span(exporter)
        assert exported.status.description is None
        assert exported.attributes["error.type"] == "RuntimeError"
        (event,) = _exception_events(exported)
        assert "exception.message" not in event.attributes

    def test_suppressed_span_is_a_no_op(self, make_tracer, exporter):
        from fi_instrumentation import suppress_tracing

        tracer = make_tracer(hide_error_details=True)
        with suppress_tracing():
            with pytest.raises(ValueError):
                with tracer.start_as_current_span("op"):
                    raise ValueError("x")
        assert exporter.get_finished_spans() == ()
