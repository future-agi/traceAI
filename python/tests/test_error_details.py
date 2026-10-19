"""Error details on spans: TraceConfig.hide_error_details and the FiSpan wrapper.

Uses a real SDK TracerProvider with an in-memory exporter, so the assertions are
on the spans that would actually be exported.
"""

import dataclasses
import gc
import os
import weakref
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


# Provider error codes are read only from exceptions of known provider SDKs,
# so the fakes below claim the SDK's module (as the real classes do).
class ProviderError(Exception):
    """Shaped like openai.APIStatusError: `.code` and `.type` from the body."""

    def __init__(self, message, code=None, type=None):
        super().__init__(message)
        self.code = code
        self.type = type


ProviderError.__module__ = "openai"


class BotoLikeError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


BotoLikeError.__module__ = "botocore.exceptions"


class AnthropicLikeError(Exception):
    """anthropic < 1.x: the code only in `.body`; newer versions add `.type`."""

    def __init__(self, message, error_type):
        super().__init__(message)
        self.body = {"type": "error", "error": {"type": error_type, "message": message}}


AnthropicLikeError.__module__ = "anthropic"


class GoogleLikeError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.code = 404
        self.status = "NOT_FOUND"


GoogleLikeError.__module__ = "google.genai.errors"


def _fake_exception(module, name, *, base=Exception, **attributes):
    cls = type(name, (base,), {"__module__": module})
    exc = cls("message with " + SECRET)
    for key, value in attributes.items():
        setattr(exc, key, value)
    return exc


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _GrpcStatus:
    def __init__(self, name):
        self.name = name


