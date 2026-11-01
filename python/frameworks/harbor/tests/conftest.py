import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from harbor.models.trial.result import TrialResult
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from traceai_harbor import Settings


@pytest.fixture
def receiver():
    state = SimpleNamespace(requests=[], responses=[])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append((self.path, dict(self.headers), data))
            code, body, headers = (
                state.responses.pop(0) if state.responses else (200, b"", {})
            )
            self.send_response(code)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f"http://127.0.0.1:{server.server_port}/tracer/v1/traces"
    state.settings = Settings(
        endpoint=state.endpoint,
        project_name="synthetic-harbor",
        api_key="synthetic-fi-api-canary",
        secret_key="synthetic-fi-secret-canary",
    )
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def trajectory():
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "synthetic-session",
        "trajectory_id": "trajectory-1",
        "agent": {
            "name": "deterministic",
            "version": "1",
            "model_name": "synthetic-model",
        },
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": "Say hello",
            },
            {
                "step_id": 2,
                "source": "agent",
                "timestamp": "2026-01-01T00:00:03Z",
                "message": "Calling tool",
                "metrics": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "cached_tokens": 4,
                    "cost_usd": 0.01,
                },
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "function_name": "hello",
                        "arguments": {"name": "world"},
                    }
                ],
            },
            {
                "step_id": 3,
                "source": "agent",
                "timestamp": "2026-01-01T00:00:04Z",
                "message": "Hello world",
                "llm_call_count": 0,
                "observation": {
                    "results": [{"source_call_id": "call-1", "content": "hello result"}]
                },
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 10,
            "total_completion_tokens": 3,
            "total_cost_usd": 0.01,
        },
    }


@pytest.fixture
def job(tmp_path, trajectory):
    path = tmp_path.resolve() / "job"
    path.mkdir()
    job_id, trial_id = str(uuid4()), str(uuid4())
    (path / "result.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:10Z",
                "n_total_trials": 1,
                "stats": {},
            }
        )
    )
    trial = path / "trial-1"
    (trial / "agent").mkdir(parents=True)
    timing = {
        "started_at": "2026-01-01T00:00:02Z",
        "finished_at": "2026-01-01T00:00:05Z",
    }
    result = TrialResult.model_validate(
        {
            "id": trial_id,
            "task_name": "hello",
            "trial_name": "trial-1",
            "trial_uri": str(trial),
            "task_id": {"path": "/synthetic/task"},
            "task_checksum": "abc123",
            "config": {"task": {"path": "/synthetic/task"}, "trial_name": "trial-1"},
            "agent_info": {
                "name": "deterministic",
                "version": "1",
                "model_info": {"name": "synthetic-model"},
            },
            "agent_result": {
                "n_input_tokens": 10,
                "n_cache_tokens": 4,
                "n_output_tokens": 3,
                "cost_usd": 0.01,
            },
            "verifier_result": {"rewards": {"reward": 0}},
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:10Z",
            "agent_execution": timing,
            "verifier": {
                "started_at": "2026-01-01T00:00:05Z",
                "finished_at": "2026-01-01T00:00:06Z",
            },
        }
    )
    (trial / "result.json").write_text(result.model_dump_json())
    (trial / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    return path


def spans(receiver):
    return [
        span
        for _, _, body in receiver.requests
        for rs in ExportTraceServiceRequest.FromString(body).resource_spans
        for scope in rs.scope_spans
        for span in scope.spans
    ]


def attrs(span):
    return {
        a.key: getattr(a.value, a.value.WhichOneof("value")) for a in span.attributes
    }


def modify_result(job, **changes):
    file = job / "trial-1" / "result.json"
    data = json.loads(file.read_text())
    data.update(changes)
    file.write_text(json.dumps(data))


def exception(kind):
    return {
        "exception_type": kind,
        "exception_message": "PRIVATE ERROR",
        "exception_traceback": "PRIVATE TRACE",
        "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
    }
