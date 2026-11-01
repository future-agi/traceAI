"""Maintained adapter contracts using real Cognee and synthetic localhost providers.

Run with the adapter, Cognee's tracing extra and pytest installed, under an OS
sandbox denying outbound network except localhost. No real provider keys are used.
"""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags
from traceai_cognee._exporter import project

TESTS = Path(__file__).resolve().parent
EXAMPLE = TESTS.parent
spec = importlib.util.spec_from_file_location("fake_openai", TESTS / "_fake_openai.py")
fake_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake_module)


def test_example_uses_maintained_adapter():
    requirements = (EXAMPLE / "requirements.txt").read_text().splitlines()
    assert "cognee[tracing]==1.6.5" in requirements
    assert "traceai-cognee==0.1.0" in requirements
    source = (EXAMPLE / "src/app.py").read_text()
    assert "from traceai_cognee import configure" in source
    assert "cognee_filter" not in source
    readme = (EXAMPLE / "README.md").read_text()
    assert "capture_content=True" in readme
    assert "COGNEE_TRACING_ENABLED=true" in readme
    assert "cognee.disable_tracing()" in readme


@pytest.mark.parametrize("capture", [False, True])
def test_recorded_native_spans_keep_identity_status_and_privacy(capture):
    recorded = json.loads((TESTS / "fixtures/cognee-1.6.2-recipe-spans.json").read_text())
    kinds = {"SPAN_KIND_INTERNAL": SpanKind.INTERNAL, "SPAN_KIND_CLIENT": SpanKind.CLIENT}
    statuses = {"STATUS_CODE_OK": StatusCode.OK, "STATUS_CODE_ERROR": StatusCode.ERROR}

    def context(trace_id, span_id):
        return SpanContext(int.from_bytes(base64.b64decode(trace_id), "big"),
                           int.from_bytes(base64.b64decode(span_id), "big"),
                           False, TraceFlags(1))

    def attrs(items):
        return {item["key"]: next(iter(item["value"].values())) for item in items}

    output = []
    for item in recorded:
        parent = context(item["traceId"], item["parentSpanId"]) if item.get("parentSpanId") else None
        status = item.get("status", {})
        native = ReadableSpan(
            name=item["name"], context=context(item["traceId"], item["spanId"]), parent=parent,
            resource=Resource({"private": "unapproved-resource"}),
            attributes=attrs(item.get("attributes", [])),
            events=[Event(e["name"], attrs(e.get("attributes", [])), int(e["timeUnixNano"]))
                    for e in item.get("events", [])],
            kind=kinds[item["kind"]], status=Status(statuses.get(status.get("code"), StatusCode.UNSET), status.get("message")),
            start_time=int(item["startTimeUnixNano"]), end_time=int(item["endTimeUnixNano"]),
        )
        before = dict(native.attributes)
        mapped = project(native, "synthetic-project", capture)
        assert mapped.context == native.context and mapped.parent == native.parent
        assert mapped.start_time == native.start_time and mapped.end_time == native.end_time
        assert mapped.status.status_code == native.status.status_code
        assert mapped.status.description is None
        assert dict(native.attributes) == before
        assert dict(mapped.resource.attributes) == {"project_name": "synthetic-project", "project_type": "observe"}
        assert all(not event.attributes for event in mapped.events)
        if item["name"] == "memory.retrieve":
            assert mapped.attributes["fi.span.kind"] == "RETRIEVER"
        if item["name"] == "cognee.observe.acreate_structured_output":
            assert mapped.attributes["fi.span.kind"] == "LLM"
        if not capture:
            assert not {"langfuse.observation.input", "langfuse.observation.output", "memory.query.text",
                        "cognee.search.query", "cognee.db.query", "input.value", "output.value"}.intersection(mapped.attributes)
        output.append(mapped)
    assert any(span.status.status_code == StatusCode.ERROR for span in output)
    assert len(output) == len(recorded)


