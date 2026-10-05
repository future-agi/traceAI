"""J5, AC-06, AC-08: errors set ERROR with the status code; no credential is exported."""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions as core_exceptions
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    ACCESS_TOKEN,
    API_KEY,
    BEARER_VALUE,
    FAIL_DENIED,
    FAIL_HUGE,
    FAIL_TOKEN,
    FAIL_UNAVAILABLE,
    FAIL_UNSEEN,
    METADATA_TOKEN,
    MINTED_TOKEN,
    UNSEEN_ACCESS_TOKEN,
    UNSEEN_API_KEY,
    UNSEEN_REFRESH_TOKEN,
    FakeDiscoveryEngine,
    answer_client,
    async_search_client,
    attrs,
    event,
    instrumented,
    minting_credentials,
    new_provider,
    oauth_credentials,
    api_key_credentials,
    search_client,
    search_client_with_api_key,
    search_request,
    answer_request,
    self_signed_jwt_credentials,
)
from traceai_discoveryengine import _wrappers

SECRETS = (ACCESS_TOKEN, API_KEY, METADATA_TOKEN)


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


def _error_texts(span):
    exception = event(span, "exception") or {}
    return (
        span.status.description or "",
        exception.get("exception.message", ""),
        exception.get("exception.stacktrace", ""),
    )


def test_permission_denied_sets_error_with_the_status_code_and_reraises(fake):
    client = search_client(fake, credentials=oauth_credentials())
    with instrumented() as traced:
        with pytest.raises(core_exceptions.PermissionDenied) as raised:
            client.search(request=search_request(FAIL_DENIED))

    # The caller's exception is the vendor's own, message untouched.
    assert ACCESS_TOKEN in str(raised.value)
    assert fake.calls[0].metadata["authorization"] == "Bearer " + ACCESS_TOKEN

    span = traced.one()
    values = attrs(span)
    assert values["discoveryengine.error.status"] == "PERMISSION_DENIED"
    assert values["discoveryengine.error.code"] == 403
    assert "discoveryengine.result_count" not in values
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.startswith("PermissionDenied: 403 Permission denied")
    exception = event(span, "exception")
    assert exception["exception.type"] == "google.api_core.exceptions.PermissionDenied"
    assert [item.name for item in span.events] == ["exception"]
    for text in _error_texts(span):
        assert ACCESS_TOKEN not in text
    assert "[redacted]" in exception["exception.message"]
    assert ACCESS_TOKEN not in traced.wire()


def test_an_api_key_from_client_options_is_never_exported(fake):
    # search_lite is the method Google documents for API-key auth.
    client = search_client_with_api_key(fake, API_KEY)
    with instrumented(capture_query=True) as traced:
        client.search_lite(request=search_request("ok"))
        with pytest.raises(core_exceptions.PermissionDenied):
            client.search_lite(request=search_request(FAIL_DENIED))

    assert [call.metadata.get("x-goog-api-key") for call in fake.calls] == [API_KEY, API_KEY]
    assert traced.names() == ["discoveryengine.search_lite"] * 2
    assert API_KEY not in traced.wire()
    assert "[redacted]" in event(traced.spans()[1], "exception")["exception.message"]


def test_api_key_credentials_on_the_transport_are_never_exported(fake):
    client = search_client(fake, credentials=api_key_credentials())
    with instrumented() as traced:
        with pytest.raises(core_exceptions.PermissionDenied):
            client.search(request=search_request(FAIL_DENIED))

    assert fake.calls[0].metadata["x-goog-api-key"] == API_KEY
    assert API_KEY not in traced.wire()


@pytest.mark.parametrize(
    "key, value",
    [("authorization", "Bearer " + METADATA_TOKEN), ("x-goog-api-key", METADATA_TOKEN)],
    ids=["authorization", "x-goog-api-key"],
)
def test_auth_metadata_passed_per_call_is_never_exported(fake, key, value):
    metadata = [(key, value)]
    with instrumented(capture_query=True) as traced:
        with pytest.raises(core_exceptions.PermissionDenied):
            search_client(fake).search(request=search_request(FAIL_DENIED), metadata=metadata)
        answer_client(fake).answer_query(request=answer_request(), metadata=metadata)

    assert fake.calls[0].metadata[key.lower()] == value
    assert METADATA_TOKEN in str(fake.calls[0].metadata)
    assert METADATA_TOKEN not in traced.wire()
    # No request metadata is recorded as an attribute.
    for span in traced.spans():
        for key in attrs(span):
            assert "authorization" not in key and "metadata" not in key, key


