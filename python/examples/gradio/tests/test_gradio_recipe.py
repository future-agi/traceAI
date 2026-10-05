"""Tests for the Gradio tracing recipe (``src/app.py``).

Two parts, no browser and no Gradio server port:

- In-process: the recipe's ``predict`` is called directly, and through
  Gradio 6.29.1's own event dispatch (``Blocks.call_function`` on the
  ChatInterface submit event, which runs a sync ``predict`` in an anyio
  worker thread). ``register`` is swapped for a provider with an
  ``InMemorySpanExporter``; the recipe's own ``init_tracing`` still installs
  the instrumentor with its ``TraceConfig``.
- Contract: ``tests/drive_turns.py`` runs the recipe with the real
  ``register()`` in a subprocess whose network is limited to 127.0.0.1
  (``_guarded_run.py``). The model is a loopback fake of the OpenAI Chat
  Completions API; spans go to the shared harness ``Receiver``, which serves
  ``/v1/traces`` and ``/tracer/v1/traces`` like fi-collector's HTTP mux but
  does not authenticate, stamp projects or store anything.

All keys are placeholders. Nothing here contacts OpenAI, Gradio's analytics
host or Future AGI.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import gradio as gr
import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer
from harness import Receiver, run
from traceai_openai import OpenAIInstrumentor

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
PYTHON_DIR = RECIPE_DIR.parents[1]
APP = RECIPE_DIR / "src" / "app.py"
README = RECIPE_DIR / "README.md"
DRIVER = TESTS_DIR / "drive_turns.py"
GUARD = TESTS_DIR / "_guarded_run.py"
GUARD_PROBE = TESTS_DIR / "_guard_probe.py"
ANALYTICS_PROBE = TESTS_DIR / "_analytics_probe.py"

sys.path.insert(0, str(TESTS_DIR))
from _fake_openai import RESPONSE_MODEL, USAGE, FakeOpenAI  # noqa: E402

PROJECT = "gradio-recipe-contract"
FI_API_KEY = "fi-api-placeholder-0000"
FI_SECRET_KEY = "fi-secret-placeholder-0000"
OPENAI_KEY = "sk-openai-placeholder-0000"
DRIVER_SESSION = "drive-session-1"  # the session hash drive_turns.py uses

# Content markers: the user's message and the fake model's answer.
QUESTION = "QMARK3f9a what is the refund window?"
ANSWER = "AMARK8b1c refunds are accepted for 30 days."
CONTENT_MARKERS = ("QMARK3f9a", "AMARK8b1c")
CONTENT_KEY_PREFIXES = ("gen_ai.input.messages", "gen_ai.output.messages")
REDACTED = "__REDACTED__"

LLM_SPAN = "ChatCompletion"
SESSION_KEYS = ("session.id", "gen_ai.conversation.id")
# What the recipe exports instead of a session hash: a 128-bit hex digest.
SESSION_ID = re.compile(r"[0-9a-f]{32}")
# A hang guard, not a speed check: one run imports Gradio, openai and traceAI.
RUN_TIMEOUT_SECONDS = 180


# --------------------------------------------------------------------------
# In-process: predict() with an in-memory exporter
# --------------------------------------------------------------------------


class _ThreadRecorder(SpanProcessor):
    """Remember which thread started each span."""

    def __init__(self) -> None:
        self.threads: dict[int, int] = {}

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        self.threads[span.context.span_id] = threading.get_ident()


@pytest.fixture(scope="module")
def recipe() -> Any:
    exporter = InMemorySpanExporter()
    threads = _ThreadRecorder()
    provider = TracerProvider()
    provider.add_span_processor(threads)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Global as well, so a span made through the global OpenTelemetry API by
    # anything else in the process (Gradio included) would be exported too.
    trace_api.set_tracer_provider(provider)
    assert trace_api.get_tracer_provider() is provider

    with FakeOpenAI(ANSWER) as fake, pytest.MonkeyPatch.context() as patch:
        patch.setenv("OPENAI_API_KEY", OPENAI_KEY)
        patch.setenv("OPENAI_BASE_URL", fake.base_url)
        patch.setenv("FI_PROJECT_NAME", PROJECT)
        spec = importlib.util.spec_from_file_location("gradio_recipe_app", APP)
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        register_calls: list[dict[str, Any]] = []

        def in_memory_register(**kwargs: Any) -> TracerProvider:
            register_calls.append(kwargs)
            return provider

        patch.setattr(app, "register", in_memory_register)
        app.init_tracing()
        yield SimpleNamespace(
            app=app,
            exporter=exporter,
            threads=threads.threads,
            fake=fake,
            provider=provider,
            register_calls=register_calls,
            tracer=FITracer(provider.get_tracer("gradio-recipe-test")),
        )
        OpenAIInstrumentor().uninstrument()


@pytest.fixture(autouse=True)
def _fresh(request: pytest.FixtureRequest) -> None:
    if "recipe" in request.fixturenames:
        state = request.getfixturevalue("recipe")
        state.exporter.clear()
        state.fake.requests.clear()
        state.fake.authorizations.clear()


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


def _session_digest(app: Any, session_hash: str) -> str:
    """The session id the recipe should export for a Gradio session hash."""
    digest = hmac.new(app._SESSION_KEY, session_hash.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def _submit_event(demo: gr.Blocks) -> Any:
    """The ChatInterface's textbox-submit event, which calls predict."""
    (event,) = [fn for fn in demo.fns.values() if fn.api_name == "_submit_fn"]
    return event


