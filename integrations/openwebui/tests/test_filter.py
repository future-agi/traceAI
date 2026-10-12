"""Contract tests for the Future AGI Open WebUI filter.

The hook arguments mirror what Open WebUI 0.11.4 passes a Function filter
(``open_webui/utils/filter.py``, ``utils/middleware.py`` ``outlet_filter_handler``):
the instance is cached between calls, its ``valves`` are set from the
database before every hook, ``inlet``/``outlet`` get ``body``, ``__user__``
and ``__metadata__``, and the outlet body carries the stored messages with
the reply's ``usage`` on the last one. Spans go to an in-memory exporter or
to a loopback OTLP/HTTP server in this file; nothing leaves the machine.
"""

import importlib.util
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

FILTER_PATH = Path(__file__).resolve().parents[1] / "futureagi_filter.py"
README = Path(__file__).resolve().parents[1] / "README.md"

_SPEC = importlib.util.spec_from_file_location("futureagi_filter", FILTER_PATH)
assert _SPEC and _SPEC.loader
_FILTER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FILTER)

CHAT_ID = "2f1c9e4e-7d4b-4a57-9a55-1d1f1a2b3c4d"
USER_MESSAGE_ID = "8a6d2a3e-1111-4c2b-9d11-aaaaaaaaaaaa"
ASSISTANT_MESSAGE_ID = "8a6d2a3e-2222-4c2b-9d11-bbbbbbbbbbbb"
USER = {"id": "u-123", "email": "ada@example.com", "name": "Ada", "role": "user"}
QUESTION = "What is the capital of France? QMARK-owui"
ANSWER = "Paris. AMARK-owui"


def metadata():
    return {
        "user_id": USER["id"],
        "chat_id": CHAT_ID,
        "user_message_id": USER_MESSAGE_ID,
        "assistant_message_id": None,
        "session_id": "socket-session-1",
        "user_message": {"id": USER_MESSAGE_ID, "role": "user", "content": QUESTION},
    }


def inlet_body():
    return {
        "stream": True,
        "model": "acme-chat-model",
        "messages": [{"role": "user", "content": QUESTION}],
        "metadata": metadata(),
    }


def outlet_body(usage=True):
    reply = {
        "id": ASSISTANT_MESSAGE_ID,
        "role": "assistant",
        "content": ANSWER,
        "info": None,
        "timestamp": 1791777326,
    }
    if usage:
        reply["usage"] = {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
            "input_tokens": 12,
            "output_tokens": 3,
        }
    return {
        "model": "acme-chat-model",
        "messages": [
            {
                "id": USER_MESSAGE_ID,
                "role": "user",
                "content": QUESTION,
                "info": None,
                "timestamp": 1791777326,
            },
            reply,
        ],
        "filter_ids": [],
        "chat_id": CHAT_ID,
        "session_id": "socket-session-1",
        "id": ASSISTANT_MESSAGE_ID,
    }


def make_filter(exporter=None, **valves):
    """A filter as Open WebUI builds it: Filter(), then valves from the database."""
    filt = _FILTER.Filter()
    values = dict(
        api_key="fi-key-placeholder",
        secret_key="fi-secret-placeholder",
        project="owui-test",
    )
    values.update(valves)
    filt.valves = filt.Valves(**values)
    if exporter is not None:
        filt._make_exporter = lambda valves: exporter
    return filt


def run_turn(filt, body=None, user=USER):
    # Open WebUI hands inlet and outlet of one request the same metadata dict.
    meta = metadata()
    filt.inlet(inlet_body(), __user__=user, __metadata__=meta)
    time.sleep(0.01)
    result = filt.outlet(body or outlet_body(), __user__=user, __metadata__=meta)
    filt.flush()
    return result


