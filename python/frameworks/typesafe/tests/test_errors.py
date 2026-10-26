"""J2: errors end the span as ERROR; no retry added; no stack trace or local path exported."""

from __future__ import annotations

import pytest

pytest.importorskip("typesafe_sdk")

from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from tests._support import (  # noqa: E402
    API_KEY,
    STATE,
    STATE_MARKER,
    FakeSystemOne,
    client,
    everything,
    instrumented,
    no_retry,
    only_span,
    plain,
    questions,
)


def _fail(model, config=None, **kwargs):
    from typesafe_sdk import TypeSafeError

    fake = FakeSystemOne()
    with instrumented(config) as exporter:
        with client(fake, **kwargs) as typesafe:
            with pytest.raises(TypeSafeError) as raised:
                typesafe.system_one(STATE, questions(), model=model)
    return fake, raised.value, only_span(exporter)


def test_401_is_an_error_span_with_status_and_server_type_and_no_retry():
    from typesafe_sdk import TypeSafeAuthenticationError

    fake, error, span = _fail("fail-401")
    assert isinstance(error, TypeSafeAuthenticationError)
    assert len(fake.requests) == 1
    a = dict(span.attributes)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == (
        "TypeSafeAuthenticationError (HTTP 401): "
        "Cannot authenticate with the server. Please check your API key and try again."
    )
    assert a["error.type"] == "TypeSafeAuthenticationError"
    assert a["http.response.status_code"] == 401
    assert a["typesafe.error_type"] == "authentication_error"
    assert a["typesafe.request_id"] == "req_fake_1"
    (event,) = span.events
    assert event.name == "exception"
    assert event.attributes["exception.type"] == "typesafe_sdk._core.errors.TypeSafeAuthenticationError"
    assert "exception.stacktrace" not in event.attributes
    # No answers, so no decision attributes beyond the question types.
    assert "typesafe.question.billing.noul" not in a
    assert a["typesafe.question.billing.type"] == "noul"


def test_422_message_shown_by_default_hidden_with_the_flags():
    _, _, span = _fail("fail-422")
    assert span.attributes["http.response.status_code"] == 422
    assert STATE_MARKER in span.status.description  # the API echoed the state
    _, _, hidden = _fail("fail-422", TraceConfig(hide_inputs=True, hide_outputs=True))
    assert STATE_MARKER not in everything(hidden)
    assert hidden.status.description == "TypeSafeUnprocessableEntityError (HTTP 422)"
    assert "exception.message" not in hidden.events[0].attributes


def test_429_without_retries_is_one_error_span():
    fake, _, span = _fail("fail-429", retry=no_retry())
    assert len(fake.requests) == 1
    assert span.attributes["http.response.status_code"] == 429
    assert span.attributes["typesafe.retry_after_ms"] == 0
    assert span.attributes["typesafe.error_type"] == "rate_limit_error"


def test_the_sdks_own_retries_stay_inside_one_span():
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:  # SDK default RetryPolicy retries 429
            typesafe.system_one(STATE, questions(), model="flaky-429")
    assert len(fake.requests) == 2
    span = only_span(exporter)
    assert span.status.status_code is StatusCode.OK
    assert fake.requests[1].headers["x-typesafe-retry-count"] == "1"


def test_echoed_key_is_redacted_from_the_error():
    _, _, span = _fail("echo-key")
    assert API_KEY not in everything(span)
    assert "[redacted]" in span.status.description


def test_client_side_validation_error_is_an_error_span():
    from typesafe_sdk import TypeSafeError

    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            with pytest.raises(TypeSafeError):
                typesafe.system_one(STATE, {})
    span = only_span(exporter)
    assert span.status.description == "TypeSafeError"
    assert span.attributes["typesafe.question_count"] == 0
    assert fake.requests == []


def test_client_side_error_text_is_never_exported(tmp_path):
    import typesafe_sdk

    if not hasattr(typesafe_sdk, "Image"):
        pytest.skip("image assets exist from typesafe-sdk 0.7.5")
    from typesafe_sdk import Image, Noul, TypeSafeError

    missing = tmp_path / "home-user-secret-dir" / "photo.png"
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            with pytest.raises(TypeSafeError) as raised:
                typesafe.system_one("x", {"q": Noul(instructions="ok?")}, assets={"p": Image(missing)})
    assert str(missing) in str(raised.value)  # the SDK names the path...
    span = only_span(exporter)
    dump = everything(span)
    assert str(tmp_path) not in dump and "home-user-secret-dir" not in dump  # ...the span does not
    assert span.status.description == "TypeSafeError"
    assert fake.requests == []


def test_connection_error_has_no_host_details():
    import httpx2
    from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeClient

    def refuse(request):
        raise httpx2.ConnectError("[Errno 61] Connection refused to 10.1.2.3:7777")

    with instrumented() as exporter:
        with TypeSafeClient(transport=httpx2.MockTransport(refuse), **plain(TypeSafeClient, api_key=API_KEY, retry=no_retry())) as typesafe:
            with pytest.raises(TypeSafeAPIConnectionError):
                typesafe.system_one(STATE, questions())
    span = only_span(exporter)
    assert span.status.description == "TypeSafeAPIConnectionError"
    assert "10.1.2.3" not in everything(span)


def test_missing_api_key_raises_before_any_span():
    from typesafe_sdk import TypeSafeClient, TypeSafeError

    with instrumented() as exporter:
        with pytest.raises(TypeSafeError):
            TypeSafeClient()
    assert exporter.get_finished_spans() == ()