async def _gradio_turn(
    demo: gr.Blocks, message: str, history: list, session_hash: str
) -> dict[str, Any]:
    """One turn through Gradio's own dispatch, as the queue would run it."""
    return await demo.call_function(
        _submit_event(demo),
        [message, history],
        requests=gr.Request(session_hash=session_hash),
    )


def test_each_turn_exports_exactly_one_llm_span(recipe: Any) -> None:
    session = gr.Request(session_hash="tab-1")
    for turn in range(1, 4):
        assert recipe.app.predict(QUESTION, [], session) == ANSWER
        assert len(recipe.exporter.get_finished_spans()) == turn
    spans = recipe.exporter.get_finished_spans()
    assert len(recipe.fake.requests) == 3
    for span in spans:
        assert span.name == LLM_SPAN
        assert _attrs(span)["gen_ai.span.kind"] == "LLM"
        assert span.parent is None
    # With no span of the app's own around it, each turn is its own trace.
    assert len({span.context.trace_id for span in spans}) == 3


def test_the_session_id_is_a_keyed_digest_of_the_session_hash(recipe: Any) -> None:
    """Gradio's session hash is the key to that session's live state: not exported."""
    recipe.app.predict(QUESTION, [], gr.Request(session_hash="tab-1"))
    (span,) = recipe.exporter.get_finished_spans()
    attrs = _attrs(span)
    # A per-process 32-byte key, so the digest cannot be recomputed from a
    # guessed session hash without it.
    assert isinstance(recipe.app._SESSION_KEY, bytes)
    assert len(recipe.app._SESSION_KEY) == 32
    for key in SESSION_KEYS:
        assert attrs[key] == _session_digest(recipe.app, "tab-1")
        assert SESSION_ID.fullmatch(attrs[key])
        assert attrs[key] != "tab-1"
        assert attrs[key] != hashlib.sha256(b"tab-1").hexdigest()[:32]
    assert "tab-1" not in json.dumps(attrs)


def test_two_turns_in_one_gradio_session_share_its_session_id(recipe: Any) -> None:
    session = gr.Request(session_hash="tab-1")
    recipe.app.predict(QUESTION, [], session)
    recipe.app.predict(QUESTION, [], session)
    recipe.app.predict(QUESTION, [], gr.Request(session_hash="tab-2"))
    first, second, other = (_attrs(span) for span in recipe.exporter.get_finished_spans())
    for key in SESSION_KEYS:
        assert first[key] == second[key] == _session_digest(recipe.app, "tab-1")
        assert other[key] == _session_digest(recipe.app, "tab-2")
        assert first[key] != other[key]
    for attrs in (first, second, other):
        dump = json.dumps(attrs)
        assert "tab-1" not in dump
        assert "tab-2" not in dump


