"""Unit tests for setup() against real AG2 with its scripted TestConfig model.

Covers AC-04 (traceparent relay, without the network hub), AC-05, AC-06,
AC-07 and the "does not wrap Agent" rule.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest
from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry.trace import StatusCode

import traceai_ag2
from traceai_ag2 import AG2SpanProcessor, create_telemetry_middleware, setup

from ._support import (
    FINAL_ANSWER,
    MODEL,
    PROVIDER,
    TOOL_CALL_ID,
    TOOL_RESULT,
    USER_PROMPT,
    ask,
    assert_no_content,
    attrs,
    expected_one_tool_kinds,
    fake_register,
    find,
    get_weather,
    memory_provider,
    weather_agent,
    weather_config,
)


def _telemetry_middlewares(agent):
    from ag2.middleware.builtin.telemetry import TelemetryMiddleware

    try:
        entries = [e.middleware for e in agent.middleware]  # ag2 >= 1.0.2
    except AttributeError:
        entries = list(agent._middleware)  # ag2 1.0.0 / 1.0.1
    return [m for m in entries if isinstance(m, TelemetryMiddleware)]


# --- upstream drift guards -------------------------------------------------


def test_scope_and_strings_match_installed_ag2():
    from ag2 import _telemetry_consts
    from ag2.middleware.builtin import telemetry

    assert traceai_ag2.AG2_INSTRUMENTATION_SCOPE == _telemetry_consts.OTEL_INSTRUMENTING_MODULE
    source = inspect.getsource(telemetry)
    for operation in ("invoke_agent", "chat", "execute_tool", "await_human_input"):
        assert f'"gen_ai.operation.name", "{operation}"' in source
    for key in traceai_ag2.USAGE_KEY_ALIASES:
        assert f'"{key}"' in source
    params = inspect.signature(telemetry.TelemetryMiddleware.__init__).parameters
    assert params["capture_content"].default is True  # upstream default the helper overrides


# --- AC-05 privacy default ---------------------------------------------------


def test_setup_defaults_capture_content_off_and_spans_have_no_content():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider)

    (mw,) = _telemetry_middlewares(agent)
    assert mw._capture_content is False

    reply = ask(agent)
    assert reply.body == FINAL_ANSWER
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        assert_no_content(attrs(span), span.name)


def test_create_telemetry_middleware_defaults_off():
    mw = create_telemetry_middleware()
    assert mw._capture_content is False


def test_capture_content_opt_in_forwards_true():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider, capture_content=True, config=TraceConfig())
    ask(agent)
    spans = exporter.get_finished_spans()
    (tool,) = find(spans, "execute_tool")
    assert "Paris" in attrs(tool)["gen_ai.tool.call.arguments"]
    assert attrs(tool)["gen_ai.tool.call.result"] == TOOL_RESULT
    (final_chat,) = find(spans, f"chat {MODEL}")
    assert USER_PROMPT in attrs(final_chat)["gen_ai.input.messages"]
    assert FINAL_ANSWER in attrs(final_chat)["gen_ai.output.messages"]


def test_trace_config_is_a_second_gate_when_content_is_on():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(
        agent,
        tracer_provider=provider,
        capture_content=True,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    ask(agent)
    for span in exporter.get_finished_spans():
        assert_no_content(attrs(span), span.name)


def test_later_setup_config_replaces_installed_processor_config(caplog):
    """README path: setup(tracer_provider=...) first, then setup(agent, config=...).
    The most recent explicit config wins and the change is logged."""
    provider, exporter = memory_provider()
    setup(tracer_provider=provider)
    agent = weather_agent()
    with caplog.at_level(logging.WARNING, logger="traceai_ag2"):
        setup(
            agent,
            tracer_provider=provider,
            capture_content=True,
            config=TraceConfig(hide_inputs=True, hide_outputs=True),
        )
    assert any("TraceConfig" in r.getMessage() for r in caplog.records), caplog.text
    chain = provider._active_span_processor._span_processors
    assert sum(isinstance(p, AG2SpanProcessor) for p in chain) == 1
    ask(agent)
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        assert_no_content(attrs(span), span.name)


def test_later_setup_without_config_keeps_installed_config(caplog):
    """config=None means "no change": a later setup() never re-exposes content."""
    provider, exporter = memory_provider()
    setup(tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    agent = weather_agent()
    with caplog.at_level(logging.WARNING, logger="traceai_ag2"):
        setup(agent, tracer_provider=provider, capture_content=True)
        setup(agent, tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    assert not [r for r in caplog.records if "TraceConfig" in r.getMessage()], caplog.text
    ask(agent)
    for span in exporter.get_finished_spans():
        assert_no_content(attrs(span), span.name)


# --- span shape, kinds, model, usage ----------------------------------------


def test_text_hide_flags_remove_ag2_message_json():
    """hide_input_text / hide_output_text act on AG2's real JSON-string content."""
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(
        agent,
        tracer_provider=provider,
        capture_content=True,
        config=TraceConfig(hide_input_text=True, hide_output_text=True),
    )
    ask(agent)
    spans = exporter.get_finished_spans()
    assert find(spans, "chat")
    for span in spans:
        assert_no_content(attrs(span), span.name)


