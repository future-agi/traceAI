"""Contract test for the TanStack AI recipe (TH-8238).

Runs the example with ``node`` against a loopback fake of the OpenAI Chat
Completions API, and exports straight to the shared harness ``Receiver``, which
decodes the Node exporter's chunked bodies and records headers and resource
attributes per export. No vendor
API is called. Every key is a placeholder.

Install the example first: ``npm install`` in typescript/examples/tanstack-ai.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

TESTS = Path(__file__).resolve().parent
EXAMPLE = TESTS.parent
WORKTREE = EXAMPLE.parents[2]
sys.path.insert(0, str(WORKTREE / "python" / "tests"))
sys.path.insert(0, str(TESTS))

from harness import Receiver, run  # noqa: E402
from _fake_openai import (  # noqa: E402
    ANSWER_USAGE,
    RESPONSE_MODEL,
    TOOL_CALL_USAGE,
    FakeOpenAI,
)

NODE = shutil.which("node")
pytestmark = [
    pytest.mark.skipif(NODE is None, reason="node is not on PATH"),
    pytest.mark.skipif(
        not (EXAMPLE / "node_modules" / "@tanstack" / "ai").is_dir(),
        reason="run `npm install` in typescript/examples/tanstack-ai first",
    ),
]

API_KEY = "placeholder-fi-api-key"
SECRET_KEY = "placeholder-fi-secret-key"
OPENAI_KEY = "placeholder-openai-key"
PLACEHOLDER_KEYS = (API_KEY, SECRET_KEY, OPENAI_KEY)
PROJECT = "th-8238-contract"
MODEL = "gpt-4o-mini"

# Content markers. They must reach the fake model and stdout, and must never
# reach a span while captureContent is at its default.
PROMPT = "PROMPT-MARKER-5f1c what is the weather?"
CITY = "CITY-MARKER-9a2e"
ANSWER = "ANSWER-MARKER-3d7b it is sunny"
SYSTEM_PROMPT = "You are a concise weather assistant."  # fixed in src/chat.mjs
THREAD_ID = "conversation-th8238-7c41"  # the caller's conversation id

ROOT = "chat {0}".format(MODEL)
ITERATION_0 = "chat {0} #0".format(MODEL)
ITERATION_1 = "chat {0} #1".format(MODEL)
TOOL = "execute_tool get_weather"

CONTENT_KEYS = {"gen_ai.input.messages", "gen_ai.output.messages"}


def _env(fi_base_url: str, openai_base_url: str) -> dict[str, str]:
    env = {
        "FI_BASE_URL": fi_base_url,
        "FI_API_KEY": API_KEY,
        "FI_SECRET_KEY": SECRET_KEY,
        "FI_PROJECT_NAME": PROJECT,
        "OPENAI_API_KEY": OPENAI_KEY,
        "OPENAI_BASE_URL": openai_base_url,
        "OPENAI_MODEL": MODEL,
    }
    for name in ("PATH", "HOME", "SYSTEMROOT"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _node(script: Path, fi_base_url: str, fake: FakeOpenAI, *args: str) -> Any:
    argv = [NODE, str(script), PROMPT, *args]
    result = run(argv, _env(fi_base_url, fake.base_url), None, 60)
    assert not result.timed_out, result.stderr.decode()
    return result


def _value(value: dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [_value(item) for item in value["arrayValue"].get("values", [])]
    return value


def _attributes(entity: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: _value(item["value"]) for item in entity.get("attributes", [])}


def _by_name(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    names = [span["name"] for span in spans]
    assert len(names) == len(set(names)), names
    return {span["name"]: span for span in spans}


def _is_error(status: dict[str, Any]) -> bool:
    return status.get("code") in (2, "STATUS_CODE_ERROR")


def _assert_no_placeholder_keys(spans: list[dict[str, Any]], result: Any) -> None:
    """No FI or OpenAI key reaches an exported span, stdout or stderr."""
    surfaces = {
        "spans": json.dumps(spans),
        "stdout": result.stdout.decode("utf-8", "replace"),
        "stderr": result.stderr.decode("utf-8", "replace"),
    }
    for surface, text in surfaces.items():
        for key in PLACEHOLDER_KEYS:
            assert key not in text, (key, surface)


@pytest.fixture(scope="module")
def default_run():
    """One run of the example exactly as shipped (captureContent unset),
    called with a thread id."""
    with Receiver() as receiver, FakeOpenAI(
        CITY, ANSWER
    ) as fake:
        result = _node(EXAMPLE / "src" / "chat.mjs", receiver.origin, fake, THREAD_ID)
        yield SimpleNamespace(
            result=result,
            spans=receiver.spans(),
            exports=receiver.requests(),
            model_requests=list(fake.requests),
            model_authorizations=list(fake.authorizations),
        )


def test_example_answers_through_the_fake_model(default_run):
    result = default_run.result
    assert result.returncode == 0, result.stderr.decode()
    assert ANSWER in result.stdout.decode()

    # Positive control for the no-content test: the markers really flowed
    # through chat() to the model and back.
    first, second = default_run.model_requests
    assert first["model"] == MODEL
    assert {"role": "user", "content": PROMPT} in first["messages"]
    assert {"role": "system", "content": SYSTEM_PROMPT} in first["messages"]
    tool_messages = [m for m in second["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1 and CITY in tool_messages[0]["content"]


def test_spans_carry_model_usage_and_span_kinds(default_run):
    spans = _by_name(default_run.spans)
    assert sorted(spans) == sorted([ROOT, ITERATION_0, ITERATION_1, TOOL])

    root = _attributes(spans[ROOT])
    first = _attributes(spans[ITERATION_0])
    second = _attributes(spans[ITERATION_1])
    tool = _attributes(spans[TOOL])

    # One LLM span per provider call, with model, operation and usage.
    for attributes, iteration, usage, finish in (
        (first, 0, TOOL_CALL_USAGE, "tool_calls"),
        (second, 1, ANSWER_USAGE, "stop"),
    ):
        assert attributes["gen_ai.operation.name"] == "chat"
        assert attributes["gen_ai.request.model"] == MODEL
        assert attributes["gen_ai.response.model"] == RESPONSE_MODEL
        assert attributes["gen_ai.usage.input_tokens"] == usage["prompt_tokens"]
        assert attributes["gen_ai.usage.output_tokens"] == usage["completion_tokens"]
        assert attributes["gen_ai.usage.total_tokens"] == usage["total_tokens"]
        assert attributes["gen_ai.response.finish_reasons"] == [finish]
        assert attributes["tanstack.ai.iteration"] == iteration
        assert attributes["gen_ai.span.kind"] == "LLM"

    # Dotted cache and reasoning keys appear only when the provider sends them.
    assert "gen_ai.usage.cache_read.input_tokens" not in first
    assert second["gen_ai.usage.cache_read.input_tokens"] == 3
    assert second["gen_ai.usage.reasoning.output_tokens"] == 2

    # Root: the whole chat() call. No operation name. TanStack sums every
    # gen_ai.usage.* key over calls onto the root; Future AGI sums promoted
    # gen_ai.usage.* over every span in a trace, so the recipe keeps the
    # root's sum under tanstack.ai.root_usage.<same suffix> and each model
    # call counts once.
    assert root["gen_ai.request.model"] == MODEL
    assert "gen_ai.operation.name" not in root
    assert root["tanstack.ai.iterations"] == 2
    assert not [k for k in root if k.startswith(("gen_ai.usage.", "gen_ai.cost."))], root
    per_call = {
        "input_tokens": (TOOL_CALL_USAGE["prompt_tokens"], ANSWER_USAGE["prompt_tokens"]),
        "output_tokens": (
            TOOL_CALL_USAGE["completion_tokens"], ANSWER_USAGE["completion_tokens"]
        ),
        "total_tokens": (TOOL_CALL_USAGE["total_tokens"], ANSWER_USAGE["total_tokens"]),
        "cache_read.input_tokens": (0, 3),
        "reasoning.output_tokens": (0, 2),
    }
    for suffix, calls in per_call.items():
        assert root["tanstack.ai.root_usage." + suffix] == sum(calls), suffix
        promoted = sum(
            _attributes(span).get("gen_ai.usage." + suffix, 0) for span in spans.values()
        )
        assert promoted == sum(calls), suffix
    assert root["gen_ai.response.finish_reasons"] == ["stop"]
    assert root["gen_ai.span.kind"] == "AGENT"

    assert tool["gen_ai.tool.name"] == "get_weather"
    assert tool["gen_ai.tool.call.id"] == "call_fake_1"
    assert tool["gen_ai.tool.type"] == "function"
    assert tool["tanstack.ai.tool.outcome"] == "success"
    assert tool["gen_ai.span.kind"] == "TOOL"

    # The caller's thread id is the Future AGI session, on every span.
    for name, span in spans.items():
        assert _attributes(span)["session.id"] == THREAD_ID, name

    # One trace, nested root -> iteration -> tool, without a context manager.
    assert len({span["traceId"] for span in spans.values()}) == 1
    assert spans[ITERATION_0]["parentSpanId"] == spans[ROOT]["spanId"]
    assert spans[ITERATION_1]["parentSpanId"] == spans[ROOT]["spanId"]
    assert spans[TOOL]["parentSpanId"] == spans[ITERATION_0]["spanId"]
    for span in spans.values():
        assert not _is_error(span.get("status", {})), span["name"]


def test_span_kind_unit_tests():
    """tests/span_kinds.test.mjs: futureAgiSpanKinds() against fake spans."""
    env = {name: os.environ[name] for name in ("PATH", "HOME", "SYSTEMROOT") if name in os.environ}
    result = run([NODE, "--test", str(TESTS / "span_kinds.test.mjs")], env, None, 60)
    output = result.stdout.decode() + result.stderr.decode()
    assert not result.timed_out, output
    assert result.returncode == 0, output


def test_no_prompt_or_response_content_by_default(default_run):
    assert default_run.spans
    exported = json.dumps(default_run.spans)
    for marker in (PROMPT, CITY, ANSWER, SYSTEM_PROMPT, "PROMPT-MARKER", "ANSWER-MARKER"):
        assert marker not in exported
    for span in default_run.spans:
        keys = set(_attributes(span))
        assert not keys & CONTENT_KEYS, span["name"]
        assert not [key for key in keys if key.startswith("langfuse.")], span["name"]
        assert "tanstack.ai.system_prompt.metadata" not in keys
        assert not span.get("events"), span["name"]


def test_placeholder_keys_stay_out_of_spans_and_output(default_run):
    # Positive control: the keys were in use, as the export headers and as
    # the model call's bearer token.
    assert default_run.exports
    for export in default_run.exports:
        assert export["headers"]["x-secret-key"] == SECRET_KEY
    assert default_run.model_authorizations
    for authorization in default_run.model_authorizations:
        assert authorization == "Bearer " + OPENAI_KEY
    _assert_no_placeholder_keys(default_run.spans, default_run.result)


def test_export_has_auth_headers_project_resource_and_collector_path(default_run):
    exports = default_run.exports
    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        headers = export["headers"]
        assert headers["x-api-key"] == API_KEY
        assert headers["x-secret-key"] == SECRET_KEY
        assert "authorization" not in headers
        assert export["resource_attributes"]
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"
            assert "openinference.project.name" not in resource
    assert len(default_run.spans) == 4


def test_capture_content_control_is_detected():
    """If content were captured, the checks above would see it."""
    with Receiver() as receiver, FakeOpenAI(
        CITY, ANSWER
    ) as fake:
        result = _node(TESTS / "capture_content_on.mjs", receiver.origin, fake)
        spans = receiver.spans()
    assert result.returncode == 0, result.stderr.decode()
    exported = json.dumps(spans)
    assert "PROMPT-MARKER" in exported
    assert "ANSWER-MARKER" in exported
    assert CITY in exported
    keys = set().union(*(_attributes(span) for span in spans))
    assert CONTENT_KEYS <= keys


def test_unreachable_collector_does_not_fail_chat():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    with FakeOpenAI(CITY, ANSWER) as fake:
        result = _node(
            EXAMPLE / "src" / "chat.mjs", "http://127.0.0.1:{0}".format(closed_port), fake
        )
    assert result.returncode == 0, result.stderr.decode()
    assert ANSWER in result.stdout.decode()
    assert "[futureagi] span export failed" in result.stderr.decode()
    _assert_no_placeholder_keys([], result)


@contextlib.contextmanager
def _silent_collector() -> Iterator[str]:
    """A collector that accepts connections and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            held.append(connection)

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield "http://127.0.0.1:{0}".format(server.getsockname()[1])
    finally:
        server.close()
        for connection in held:
            connection.close()


