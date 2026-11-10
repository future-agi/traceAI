"""Exercise the installed core in fresh interpreters, outside conftest's mocks."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_PROBE = r'''
import ast
import importlib.metadata
import json
from pathlib import Path
import sys
import traceback
from unittest.mock import patch


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise AssertionError("Real-core regression checks must stay offline")


sys.addaudithook(deny_network)

from fi_instrumentation import TraceConfig
from fi_instrumentation import fi_types
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
import traceai_minimax
from traceai_minimax import MiniMaxInstrumentor
from traceai_minimax._request_attributes_extractor import (
    _ChatCompletionRequestAttributesExtractor,
)

SYSTEM_CONTENT = "Synthetic system instruction: answer in short sentences."
USER_CONTENT = "Synthetic user question."
REQUEST = {
    "model": "synthetic-model",
    "messages": [
        {"role": "system", "content": SYSTEM_CONTENT},
        {"role": "user", "content": USER_CONTENT},
    ],
}
ROLE_KEY = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}"
CONTENT_KEY = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"


def check_constants():
    class_names = {
        "SpanAttributes", "MessageAttributes", "ToolCallAttributes",
        "EmbeddingAttributes", "FiSpanKindValues", "FiMimeTypeValues",
    }
    package_dir = Path(traceai_minimax.__file__).parent
    files = sorted(package_dir.rglob("*.py"))
    missing = []
    checked = 0
    for source in files:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "fi_instrumentation.fi_types":
                for name in node.names:
                    if name.name in class_names:
                        aliases[name.asname or name.name] = name.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            class_name = aliases.get(node.value.id)
            if class_name is None:
                continue
            checked += 1
            real_class = getattr(fi_types, class_name, None)
            if real_class is None or not hasattr(real_class, node.attr):
                missing.append(f"{source.name}:{node.lineno}: {class_name}.{node.attr}")
    assert checked > 0, "The semantic-constant scan must not be empty"
    assert not missing, "Missing real core constants: " + "; ".join(missing)
    return {"files_scanned": len(files), "references_checked": checked, "missing": missing}


def assert_system_message(attributes):
    assert SpanAttributes.GEN_AI_INPUT_MESSAGES == "gen_ai.input.messages"
    assert attributes[ROLE_KEY] == "system", attributes
    assert attributes[CONTENT_KEY] == SYSTEM_CONTENT, attributes


def check_extractor():
    extractor = _ChatCompletionRequestAttributesExtractor()
    attributes = dict(extractor.get_attributes_from_request(REQUEST))
    attributes.update(extractor.get_extra_attributes_from_request(REQUEST))
    assert_system_message(attributes)
    return {"attributes": attributes}


def check_chat(hide_inputs):
    from openai import OpenAI
    from openai.resources.chat.completions import Completions

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MiniMaxInstrumentor()
    calls = []
    response = {
        "id": "synthetic-completion",
        "model": REQUEST["model"],
        "choices": [{"message": {"role": "assistant", "content": "Synthetic reply."}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }

    def offline_create(self, *args, **kwargs):
        assert kwargs == REQUEST
        calls.append(trace.get_current_span().is_recording())
        return response

    # Replace the exact method the instrumentor wraps, before installing the
    # wrapper. The real client and tracing stack run without making HTTP calls.
    try:
        with patch.object(Completions, "create", offline_create):
            instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=hide_inputs))
            try:
                with OpenAI(
                    api_key="synthetic-offline-key",
                    base_url="https://api.minimax.io/v1",
                    max_retries=0,
                ) as client:
                    assert client.chat.completions.create(**REQUEST) is response
            finally:
                instrumentor.uninstrument()
        provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len(calls) == 1, f"Expected one offline client call, got {calls}"
        assert len(spans) == 1, (
            f"Expected one exported span, got {len(spans)}; recording during call: {calls}"
        )
        assert calls == [True], "The chat call must run inside a recording span"
        attributes = dict(spans[0].attributes)
        if hide_inputs:
            serialized = json.dumps(attributes, sort_keys=True)
            assert SYSTEM_CONTENT not in serialized, attributes
            assert USER_CONTENT not in serialized, attributes
        else:
            assert_system_message(attributes)
        return {"span_count": len(spans), "recording_during_call": calls, "attributes": attributes}
    finally:
        provider.shutdown()


case = sys.argv[1]
output = {"case": case, "core_version": importlib.metadata.version("fi-instrumentation-otel")}
try:
    if case == "constants":
        output["result"] = check_constants()
    elif case == "extractor":
        output["result"] = check_extractor()
    else:
        output["result"] = check_chat(hide_inputs=case == "hide_inputs")
    output["ok"] = True
except Exception as error:
    output.update(ok=False, error_type=type(error).__name__, error=str(error))
    traceback.print_exc(file=sys.stderr)
print(json.dumps(output, sort_keys=True))
sys.exit(0 if output["ok"] else 1)
'''


@pytest.mark.parametrize("case", ["constants", "extractor", "chat", "hide_inputs"])
def test_real_core(case):
    # Do not inherit credentials, content-hiding flags, or tracing configuration.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "SystemRoot")
        if key in os.environ
    }
    env.update(FI_BASE_URL="http://127.0.0.1:9", FI_GRPC_URL="http://127.0.0.1:9")
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _PROBE, case],
        cwd=_PACKAGE_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.stdout.strip(), completed.stderr
    result = json.loads(completed.stdout)
    assert result["case"] == case
    assert completed.returncode == 0, f"{result}\n{completed.stderr}"
    assert result["ok"] is True, result
    if case == "constants":
        assert result["result"]["references_checked"] > 0
        assert result["result"]["missing"] == []
    elif case in {"chat", "hide_inputs"}:
        assert result["result"]["span_count"] == 1
        assert result["result"]["recording_during_call"] == [True]
