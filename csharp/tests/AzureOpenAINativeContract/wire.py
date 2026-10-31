"""Offline contract for published Azure/OpenAI .NET native telemetry.

Run after restoring/building, in a network sandbox that allows only loopback.
The HTTP fixtures are the only fake: the SDK, provider and OTLP exporter are real.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

SCENARIOS = (
    "sync",
    "async",
    "tools",
    "retry",
    "400",
    "401",
    "429",
    "500",
    "cancel-before",
    "cancel-during",
    "disabled",
    "stream-sync",
    "stream-async",
    "embedding",
    "protocol",
    "responses",
    "snippet",
)
EXCLUDED = {
    "disabled",
    "stream-sync",
    "stream-async",
    "embedding",
    "protocol",
    "responses",
}


def attributes(items):
    def value(item):
        kind = item.WhichOneof("value")
        if kind == "array_value":
            return [value(v) for v in item.array_value.values]
        return getattr(item, kind)

    return {item.key: value(item.value) for item in items}


def run_case(dotnet, dll, scenario, conventions, output_dir):
    azure_requests, exports, faults = [], [], []
    request_started = threading.Event()
    cancellation_signalled = threading.Event()

    class AzureHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            assert self.path == "/request-started"
            self.send_response(200 if request_started.wait(5) else 504)
            self.send_header("Content-Length", "0")
            self.end_headers()
            cancellation_signalled.set()

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                azure_requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                status = int(scenario) if scenario.isdigit() else 200
                if scenario == "retry" and len(azure_requests) < 3:
                    status = 429
                if scenario == "cancel-during":
                    request_started.set()
                    assert cancellation_signalled.wait(5)
                    time.sleep(0.6)
                if status != 200:
                    data = {
                        "error": {
                            "message": "ERROR_BODY_PRIVATE_MARKER",
                            "type": "invalid_request_error",
                            "code": "synthetic_error",
                        }
                    }
                elif scenario == "responses":
                    data = {
                        "id": "resp-test",
                        "object": "response",
                        "created_at": 1700000000,
                        "model": "gpt-4o-mini-2024-07-18",
                        "status": "completed",
                        "output": [
                            {
                                "id": "msg-test",
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "OUTPUT_PRIVATE_MARKER",
                                        "annotations": [],
                                    }
                                ],
                            }
                        ],
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 5,
                            "total_tokens": 17,
                        },
                    }
                elif scenario == "embedding":
                    data = {
                        "object": "list",
                        "model": "text-embedding-3-small",
                        "data": [
                            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}
                        ],
                        "usage": {"prompt_tokens": 12, "total_tokens": 12},
                    }
                else:
                    message = {"role": "assistant", "content": "OUTPUT_PRIVATE_MARKER"}
                    if scenario == "tools":
                        message = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-test",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city":"TOOL_ARGUMENT_PRIVATE_MARKER"}',
                                    },
                                }
                            ],
                        }
                    data = {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": "gpt-4o-mini-2024-07-18",
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": "tool_calls"
                                if scenario == "tools"
                                else "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 5,
                            "total_tokens": 17,
                        },
                    }
                content_type = "application/json"
                if scenario.startswith("stream-"):
                    data = {
                        "id": "chatcmpl-test",
                        "object": "chat.completion.chunk",
                        "created": 1700000000,
                        "model": "gpt-4o-mini-2024-07-18",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": "OUTPUT_PRIVATE_MARKER",
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    finish = dict(
                        data,
                        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    )
                    encoded = (
                        "data: "
                        + json.dumps(data)
                        + "\n\ndata: "
                        + json.dumps(finish)
                        + "\n\ndata: [DONE]\n\n"
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    encoded = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("retry-after-ms", "1")
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The cancellation case closes the request intentionally.
            except Exception as error:
                faults.append(repr(error))

    class OtlpHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                request = ExportTraceServiceRequest.FromString(body)
                exports.append((self.path, dict(self.headers), request))
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / f"{len(exports)}.pb").write_bytes(body)
                (output_dir / f"{len(exports)}.json").write_text(
                    json.dumps(
                        MessageToDict(request, preserving_proto_field_name=True),
                        indent=2,
                    )
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception as error:
                faults.append(repr(error))

    azure = ThreadingHTTPServer(("127.0.0.1", 0), AzureHandler)
    otlp = ThreadingHTTPServer(("127.0.0.1", 0), OtlpHandler)
    for server in (azure, otlp):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    # Do not pass ambient credentials, proxy settings or OTel configuration to the app.
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "DOTNET_ROOT",
            "TMPDIR",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
        )
        if key in os.environ
    }
    env.update(
        {
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_SYSTEM_NET_DISABLEIPV6": "1",
            "AZURE_OPENAI_API_KEY": "synthetic-azure-key",
            "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
            "FI_API_KEY": "synthetic-fi-api",
            "FI_SECRET_KEY": "synthetic-fi-secret",
            "FI_PROJECT_NAME": "azure-csharp-contract",
            "AZURE_OPENAI_ENDPOINT": f"http://127.0.0.1:{azure.server_port}/openai/v1/",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"http://127.0.0.1:{otlp.server_port}/tracer/v1/traces",
        }
    )
    if conventions == "latest":
        env["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
    try:
        process = subprocess.run(
            [dotnet, dll, scenario], env=env, text=True, capture_output=True, timeout=45
        )
    finally:
        for server in (azure, otlp):
            server.shutdown()
            server.server_close()
    assert process.returncode == 0, process.stderr
    outcome = (
        {"outcome": "success", "output": process.stdout.strip()}
        if scenario == "snippet"
        else json.loads(process.stdout.strip().splitlines()[-1])
    )
    assert not faults, faults
    assert exports, "No OTLP request (application span should always export)"
    all_spans, model_spans = [], []
    for path, headers, request in exports:
        headers = {k.lower(): v for k, v in headers.items()}
        assert path == "/tracer/v1/traces"
        assert headers["content-type"] == "application/x-protobuf"
        assert headers["x-api-key"] == "synthetic-fi-api"
        assert headers["x-secret-key"] == "synthetic-fi-secret"
        assert headers["fi-project-name"] == "azure-csharp-contract"
        for resource in request.resource_spans:
            resource_attrs = attributes(resource.resource.attributes)
            assert resource_attrs["project_name"] == "azure-csharp-contract"
            assert resource_attrs["project_type"] == "observe"
            assert resource_attrs["service.name"] == (
                "azure-openai-csharp"
                if scenario == "snippet"
                else "azure-openai-csharp-contract"
            )
            for scope in resource.scope_spans:
                for span in scope.spans:
                    all_spans.append(span)
                    if scope.scope.name == "OpenAI.ChatClient":
                        model_spans.append(span)
    parents = [
        span
        for span in all_spans
        if span.name == ("answer-question" if scenario == "snippet" else "application")
    ]
    assert len(parents) == 1
    assert len(all_spans) == 1 + len(model_spans), "Unexpected or duplicate spans"
    assert len({s.span_id for s in all_spans}) == len(all_spans)
    for request in azure_requests:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert headers["authorization"] == "Bearer synthetic-azure-key"
        assert "x-api-key" not in headers and "x-secret-key" not in headers
        assert "fi-project-name" not in headers
        assert "synthetic-fi-" not in json.dumps(request)
        assert request["path"] == (
            "/openai/v1/embeddings"
            if scenario == "embedding"
            else "/openai/v1/responses"
            if scenario == "responses"
            else "/openai/v1/chat/completions"
        )
        assert request["body"]["model"] == "test-deployment"
    assert len(model_spans) == (0 if scenario in EXCLUDED else 1), len(model_spans)
    combined = "\n".join(str(export[2]) for export in exports)
    for marker in (
        "PROMPT_PRIVATE_MARKER",
        "OUTPUT_PRIVATE_MARKER",
        "TOOL_DESCRIPTION_PRIVATE_MARKER",
        "TOOL_ARGUMENT_PRIVATE_MARKER",
        "synthetic-fi-api",
        "synthetic-fi-secret",
        "synthetic-azure-key",
    ):
        assert marker not in combined, marker
    if model_spans:
        span = model_spans[0]
        attrs = attributes(span.attributes)
        assert span.kind == 3  # CLIENT
        assert span.trace_id == parents[0].trace_id
        assert span.parent_span_id == parents[0].span_id
        assert (
            parents[0].start_time_unix_nano
            <= span.start_time_unix_nano
            <= span.end_time_unix_nano
            <= parents[0].end_time_unix_nano
        )
        assert attrs["gen_ai.request.model"] == "test-deployment"
        assert attrs["gen_ai.operation.name"] == "chat"
        provider_key = (
            "gen_ai.provider.name" if conventions == "latest" else "gen_ai.system"
        )
        other_key = (
            "gen_ai.system" if conventions == "latest" else "gen_ai.provider.name"
        )
        assert attrs[provider_key] == "openai" and other_key not in attrs
        assert attrs["server.address"] == "127.0.0.1"
        assert attrs["server.port"] == azure.server_port
        if scenario != "snippet":
            assert attrs["gen_ai.request.max_tokens"] == 32
            assert abs(attrs["gen_ai.request.temperature"] - 0.2) < 0.0001
        if scenario.isdigit():
            assert outcome == {"outcome": "error", "status": int(scenario)}
            assert span.status.code == 2
            assert attrs["error.type"] == scenario
            assert "gen_ai.usage.input_tokens" not in attrs
            # Pin the upstream privacy limitation instead of claiming metadata-only safety.
            assert "ERROR_BODY_PRIVATE_MARKER" in span.status.message
        elif scenario.startswith("cancel-"):
            assert outcome["outcome"] == "cancelled"
            assert span.status.code == 2
            assert "CanceledException" in attrs["error.type"]
            assert "gen_ai.usage.input_tokens" not in attrs
        else:
            assert outcome["outcome"] == "success"
            assert span.status.code != 2
            assert attrs["gen_ai.response.id"] == "chatcmpl-test"
            assert attrs["gen_ai.response.model"] == "gpt-4o-mini-2024-07-18"
            assert attrs["gen_ai.response.finish_reasons"] == [
                "tool_calls" if scenario == "tools" else "stop"
            ]
            assert attrs["gen_ai.usage.input_tokens"] == 12
            assert attrs["gen_ai.usage.output_tokens"] == 5
    expected_requests = (
        3
        if scenario in {"429", "500", "retry"}
        else (0 if scenario == "cancel-before" else 1)
    )
    assert len(azure_requests) == expected_requests, len(azure_requests)
    if not (scenario.isdigit() or scenario.startswith("cancel-")):
        expected_output = (
            "2"
            if scenario == "embedding"
            else "200"
            if scenario == "protocol"
            else '{"city":"TOOL_ARGUMENT_PRIVATE_MARKER"}'
            if scenario == "tools"
            else "OUTPUT_PRIVATE_MARKER"
        )
        assert outcome == {"outcome": "success", "output": expected_output}
    return {
        "scenario": scenario,
        "conventions": conventions,
        "model_spans": len(model_spans),
        "azure_requests": len(azure_requests),
        "error_body_exported": "ERROR_BODY_PRIVATE_MARKER" in combined,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dotnet", required=True)
    parser.add_argument("--dll", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS)
    args = parser.parse_args()
    results, failures = [], []
    for conventions in ("default", "latest"):
        for scenario in (
            [args.scenario]
            if args.scenario
            else [case for case in SCENARIOS if case != "snippet"]
        ):
            try:
                result = run_case(
                    args.dotnet,
                    args.dll,
                    scenario,
                    conventions,
                    args.output / conventions / scenario,
                )
                results.append(result)
                print("PASS", conventions, scenario)
            except Exception as error:
                failures.append(
                    {
                        "scenario": scenario,
                        "conventions": conventions,
                        "error": repr(error),
                    }
                )
                print("FAIL", conventions, scenario, repr(error))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(
        json.dumps({"passed": results, "failed": failures}, indent=2)
    )
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