@pytest.mark.parametrize("capture", [False, True])
def test_example_native_operations_export_once_with_content_policy(tmp_path, capture):
    # The process runs real Cognee; only model responses and OTLP transport are local fakes.
    fake = fake_module.FakeOpenAI()
    bodies = []
    auth_headers = []

    class CombinedHandler(fake._server.RequestHandlerClass):
        def do_POST(self):
            if self.path != "/tracer/v1/traces":
                return super().do_POST()
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4_000_000:
                self.send_error(413)
                return
            bodies.append(self.rfile.read(length))
            auth_headers.append((self.headers.get("X-Api-Key"), self.headers.get("X-Secret-Key")))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    fake._server.RequestHandlerClass = CombinedHandler
    for name in ("home", "tmp", "cache", "config", "data", "system", "logs"):
        (tmp_path / name).mkdir()
    env = {
        "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "NO_COLOR": "1",
        "TELEMETRY_DISABLED": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
        "COGNEE_TRACING_ENABLED": "true", "TEST_CAPTURE_CONTENT": str(capture).lower(),
        "LLM_PROVIDER": "openai", "LLM_MODEL": "openai/gpt-4o-mini", "LLM_ENDPOINT": fake.base_url,
        "LLM_API_KEY": "synthetic-model-key", "OPENAI_API_KEY": "synthetic-model-key",
        "EMBEDDING_PROVIDER": "openai", "EMBEDDING_MODEL": "openai/text-embedding-3-small",
        "EMBEDDING_ENDPOINT": fake.base_url, "EMBEDDING_API_KEY": "synthetic-model-key", "EMBEDDING_DIMENSIONS": "16",
        "DATA_ROOT_DIRECTORY": str(tmp_path / "data"), "SYSTEM_ROOT_DIRECTORY": str(tmp_path / "system"),
        "CACHE_ROOT_DIRECTORY": str(tmp_path / "cache"), "COGNEE_LOGS_DIR": str(tmp_path / "logs"),
        "FI_BASE_URL": fake.base_url.removesuffix("/v1"), "FI_PROJECT_NAME": "synthetic-project",
        "FI_API_KEY": "synthetic-fi-api", "FI_SECRET_KEY": "synthetic-fi-secret",
    }
    import litellm
    tokenizers = Path(litellm.__file__).parent / "litellm_core_utils/tokenizers"
    env["TIKTOKEN_CACHE_DIR"] = str(tokenizers)
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(TESTS / "_run_app.py"),
             "Who works on Lighthouse? QUERY-MARKER-4c9", "Ada works on Lighthouse. DOC-MARKER-3b7"],
            cwd=tmp_path, env=env, capture_output=True, timeout=240,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")[-4000:]
        assert fake_module.ANSWER_MARKER.encode() in result.stdout
    finally:
        fake.close()
    assert bodies and all(headers == ("synthetic-fi-api", "synthetic-fi-secret") for headers in auth_headers)
    spans = []
    for body in bodies:
        request = ExportTraceServiceRequest.FromString(body)
        for resource in request.resource_spans:
            resource_attrs = {a.key: a.value.string_value for a in resource.resource.attributes}
            assert resource_attrs == {"project_name": "synthetic-project", "project_type": "observe"}
            spans.extend(span for scope in resource.scope_spans for span in scope.spans)
    identities = {(s.trace_id, s.span_id) for s in spans}
    assert len(identities) == len(spans)
    assert len({s.trace_id for s in spans}) == 1
    for name in ("remember_and_ask", "memory.store", "memory.process", "memory.retrieve"):
        assert sum(span.name == name for span in spans) == 1
    assert any(s.status.code == 2 for s in spans)  # Do not rewrite native errors to OK.
    assert all(not s.status.message and all(not e.attributes for e in s.events) for s in spans)
    wire = b"".join(bodies)
    for marker in (b"QUERY-MARKER-4c9", fake_module.ANSWER_MARKER.encode()):
        assert (marker in wire) is capture
    assert all(secret not in wire for secret in (b"synthetic-model-key", b"synthetic-fi-api", b"synthetic-fi-secret"))
    for name, kind in (("memory.retrieve", "RETRIEVER"), ("cognee.observe.acreate_structured_output", "LLM")):
        assert any(s.name == name and any(a.key == "fi.span.kind" and a.value.string_value == kind for a in s.attributes) for s in spans)