def test_turns_without_a_gradio_session_carry_no_session_id(recipe: Any) -> None:
    recipe.app.predict(QUESTION, [])
    recipe.app.predict(QUESTION, [], None)
    recipe.app.predict(QUESTION, [], gr.Request())  # a request without a session hash
    spans = recipe.exporter.get_finished_spans()
    assert len(spans) == 3
    for span in spans:
        assert not set(SESSION_KEYS) & set(_attrs(span)), span.name


def test_gradio_is_not_the_span_source(recipe: Any) -> None:
    """With the OpenAI instrumentor removed, a full Gradio turn exports nothing."""
    assert trace_api.get_tracer_provider() is recipe.provider
    OpenAIInstrumentor().uninstrument()
    try:
        recipe.app.predict(QUESTION, [], gr.Request(session_hash="tab-1"))
        asyncio.run(_gradio_turn(recipe.app.demo, QUESTION, [], "tab-1"))
        asyncio.run(_gradio_turn(recipe.app.demo, QUESTION, [], "tab-1"))
        assert len(recipe.fake.requests) == 3  # the model was called each time
        assert recipe.exporter.get_finished_spans() == ()
    finally:
        recipe.app.init_tracing()
    # Control: the same exporter receives the span once the instrumentor is back.
    recipe.app.predict(QUESTION, [], gr.Request(session_hash="tab-1"))
    assert [span.name for span in recipe.exporter.get_finished_spans()] == [LLM_SPAN]


def test_gradio_dispatch_runs_predict_on_a_worker_thread_with_the_session(
    recipe: Any,
) -> None:
    async def turn() -> tuple[int, dict[str, Any]]:
        return threading.get_ident(), await _gradio_turn(recipe.app.demo, QUESTION, [], "tab-9")

    loop_thread, result = asyncio.run(turn())
    response, _history = result["prediction"]
    assert response == ANSWER
    (span,) = recipe.exporter.get_finished_spans()
    # Gradio ran the sync predict off the event loop's thread ...
    assert recipe.threads[span.context.span_id] != loop_thread
    # ... and injected gr.Request, whose session hash reached the span as a digest.
    assert _attrs(span)["session.id"] == _session_digest(recipe.app, "tab-9")


def test_gradio_history_reaches_the_model_as_plain_text(recipe: Any) -> None:
    """Gradio 6 hands predict each history message as a list of content parts."""
    demo = recipe.app.demo
    first = asyncio.run(_gradio_turn(demo, "first question", [], "tab-1"))
    _response, history = first["prediction"]
    # What the browser sends back on the next turn: postprocess, then preprocess.
    history = demo.chatbot.preprocess(demo.chatbot.postprocess(history))
    assert history[0]["content"] == [{"type": "text", "text": "first question"}]
    asyncio.run(_gradio_turn(demo, "second question", history, "tab-1"))
    assert recipe.fake.requests[1]["messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "second question"},
    ]
    assert len(recipe.exporter.get_finished_spans()) == 2


# --------------------------------------------------------------------------
# In-process: does the LLM span stay under a turn span across Gradio's
# thread pool?
# --------------------------------------------------------------------------