def spans_by_name(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


# --------------------------------------------------------------------------
# What Open WebUI needs to load and configure the function
# --------------------------------------------------------------------------


def extract_frontmatter(content):
    """Open WebUI 0.11.4 ``utils/plugin.py`` extract_frontmatter, reproduced."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != '"""':
        return {}
    out = {}
    for line in lines[1:]:
        if '"""' in line:
            break
        match = re.match(r"^\s*([a-z_]+):\s*(.*)\s*$", line, re.IGNORECASE)
        if match:
            out[match.group(1).strip()] = match.group(2).strip()
    return out


def test_frontmatter_is_parsed_by_open_webui():
    frontmatter = extract_frontmatter(FILTER_PATH.read_text(encoding="utf-8"))
    assert frontmatter["title"] == "Future AGI"
    requirements = [r.strip() for r in frontmatter["requirements"].split(",")]
    # Open WebUI 0.11.4 ships the OTel SDK but not the OTLP/HTTP exporter.
    assert "opentelemetry-exporter-otlp-proto-http" in requirements


def test_valves_are_a_filter_attribute_with_defaults():
    # Open WebUI applies valves only when the Filter instance has both
    # ``valves`` and ``Valves`` (utils/filter.py apply_filter_valves), and
    # builds ``Valves(**{})`` before an admin saved any.
    filt = _FILTER.Filter()
    assert hasattr(filt, "Valves") and hasattr(filt, "valves")
    defaults = filt.Valves()
    assert defaults.endpoint == "https://api.futureagi.com"
    # Content off by default: Open WebUI serves many users.
    assert defaults.redact is True
    assert defaults.include_email_hash is False


def test_secret_valves_are_masked_but_stored_by_value():
    valves = _FILTER.Filter.Valves(
        api_key="fi-key-placeholder", secret_key="fi-secret-placeholder", project="p"
    )
    assert "fi-key-placeholder" not in repr(valves)
    # Open WebUI stores valves with model_dump(exclude_unset=True) as JSON.
    stored = json.loads(json.dumps(valves.model_dump(exclude_unset=True)))
    assert stored["api_key"] == "fi-key-placeholder"
    assert stored["secret_key"] == "fi-secret-placeholder"
    reloaded = _FILTER.Filter.Valves(**stored)
    assert reloaded.api_key.get_secret_value() == "fi-key-placeholder"


def test_secret_valves_render_as_password_inputs():
    schema = _FILTER.Filter.Valves.model_json_schema()
    for key in ("api_key", "secret_key"):
        assert schema["properties"][key]["input"] == {"type": "password"}


# --------------------------------------------------------------------------
# One chat turn
# --------------------------------------------------------------------------


def test_turn_is_a_root_span_with_an_llm_child():
    exporter = InMemorySpanExporter()
    run_turn(make_filter(exporter))
    spans = spans_by_name(exporter)
    assert set(spans) == {"openwebui.chat", "openwebui.llm"}
    root, llm = spans["openwebui.chat"], spans["openwebui.llm"]
    assert root.parent is None
    assert llm.parent.span_id == root.context.span_id
    assert llm.context.trace_id == root.context.trace_id
    assert root.attributes["fi.span.kind"] == "CHAIN"
    assert llm.attributes["fi.span.kind"] == "LLM"
    assert root.status.status_code != StatusCode.ERROR


def test_llm_span_has_model_usage_and_content():
    exporter = InMemorySpanExporter()
    run_turn(make_filter(exporter, redact=False))
    spans = spans_by_name(exporter)
    llm = spans["openwebui.llm"].attributes
    assert llm["gen_ai.request.model"] == "acme-chat-model"
    assert llm["llm.model_name"] == "acme-chat-model"
    assert llm["gen_ai.usage.input_tokens"] == 12
    assert llm["gen_ai.usage.output_tokens"] == 3
    assert llm["llm.token_count.total"] == 15
    assert json.loads(llm["input.value"]) == [{"role": "user", "content": QUESTION}]
    assert llm["output.value"] == ANSWER
    root = spans["openwebui.chat"].attributes
    assert root["input.value"] == QUESTION
    assert root["output.value"] == ANSWER


def test_user_and_session_come_from_open_webui():
    exporter = InMemorySpanExporter()
    run_turn(make_filter(exporter))
    for span in exporter.get_finished_spans():
        assert span.attributes["user.id"] == USER["id"]
        assert span.attributes["session.id"] == CHAT_ID
        assert span.attributes["gen_ai.conversation.id"] == CHAT_ID
        assert "user.email_hash" not in span.attributes
        assert USER["email"] not in json.dumps(dict(span.attributes))


def test_email_hash_is_opt_in_and_never_raw():
    exporter = InMemorySpanExporter()
    run_turn(make_filter(exporter, include_email_hash=True))
    root = spans_by_name(exporter)["openwebui.chat"]
    assert root.attributes["user.email_hash"] == _FILTER.email_hash(USER["email"])
    for span in exporter.get_finished_spans():
        assert USER["email"] not in json.dumps(dict(span.attributes))


@pytest.mark.parametrize("valves", [{}, {"redact": True}], ids=["default", "redact"])
def test_content_stays_out_of_every_span(valves):
    exporter = InMemorySpanExporter()
    run_turn(make_filter(exporter, **valves))
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    wire = json.dumps([dict(span.attributes) for span in spans])
    assert "QMARK-owui" not in wire and "AMARK-owui" not in wire
    for span in spans:
        assert "input.value" not in span.attributes
        assert "output.value" not in span.attributes
    # Ids, model and usage still arrive.
    llm = spans_by_name(exporter)["openwebui.llm"].attributes
    assert llm["gen_ai.request.model"] == "acme-chat-model"
    assert llm["gen_ai.usage.input_tokens"] == 12


def test_total_tokens_are_prompt_plus_completion():
    # Open WebUI 0.11.4 sums total/input/output tokens over stream chunks; a
    # provider that repeats usage in two chunks gave, live: prompt 8838,
    # completion 7, total 17690, input 17676, output 14. The provider said 8845.
    body = outlet_body()
    body["messages"][-1]["usage"] = {
        "prompt_tokens": 8838,
        "completion_tokens": 7,
        "total_tokens": 17690,
        "input_tokens": 17676,
        "output_tokens": 14,
    }
    exporter = InMemorySpanExporter()
    run_turn(make_filter(exporter), body=body)
    llm = spans_by_name(exporter)["openwebui.llm"].attributes
    assert llm["gen_ai.usage.input_tokens"] == 8838
    assert llm["gen_ai.usage.output_tokens"] == 7
    assert llm["llm.token_count.total"] == 8845


def test_missing_usage_is_flagged():
    exporter = InMemorySpanExporter()
    run_turn(make_filter(exporter), body=outlet_body(usage=False))
    llm = spans_by_name(exporter)["openwebui.llm"].attributes
    assert llm["gen_ai.usage.unavailable"] is True
    assert "gen_ai.usage.input_tokens" not in llm


def test_turn_starts_at_inlet():
    exporter = InMemorySpanExporter()
    filt = make_filter(exporter)
    meta = metadata()
    filt.inlet(inlet_body(), __user__=USER, __metadata__=meta)
    time.sleep(0.2)
    filt.outlet(outlet_body(), __user__=USER, __metadata__=meta)
    filt.flush()
    root = spans_by_name(exporter)["openwebui.chat"]
    llm = spans_by_name(exporter)["openwebui.llm"]
    assert root.end_time - root.start_time >= 150_000_000
    assert (llm.start_time, llm.end_time) == (root.start_time, root.end_time)
    # The start time rode on the request's metadata and is gone again.
    assert not any(key.startswith("_futureagi") for key in meta)


def test_outlet_without_inlet_still_exports():
    exporter = InMemorySpanExporter()
    filt = make_filter(exporter)
    filt.outlet(outlet_body(), __user__=USER, __metadata__=metadata())
    filt.flush()
    assert set(spans_by_name(exporter)) == {"openwebui.chat", "openwebui.llm"}


def test_inlets_without_outlets_leave_nothing_on_the_filter():
    # Open WebUI runs no outlet when the model call fails.
    filt = make_filter(InMemorySpanExporter())
    before = dict(vars(filt))
    for _ in range(100):
        filt.inlet(inlet_body(), __user__=USER, __metadata__=metadata())
    assert vars(filt) == before


def test_no_keys_means_no_export_and_no_error():
    exporter = InMemorySpanExporter()
    filt = _FILTER.Filter()
    filt._make_exporter = lambda valves: exporter
    body = outlet_body()
    assert filt.outlet(body, __user__=USER, __metadata__=metadata()) is body
    filt.flush()
    assert exporter.get_finished_spans() == ()


# --------------------------------------------------------------------------
# The chat never waits for, or breaks on, the export
# --------------------------------------------------------------------------


class SlowExporter(SpanExporter):
    def __init__(self):
        self.started = threading.Event()

    def export(self, spans):
        self.started.set()
        time.sleep(2)
        return SpanExportResult.FAILURE

    def shutdown(self):
        return None


def test_outlet_does_not_wait_for_the_export():
    exporter = SlowExporter()
    filt = make_filter(exporter)
    meta = metadata()
    filt.inlet(inlet_body(), __user__=USER, __metadata__=meta)
    started = time.monotonic()
    body = outlet_body()
    assert filt.outlet(body, __user__=USER, __metadata__=meta) is body
    assert time.monotonic() - started < 0.5


def test_hooks_return_the_body_when_tracing_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("collector down")

    filt = make_filter(InMemorySpanExporter())
    monkeypatch.setattr(filt, "_record_turn", boom)
    body = outlet_body()
    assert filt.outlet(body, __user__=USER, __metadata__=metadata()) is body
    monkeypatch.setattr(filt, "_start_turn", boom)
    first = inlet_body()
    assert filt.inlet(first, __user__=USER, __metadata__=metadata()) is first


def test_new_valves_rebuild_the_exporter():
    filt = make_filter()
    first = filt._provider_for(filt.valves)
    assert filt._provider_for(filt.valves) is first
    filt.valves = filt.Valves(
        api_key="other", secret_key="fi-secret-placeholder", project="owui-test"
    )
    assert filt._provider_for(filt.valves) is not first


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def test_collector_path():
    assert (
        _FILTER.collector_endpoint("https://api.futureagi.com")
        == "https://api.futureagi.com/tracer/v1/traces"
    )
    assert (
        _FILTER.collector_endpoint("https://api.futureagi.com/tracer/v1/traces/")
        == "https://api.futureagi.com/tracer/v1/traces"
    )


class CaptureServer:
    """Loopback OTLP/HTTP endpoint that keeps each request's path, headers and decoded body."""

    def __init__(self):
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                message = ExportTraceServiceRequest()
                message.ParseFromString(body)
                headers = {k.lower(): v for k, v in self.headers.items()}
                owner.requests.append(
                    {
                        "path": self.path,
                        "headers": headers,
                        "body": MessageToDict(message),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def test_turn_reaches_the_collector_path_with_keys_and_project():
    server = CaptureServer()
    try:
        filt = make_filter(endpoint=server.origin + "/")
        run_turn(filt)
    finally:
        server.close()
    assert server.requests
    names = []
    for request in server.requests:
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == "fi-key-placeholder"
        assert request["headers"]["x-secret-key"] == "fi-secret-placeholder"
        for resource_spans in request["body"]["resourceSpans"]:
            resource = {
                a["key"]: a["value"].get("stringValue")
                for a in resource_spans["resource"]["attributes"]
            }
            assert resource["project_name"] == "owui-test"
            assert resource["project_type"] == "observe"
            for scope_spans in resource_spans["scopeSpans"]:
                names.extend(span["name"] for span in scope_spans["spans"])
        assert "fi-key-placeholder" not in json.dumps(request["body"])
    assert sorted(names) == ["openwebui.chat", "openwebui.llm"]


def test_readme_lists_the_steps_open_webui_needs():
    readme = README.read_text(encoding="utf-8")
    assert not readme.startswith('"""')
    for fact in (
        "Global",
        "Valves",
        "/tracer/v1/traces",
        "X-Api-Key",
        "X-Secret-Key",
        "0.11.4",
        "redact",
    ):
        assert fact in readme, fact
