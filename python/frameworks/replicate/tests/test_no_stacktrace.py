"""No stack trace, local path or user name leaves the process (re-verification 2026-10-16).

A Python traceback lists the caller's file paths (home directory, user name,
virtualenv). traceAI never exports them, in either content mode, so the
exception event carries the type and the (redacted) message only.
"""

from __future__ import annotations

import getpass
import os

import pytest
from opentelemetry.trace import StatusCode

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import replicate  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from replicate.exceptions import ModelError, ReplicateError  # noqa: E402

from _support import (  # noqa: E402
    FAIL_MODEL,
    MODEL_ERROR,
    PROMPT,
    STREAM_ERROR_MODEL,
    FakeReplicate,
    RecordingTransport,
    exception_events,
    instrumented,
    make_client,
)

# Published core 1.1 retains the legacy descriptions; current core owns error privacy.
NATIVE_ERROR_POLICY = hasattr(TraceConfig(), "hide_error_details")

LOCAL = (__file__, os.path.dirname(os.path.dirname(replicate.__file__)), "Traceback", "site-packages")


def _assert_no_local_detail(traced):
    wire = traced.wire()
    for needle in LOCAL:
        assert needle not in wire, needle
    user = getpass.getuser()
    if len(user) >= 4:  # a short user name can occur by chance
        assert os.sep + user + os.sep not in wire
    for span in traced.replicate_spans():
        for event in exception_events(span):
            assert "exception.stacktrace" not in event.attributes


@pytest.mark.parametrize("config", [TraceConfig(), TraceConfig(hide_outputs=True)], ids=["default", "hidden"])
def test_a_failed_run_exports_type_and_message_but_no_stacktrace(config):
    with FakeReplicate() as fake, instrumented(config=config) as traced:
        with pytest.raises(ModelError):
            make_client(fake).run(FAIL_MODEL, input={"prompt": PROMPT})

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    (event,) = exception_events(span)
    assert event.attributes["exception.type"] == "replicate.exceptions.ModelError"
    if NATIVE_ERROR_POLICY and config.hide_outputs:
        assert span.status.description is None
        assert "exception.message" not in event.attributes
        assert MODEL_ERROR not in traced.wire()
    else:
        expected = "__REDACTED__" if config.hide_outputs else MODEL_ERROR
        assert event.attributes["exception.message"] == expected
    _assert_no_local_detail(traced)


def test_an_http_error_exports_no_stacktrace():
    with FakeReplicate() as fake, instrumented() as traced:
        client = replicate.Client(base_url=fake.origin + "/missing", api_token="r8_wrong-token-0000",
                                  transport=RecordingTransport())
        with pytest.raises(ReplicateError):
            client.run("acme/text-model", input={"prompt": PROMPT})

    (event,) = exception_events(traced.one())
    assert event.attributes["exception.type"] == "replicate.exceptions.ReplicateError"
    _assert_no_local_detail(traced)


def test_a_bad_ref_and_a_stream_error_export_no_stacktrace():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        with pytest.raises(ValueError):
            client.run("not a model ref", input={})
        with pytest.raises(RuntimeError):
            list(client.stream(STREAM_ERROR_MODEL, input={}))

    types = sorted(exception_events(s)[0].attributes["exception.type"] for s in traced.replicate_spans())
    assert types == ["RuntimeError", "ValueError"]
    _assert_no_local_detail(traced)