def test_turn_span_opened_before_gradio_dispatch_parents_the_llm_span(recipe: Any) -> None:
    async def turn() -> int:
        with recipe.tracer.start_as_current_span("chat_turn", fi_span_kind="chain"):
            await _gradio_turn(recipe.app.demo, QUESTION, [], "tab-1")
        return threading.get_ident()

    loop_thread = asyncio.run(turn())
    spans = {span.name: span for span in recipe.exporter.get_finished_spans()}
    assert set(spans) == {"chat_turn", LLM_SPAN}
    turn_span, llm = spans["chat_turn"], spans[LLM_SPAN]
    assert recipe.threads[turn_span.context.span_id] == loop_thread
    assert recipe.threads[llm.context.span_id] != loop_thread
    assert llm.parent.span_id == turn_span.context.span_id
    assert llm.context.trace_id == turn_span.context.trace_id


def test_turn_span_inside_predict_parents_the_llm_span(recipe: Any) -> None:
    tracer = recipe.tracer

    def traced_predict(message: str, history: list, request: gr.Request = None) -> str:
        with tracer.start_as_current_span("chat_turn", fi_span_kind="chain"):
            return recipe.app.predict(message, history, request)

    demo = gr.ChatInterface(traced_predict, analytics_enabled=False)

    async def turn() -> int:
        await _gradio_turn(demo, QUESTION, [], "tab-1")
        return threading.get_ident()

    loop_thread = asyncio.run(turn())
    spans = {span.name: span for span in recipe.exporter.get_finished_spans()}
    turn_span, llm = spans["chat_turn"], spans[LLM_SPAN]
    assert _attrs(turn_span)["gen_ai.span.kind"] == "CHAIN"
    assert recipe.threads[llm.context.span_id] != loop_thread
    assert llm.parent.span_id == turn_span.context.span_id
    assert _attrs(llm)["session.id"] == _session_digest(recipe.app, "tab-1")


def test_sync_generator_turn_span_detaches_after_the_first_yield(
    recipe: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Known limit: Gradio steps a sync generator in a fresh worker call per yield."""
    tracer = recipe.tracer

    def streaming_predict(message: str, history: list, request: gr.Request = None):
        with tracer.start_as_current_span("chat_turn", fi_span_kind="chain"):
            yield recipe.app.predict(message, history, request)
            yield recipe.app.predict(message, history, request)

    demo = gr.ChatInterface(streaming_predict, analytics_enabled=False)

    async def turn() -> None:
        result = await _gradio_turn(demo, QUESTION, [], "tab-1")
        while result["is_generating"]:
            result = await demo.call_function(
                _submit_event(demo),
                [QUESTION, []],
                iterator=result["iterator"],
                requests=gr.Request(session_hash="tab-1"),
            )

    with caplog.at_level(logging.ERROR, logger="opentelemetry.context"):
        asyncio.run(turn())
    spans = recipe.exporter.get_finished_spans()
    (turn_span,) = [span for span in spans if span.name == "chat_turn"]
    first, second = [span for span in spans if span.name == LLM_SPAN]
    # Started before the first yield: a child of the turn span.
    assert first.parent.span_id == turn_span.context.span_id
    # Started after it: a root span in a separate trace.
    assert second.parent is None
    assert second.context.trace_id != turn_span.context.trace_id
    # Closing the turn span in a later step cannot restore the context.
    assert "Failed to detach context" in caplog.text


# --------------------------------------------------------------------------
# Contract: the recipe through register() into the harness Receiver
# --------------------------------------------------------------------------


def _base_env(home: Path, guard_log: Path) -> dict[str, str]:
    """Built from scratch: nothing inherited but PATH, no .pyc left in the repo."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        # This repository's register() and OpenAI instrumentor.
        "PYTHONPATH": os.pathsep.join(
            [str(PYTHON_DIR), str(PYTHON_DIR / "frameworks" / "openai")]
        ),
        "LOOPBACK_GUARD_LOG": str(guard_log),
    }