def test_hide_input_text_keeps_outputs():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider, capture_content=True, config=TraceConfig(hide_input_text=True))
    ask(agent)
    spans = exporter.get_finished_spans()
    (final_chat,) = find(spans, f"chat {MODEL}")
    assert "gen_ai.input.messages" not in attrs(final_chat)
    assert FINAL_ANSWER in attrs(final_chat)["gen_ai.output.messages"]
    (tool,) = find(spans, "execute_tool")
    assert "gen_ai.tool.call.arguments" not in attrs(tool)
    assert attrs(tool)["gen_ai.tool.call.result"] == TOOL_RESULT


def test_ag2_never_records_image_bytes():
    """Why hide_input_images has nothing to act on: AG2 serialises only text
    parts into gen_ai.input.messages, even with capture_content=True."""
    from ag2 import Agent
    from ag2.events import BinaryInput, TextInput
    from ag2.testing import TestConfig

    try:
        from ag2.events import BinaryType

        image = BinaryInput(b"\x89PNG-SECRET-PIXELS", type=BinaryType.IMAGE, media_type="image/png")
    except Exception as exc:  # pragma: no cover - older BinaryInput signature
        pytest.skip(f"cannot build an image BinaryInput on this ag2: {exc!r}")
    provider, exporter = memory_provider()
    agent = Agent("img_bot", config=TestConfig("seen"))
    setup(agent, tracer_provider=provider, capture_content=True)

    async def _run():
        return await agent.ask(TextInput("describe"), image)

    asyncio.run(_run())
    (chat,) = find(exporter.get_finished_spans(), "chat")
    assert "describe" in attrs(chat)["gen_ai.input.messages"]
    for span in exporter.get_finished_spans():
        for value in attrs(span).values():
            assert "SECRET-PIXELS" not in str(value), span.name