class Boom(Exception):
    """Supports weak references, unlike the built-in exceptions."""


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
        with pytest.raises(TypeError, match="hide_error_details"):
            TraceConfig(hide_error_details="yes")

    @pytest.mark.parametrize("flag,_env", CONTENT_FLAGS)
    def test_replace_derives_it_again(self, clean_env, flag, _env):
        config = dataclasses.replace(TraceConfig(), **{flag: True})
        assert config.hide_error_details is True
        assert dataclasses.replace(config, **{flag: False}).hide_error_details is False

    def test_replace_keeps_an_explicit_value(self, clean_env):
        explicit_false = TraceConfig(hide_error_details=False)
        assert dataclasses.replace(explicit_false, hide_inputs=True).hide_error_details is False
        explicit_true = TraceConfig(hide_error_details=True)
        assert dataclasses.replace(explicit_true, pii_redaction=True).hide_error_details is True

    def test_replace_with_an_explicit_value(self, clean_env):
        derived = TraceConfig(hide_outputs=True)
        assert dataclasses.replace(derived, hide_error_details=False).hide_error_details is False
        replaced = dataclasses.replace(TraceConfig(), hide_error_details=True)
        assert replaced.hide_error_details is True

    def test_replace_keeps_the_env_value(self, clean_env):
        with patch.dict(os.environ, {FI_HIDE_ERROR_DETAILS: "false"}):
            config = TraceConfig()
        assert dataclasses.replace(config, hide_inputs=True).hide_error_details is False

    def test_replace_without_changes_is_equal(self, clean_env):
        config = TraceConfig(hide_inputs=True, base64_image_max_length=10)
        assert dataclasses.replace(config) == config

    def test_positional_arguments(self, clean_env):
        args = [None] * 11 + [True]
        assert TraceConfig(*args).hide_error_details is True

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
            (ProviderError("x"), "openai.ProviderError"),
        ],
    )
    def test_error_type(self, exc, expected):
        assert get_error_type(exc) == expected

    def test_free_text_code_is_not_used(self):
        exc = ProviderError("x", code=f"bad request: {SECRET}")
        assert get_error_type(exc) == "openai.ProviderError"

    @pytest.mark.parametrize(
        "exc,expected",
        [
            # anthropic >= 1.x sets `.type` from the body as well.
            (
                _fake_exception("anthropic", "NotFoundError", type="not_found_error"),
                "not_found_error",
            ),
            # groq: the whole error response is the body.
            (
                _fake_exception(
                    "groq",
                    "NotFoundError",
                    body={"error": {"type": "invalid_request_error", "code": "model_not_found"}},
                ),
                "model_not_found",
            ),
            # litellm errors subclass openai's: litellm.exceptions.NotFoundError.
            (
                _fake_exception(
                    "litellm.exceptions", "NotFoundError", base=ProviderError, code="model_not_found"
                ),
                "model_not_found",
            ),
            (
                _fake_exception(
                    "litellm.exceptions", "RateLimitError", base=ProviderError, code="429"
                ),
                "litellm.exceptions.RateLimitError",
            ),
            # Any subclass of a provider error, wherever it is defined.
            (
                _fake_exception("myapp.errors", "Wrapped", base=ProviderError, code="rate_limit"),
                "rate_limit",
            ),
            (
                _fake_exception(
                    "botocore.errorfactory",
                    "ValidationException",
                    response={"Error": {"Code": "ValidationException"}},
                ),
                "ValidationException",
            ),
            (
                _fake_exception("botocore.exceptions", "NoCredentialsError"),
                "botocore.exceptions.NoCredentialsError",
            ),
            (
                _fake_exception(
                    "google.api_core.exceptions",
                    "NotFound",
                    code=404,
                    grpc_status_code=_GrpcStatus("NOT_FOUND"),
                ),
                "NOT_FOUND",
            ),
            # grpc not installed: grpc_status_code is None.
            (
                _fake_exception("google.api_core.exceptions", "NotFound", grpc_status_code=None),
                "google.api_core.exceptions.NotFound",
            ),
            (
                _fake_exception(
                    "mistralai.client.errors.sdkerror",
                    "SDKError",
                    status_code=400,
                    body='{"object":"error","message":"Invalid model","type":"invalid_model",'
                    '"param":null,"code":"1500"}',
                ),
                "invalid_model",
            ),
            (
                _fake_exception("mistralai.models", "SDKError", body="<html>bad gateway</html>"),
                "mistralai.models.SDKError",
            ),
            # cohere errors carry no code, only a status code and a free-text body.
            (
                _fake_exception(
                    "cohere.errors.not_found_error",
                    "NotFoundError",
                    status_code=404,
                    body={"message": SECRET},
                ),
                "cohere.errors.not_found_error.NotFoundError",
            ),
            (
                _fake_exception("httpx", "HTTPStatusError", response=_Response(404)),
                "404",
            ),
            (
                _fake_exception("httpx", "ConnectError"),
                "httpx.ConnectError",
            ),
        ],
    )
    def test_provider_sdks(self, exc, expected):
        assert get_error_type(exc) == expected

    @pytest.mark.parametrize(
        "exc,expected",
        [
            # SQLAlchemy errors carry a short documentation code in `.code`.
            (
                _fake_exception("sqlalchemy.exc", "OperationalError", code="e3q8"),
                "sqlalchemy.exc.OperationalError",
            ),
            # Django's ValidationError carries a validator code.
            (
                _fake_exception("django.core.exceptions", "ValidationError", code="invalid"),
                "django.core.exceptions.ValidationError",
            ),
            (
                _fake_exception("myapp.errors", "LookupFailed", status="NOT_FOUND", type="lookup"),
                "myapp.errors.LookupFailed",
            ),
            (
                _fake_exception("requests.exceptions", "HTTPError", response=_Response(500)),
                "requests.exceptions.HTTPError",
            ),
        ],
    )
    def test_other_libraries_use_the_class_name(self, exc, expected):
        assert get_error_type(exc) == expected

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
        desc = "openai.ProviderError: boom"
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

    def test_one_line_per_exception(self):
        """llama-index joins one "<Type>: <message>" line per exception."""
        excs = [ValueError("first"), KeyError("k"), ProviderError("third\nline")]
        desc = "\n".join(f"{type(e).__name__}: {e}" for e in excs)
        assert clean_error_description(desc, excs) == "first\n'k'\nthird\nline"

    def test_message_starting_with_a_type_is_kept(self):
        excs = [ValueError("KeyError: k"), KeyError("k")]
        assert clean_error_description("ValueError: KeyError: k", excs) == "KeyError: k"

    def test_prefix_needs_the_whole_message(self):
        exc = ValueError("boom")
        assert clean_error_description("ValueError: boom and more", [exc]) == (
            "ValueError: boom and more"
        )
        assert clean_error_description("see ValueError: boom", [exc]) == "see ValueError: boom"