def test_a_query_that_contains_the_token_is_redacted_when_captured(fake):
    client = search_client(fake, credentials=oauth_credentials())
    with instrumented(capture_query=True) as traced:
        client.search(request=search_request("find " + ACCESS_TOKEN))

    values = attrs(traced.one())
    assert values["input.value"] == "find [redacted]"
    assert values["gen_ai.retrieval.query"] == "find [redacted]"


def test_token_shaped_strings_the_client_never_held_are_redacted(fake):
    with instrumented() as traced:
        with pytest.raises(core_exceptions.InvalidArgument) as raised:
            search_client(fake).search(request=search_request(FAIL_UNSEEN))

    assert UNSEEN_ACCESS_TOKEN in str(raised.value)
    span = traced.one()
    for text in _error_texts(span):
        assert text
        for secret in (
            UNSEEN_ACCESS_TOKEN,
            UNSEEN_API_KEY,
            UNSEEN_REFRESH_TOKEN,
            BEARER_VALUE,
            "ya29.",
            "AIza",
            "1//",
        ):
            assert secret not in text, secret
        assert "Bearer [redacted]" in text


@pytest.mark.parametrize(
    "token", [None, "placeholder-stale-token-value"], ids=["first-use", "refresh"]
)
def test_a_token_minted_during_the_call_is_redacted(fake, token):
    # The wrapper reads the credentials before the call; the token is set
    # during it. The server quotes it without "Bearer" and in no Google shape.
    client = search_client(fake, credentials=minting_credentials(token))
    with instrumented() as traced:
        with pytest.raises(core_exceptions.Unauthenticated) as raised:
            client.search(request=search_request(FAIL_TOKEN))

    assert fake.calls[0].metadata["authorization"] == "Bearer " + MINTED_TOKEN
    assert MINTED_TOKEN in str(raised.value)
    span = traced.one()
    for text in _error_texts(span):
        assert text
        assert MINTED_TOKEN not in text
    assert "[redacted]" in event(span, "exception")["exception.message"]
    assert MINTED_TOKEN not in traced.wire()


def test_a_self_signed_jwt_minted_for_a_service_account_is_redacted(fake):
    # google-api-core gives the channel a scoped copy of service-account
    # credentials, so the JWT google-auth mints during the call is never on
    # the credentials the client holds: only its shape can remove it.
    credentials = self_signed_jwt_credentials()
    client = search_client(fake, credentials=credentials)
    with instrumented() as traced:
        with pytest.raises(core_exceptions.Unauthenticated) as raised:
            client.search(request=search_request(FAIL_TOKEN))

    scheme, jwt = fake.calls[0].metadata["authorization"].split(" ", 1)
    assert scheme == "Bearer"
    assert jwt.startswith("eyJ") and jwt.count(".") == 2
    assert client._transport._credentials.token is None
    assert jwt in str(raised.value)
    span = traced.one()
    for text in _error_texts(span):
        assert text
        assert jwt not in text
        assert "eyJ" not in text
    assert "credentials: [redacted]" in event(span, "exception")["exception.message"]
    assert jwt not in traced.wire()


def test_the_bearer_pass_runs_on_server_text_only(fake):
    # A captured query is the user's words: "bearer <word>" stays. Token
    # shapes and held credentials are still removed from it.
    query = "bearer responsibility " + UNSEEN_ACCESS_TOKEN
    with instrumented(capture_query=True) as traced:
        search_client(fake).search(request=search_request(query))

    assert attrs(traced.one())["input.value"] == "bearer responsibility [redacted]"


def test_refresh_token_and_client_secret_are_never_exported():
    from google.oauth2.credentials import Credentials

    refresh_token = "placeholder-refresh-token-value"
    client_secret = "placeholder-client-secret-value"

    class Transport:
        _credentials = Credentials(
            token=None, refresh_token=refresh_token, client_id="client-id", client_secret=client_secret
        )

    class Client:
        _transport = Transport()
        _client_options = None

    error = core_exceptions.Unauthenticated(
        "refresh failed for {0} with {1}".format(refresh_token, client_secret)
    )
    span = _call_wrapper(
        _wrappers.OperationWrapper, "search", Client(), error, search_request("q"), capture_query=True
    )

    wire = span.to_json()
    assert refresh_token not in wire and client_secret not in wire
    assert event(span, "exception")["exception.message"] == "401 refresh failed for [redacted] with [redacted]"


