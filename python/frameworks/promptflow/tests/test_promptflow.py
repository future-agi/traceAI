"""Prompt flow instrumentation over the real promptflow-tracing / promptflow-core packages."""
import asyncio
import gc
import os
import sys

import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import StatusCode

from conftest import (
    SECRET_ERROR, SECRET_INPUT, attrs, blob, by_name, write_dag_flow, write_flex_flow,
)

NATIVE_CONTENT = ("inputs", "output", "prompt.template", "prompt.variables", "llm.generated_message")


def finished(spans):
    return spans.get_finished_spans()


def root_of(spans):
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1, [s.name for s in spans]
    return roots[0]


def run_flow(path, inputs):
    from promptflow.executor import FlowExecutor

    result = FlowExecutor.create(path, {}).exec_line(inputs)
    return result


# --- flows -------------------------------------------------------------------------------------

def test_dag_flow_is_a_chain_with_a_tool_span_per_node(spans, tmp_path):
    result = run_flow(write_dag_flow(tmp_path), {"question": "q"})
    assert result.output == {"answer": "HELLO Q"}
    out = finished(spans)
    flow = root_of(out)
    assert attrs(flow)["gen_ai.span.kind"] == "CHAIN"
    assert flow.parent is None
    for node in ("greet", "shout"):
        span = by_name(out, node)
        a = attrs(span)
        assert a["gen_ai.span.kind"] == "TOOL"
        assert span.parent.span_id == flow.context.span_id
        assert span.status.status_code != StatusCode.ERROR
    shout = attrs(by_name(out, "shout"))
    assert shout["input.value"].count("hello q") == 1
    assert "HELLO Q" in shout["output.value"]
    assert "HELLO Q" in attrs(flow)["output.value"]
    assert "question" in attrs(flow)["input.value"]
    assert attrs(flow)["gen_ai.provider.name"] == "promptflow"