def test_silent_collector_does_not_hold_the_response():
    """The route's flush is bounded, so a collector that never answers costs
    the request at most the flush bound, not the exporter's 10 s timeout."""
    with _silent_collector() as fi_base_url, FakeOpenAI(CITY, ANSWER) as fake:
        result = _node(TESTS / "timed_route.mjs", fi_base_url, fake)
    stdout = result.stdout.decode()
    assert result.returncode == 0, result.stderr.decode()
    assert ANSWER in stdout
    route_ms = int(re.search(r"routeMs=(\d+)", stdout).group(1))
    assert route_ms < 3000, route_ms
    assert "[futureagi] span export still pending" in result.stderr.decode()
    _assert_no_placeholder_keys([], result)


def test_failed_model_call_exports_error_spans():
    """A model call that fails still ends and exports the root and LLM spans.

    chat({ stream: false }) stops reading at RUN_ERROR, the run never reaches
    onError, and otelMiddleware never ends its spans. The route drains the
    stream instead, then fails the request.
    """
    with Receiver() as receiver, FakeOpenAI(CITY, ANSWER, fail_status=500) as fake:
        result = _node(EXAMPLE / "src" / "chat.mjs", receiver.origin, fake)
        raw_spans = receiver.spans()
        spans = _by_name(raw_spans)
        exports = receiver.requests()
        model_requests = list(fake.requests)

    # The caller still gets an error, not an answer.
    assert result.returncode != 0, result.stdout.decode()
    assert "500 boom" in result.stderr.decode()
    assert ANSWER not in result.stdout.decode()
    assert model_requests

    # And the failure is in Future AGI.
    assert exports
    assert all(export["path"] == "/tracer/v1/traces" for export in exports)
    assert sorted(spans) == sorted([ROOT, ITERATION_0])
    for name in (ROOT, ITERATION_0):
        status = spans[name].get("status", {})
        assert _is_error(status), (name, status)
        assert status.get("message") == "500 boom", (name, status)
    assert spans[ITERATION_0]["parentSpanId"] == spans[ROOT]["spanId"]
    assert _attributes(spans[ITERATION_0])["gen_ai.span.kind"] == "LLM"
    # No thread id was passed, so no session (chat() generated its own id).
    for name in (ROOT, ITERATION_0):
        assert "session.id" not in _attributes(spans[name]), name
    # The provider error is printed; the credentials are not.
    _assert_no_placeholder_keys(raw_spans, result)