def test_server_unavailable_records_its_code(fake):
    with instrumented() as traced:
        with pytest.raises(core_exceptions.ServiceUnavailable):
            search_client(fake).search(request=search_request(FAIL_UNAVAILABLE))

    values = attrs(traced.one())
    assert values["discoveryengine.error.status"] == "UNAVAILABLE"
    assert values["discoveryengine.error.code"] == 503


def test_retry_exhaustion_is_one_error_span_with_the_last_status(fake):
    from google.api_core import retry as retries

    retry = retries.Retry(
        predicate=retries.if_exception_type(core_exceptions.ServiceUnavailable),
        initial=0.01,
        maximum=0.02,
        timeout=0.3,
    )
    with instrumented() as traced:
        with pytest.raises(core_exceptions.RetryError):
            search_client(fake).search(request=search_request(FAIL_UNAVAILABLE), retry=retry)

    assert len(fake.calls) >= 2
    span = traced.one()
    values = attrs(span)
    # RetryError has no status of its own; the last attempt's is recorded.
    assert values["discoveryengine.error.status"] == "UNAVAILABLE"
    assert values["discoveryengine.error.code"] == 503
    assert span.status.description.startswith("RetryError: Timeout of 0.3s exceeded")
    assert [item.name for item in span.events] == ["exception"]


def test_error_text_is_capped_after_redaction(fake):
    client = search_client(fake, credentials=oauth_credentials())
    with instrumented() as traced:
        with pytest.raises(core_exceptions.InvalidArgument):
            client.search(request=search_request(FAIL_HUGE))

    span = traced.one()
    description, message, stacktrace = _error_texts(span)
    prefix = "InvalidArgument: "
    assert description.startswith(prefix)
    assert len(description[len(prefix):].encode("utf-8")) <= _wrappers.MAX_VALUE_BYTES
    assert _wrappers.MAX_VALUE_BYTES - 3 <= len(message.encode("utf-8")) <= _wrappers.MAX_VALUE_BYTES
    assert (
        _wrappers.MAX_STACKTRACE_BYTES - 3
        <= len(stacktrace.encode("utf-8"))
        <= _wrappers.MAX_STACKTRACE_BYTES
    )
    for text in (description, message, stacktrace):
        assert ACCESS_TOKEN not in text
        # Whole characters only: the cut never leaves a broken euro sign.
        text.encode("utf-8").decode("utf-8")


class _Transport:
    def __init__(self, token):
        self._credentials = type("Creds", (), {"token": token})()


class _Client:
    def __init__(self, token):
        self._transport = _Transport(token)
        self._client_options = None


def _call_wrapper(wrapper_type, operation, instance, error, request, **options):
    exporter, provider = new_provider()
    wrapper = wrapper_type(provider.get_tracer("t"), operation, _wrappers.Options(**options))

    def wrapped(*_args, **_kwargs):
        raise error

    with pytest.raises(type(error)) as raised:
        wrapper(wrapped, instance, (), {"request": request})
    assert raised.value is error
    (span,) = exporter.get_finished_spans()
    return span


def test_a_token_straddling_the_cap_is_redacted_before_the_cut():
    token = "straddling-token-value-0123456789"
    filler = "x" * (_wrappers.MAX_VALUE_BYTES - 10)
    error = core_exceptions.PermissionDenied(filler + token + " tail")
    span = _call_wrapper(_wrappers.OperationWrapper, "search", _Client(token), error, search_request("q"))

    message = event(span, "exception")["exception.message"]
    assert "straddl" not in message and token[:8] not in message
    assert message.startswith("403 " + filler[:20])
    assert len(message.encode("utf-8")) <= _wrappers.MAX_VALUE_BYTES


ESCAPED_QUERY = "Zelda's \"quoted\" a\\b café 2024"


