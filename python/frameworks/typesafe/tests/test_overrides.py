"""Capture the SDK's shallow, last-write-wins body without retaining replaced input."""

from __future__ import annotations

import asyncio
import copy
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx2
import pytest

pytest.importorskip("typesafe_sdk")

from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.exporter.otlp.proto.http import Compression  # noqa: E402
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: E402
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: E402
from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: E402
from typesafe_sdk import Noul, TypeSafeAPIError  # noqa: E402

from tests._support import (  # noqa: E402
    API_KEY,
    NOUL,
    FakeSystemOne,
    async_client,
    client,
    everything,
    instrumented,
    no_retry,
    only_span,
    request_body,
)

ORIGINAL_STATE = {"unused-state": "unused-state-content"}
ORIGINAL_QUESTIONS = {
    "unused-first": Noul(instructions="unused-first-instruction"),
    "unused-second": Noul(instructions="unused-second-instruction"),
}
EFFECTIVE_STATE = {"effective-state": "effective-state-content"}
EFFECTIVE_QUESTIONS = {
    "effective": {"type": "noul", "instructions": "effective-instruction"},
}


def _call(fake, asynchronous, keyword_args=False, **kwargs):
    args = () if keyword_args else (ORIGINAL_STATE, ORIGINAL_QUESTIONS)
    if keyword_args:
        kwargs.update(state=ORIGINAL_STATE, questions=ORIGINAL_QUESTIONS)
    if asynchronous:
        async def run():
            async with async_client(fake, retry=no_retry()) as typesafe:
                return await typesafe.system_one(*args, **kwargs)

        return asyncio.run(run())
    with client(fake, retry=no_retry()) as typesafe:
        return typesafe.system_one(*args, **kwargs)


