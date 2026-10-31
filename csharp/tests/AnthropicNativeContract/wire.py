"""Offline engineering checks; run with outbound networking denied after restore/build.

python -I wire.py --dotnet /path/to/dotnet --dll /path/to/AnthropicNativeContract.dll
Install requirements.txt first. This is not a customer telemetry bridge.
"""

import argparse
import contextlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

SCENARIOS = [
    "metadata",
    "content",
    "environment-content",
    "option-precedence",
    "option-enables",
    "disabled-env",
    "disabled-option",
    "unsubscribed",
    "retry",
    "http-error",
    "stream",
    "early-break",
    "early-dispose",
    "cancel",
    "stream-error",
    "truncated-stream",
    "truncation",
    "concurrent",
    "tool-content",
    "tool-metadata",
    "binary-content",
    "binary-metadata",
]


def message(output=5):
    return {
        "id": "msg_contract",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "RESPONSE_SENTINEL"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 7,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
            "output_tokens": output,
        },
    }


class Fixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.server.role == "otlp":
            req = ExportTraceServiceRequest.FromString(body)
            self.server.captures.append((self.path, dict(self.headers), body, req))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.loads(body)
        self.server.requests.append((self.path, dict(self.headers), data))
        scenario = self.server.scenario
        if scenario == "http-error" or (
            scenario == "retry" and len(self.server.requests) == 1
        ):
            error = {
                "type": "error",
                "error": {
                    "type": "invalid_request_error"
                    if scenario == "http-error"
                    else "overloaded_error",
                    "message": "ERROR_CONTENT_SENTINEL",
                },
            }
            encoded = json.dumps(error).encode()
            self.send_response(400 if scenario == "http-error" else 529)
            self.send_header("retry-after", "0")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if data.get("stream"):
            start = message(1)
            start.update(content=[], stop_reason=None)
            events = [
                {"type": "message_start", "message": start},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "RESPONSE_SENTINEL"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
            if scenario == "stream-error":
                events = events[:2] + [
                    {
                        "type": "error",
                        "error": {
                            "type": "overloaded_error",
                            "message": "ERROR_CONTENT_SENTINEL",
                        },
                    }
                ]
            if scenario == "truncated-stream":
                events = events[:3]
            if scenario == "cancel":
                events = events[:1]
            encoded = "".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
            ).encode()
            content_type = "text/event-stream"
        else:
            reply = message()
            if scenario.startswith("tool-") and len(self.server.requests) == 1:
                reply.update(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "tool_contract",
                            "name": "lookup",
                            "input": {},
                            "caller": {"type": "direct"},
                        }
                    ],
                    stop_reason="tool_use",
                )
            encoded = json.dumps(reply).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if scenario != "cancel":
            self.send_header("Content-Length", str(len(encoded)))
        self.send_header("request-id", "req_contract")
        self.end_headers()
        try:
            self.wfile.write(encoded)
            self.wfile.flush()
            if scenario == "cancel":
                self.server.release.wait(10)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextlib.contextmanager
def server(role, scenario):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    srv.role, srv.scenario = role, scenario
    srv.captures, srv.requests = [], []
    srv.release = threading.Event()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.release.set()
        srv.shutdown()
        srv.server_close()
        thread.join()