@pytest.mark.parametrize(
    "copy",
    [
        # Python repr (single quotes, as for a text with both quote marks).
        r"""Zelda\'s "quoted" a\\b caf""" + "é 2024",
        # JSON string, non-ASCII as \u.
        r"""Zelda's \"quoted\" a\\b café 2024""",
        # Protobuf text format: a status detail google-api-core appends.
        r"""Zelda\'s \"quoted\" a\\b caf""" + "é 2024",
        # absl CHexEscape (debug_error_string of older gRPC cores): bytes
        # outside printable ASCII as \xHH.
        r"""Zelda\'s \"quoted\" a\\b caf\xc3\xa9 2024""",
    ],
    ids=["repr", "json", "protobuf-text", "grpc-chexescape"],
)
def test_an_escaped_copy_of_a_hidden_query_is_removed(copy):
    cause = RuntimeError('debug_error_string = "{grpc_message:\\"' + copy + '\\"}"')
    error = core_exceptions.InvalidArgument("bad query '{0}'".format(copy))
    error.__cause__ = cause
    span = _call_wrapper(
        _wrappers.OperationWrapper, "search", _Client(None), error, search_request(ESCAPED_QUERY)
    )

    for text in _error_texts(span):
        assert "Zelda" not in text and "quoted" not in text, text
        assert "__REDACTED__" in text
    stacktrace = event(span, "exception")["exception.stacktrace"]
    assert "direct cause" in stacktrace and "debug_error_string" in stacktrace


def test_a_hex_digit_after_a_hex_escape_is_matched_as_grpc_escapes_it():
    # absl escapes a hex digit that follows \xHH too: "é1" is \xc3\xa9\x31.
    error = core_exceptions.InvalidArgument(r"bad query 'caf\xc3\xa9\x31\x32 x'")
    span = _call_wrapper(
        _wrappers.OperationWrapper, "search", _Client(None), error, search_request("café12 x")
    )

    assert event(span, "exception")["exception.message"] == "400 bad query '__REDACTED__'"


def test_an_exception_whose_str_raises_is_recorded_without_breaking_the_call():
    class Unprintable(Exception):
        def __str__(self):
            raise ValueError("no str for you")

    span = _call_wrapper(_wrappers.OperationWrapper, "search", _Client(None), Unprintable(), search_request("q"))
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.startswith("Unprintable")


def test_credentials_that_cannot_be_read_fail_closed():
    # If any place the client keeps a credential cannot be read, no free
    # text (query, error message, stack trace) is recorded for the call.
    class Unreadable:
        @property
        def _transport(self):
            raise RuntimeError("credential lookup failed")

    error = core_exceptions.PermissionDenied("denied for secret-query and secret-credential")
    span = _call_wrapper(
        _wrappers.OperationWrapper,
        "search",
        Unreadable(),
        error,
        search_request("secret-query"),
        capture_query=True,
    )
    values = attrs(span)
    assert "input.value" not in values
    assert "gen_ai.retrieval.query" not in values
    assert values["discoveryengine.error.status"] == "PERMISSION_DENIED"
    wire = span.to_json()
    assert "secret-query" not in wire and "secret-credential" not in wire
    assert span.status.description == "PermissionDenied: " + _wrappers.UNREADABLE
    exception = event(span, "exception")
    assert exception["exception.type"] == "google.api_core.exceptions.PermissionDenied"
    assert exception["exception.message"] == _wrappers.UNREADABLE
    assert "exception.stacktrace" not in exception


def test_credentials_that_cannot_be_read_again_after_the_call_fail_closed():
    # They are read again when an error is recorded; if that read fails,
    # no free text is recorded, as when the first read fails.
    class Transport:
        def __init__(self):
            self.reads = 0

        @property
        def _credentials(self):
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("credential lookup failed")
            return None

    client = _Client(None)
    client._transport = Transport()
    error = core_exceptions.PermissionDenied("denied for secret-credential")
    span = _call_wrapper(_wrappers.OperationWrapper, "search", client, error, search_request("q"))

    assert client._transport.reads == 2
    assert span.status.description == "PermissionDenied: " + _wrappers.UNREADABLE
    exception = event(span, "exception")
    assert exception["exception.message"] == _wrappers.UNREADABLE
    assert "exception.stacktrace" not in exception
    assert "secret-credential" not in span.to_json()


def test_async_errors_are_redacted_the_same_way(fake):
    async def call():
        client = async_search_client(fake, credentials=oauth_credentials())
        try:
            await client.search(request=search_request(FAIL_DENIED))
        finally:
            await client.transport.close()

    with instrumented() as traced:
        with pytest.raises(core_exceptions.PermissionDenied) as raised:
            asyncio.run(call())

    assert ACCESS_TOKEN in str(raised.value)
    span = traced.one()
    assert attrs(span)["discoveryengine.error.code"] == 403
    assert ACCESS_TOKEN not in traced.wire()