def test_flex_flow_is_a_chain_with_a_tool_child(spans, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    result = run_flow(write_flex_flow(tmp_path), {"n": 3})
    assert result.output == 7
    out = finished(spans)
    flow = [s for s in out if s.parent is None]
    assert len(flow) == 1 and attrs(flow[0])["gen_ai.span.kind"] == "CHAIN"
    helper = by_name(out, "helper")
    assert attrs(helper)["gen_ai.span.kind"] == "TOOL"
    assert helper.parent.span_id == flow[0].context.span_id
    assert attrs(helper)["output.value"] == "6"


def test_a_standalone_trace_function_is_a_chain_and_nested_ones_are_tools(spans):
    from promptflow.tracing import trace

    @trace
    def inner(x):
        return x + 1

    @trace
    def outer(x):
        return inner(x) * 2

    assert outer(1) == 4
    out = finished(spans)
    assert attrs(by_name(out, "outer"))["gen_ai.span.kind"] == "CHAIN"
    assert attrs(by_name(out, "inner"))["gen_ai.span.kind"] == "TOOL"
    assert by_name(out, "inner").parent.span_id == by_name(out, "outer").context.span_id


def test_async_trace_function(spans):
    from promptflow.tracing import trace

    @trace
    async def work(x):
        return x * 3

    assert asyncio.run(work(2)) == 6
    a = attrs(by_name(finished(spans), "work"))
    assert a["output.value"] == "6" and a["gen_ai.span.kind"] == "CHAIN"


# --- privacy -----------------------------------------------------------------------------------

def test_native_content_attributes_and_events_are_not_exported(spans, tmp_path):
    run_flow(write_dag_flow(tmp_path), {"question": SECRET_INPUT})
    for span in finished(spans):
        a = attrs(span)
        assert not [k for k in NATIVE_CONTENT if k in a], (span.name, list(a))
        assert not [e.name for e in span.events if e.name.startswith("promptflow.")]


@pytest.mark.parametrize(
    "config",
    [TraceConfig(hide_inputs=True, hide_outputs=True)],
)
def test_hide_inputs_and_outputs(instrument, tmp_path, config):
    spans = instrument(config=config)
    run_flow(write_dag_flow(tmp_path), {"question": SECRET_INPUT})
    out = finished(spans)
    assert out
    for span in out:
        assert SECRET_INPUT not in blob(span) and SECRET_INPUT.upper() not in blob(span)
        assert "input.value" not in attrs(span) and "output.value" not in attrs(span)


def test_content_captured_by_default_but_output_can_be_hidden_alone(instrument):
    from promptflow.tracing import trace

    spans = instrument(config=TraceConfig(hide_outputs=True))

    @trace
    def echo(text):
        return "out-" + text

    echo("in-visible")
    a = attrs(by_name(finished(spans), "echo"))
    assert "in-visible" in a["input.value"] and "output.value" not in a


# --- errors ------------------------------------------------------------------------------------

def test_error_in_trace_function_is_reraised_unchanged_and_message_not_exported(spans):
    from promptflow.tracing import trace

    boom = ValueError(SECRET_ERROR)

    @trace
    def fails():
        raise boom

    with pytest.raises(ValueError) as caught:
        fails()
    assert caught.value is boom
    span = by_name(finished(spans), "fails")
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"].endswith("ValueError")
    assert SECRET_ERROR not in blob(span)
    assert not [e for e in span.events if SECRET_ERROR in str(dict(e.attributes or {}))]


def test_error_in_a_dag_node_marks_node_and_flow_without_the_message(spans, tmp_path):
    with pytest.raises(Exception) as caught:
        run_flow(write_dag_flow(tmp_path, fail=True), {"question": "q"})
    assert "Execution failure in 'shout'" in str(caught.value)
    out = finished(spans)
    for span in (by_name(out, "shout"), root_of(out)):
        assert span.status.status_code == StatusCode.ERROR, span.name
        assert "error.type" in attrs(span)
    for span in out:
        assert SECRET_ERROR not in blob(span)
    assert by_name(out, "greet").status.status_code != StatusCode.ERROR


def test_async_error_is_reraised(spans):
    from promptflow.tracing import trace

    @trace
    async def fails():
        raise KeyError(SECRET_ERROR)

    with pytest.raises(KeyError):
        asyncio.run(fails())
    span = by_name(finished(spans), "fails")
    assert span.status.status_code == StatusCode.ERROR and SECRET_ERROR not in blob(span)


# --- streams -----------------------------------------------------------------------------------

def test_sync_generator_span_ends_on_exhaustion(spans):
    from promptflow.tracing import trace

    @trace
    def stream():
        yield "a"
        yield "b"

    it = stream()
    assert finished(spans) == ()
    first = next(it)
    assert first == "a" and finished(spans) == ()
    assert list(it) == ["b"]
    span = by_name(finished(spans), "stream")
    assert "incomplete" not in " ".join(attrs(span))
    assert span.status.status_code != StatusCode.ERROR
    assert "a" in attrs(span)["output.value"] and "b" in attrs(span)["output.value"]


def test_sync_generator_closed_early_ends_the_span(spans):
    from promptflow.tracing import trace

    @trace
    def stream():
        yield "a"
        yield "b"

    it = stream()
    next(it)
    assert finished(spans) == ()
    it.close()
    span = by_name(finished(spans), "stream")
    assert attrs(span)["promptflow.stream.incomplete"] is True


def test_sync_generator_dropped_early_ends_the_span(spans):
    from promptflow.tracing import trace

    @trace
    def stream():
        yield "a"
        yield "b"

    it = stream()
    next(it)
    del it
    gc.collect()
    assert attrs(by_name(finished(spans), "stream"))["promptflow.stream.incomplete"] is True


def test_sync_generator_error_ends_the_span_with_error(spans):
    from promptflow.tracing import trace

    @trace
    def stream():
        yield "a"
        raise RuntimeError(SECRET_ERROR)

    it = stream()
    next(it)
    with pytest.raises(RuntimeError):
        next(it)
    span = by_name(finished(spans), "stream")
    assert span.status.status_code == StatusCode.ERROR
    assert SECRET_ERROR not in blob(span) and "error.type" in attrs(span)


def test_async_generator_ends_on_exhaustion_and_on_early_close(spans):
    from promptflow.tracing import trace

    @trace
    async def full():
        yield "a"
        yield "b"

    @trace
    async def partial():
        yield "a"
        yield "b"

    async def go():
        assert [x async for x in full()] == ["a", "b"]
        it = partial()
        assert await it.__anext__() == "a"
        await it.aclose()

    asyncio.run(go())
    out = finished(spans)
    assert "promptflow.stream.incomplete" not in attrs(by_name(out, "full"))
    assert attrs(by_name(out, "partial"))["promptflow.stream.incomplete"] is True


# --- LLM spans through promptflow's own OpenAI injection ----------------------------------------

def make_openai_client():
    import openai

    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-served",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Tokyo"}}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    return openai.OpenAI(
        api_key="synthetic-openai-key-DO-NOT-EXPORT",
        base_url="http://openai.test/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def injected_openai():
    from promptflow.tracing._integrations._openai_injector import inject_openai_api, recover_openai_api

    inject_openai_api()
    yield
    recover_openai_api()


def test_llm_span_has_model_and_token_usage(spans, injected_openai):
    from promptflow.tracing import trace

    client = make_openai_client()

    @trace
    def ask():
        return client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "Capital of Japan?"}]
        )

    assert ask().choices[0].message.content == "Tokyo"
    out = finished(spans)
    llm = [s for s in out if attrs(s).get("gen_ai.span.kind") == "LLM"]
    assert len(llm) == 1
    a = attrs(llm[0])
    assert a["gen_ai.response.model"] == "gpt-served"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (3, 2, 5)
    assert "Tokyo" in a["output.value"]
    assert "synthetic-openai-key" not in blob(llm[0])
    assert not [k for k in NATIVE_CONTENT if k in a]
    assert llm[0].parent.span_id == by_name(out, "ask").context.span_id