def _child_env(home: Path, guard_log: Path, **overrides: Optional[str]) -> dict[str, str]:
    """The recipe's environment."""
    env = {
        **_base_env(home, guard_log),
        "FI_API_KEY": FI_API_KEY,
        "FI_SECRET_KEY": FI_SECRET_KEY,
        "FI_PROJECT_NAME": PROJECT,
        "OPENAI_API_KEY": OPENAI_KEY,
        # Longer than any run, so only register()'s exit flush can export.
        "OTEL_BSP_SCHEDULE_DELAY": "600000",
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _guard_attempts(guard_log: Path) -> list[dict[str, Any]]:
    if not guard_log.exists():
        return []
    return [json.loads(line) for line in guard_log.read_text().splitlines()]


def _run_driver(tmp_path: Path, *flags: str) -> dict[str, Any]:
    """Run drive_turns.py under the loopback guard; return everything the tests read."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard_log = tmp_path / "guard.jsonl"
    with Receiver() as receiver, FakeOpenAI(ANSWER) as fake:
        env = _child_env(
            tmp_path, guard_log, FI_BASE_URL=receiver.origin, OPENAI_BASE_URL=fake.base_url
        )
        result = run(
            [sys.executable, str(GUARD), str(DRIVER), QUESTION, *flags],
            env=env,
            stdin=None,
            timeout=RUN_TIMEOUT_SECONDS,
        )
        record = {
            "result": result,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace"),
            "requests": receiver.requests(),
            "spans": receiver.spans(),
            "model_requests": list(fake.requests),
            "model_authorizations": list(fake.authorizations),
        }
    record["guard_attempts"] = _guard_attempts(guard_log)
    return record


def _assert_ran(record: dict[str, Any]) -> None:
    result = record["result"]
    assert not result.timed_out, record["stderr"]
    assert result.returncode == 0, record["stderr"]
    assert record["guard_attempts"] == [], record["guard_attempts"]
    assert record["requests"], "no export reached the receiver"
    assert record["spans"], "the exports carried no spans"


def _report(record: dict[str, Any]) -> dict[str, Any]:
    lines = [line for line in record["stdout"].splitlines() if line.startswith("{")]
    return json.loads(lines[-1])


def _value(value: dict[str, Any]) -> Any:
    for kind in ("stringValue", "boolValue", "doubleValue"):
        if kind in value:
            return value[kind]
    if "intValue" in value:
        return int(value["intValue"])
    return value


def _otlp_attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: _value(item.get("value", {})) for item in span.get("attributes", [])}


def _export_dump(record: dict[str, Any]) -> str:
    """Everything the export carried except HTTP headers: spans and resources."""
    return json.dumps(
        {
            "spans": record["spans"],
            "resources": [r["resource_attributes"] for r in record["requests"]],
        },
        sort_keys=True,
    )


@pytest.fixture(scope="module")
def recipe_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = _run_driver(tmp_path_factory.mktemp("recipe"))
    _assert_ran(record)
    return record


@pytest.fixture(scope="module")
def content_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The same two turns with init_tracing(trace_content=True), in another process."""
    record = _run_driver(tmp_path_factory.mktemp("content"), "--content")
    _assert_ran(record)
    return record


def test_recipe_runs_with_loopback_network_only(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["guard_attempts"] == []
    assert _report(recipe_run)["turns"] == 2
    # Both turns reached the fake, with the OpenAI key the client was given.
    assert len(recipe_run["model_requests"]) == 2
    assert recipe_run["model_authorizations"] == ["Bearer " + OPENAI_KEY] * 2


def test_guard_refuses_and_logs_non_loopback_connections(tmp_path: Path) -> None:
    """Positive control for every ``guard_attempts == []`` assertion in this file."""
    guard_log = tmp_path / "guard.jsonl"
    result = run(
        [sys.executable, str(GUARD), str(GUARD_PROBE)],
        env=_base_env(tmp_path, guard_log),
        stdin=None,
        timeout=60,
    )
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "refused: connect,connect_ex,getaddrinfo" in result.stdout.decode("utf-8", "replace")
    logged = _guard_attempts(guard_log)
    assert [entry["kind"] for entry in logged] == ["connect", "connect_ex", "getaddrinfo"]
    assert "192.0.2.1" in logged[0]["target"]
    assert "2001:db8::1" in logged[1]["target"]
    assert "example.invalid" in logged[2]["target"]


def test_export_goes_to_the_collector_path_with_both_keys(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    for request in recipe_run["requests"]:
        headers = request["headers"]
        assert request["path"] == "/tracer/v1/traces"
        assert headers.get("x-api-key") == FI_API_KEY
        assert headers.get("x-secret-key") == FI_SECRET_KEY
        assert headers.get("content-type") == "application/x-protobuf"
        assert "authorization" not in headers
        assert all(OPENAI_KEY not in value for value in headers.values())


def test_resource_is_an_observe_project(recipe_run: dict[str, Any]) -> None:
    resources = [
        resource for request in recipe_run["requests"] for resource in request["resource_attributes"]
    ]
    assert resources, "export without a resource"
    for resource in resources:
        assert resource.get("project_name") == PROJECT
        assert resource.get("project_type") == "observe"


def test_the_batch_leaves_through_the_exit_flush(recipe_run: dict[str, Any]) -> None:
    # The batch delay is 10 minutes, so the one export is register()'s flush
    # at interpreter exit.
    assert len(recipe_run["requests"]) == 1
    assert len(recipe_run["spans"]) == 2


def test_one_llm_span_per_turn_with_the_session(recipe_run: dict[str, Any]) -> None:
    spans = recipe_run["spans"]
    assert len(spans) == len(recipe_run["model_requests"]) == 2
    assert len({span["traceId"] for span in spans}) == 2
    for span in spans:
        attrs = _otlp_attrs(span)
        assert span["name"] == LLM_SPAN
        assert not span.get("parentSpanId")
        assert span["status"].get("code") == "STATUS_CODE_OK"
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.provider.name"] == "openai"
        # traceai_openai records the model the response names.
        assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
        assert attrs["gen_ai.usage.input_tokens"] == USAGE["prompt_tokens"]
        assert attrs["gen_ai.usage.output_tokens"] == USAGE["completion_tokens"]
        assert attrs["gen_ai.usage.total_tokens"] == USAGE["total_tokens"]
    # Both turns of the one Gradio session carry one id, on both keys, and it
    # is a digest, not the session hash.
    (session_id,) = {_otlp_attrs(span)[key] for span in spans for key in SESSION_KEYS}
    assert SESSION_ID.fullmatch(session_id)
    assert session_id != DRIVER_SESSION


def test_the_raw_session_hash_is_not_exported(recipe_run: dict[str, Any]) -> None:
    assert DRIVER_SESSION not in _export_dump(recipe_run)
    assert DRIVER_SESSION not in recipe_run["stdout"] + recipe_run["stderr"]


def test_each_process_has_its_own_session_key(
    recipe_run: dict[str, Any], content_run: dict[str, Any]
) -> None:
    """Two runs, one session hash: two ids, so the id cannot be precomputed."""
    ids = [
        {_otlp_attrs(span)["session.id"] for span in record["spans"]}
        for record in (recipe_run, content_run)
    ]
    assert all(len(run_ids) == 1 for run_ids in ids), ids
    assert ids[0] != ids[1]


def test_content_is_off_by_default(recipe_run: dict[str, Any]) -> None:
    dump = _export_dump(recipe_run)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker
    for span in recipe_run["spans"]:
        attrs = _otlp_attrs(span)
        assert not [k for k in attrs if k.startswith(CONTENT_KEY_PREFIXES)], span["name"]
        assert attrs["input.value"] == REDACTED
        assert attrs["output.value"] == REDACTED
        assert "input.mime_type" not in attrs
        assert not span.get("events")


def test_no_key_in_the_export_or_output(recipe_run: dict[str, Any]) -> None:
    dump = _export_dump(recipe_run)
    output = recipe_run["stdout"] + recipe_run["stderr"]
    for secret in (FI_API_KEY, FI_SECRET_KEY, OPENAI_KEY):
        assert secret not in dump
        assert secret not in output


def test_the_prompt_is_never_printed(recipe_run: dict[str, Any]) -> None:
    output = recipe_run["stdout"] + recipe_run["stderr"]
    for marker in CONTENT_MARKERS:
        assert marker not in output


def test_content_is_exported_when_the_recipe_turns_it_on(content_run: dict[str, Any]) -> None:
    """Control for the content-off test: init_tracing(trace_content=True)."""
    record = content_run
    first, second = (_otlp_attrs(span) for span in record["spans"])
    assert first["gen_ai.input.messages.0.message.content"] == QUESTION
    assert first["gen_ai.output.messages.0.message.content"] == ANSWER
    assert first["output.value"] == ANSWER
    assert second["gen_ai.input.messages.1.message.content"] == ANSWER
    assert second["gen_ai.input.messages.2.message.content"] == QUESTION
    # input.value holds the first message of the request, for every turn.
    assert first["input.value"] == second["input.value"] == QUESTION
    dump = _export_dump(record)
    for secret in (FI_API_KEY, FI_SECRET_KEY, OPENAI_KEY):
        assert secret not in dump
    for marker in CONTENT_MARKERS:
        assert marker not in record["stdout"] + record["stderr"]


def test_reload_keeps_the_first_provider_and_ignores_tracing_edits(tmp_path: Path) -> None:
    """Gradio reload mode, replayed: nothing is dropped, and edits do not apply."""
    record = _run_driver(tmp_path, "--reload")
    _assert_ran(record)
    # Both turns, before and after the reload, were exported at exit ...
    assert len(record["spans"]) == 2
    # ... by the provider the first init_tracing() created.
    first_provider = _report(record)["first_provider"]
    for request in record["requests"]:
        for resource in request["resource_attributes"]:
            assert resource["project_version_id"] == first_provider
    # init_tracing() ran on the watcher thread twice, at startup and after the
    # save. Each run's register() made another provider, which exported
    # nothing, and each run logged two warnings.
    stderr = record["stderr"]
    assert stderr.count("Attempting to instrument while already instrumented") == 2
    assert stderr.count("Failed to register signal handlers") == 2
    # The reloaded source turned content on; the instrumentor ignored it.
    for marker in CONTENT_MARKERS:
        assert marker not in _export_dump(record)
    # The session key survived both runs of the file: one id for both turns.
    first, second = (_otlp_attrs(span) for span in record["spans"])
    assert first["session.id"] == second["session.id"]


def test_building_a_gradio_app_contacts_gradio_analytics_unless_turned_off(
    tmp_path: Path,
) -> None:
    """Why src/app.py passes analytics_enabled=False (the recipe run logs nothing)."""
    guard_log = tmp_path / "guard.jsonl"
    result = run(
        [sys.executable, str(GUARD), str(ANALYTICS_PROBE)],
        env=_base_env(tmp_path, guard_log),  # GRADIO_ANALYTICS_ENABLED unset
        stdin=None,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    targets = {entry["target"] for entry in _guard_attempts(guard_log)}
    assert "'api.gradio.app'" in targets


def test_readme_shows_the_recipe_as_written() -> None:
    readme = README.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for line in (
        "project_type=ProjectType.OBSERVE,",
        "config = TraceConfig(hide_inputs=True, hide_outputs=True)",
        "OpenAIInstrumentor().instrument(tracer_provider=provider, config=config)",
        '_SESSION_KEY = globals().get("_SESSION_KEY") or secrets.token_bytes(32)',
        'digest = hmac.new(_SESSION_KEY, request.session_hash.encode("utf-8"), hashlib.sha256)',
        "session_id = _session_id(request)",
        "with using_session(session_id) if session_id else nullcontext():",
        "demo = gr.ChatInterface(predict, analytics_enabled=False)",
    ):
        assert line in app, line
        assert line in readme, line
    for fact in ("gradio==6.29.1", "/tracer/v1/traces", "project_type=observe"):
        assert fact in readme, fact