# --------------------------------------------------------------------------- #
# FiSpan wrapper paths
# --------------------------------------------------------------------------- #
class TestRecordException:
    def test_default_keeps_details_and_sets_error_type(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.record_exception(_raise(ProviderError(SECRET, code="model_not_found")))
        span.set_status(StatusCode.ERROR)
        span.end()
        exported = _only_span(exporter)
        assert exported.attributes["error.type"] == "model_not_found"
        (event,) = _exception_events(exported)
        assert event.attributes["exception.message"] == SECRET
        assert "Traceback" in event.attributes["exception.stacktrace"]
        assert event.attributes["exception.type"] == "openai.ProviderError"

    def test_hidden_keeps_only_type_and_escaped(self, make_tracer, exporter):
        span = make_tracer(hide_error_details=True).start_span("op")
        span.record_exception(
            _raise(ValueError(SECRET)),
            attributes={"exception.message": SECRET, "custom": "kept"},
            escaped=True,
        )
        span.set_status(StatusCode.ERROR)
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
        span.set_status(StatusCode.ERROR)
        span.end()
        assert _only_span(exporter).attributes["error.type"] == "rate_limited"

    def test_last_recorded_exception_wins(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.record_exception(KeyError("a"))
        span.record_exception(ValueError("b"))
        span.set_status(StatusCode.ERROR)
        span.end()
        assert _only_span(exporter).attributes["error.type"] == "ValueError"

    def test_exception_is_not_held(self, make_tracer, exporter):
        """Neither the exception nor its traceback frames outlive the call."""
        span = make_tracer().start_span("op")

        def fail():
            frame_local = SECRET  # noqa: F841
            raise Boom(SECRET)

        try:
            fail()
        except Boom as caught:
            ref = weakref.ref(caught)
            span.record_exception(caught)
            span.set_status(Status(StatusCode.ERROR, f"Boom: {caught}"))
        gc.collect()
        assert ref() is None
        span.end()
        exported = _only_span(exporter)
        assert exported.status.description == SECRET
        assert exported.attributes["error.type"] == f"{__name__}.Boom"
        assert span._self_exception_summaries == []


class TestErrorTypeOnlyWhenFailed:
    """error.type marks a failed operation (OTel semconv), so it is set only when
    the span ends with status ERROR."""

    def test_recovered_retry_ends_ok(self, make_tracer, exporter):
        tracer = make_tracer()
        with tracer.start_as_current_span("op", fi_span_kind=FiSpanKindValues.LLM) as span:
            span.record_exception(_raise(TimeoutError("attempt 1 timed out")))
            span.set_status(StatusCode.OK)
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.OK
        assert "error.type" not in exported.attributes
        (event,) = _exception_events(exported)
        assert event.attributes["exception.type"] == "TimeoutError"

    def test_recorded_exception_without_status(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.record_exception(_raise(ProviderError("x", code="model_not_found")))
        span.end()
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.UNSET
        assert "error.type" not in exported.attributes

    def test_exception_event_without_error_status(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.add_event("exception", {"exception.type": "KeyboardInterrupt"})
        span.end()
        assert "error.type" not in _only_span(exporter).attributes

    def test_error_then_ok(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        try:
            raise TimeoutError("attempt 1")
        except TimeoutError as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.set_status(StatusCode.OK)
        span.end()
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.OK
        assert "error.type" not in exported.attributes

    def test_retry_then_failure_uses_the_failing_exception(self, make_tracer, exporter):
        tracer = make_tracer()
        with pytest.raises(ValueError):
            with tracer.start_as_current_span("op") as span:
                span.record_exception(_raise(TimeoutError("attempt 1 timed out")))
                raise ValueError("attempt 2 failed")
        exported = _only_span(exporter)
        assert exported.status.status_code is StatusCode.ERROR
        assert exported.status.description == "attempt 2 failed"
        assert exported.attributes["error.type"] == "ValueError"

    def test_status_set_outside_except(self, make_tracer, exporter):
        """LangChain records the error in on_*_error and sets the status later."""
        span = make_tracer().start_span("op")
        exc = _raise(ProviderError(SECRET, code="model_not_found"))
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.end()
        exported = _only_span(exporter)
        assert exported.status.description == SECRET
        assert exported.attributes["error.type"] == "model_not_found"

    def test_status_before_record_exception(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        exc = _raise(KeyError("k"))
        span.set_status(Status(StatusCode.ERROR, "KeyError: 'k'"))
        span.record_exception(exc)
        span.end()
        assert _only_span(exporter).attributes["error.type"] == "KeyError"

    def test_in_flight_exception_wins_over_recorded(self, make_tracer, exporter):
        span = make_tracer().start_span("op")
        span.record_exception(_raise(TimeoutError("recovered")))
        try:
            raise ProviderError("x", code="model_not_found")
        except ProviderError:
            span.set_status(StatusCode.ERROR)
        span.end()
        assert _only_span(exporter).attributes["error.type"] == "model_not_found"

    def test_hidden_recovered_retry(self, make_tracer, exporter):
        tracer = make_tracer(hide_inputs=True)
        with tracer.start_as_current_span("op") as span:
            span.record_exception(_raise(TimeoutError(SECRET)))
        exported = _only_span(exporter)
        assert "error.type" not in exported.attributes
        (event,) = _exception_events(exported)
        assert set(event.attributes) == {"exception.type", "exception.escaped"}

    def test_one_line_per_exception_status(self, make_tracer, exporter):
        """llama-index records each exception, then joins "<Type>: <msg>" lines."""
        span = make_tracer().start_span("op")
        excs = [_raise(ValueError("first")), _raise(KeyError("k"))]
        for exc in excs:
            span.record_exception(exc)
        span.set_status(
            Status(StatusCode.ERROR, "\n".join(f"{type(e).__name__}: {e}" for e in excs))
        )
        span.end()
        exported = _only_span(exporter)
        assert exported.status.description == "first\n'k'"
        assert exported.attributes["error.type"] == "KeyError"


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
        assert "error.type" not in exported.attributes

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
        span.set_status(StatusCode.ERROR)
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