def _otlp_span(spans):
    """Inspect protobuf sent by the real HTTP exporter to a loopback collector."""
    received = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, body))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    exporter = OTLPSpanExporter(
        endpoint="http://127.0.0.1:{0}/v1/traces".format(server.server_port),
        headers={},
        compression=Compression.NoCompression,
        timeout=5,
    )
    try:
        assert exporter.export(spans) is SpanExportResult.SUCCESS
    finally:
        exporter.shutdown()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert len(received) == 1
    path, body = received[0]
    assert path == "/v1/traces"
    request = ExportTraceServiceRequest.FromString(body)
    wire_spans = [
        span
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    assert len(wire_spans) == 1
    return wire_spans[0], body


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("keyword_args", [False, True], ids=["positional", "keyword"])
@pytest.mark.parametrize("hide_flag", [None, "hide_inputs", "hide_input_text"])
def test_overrides_match_sdk_request_and_otlp(asynchronous, keyword_args, hide_flag):
    overrides = {
        "state": copy.deepcopy(EFFECTIVE_STATE),
        "questions": copy.deepcopy(EFFECTIVE_QUESTIONS),
        "model": "jev-effective",
        # Only supported input fields should be captured, not arbitrary extra_body data.
        "private-extension": "extension-must-not-be-exported",
    }
    before = copy.deepcopy(overrides)
    config = TraceConfig(**({hide_flag: True} if hide_flag else {}))
    fake = FakeSystemOne()
    with instrumented(config) as exporter:
        response = _call(fake, asynchronous, keyword_args, model="jev-unused", extra_body=overrides)
    span = only_span(exporter)
    assert overrides == before
    assert len(fake.requests) == 1
    wire_request = request_body(fake.requests[0])
    assert wire_request == overrides
    assert response.nouls["effective"].noul == NOUL
    attributes = dict(span.attributes)
    assert attributes["gen_ai.request.model"] == wire_request["model"]
    assert attributes["typesafe.question_count"] == len(wire_request["questions"]) == 1
    assert attributes["typesafe.question.effective.type"] == "noul"
    assert attributes["typesafe.question.effective.noul"] == NOUL
    if hide_flag:
        assert "input.value" not in attributes
    else:
        assert json.loads(attributes["input.value"]) == {
            "state": wire_request["state"], "questions": wire_request["questions"],
        }
    serialized = everything(span)
    assert "unused-" not in serialized
    assert "extension-must-not-be-exported" not in serialized
    if hide_flag:
        assert "effective-state-content" not in serialized
        assert "effective-instruction" not in serialized
    wire_span, body = _otlp_span(exporter.get_finished_spans())
    wire_attributes = {
        attribute.key: getattr(attribute.value, attribute.value.WhichOneof("value"))
        for attribute in wire_span.attributes
    }
    assert wire_attributes == attributes
    assert b"unused-" not in body
    assert b"extension-must-not-be-exported" not in body
    assert API_KEY.encode() not in body
    if hide_flag:
        assert b"effective-state-content" not in body
        assert b"effective-instruction" not in body


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("state", [None, "", {}, [], False])
def test_falsy_state_override_is_not_replaced_with_original(asynchronous, state):
    fake = FakeSystemOne()
    with instrumented() as exporter:
        _call(fake, asynchronous, extra_body={"state": state})
    span = only_span(exporter)
    captured = json.loads(span.attributes["input.value"])
    assert captured["state"] == request_body(fake.requests[0])["state"] == state
    assert "unused-state" not in everything(span)
    assert captured["questions"] == request_body(fake.requests[0])["questions"]
    assert span.attributes["typesafe.question_count"] == 2


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_empty_questions_override_replaces_rather_than_merges(asynchronous):
    fake = FakeSystemOne()
    with instrumented() as exporter:
        _call(fake, asynchronous, extra_body={"questions": {}})
    span = only_span(exporter)
    captured = json.loads(span.attributes["input.value"])
    assert captured == {"state": ORIGINAL_STATE, "questions": {}}
    assert captured == {key: request_body(fake.requests[0])[key] for key in captured}
    assert span.attributes["typesafe.question_count"] == 0
    assert not any(key.startswith("typesafe.question.") for key in span.attributes)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("model", [None, ""])
def test_null_or_empty_model_override_does_not_fall_back_on_error(asynchronous, model):
    requests = []

    def reject(request):
        requests.append(request)
        return httpx2.Response(422, json={"detail": "Synthetic invalid override"})

    with instrumented() as exporter:
        with pytest.raises(TypeSafeAPIError):
            _call(reject, asynchronous, model="jev-unused", extra_body={"model": model, "questions": None})
    span = only_span(exporter)
    assert request_body(requests[0])["model"] == model
    assert request_body(requests[0])["questions"] is None
    assert "gen_ai.request.model" not in span.attributes
    assert "typesafe.question_count" not in span.attributes
    assert json.loads(span.attributes["input.value"])["questions"] is None
    assert "unused-first" not in everything(span)
    assert span.attributes["http.response.status_code"] == 422


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_override_content_still_redacts_secrets_and_pii_on_otlp(asynchronous):
    fake = FakeSystemOne()
    overrides = {
        "state": "Contact jane.doe@example.com using {0}".format(API_KEY),
        "questions": EFFECTIVE_QUESTIONS,
    }
    with instrumented(TraceConfig(pii_redaction=True)) as exporter:
        _call(fake, asynchronous, extra_body=overrides)
    span = only_span(exporter)
    assert request_body(fake.requests[0])["state"] == overrides["state"]
    captured = json.loads(span.attributes["input.value"])
    assert captured["state"].startswith("Contact ")
    assert "[redacted]" in captured["state"]
    assert "jane.doe@example.com" not in everything(span)
    assert API_KEY not in everything(span)
    _, body = _otlp_span(exporter.get_finished_spans())
    assert b"jane.doe@example.com" not in body
    assert API_KEY.encode() not in body


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_empty_model_argument_does_not_report_client_default(asynchronous):
    requests = []

    def reject(request):
        requests.append(request)
        return httpx2.Response(422, json={"detail": "Synthetic invalid model"})

    with instrumented() as exporter:
        with pytest.raises(TypeSafeAPIError):
            _call(reject, asynchronous, model="")
    assert request_body(requests[0])["model"] == ""
    assert "gen_ai.request.model" not in only_span(exporter).attributes