def test_one_tool_run_span_shape():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider)
    ask(agent)
    spans = exporter.get_finished_spans()
    kinds = {s.name: attrs(s).get("gen_ai.span.kind") for s in spans}
    assert kinds == expected_one_tool_kinds()

    (tool,) = find(spans, "execute_tool")
    assert attrs(tool)["gen_ai.tool.name"] == "get_weather"
    assert attrs(tool)["gen_ai.tool.call.id"] == TOOL_CALL_ID

    (final_chat,) = find(spans, f"chat {MODEL}")
    a = attrs(final_chat)
    assert a["gen_ai.request.model"] == MODEL
    assert a["gen_ai.response.model"] == MODEL
    assert a["gen_ai.provider.name"] == PROVIDER
    assert a["gen_ai.usage.input_tokens"] == 11
    assert a["gen_ai.usage.output_tokens"] == 7
    assert a["gen_ai.usage.cache_creation_input_tokens"] == 2
    assert a["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert a["gen_ai.usage.cache_read_input_tokens"] == 3
    assert a["gen_ai.usage.cache_read.input_tokens"] == 3
    assert a["gen_ai.usage.thinking_tokens"] == 5
    assert a["gen_ai.usage.reasoning.output_tokens"] == 5

    root = [s for s in spans if s.parent is None]
    assert [s.name for s in root] == ["invoke_agent weather_bot"]
    assert len({s.context.trace_id for s in spans}) == 1


def test_provider_name_is_not_invented():
    provider, exporter = memory_provider()
    agent = weather_agent(with_provider=False)
    setup(agent, tracer_provider=provider)
    ask(agent)
    for span in exporter.get_finished_spans():
        assert "gen_ai.provider.name" not in attrs(span), span.name


def test_provider_name_is_forwarded_when_given():
    provider, exporter = memory_provider()
    agent = weather_agent(with_provider=False)
    setup(agent, tracer_provider=provider, provider_name="openai")
    ask(agent)
    (root,) = find(exporter.get_finished_spans(), "invoke_agent")
    assert attrs(root)["gen_ai.provider.name"] == "openai"


# --- AC-07 session -------------------------------------------------------------


def test_session_is_absent_unless_app_sets_it():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider)
    ask(agent)
    for span in exporter.get_finished_spans():
        a = attrs(span)
        assert "session.id" not in a
        assert "gen_ai.conversation.id" not in a


def test_app_can_stamp_session_through_span_attributes():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider, span_attributes={"session.id": "sess-42"})
    ask(agent)
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        assert attrs(span)["session.id"] == "sess-42", span.name


def _ask_inside(agent, *context_managers):
    from contextlib import ExitStack

    async def _run():
        with ExitStack() as stack:
            for cm in context_managers:
                stack.enter_context(cm)
            return await agent.ask(USER_PROMPT)

    return asyncio.run(_run())


def test_using_session_sets_session_on_every_ag2_span():
    from fi_instrumentation import using_session

    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider)
    _ask_inside(agent, using_session("sess-ctx-1"))
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} >= {"invoke_agent weather_bot", "execute_tool get_weather"}
    for span in spans:
        assert attrs(span).get("session.id") == "sess-ctx-1", span.name


def test_using_attributes_context_reaches_ag2_spans():
    from fi_instrumentation import using_attributes

    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider)
    _ask_inside(agent, using_attributes(session_id="s-1", user_id="u-1", metadata={"tenant": "acme"}, tags=["t1"]))
    for span in exporter.get_finished_spans():
        a = attrs(span)
        assert a.get("session.id") == "s-1", span.name
        assert a.get("user.id") == "u-1", span.name
        assert "acme" in a.get("metadata", ""), span.name
        assert list(a.get("tag.tags", ())) == ["t1"], span.name


def test_context_session_wins_but_other_ag2_keys_are_not_overridden():
    """session.id from using_session replaces a static span_attributes value;
    any other key AG2 set itself is kept."""
    from fi_instrumentation import using_session, using_user

    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider, span_attributes={"session.id": "static", "user.id": "from-ag2"})
    _ask_inside(agent, using_session("dynamic"), using_user("from-context"))
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        a = attrs(span)
        assert a["session.id"] == "dynamic", span.name
        assert a["user.id"] == "from-ag2", span.name


def test_context_attributes_skip_foreign_scope_spans():
    from fi_instrumentation import using_session

    provider, exporter = memory_provider()
    setup(tracer_provider=provider)
    with using_session("sess-ctx-1"):
        with provider.get_tracer("some.other.instrumentation").start_as_current_span("other"):
            pass
    (span,) = exporter.get_finished_spans()
    assert "session.id" not in attrs(span)