def test_llm_content_is_hidden_when_configured(instrument, injected_openai):
    from promptflow.tracing import trace

    spans = instrument(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    client = make_openai_client()

    @trace
    def ask():
        return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": SECRET_INPUT}])

    ask()
    for span in finished(spans):
        assert SECRET_INPUT not in blob(span) and "Tokyo" not in blob(span)
    llm = [s for s in finished(spans) if attrs(s).get("gen_ai.span.kind") == "LLM"]
    assert llm and attrs(llm[0])["gen_ai.usage.total_tokens"] == 5


# --- lifecycle and robustness -------------------------------------------------------------------

def test_instrument_switches_tracing_on_and_uninstrument_restores_everything(provider):
    from promptflow.tracing import _trace
    from traceai_promptflow import PromptflowInstrumentor

    originals = {n: getattr(_trace, n) for n in ("start_as_current_span", "enrich_span_with_input", "handle_span_exception")}
    assert "PF_DISABLE_TRACING" not in os.environ
    instrumentor = PromptflowInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    assert os.environ["PF_DISABLE_TRACING"] == "false"
    assert _trace.start_as_current_span is not originals["start_as_current_span"]
    instrumentor.uninstrument()
    assert "PF_DISABLE_TRACING" not in os.environ
    for name, original in originals.items():
        assert getattr(_trace, name) is original
    from promptflow.tracing import trace

    @trace
    def f():
        return 1

    f()
    assert provider.exporter.get_finished_spans() == ()


def test_an_explicit_pf_disable_tracing_is_respected(provider, monkeypatch):
    from promptflow.tracing import trace
    from traceai_promptflow import PromptflowInstrumentor

    monkeypatch.setenv("PF_DISABLE_TRACING", "true")
    instrumentor = PromptflowInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert os.environ["PF_DISABLE_TRACING"] == "true"

        @trace
        def f():
            return 1

        assert f() == 1
        assert provider.exporter.get_finished_spans() == ()
    finally:
        instrumentor.uninstrument()
    assert os.environ["PF_DISABLE_TRACING"] == "true"


def test_suppressed_instrumentation_exports_nothing(spans):
    from promptflow.tracing import trace

    @trace
    def f():
        return 1

    token = context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert f() == 1
    finally:
        context_api.detach(token)
    assert finished(spans) == ()


def test_a_failure_while_recording_never_breaks_the_user_call(spans, monkeypatch):
    import traceai_promptflow
    from promptflow.tracing import trace

    def explode(*args, **kwargs):
        raise RuntimeError("tracing bug")

    monkeypatch.setattr(traceai_promptflow, "_serialize", explode)

    @trace
    def f(x):
        return x + 1

    @trace
    def g():
        yield 1

    assert f(1) == 2
    assert list(g()) == [1]
    out = finished(spans)
    assert {s.name.rsplit(".", 1)[-1] for s in out} == {"f", "g"}
    assert all("input.value" not in attrs(s) and "output.value" not in attrs(s) for s in out)


def test_a_failure_starting_the_span_never_breaks_the_user_call(spans, monkeypatch):
    import traceai_promptflow
    from promptflow.tracing import trace

    class Broken:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("tracer bug")

    monkeypatch.setitem(traceai_promptflow._state, "tracer", Broken())

    @trace
    def f(x):
        return x + 1

    assert f(1) == 2
    assert finished(spans) == ()