def value(v):
    kind = v.WhichOneof("value")
    if kind == "array_value":
        return [value(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return attrs(v.kvlist_value.values)
    return getattr(v, kind)


def attrs(kvs):
    return {kv.key: value(kv.value) for kv in kvs}


def check(scenario, collector, api, negative):
    native, parents, all_attributes = [], {}, []
    for path, headers, body, req in collector.captures:
        headers = {k.lower(): v for k, v in headers.items()}
        assert path == "/tracer/v1/traces", path
        assert headers["content-type"] == "application/x-protobuf"
        assert headers["x-api-key"] == "synthetic-fi-api"
        assert headers["x-secret-key"] == "synthetic-fi-secret"
        for rs in req.resource_spans:
            resource = attrs(rs.resource.attributes)
            assert resource["project_name"] == "anthropic-csharp-contract"
            assert resource["project_type"] == "observe"
            assert resource["service.name"] == "anthropic-csharp-contract"
            for scope in rs.scope_spans:
                for span in scope.spans:
                    all_attributes.append(MessageToDict(span))
                    if scope.scope.name.startswith("com.anthropic.sdk.csharp"):
                        native.append(span)
                    else:
                        parents[span.span_id] = span
    expected = (
        0
        if negative or scenario in ("unsubscribed", "disabled-env", "disabled-option")
        else (
            4 if scenario.startswith("tool-") else 2 if scenario == "concurrent" else 1
        )
    )
    assert len(native) == expected, (scenario, len(native), expected)
    assert len(parents) == (2 if scenario == "concurrent" else 1)
    serialized = json.dumps(all_attributes)
    for sentinel in [
        "synthetic-anthropic-key",
        "synthetic-fi-api",
        "synthetic-fi-secret",
        "ERROR_CONTENT_SENTINEL",
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6D8AAAAASUVORK5CYII=",
    ]:
        assert sentinel not in serialized, (
            f"Credential/error content leaked: {sentinel}"
        )
    for path, headers, _ in api.requests:
        headers = {k.lower(): v for k, v in headers.items()}
        assert urlsplit(path).path == "/v1/messages", path
        assert urlsplit(path).query in ("", "beta=true"), path
        assert (
            headers.get("x-api-key") == "synthetic-anthropic-key"
            or headers.get("authorization") == "Bearer synthetic-anthropic-key"
        ), sorted(headers)
        assert "x-secret-key" not in headers
        assert "synthetic-fi" not in json.dumps(headers)
    assert len(api.requests) == (
        2 if scenario in ("retry", "concurrent", "tool-content", "tool-metadata") else 1
    )
    if not native:
        return
    if scenario.startswith("tool-"):
        runner = next(s for s in native if s.name == "anthropic.messages.tool_runner")
        tool = next(s for s in native if s.name == "anthropic.custom_tool_use lookup")
        models = [s for s in native if s.name == "anthropic.messages.create"]
        assert len(models) == 2
        assert runner.parent_span_id in parents
        for child in [tool, *models]:
            assert child.parent_span_id == runner.span_id
            assert child.trace_id == runner.trace_id
            assert child.status.code == 1
        if scenario == "tool-metadata":
            for sentinel in [
                "PROMPT_SENTINEL",
                "RESPONSE_SENTINEL",
                "TOOL_RESULT_SENTINEL",
            ]:
                assert sentinel not in serialized
        else:
            assert "TOOL_RESULT_SENTINEL" in serialized
        parents[runner.span_id] = runner
        native = models
    content = scenario in (
        "binary-content",
        "tool-content",
        "content",
        "environment-content",
        "stream",
        "early-break",
        "early-dispose",
        "cancel",
        "stream-error",
        "truncated-stream",
        "truncation",
    )
    if not content:
        for sentinel in ["SYSTEM_SENTINEL", "PROMPT_SENTINEL", "RESPONSE_SENTINEL"]:
            assert sentinel not in serialized, f"Metadata-only leaked {sentinel}"
    conversations = set()
    for span in native:
        a = attrs(span.attributes)
        assert span.name == "anthropic.messages.create"
        assert span.kind == 3  # CLIENT
        assert span.parent_span_id in parents
        parent = parents[span.parent_span_id]
        assert span.trace_id == parent.trace_id
        assert span.end_time_unix_nano >= span.start_time_unix_nano > 0
        assert a["gen_ai.provider.name"] == "anthropic"
        assert a["gen_ai.request.model"] == "claude-opus-5-5"
        if scenario == "concurrent":
            conversations.add(a["gen_ai.conversation.id"])
            assert a["gen_ai.conversation.id"] == parent.name
        else:
            assert "gen_ai.conversation.id" not in a
        expected_status = (
            2
            if scenario in ("http-error", "stream-error", "truncated-stream")
            else 0
            if scenario in ("early-break", "early-dispose", "cancel")
            else 1
        )
        assert span.status.code == expected_status, (scenario, span.status, a)
        if scenario in (
            "early-break",
            "early-dispose",
            "cancel",
            "stream-error",
            "truncated-stream",
        ):
            assert a.get("gen_ai.usage.output_tokens") != 5, a
            assert not a.get("gen_ai.response.finish_reasons"), a
        elif scenario != "http-error":
            assert a["gen_ai.response.model"] == "claude-opus-5-5"
            assert a["gen_ai.response.id"] == "msg_contract"
            assert a["gen_ai.usage.input_tokens"] == 12, a
            assert a["gen_ai.usage.output_tokens"] == 5, a
            assert a["gen_ai.usage.cache_read.input_tokens"] == 3
            assert a["gen_ai.usage.cache_write.input_tokens"] == 2
            assert (
                a["gen_ai.usage.input_tokens"] + a["gen_ai.usage.output_tokens"] == 17
            )
        if scenario in ("binary-content", "content", "environment-content", "stream"):
            assert "PROMPT_SENTINEL" in a["gen_ai.input.messages"]
            assert "SYSTEM_SENTINEL" in a["gen_ai.system_instructions"]
            assert "RESPONSE_SENTINEL" in a["gen_ai.output.messages"]
        if scenario == "binary-content":
            assert json.loads(a["gen_ai.input.messages"])[0]["parts"][1] == {
                "type": "anthropic_image"
            }
        if scenario == "truncation":
            assert a["anthropic.content.truncated"] is True
            assert "PROMPT_SENTINEL" not in json.dumps(a)
        if scenario == "retry":
            assert a["http.request.resend_count"] == 1
    if scenario == "concurrent":
        assert conversations == {"conversation-a", "conversation-b"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dotnet", required=True)
    parser.add_argument("--dll", required=True)
    parser.add_argument("--negative", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario in ["metadata"] if args.negative else SCENARIOS:
        with server("api", scenario) as api, server("otlp", scenario) as collector:
            # Do not inherit developer credentials, proxies or arbitrary OTel configuration.
            env = {
                "PATH": os.defpath,
                "HOME": str(args.output),
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_SYSTEM_NET_DISABLEIPV6": "1",
            }
            run = subprocess.run(
                [
                    args.dotnet,
                    args.dll,
                    scenario,
                    f"http://127.0.0.1:{api.server_port}/",
                    f"http://127.0.0.1:{collector.server_port}/tracer/v1/traces",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            for i, (_, _, body, req) in enumerate(collector.captures):
                (args.output / f"{scenario}-{i}.pb").write_bytes(body)
                (args.output / f"{scenario}-{i}.json").write_text(
                    json.dumps(MessageToDict(req), indent=2) + "\n"
                )
            try:
                assert run.returncode == 0, run.stdout + run.stderr
                check(scenario, collector, api, args.negative)
                results.append({"scenario": scenario, "status": "PASS"})
            except AssertionError as error:
                results.append(
                    {"scenario": scenario, "status": "FAIL", "error": str(error)}
                )
            print(
                scenario,
                results[-1]["status"],
                results[-1].get("error", "")[:1000],
                flush=True,
            )
    (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    if any(r["status"] != "PASS" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