def test_abort_mid_stream_ends_spans_as_cancelled():
    with Receiver() as receiver, FakeOpenAI(
        CITY, ANSWER, stall=True
    ) as fake:
        result = _node(TESTS / "abort_mid_stream.mjs", receiver.origin, fake)
        spans = _by_name(receiver.spans())
    assert result.returncode == 0, result.stderr.decode()
    assert sorted(spans) == sorted([ROOT, ITERATION_0])
    for name in (ROOT, ITERATION_0):
        status = spans[name].get("status", {})
        assert _is_error(status), status
        assert status.get("message") == "cancelled"
        assert _attributes(spans[name])["tanstack.ai.completion.reason"] == "cancelled"
    # One iteration: LLM on the iteration span, no kind on the root.
    assert _attributes(spans[ITERATION_0])["gen_ai.span.kind"] == "LLM"
    assert "gen_ai.span.kind" not in _attributes(spans[ROOT])


def _readme_recipe() -> str:
    readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    section = readme.split("\n## The recipe\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```js\n(.*?)```", section, re.S)
    assert len(blocks) == 1, blocks
    return blocks[0]


def test_readme_recipe_uses_the_recipe_middleware_and_bounded_flush():
    code = _readme_recipe()
    # Bare otelMiddleware has no span kinds and no root usage move, so Future
    # AGI would count every call twice. An unguarded forceFlush() rejects
    # when the collector is down and replaces the route's answer.
    assert "futureAgiOtelMiddleware(" in code
    assert "flushTraces(" in code
    assert "otelMiddleware({" not in code
    assert "forceFlush" not in code
    assert "stream: false" not in code
    imported = re.search(r"import \{([^}]*)\} from \"\./tracing\.mjs\"", code)
    assert imported, code
    tracing = (EXAMPLE / "src" / "tracing.mjs").read_text(encoding="utf-8")
    for name in (n.strip() for n in imported.group(1).split(",") if n.strip()):
        assert re.search(r"export (async )?(function|const) {0}\b".format(name), tracing), name