def test_context_keys_are_evicted_last_when_processor_keys_fill_a_limited_span():
    """on_start writes traceAI context keys before AG2 sets any attribute, so
    they are the oldest keys on the span. When the keys the processor adds
    (span kind, usage aliases) overflow max_span_attributes, the oldest keys
    are evicted; session.id and user.id must not be among them."""
    from fi_instrumentation import using_attributes
    from opentelemetry.sdk.trace import SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    def final_chat(max_span_attributes):
        exporter = InMemorySpanExporter()
        provider = TracerProvider(
            span_limits=SpanLimits(max_span_attributes=max_span_attributes), shutdown_on_exit=False
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        agent = weather_agent()
        setup(agent, tracer_provider=provider)
        _ask_inside(agent, using_attributes(session_id="sess-limit", user_id="user-limit"))
        (chat,) = find(exporter.get_finished_spans(), f"chat {MODEL}")
        return chat

    added_by_processor = {"gen_ai.span.kind", *traceai_ag2.USAGE_KEY_ALIASES.values()}
    unlimited = attrs(final_chat(128))
    assert added_by_processor <= set(unlimited)
    written_by_ag2_and_context = [k for k in unlimited if k not in added_by_processor]

    # Exactly full when AG2 is done, so only the processor's keys evict.
    chat = final_chat(len(written_by_ag2_and_context))
    a = attrs(chat)
    assert chat.dropped_attributes == len(added_by_processor)
    assert a.get("session.id") == "sess-limit"
    assert a.get("user.id") == "user-limit"
    assert a.get("gen_ai.usage.input_tokens") == 11
    assert a.get("gen_ai.span.kind") == "LLM"


# --- AC-06 errors and cancellation -------------------------------------------


def _test_config_supports(parameter: str) -> bool:
    from ag2.testing import TestConfig

    return parameter in inspect.signature(TestConfig).parameters


def test_tool_exception_sets_error_status():
    from ag2 import Agent
    from ag2.events import ToolCallEvent
    from ag2.testing import TestConfig

    def explode(city: str) -> str:
        """Always fails."""
        raise ValueError("weather service down")

    provider, exporter = memory_provider()
    script = (ToolCallEvent("explode", arguments='{"city": "Paris"}'), "Sorry, the tool failed.")
    tolerant = _test_config_supports("raise_tool_errors")  # ag2 >= 1.0.3
    config = TestConfig(*script, raise_tool_errors=False) if tolerant else TestConfig(*script)
    agent = Agent("fragile_bot", config=config, tools=[explode])
    setup(agent, tracer_provider=provider)
    if tolerant:
        ask(agent)
    else:  # older TestClient re-raises the tool error from the next model call
        with pytest.raises(ValueError):
            ask(agent)
    (tool,) = find(exporter.get_finished_spans(), "execute_tool explode")
    assert tool.status.status_code == StatusCode.ERROR
    assert attrs(tool)["gen_ai.span.kind"] == "TOOL"


def test_model_call_exception_sets_error_and_closes_spans():
    """The scripted client raises inside the LLM call (the tool's error, re-raised
    by TestClient's default ``raise_tool_errors``); works on every ag2 1.x."""
    from ag2 import Agent
    from ag2.events import ToolCallEvent
    from ag2.testing import TestConfig

    def explode(city: str) -> str:
        """Always fails."""
        raise ValueError("weather service down")

    provider, exporter = memory_provider()
    agent = Agent(
        "broken_bot",
        config=TestConfig(ToolCallEvent("explode", arguments='{"city": "Paris"}'), "unreachable"),
        tools=[explode],
    )
    setup(agent, tracer_provider=provider)
    with pytest.raises(ValueError):
        ask(agent)
    spans = exporter.get_finished_spans()
    chats = find(spans, "chat")
    assert [c.status.status_code for c in chats] == [StatusCode.UNSET, StatusCode.ERROR]
    (root,) = find(spans, "invoke_agent")
    assert root.status.status_code == StatusCode.ERROR
    assert all(s.end_time is not None for s in spans)


def test_provider_exception_sets_error_and_closes_spans():
    import ag2.testing as ag2_testing
    from ag2 import Agent
    from ag2.testing import TestConfig

    if "BaseException" not in inspect.getsource(ag2_testing.TestClient):
        pytest.skip("scripting a raised exception needs ag2.testing from ag2 >= 1.0.4")

    provider, exporter = memory_provider()
    agent = Agent("broken_bot", config=TestConfig(RuntimeError("provider 500")))
    setup(agent, tracer_provider=provider)
    with pytest.raises(RuntimeError):
        ask(agent)
    spans = exporter.get_finished_spans()
    (chat,) = find(spans, "chat")
    (root,) = find(spans, "invoke_agent")
    assert chat.status.status_code == StatusCode.ERROR
    assert root.status.status_code == StatusCode.ERROR


def test_cancel_closes_spans():
    from ag2 import Agent
    from ag2.events import ToolCallEvent
    from ag2.testing import TestConfig

    started = asyncio.Event()

    async def hang(city: str) -> str:
        """Never returns."""
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    provider, exporter = memory_provider()
    agent = Agent("hanging_bot", config=TestConfig(ToolCallEvent("hang", arguments='{"city": "Paris"}'), "done"), tools=[hang])
    setup(agent, tracer_provider=provider)

    async def run_and_cancel():
        task = asyncio.ensure_future(agent.ask("go"))
        await asyncio.wait_for(started.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "invoke_agent hanging_bot" in names
    assert "execute_tool hang" in names
    assert all(s.end_time is not None for s in spans)


# --- AC-04 (partial): traceparent relay across two agents --------------------


def test_traceparent_relay_keeps_one_trace_across_two_agents():
    """Relays the upstream key exactly as ag2/network/client/handlers.py does,
    without running the network hub (not exercised here)."""
    from ag2 import Agent
    from ag2._telemetry_consts import TRACEPARENT_DEP_KEY
    from ag2.testing import TestConfig

    provider, exporter = memory_provider()
    planner = Agent("planner", config=TestConfig("plan ready"))
    worker = Agent("worker", config=TestConfig("work done"))
    setup(planner, worker, tracer_provider=provider)

    ask(planner, "plan it")
    (planner_span,) = find(exporter.get_finished_spans(), "invoke_agent planner")
    ctx = planner_span.context
    traceparent = f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"

    ask(worker, "do it", dependencies={TRACEPARENT_DEP_KEY: traceparent})
    (worker_span,) = find(exporter.get_finished_spans(), "invoke_agent worker")
    assert worker_span.context.trace_id == ctx.trace_id
    assert worker_span.parent is not None and worker_span.parent.span_id == ctx.span_id


# --- attach semantics --------------------------------------------------------


def test_setup_is_idempotent_per_agent():
    provider, exporter = memory_provider()
    agent = weather_agent()
    setup(agent, tracer_provider=provider)
    setup(agent, tracer_provider=provider)
    assert len(_telemetry_middlewares(agent)) == 1
    chain = provider._active_span_processor._span_processors
    assert sum(isinstance(p, AG2SpanProcessor) for p in chain) == 1
    ask(agent)
    assert len(find(exporter.get_finished_spans(), "invoke_agent")) == 1


def test_setup_without_provider_registers_once_and_reuses_it(monkeypatch):
    """README: setup is idempotent. A second setup() without tracer_provider
    reuses the provider, and its one AG2SpanProcessor, from the first call
    instead of registering another; a later config still replaces the
    installed one."""
    exporter, calls = fake_register(monkeypatch)
    planner = weather_agent("planner")
    worker = weather_agent("worker")
    first = setup(planner, project_name="ag2-app")
    second = setup(worker, capture_content=True, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    assert second is first
    assert calls == ["ag2-app"]
    chain = first._active_span_processor._span_processors
    assert sum(isinstance(p, AG2SpanProcessor) for p in chain) == 1
    ask(worker)
    spans = exporter.get_finished_spans()
    assert find(spans, "invoke_agent worker")
    for span in spans:
        assert_no_content(attrs(span), span.name)


def test_setup_reusing_its_provider_warns_when_project_name_differs(monkeypatch, caplog):
    _, calls = fake_register(monkeypatch)
    first = setup(project_name="ag2-app")
    with caplog.at_level(logging.WARNING, logger="traceai_ag2"):
        assert setup(project_name="other-app") is first
    assert calls == ["ag2-app"]
    assert any("other-app" in r.getMessage() for r in caplog.records), caplog.text


def test_setup_registers_again_after_its_provider_is_shut_down(monkeypatch):
    """A provider the application shut down is not reused."""
    _, calls = fake_register(monkeypatch)
    first = setup(project_name="ag2-app")
    first.shutdown()
    second = setup(project_name="ag2-app")
    assert second is not first
    assert calls == ["ag2-app", "ag2-app"]


def test_setup_names_middleware_after_each_agent():
    from ag2 import Agent
    from ag2.testing import TestConfig

    provider, exporter = memory_provider()
    a = Agent("alpha", config=TestConfig("a"))
    b = Agent("beta", config=TestConfig("b"))
    setup(a, b, tracer_provider=provider)
    ask(a)
    ask(b)
    names = {s.name for s in exporter.get_finished_spans()}
    assert {"invoke_agent alpha", "invoke_agent beta"} <= names


def test_manual_middleware_with_setup_installing_only_the_processor():
    """README path for agents built after setup: Agent(middleware=[...]) and ask(middleware=[...])."""
    from ag2 import Agent

    provider, exporter = memory_provider()
    assert setup(tracer_provider=provider) is provider
    mw = create_telemetry_middleware(tracer_provider=provider, agent_name="late_bot")
    agent = Agent("late_bot", config=weather_config(), tools=[get_weather], middleware=[mw])
    ask(agent)
    per_call = Agent("per_call_bot", config=weather_config(), tools=[get_weather])
    ask(per_call, middleware=[create_telemetry_middleware(tracer_provider=provider, agent_name="per_call_bot")])

    spans = exporter.get_finished_spans()
    for name in ("invoke_agent late_bot", "invoke_agent per_call_bot"):
        (root,) = [s for s in spans if s.name == name]
        assert attrs(root)["gen_ai.span.kind"] == "AGENT"
    for span in spans:
        assert_no_content(attrs(span), span.name)
    tools = find(spans, "execute_tool")
    assert len(tools) == 2
    assert all(attrs(s)["gen_ai.span.kind"] == "TOOL" for s in tools)


def test_setup_does_not_wrap_agent():
    from ag2 import Agent

    before = {name: inspect.getattr_static(Agent, name) for name in ("__init__", "ask", "add_middleware")}
    provider, _ = memory_provider()
    setup(weather_agent(), tracer_provider=provider)
    after = {name: inspect.getattr_static(Agent, name) for name in before}
    assert before == after


def test_max_tool_result_chars_forwarded_only_when_given():
    from ag2.middleware.builtin.telemetry import TelemetryMiddleware

    if "max_tool_result_chars" not in inspect.signature(TelemetryMiddleware.__init__).parameters:
        pytest.skip("installed ag2 predates max_tool_result_chars (added in 1.0.4)")
    default = create_telemetry_middleware()
    upstream_default = inspect.signature(TelemetryMiddleware.__init__).parameters["max_tool_result_chars"].default
    assert default._max_tool_result_chars == upstream_default
    assert create_telemetry_middleware(max_tool_result_chars=16)._max_tool_result_chars == 16
