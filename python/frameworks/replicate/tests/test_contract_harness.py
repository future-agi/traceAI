"""Shared-harness contract test for traceAI-replicate (TH-8320, TH-8339 Receiver).

The real ``replicate`` client makes real HTTP calls to the loopback
``FakeReplicate``. Spans leave through the real ``fi_instrumentation.register()``
OTLP exporter into ``harness.Receiver``. Nothing calls Replicate and every key
is a placeholder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402
from replicate.exceptions import ModelError  # noqa: E402

from _support import (  # noqa: E402
    API_TOKEN,
    CONTENT_MARKERS,
    FAIL_MODEL,
    FILE_URL,
    IMAGE_MODEL,
    MODEL_ERROR,
    PROMPT,
    STREAM_MODEL,
    STREAM_TEXT,
    TEXT_MODEL,
    TEXT_OUTPUT,
    FakeReplicate,
    RecordingTransport,
    make_client,
)

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
PROJECT = "replicate-contract"


def _journey(monkeypatch: pytest.MonkeyPatch, hide_content: bool):
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_replicate import ReplicateInstrumentor

    for name in ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
        if hide_content:
            monkeypatch.setenv(name, "true")
        else:
            monkeypatch.delenv(name, raising=False)

    with FakeReplicate() as fake, Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
        monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
        provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)
        instrumentor = ReplicateInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        transport = RecordingTransport()
        try:
            client = make_client(fake, transport)
            assert client.run(TEXT_MODEL, input={"prompt": PROMPT}) == ["OUTPUT-", "TEXT-", "MARKER"]
            prediction = client.predictions.create(model=IMAGE_MODEL, input={"prompt": PROMPT})
            prediction.wait()
            with pytest.raises(ModelError):
                client.run(FAIL_MODEL, input={"prompt": PROMPT})
            events = [str(event) for event in client.stream(STREAM_MODEL, input={"prompt": PROMPT})]
            assert "".join(events) == STREAM_TEXT
            assert provider.force_flush(timeout_millis=10_000)
        finally:
            instrumentor.uninstrument()
            provider.shutdown()
        return receiver.spans(), receiver.requests(), transport.requests


def _by_name(spans: List[dict]) -> Dict[str, List[dict]]:
    groups: Dict[str, List[dict]] = {}
    for span in spans:
        groups.setdefault(span["name"], []).append(span)
    return groups


def test_real_client_calls_reach_the_collector_contract(monkeypatch):
    spans, exports, requests = _journey(monkeypatch, hide_content=False)

    # The real SDK made only loopback calls and never fetched the output file.
    assert all("127.0.0.1" in url for _, url in requests)

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert "authorization" not in export["headers"]
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"

    groups = _by_name(spans)
    assert sorted((name, len(group)) for name, group in groups.items()) == [
        ("replicate.predictions.create", 1),
        ("replicate.run", 2),
        ("replicate.stream", 1),
    ]

    ok_run, failed_run = groups["replicate.run"]
    ok = _flatten_attributes(ok_run["attributes"])
    assert ok["gen_ai.provider.name"] == "replicate"
    assert ok["gen_ai.request.model"] == TEXT_MODEL
    assert ok["gen_ai.span.kind"] == "LLM"
    assert ok["replicate.prediction.status"] == "succeeded"
    assert ok["output.value"] == TEXT_OUTPUT
    # register() promotes UNSET to OK on export, so the wrapper's own OK is
    # pinned by the in-memory tests; here it is the wire contract.
    assert ok_run["status"]["code"] == "STATUS_CODE_OK"

    failed = _flatten_attributes(failed_run["attributes"])
    assert failed["replicate.prediction.status"] == "failed"
    assert failed_run["status"]["code"] == "STATUS_CODE_ERROR"
    assert MODEL_ERROR in failed_run["status"]["message"]

    created = _flatten_attributes(groups["replicate.predictions.create"][0]["attributes"])
    assert created["gen_ai.span.kind"] == "CHAIN"
    assert created["replicate.output.type"] == "url"
    assert created["output.value"] == FILE_URL

    streamed = _flatten_attributes(groups["replicate.stream"][0]["attributes"])
    assert streamed["output.value"] == STREAM_TEXT

    # The token never reaches the wire. Content does by default (control run).
    wire = json.dumps(spans) + json.dumps(exports)
    assert API_TOKEN not in wire
    for marker in (PROMPT, TEXT_OUTPUT, STREAM_TEXT):
        assert marker in wire, marker


def test_hidden_content_never_reaches_the_collector(monkeypatch):
    spans, exports, _ = _journey(monkeypatch, hide_content=True)

    assert len(spans) == 4
    wire = json.dumps(spans) + json.dumps(exports)
    assert API_TOKEN not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker
    failed = [
        span
        for span in spans
        if _flatten_attributes(span["attributes"]).get("replicate.prediction.status") == "failed"
    ]
    assert failed and failed[0]["status"]["code"] == "STATUS_CODE_ERROR"