def test_readme_recipe_runs_and_exports_the_contract_spans():
    """Run the README snippet itself, saved next to src/tracing.mjs."""
    script = EXAMPLE / "src" / ".readme-recipe-{0}.mjs".format(uuid.uuid4().hex)
    script.write_text(
        _readme_recipe()
        + "\nconsole.log(await chatRoute(process.argv[2], { threadId: process.argv[3] }));\n",
        encoding="utf-8",
    )
    try:
        with Receiver() as receiver, FakeOpenAI(CITY, ANSWER) as fake:
            result = _node(script, receiver.origin, fake, THREAD_ID)
            raw_spans = receiver.spans()
            spans = _by_name(raw_spans)
    finally:
        script.unlink()
    assert result.returncode == 0, result.stderr.decode()
    assert ANSWER in result.stdout.decode()
    # The snippet offers no tools, so the fake model answers in one call.
    assert sorted(spans) == sorted([ROOT, ITERATION_0])
    root = _attributes(spans[ROOT])
    first = _attributes(spans[ITERATION_0])
    assert "gen_ai.span.kind" not in root
    assert first["gen_ai.span.kind"] == "LLM"
    assert not [k for k in root if k.startswith(("gen_ai.usage.", "gen_ai.cost."))], root
    assert root["tanstack.ai.root_usage.input_tokens"] == ANSWER_USAGE["prompt_tokens"]
    assert first["gen_ai.usage.input_tokens"] == ANSWER_USAGE["prompt_tokens"]
    for name, span in spans.items():
        assert _attributes(span)["session.id"] == THREAD_ID, name
    assert PROMPT not in json.dumps(list(spans.values()))
    _assert_no_placeholder_keys(raw_spans, result)
