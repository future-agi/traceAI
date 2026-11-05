"""Offline lifecycle regressions; no model or vendor endpoints are used."""

import asyncio
import copy
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import dspy
import pytest
from fi_instrumentation import TraceConfig, using_attributes
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_dspy import DSPyInstrumentor

HAS_CALLBACKS = importlib.util.find_spec("dspy.utils.callback") is not None
callbacks_only = pytest.mark.skipif(
    not HAS_CALLBACKS, reason="DSPy 2.5.1 predates callbacks"
)
async_only = pytest.mark.skipif(
    not hasattr(dspy.Module, "acall"), reason="DSPy has no acall"
)


class Local(dspy.Module):
    def forward(self, question):
        return dspy.Prediction(answer=question)

    async def aforward(self, question):
        await asyncio.sleep(0)
        return dspy.Prediction(answer=question)


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = DSPyInstrumentor()
    original_callbacks = list(dspy.settings.get("callbacks", []))
    instrumentor.instrument(tracer_provider=provider)
    yield instrumentor, exporter, provider
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    if HAS_CALLBACKS:
        dspy.configure(callbacks=original_callbacks)
    provider.shutdown()


def test_sync_cleanup_and_reinstrument(tracing):
    instrumentor, exporter, provider = tracing
    module = Local()
    instrumentor.instrument(tracer_provider=provider)
    module(question="one")
    assert len(exporter.get_finished_spans()) == 1
    instrumentor.uninstrument()
    exporter.clear()
    module(question="two")
    assert exporter.get_finished_spans() == ()
    instrumentor.instrument(tracer_provider=provider)
    module(question="three")
    assert len(exporter.get_finished_spans()) == 1


def test_all_patches_restore_and_stale_wrappers_disable(tracing):
    instrumentor, exporter, _ = tracing
    patches = list(instrumentor._patches)
    # A caller may retain a bound wrapper across uninstrumentation.
    captured = Local().__call__
    instrumentor.uninstrument()
    for cls, method, original, _, owned in patches:
        assert (
            cls.__dict__.get(method) is original
            if owned
            else method not in cls.__dict__
        )
    captured(question="after removal")
    assert not exporter.get_finished_spans()


def test_deepcopy_and_hierarchy(tracing):
    _, exporter, provider = tracing

    class Parent(dspy.Module):
        def forward(self, question):
            return copy.deepcopy(Local())(question=question)

    with provider.get_tracer("application").start_as_current_span(
        "application"
    ) as parent:
        Parent()(question="local")
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"application", "Parent.forward", "Local.forward"}
    assert (
        spans["Local.forward"].parent.span_id == spans["Parent.forward"].context.span_id
    )
    assert spans["Parent.forward"].parent.span_id == parent.get_span_context().span_id
    assert not trace.get_current_span().get_span_context().is_valid


def test_suppression(tracing):
    _, exporter, _ = tracing
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        Local()(question="not recorded")
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()


@callbacks_only
def test_preserves_user_callbacks_and_captured_lists(tracing):
    from dspy.utils.callback import BaseCallback

    instrumentor, exporter, provider = tracing
    instrumentor.uninstrument()
    events = []

    class User(BaseCallback):
        def on_module_start(self, **kwargs):
            events.append("start")

        def on_module_end(self, **kwargs):
            events.append("end")

    user = User()
    original = [user]
    dspy.configure(callbacks=original)
    instrumentor.instrument(tracer_provider=provider)
    captured = list(dspy.settings.callbacks)
    callback = instrumentor._callback
    assert original == [user]
    Local()(question="before removal")
    instrumentor.uninstrument()
    assert dspy.settings.callbacks == [user]
    exporter.clear()
    with dspy.context(callbacks=captured):
        Local()(question="old context")
    assert events == ["start", "end", "start", "end"]
    assert not exporter.get_finished_spans()
    assert not callback._calls


@async_only
def test_async_concurrency_and_tools(tracing):
    instrumentor, exporter, provider = tracing

    async def tool(value: str) -> str:
        await asyncio.sleep(0)
        return value

    local_tool = dspy.Tool(tool)

    class Parent(dspy.Module):
        async def aforward(self, question):
            return dspy.Prediction(answer=await local_tool.acall(value=question))

    async def run(question):
        with provider.get_tracer("application").start_as_current_span(question):
            return await Parent().acall(question=question)

    async def concurrent():
        return await asyncio.gather(run("first"), run("second"))

    # DSPy 2.x uses thread-local rather than task-local settings. Restore its
    # surrounding settings scope after overlapping calls; OTel context remains
    # task-local on both versions.
    with dspy.context():
        assert len(asyncio.run(concurrent())) == 2
    spans = exporter.get_finished_spans()
    assert len(spans) == 6
    by_id = {span.context.span_id: span for span in spans}
    for span in spans:
        if span.name == "Tool.__call__":
            module = by_id[span.parent.span_id]
            root = by_id[module.parent.span_id]
            assert module.name == "Parent.forward"
            assert root.name in span.attributes["input.value"]
            assert span.attributes["gen_ai.span.kind"] == "TOOL"
    assert not instrumentor._callback._calls


@callbacks_only
def test_sync_tool_and_exception(tracing):
    instrumentor, exporter, _ = tracing

    def fail(value: str) -> str:
        raise ValueError(value)

    with pytest.raises(Exception, match="synthetic failure"):
        dspy.Tool(fail)(value="synthetic failure")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].events[0].name == "exception"
    assert not instrumentor._callback._calls
    assert not trace.get_current_span().get_span_context().is_valid


@async_only
def test_cancellation_and_uninstrument_during_call(tracing):
    instrumentor, exporter, _ = tracing
    callback = instrumentor._callback

    class Waiting(dspy.Module):
        async def aforward(self, question):
            await asyncio.Event().wait()

    async def run():
        task = asyncio.create_task(Waiting().acall(question="cancel"))
        await asyncio.sleep(0)
        # Unregister from the owner thread outside this task (DSPy configure
        # ownership may be attached to another async task in a host application).
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not trace.get_current_span().get_span_context().is_valid

    asyncio.run(run())
    assert len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR
    assert not callback._calls

    class RemoveDuringCall(dspy.Module):
        def forward(self, question):
            instrumentor.uninstrument()
            return Local()(question=question)

    exporter.clear()
    RemoveDuringCall()(question="finish existing")
    assert len(exporter.get_finished_spans()) == 1
    assert not callback._calls


@callbacks_only
def test_parallel_threads(tracing):
    _, exporter, _ = tracing
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda n: Local()(question=str(n)), range(12)))
    spans = exporter.get_finished_spans()
    assert len(spans) == 12
    assert all(span.parent is None for span in spans)


@callbacks_only
def test_real_predict_lm_and_adapter_without_duplicate_spans(tracing):
    from dspy.utils.dummies import DummyLM

    _, exporter, _ = tracing
    with dspy.context(lm=DummyLM([{"answer": "local answer"}])):
        result = dspy.Predict("question -> answer")(question="local question")
    assert result.answer == "local answer"
    spans = exporter.get_finished_spans()
    predicts = [span for span in spans if span.name.startswith("Predict(")]
    assert len(predicts) == 1
    assert any(span.name.endswith(".format") for span in spans)
    assert any(span.name.endswith(".parse") for span in spans)
    lm_spans = [span for span in spans if span.attributes["gen_ai.span.kind"] == "LLM"]
    assert len(lm_spans) == 1
    assert lm_spans[0].parent.span_id == predicts[0].context.span_id


@async_only
def test_async_lm(tracing):
    from dspy.utils.callback import with_callbacks

    _, exporter, _ = tracing

    class LocalLM(dspy.BaseLM):
        @with_callbacks
        async def acall(self, prompt=None, messages=None, **kwargs):
            await asyncio.sleep(0)
            return ["offline"]

    lm = LocalLM("offline/model", api_key="not-a-real-key")
    assert asyncio.run(lm.acall(prompt="hello")) == ["offline"]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.request.model"] == "offline/model"
    assert "not-a-real-key" not in str(spans[0].attributes)


@async_only
def test_stream_completion_and_failure(tracing):
    instrumentor, exporter, _ = tracing

    async def run(program):
        return [
            item
            async for item in dspy.streamify(program, is_async_program=True)(
                question="stream"
            )
        ]

    result = asyncio.run(run(Local()))
    assert result[-1].answer == "stream"
    assert len(exporter.get_finished_spans()) == 1
    assert not instrumentor._callback._calls
    exporter.clear()

    class Broken(Local):
        async def aforward(self, question):
            raise ValueError("offline stream failure")

    with pytest.raises(Exception):
        asyncio.run(run(Broken()))
    assert len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR
    assert not instrumentor._callback._calls


@callbacks_only
def test_evaluation_compile_and_interpreter_hooks(tracing):
    from dspy.teleprompt.teleprompt import Teleprompter
    from dspy.utils.callback import BaseCallback

    instrumentor, exporter, _ = tracing
    evaluator = dspy.Evaluate(
        devset=[dspy.Example(question="local", answer="local").with_inputs("question")],
        metric=lambda example, prediction, trace=None: example.answer
        == prediction.answer,
        num_threads=1,
        display_progress=False,
        display_table=False,
    )
    evaluator(Local())
    spans = exporter.get_finished_spans()
    if hasattr(BaseCallback, "on_evaluate_start"):
        assert any(span.name == "Evaluate.evaluate" for span in spans)
    if hasattr(BaseCallback, "on_compile_start"):

        class LocalOptimizer(Teleprompter):
            def compile(self, student, **kwargs):
                student(question="compile")
                return student

        LocalOptimizer().compile(Local(), trainset=[])
        assert any(
            span.name == "LocalOptimizer.compile"
            for span in exporter.get_finished_spans()
        )
    # Invoke the official lifecycle API without starting a subprocess/interpreter.
    for operation in ("execute", "tool_call", "startup", "shutdown"):
        if not hasattr(BaseCallback, f"on_interpreter_{operation}_start"):
            continue
        callback = instrumentor._callback
        getattr(callback, f"on_interpreter_{operation}_start")(
            operation, object(), {"code": "local"}
        )
        getattr(callback, f"on_interpreter_{operation}_end")(operation, "local")
    assert not instrumentor._callback._calls


def test_retrieval_documents(tracing):
    from types import SimpleNamespace

    _, exporter, _ = tracing

    def retrieve(query, k=1, **kwargs):
        return [SimpleNamespace(long_text="local document")]

    with dspy.context(rm=retrieve):
        assert dspy.Retrieve(k=1)("query").passages == ["local document"]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.span.kind"] == "RETRIEVER"
    assert (
        spans[0].attributes["retrieval.documents.0.document.content"]
        == "local document"
    )


@pytest.mark.skipif(not hasattr(dspy, "Embedder"), reason="DSPy has no Embedder")
def test_supplemental_embeddings_and_cleanup(tracing):
    import numpy as np

    instrumentor, exporter, _ = tracing
    embedder = dspy.Embedder(
        lambda inputs: np.array([[1.0, 2.0] for _ in inputs]), caching=False
    )
    assert embedder(inputs="local").shape == (2,)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.span.kind"] == "EMBEDDING"
    assert tuple(spans[0].attributes["embedding.embeddings.0.embedding.vector"]) == (
        1.0,
        2.0,
    )
    instrumentor.uninstrument()
    exporter.clear()
    embedder(inputs=["after removal"])
    assert not exporter.get_finished_spans()


@async_only
def test_stream_consumer_cancellation(tracing):
    instrumentor, exporter, _ = tracing

    async def run():
        started = asyncio.Event()

        class Waiting(Local):
            async def aforward(self, question):
                started.set()
                await asyncio.Event().wait()

        async def consume():
            async for _ in dspy.streamify(Waiting(), is_async_program=True)(
                question="cancel"
            ):
                pass

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR
    assert not instrumentor._callback._calls


@callbacks_only
def test_privacy_metadata_and_otlp_wire(tracing):
    instrumentor, _, _ = tracing
    instrumentor.uninstrument()
    requests = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                headers={},
                timeout=2,
            )
        )
    )
    try:
        instrumentor.instrument(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=True, hide_outputs=True),
        )
        with using_attributes(session_id="local-session"):
            Local()(question="SENSITIVE_PAYLOAD")

        def fail(value: str) -> str:
            raise ValueError(value)

        with pytest.raises(Exception):
            dspy.Tool(fail)(value="SENSITIVE_EXCEPTION")
        provider.force_flush()
        assert len(requests) == 2
        wire = b"".join(requests)
        assert b"SENSITIVE_PAYLOAD" not in wire
        assert b"SENSITIVE_EXCEPTION" not in wire
        decoded = ExportTraceServiceRequest.FromString(requests[0])
        span = decoded.resource_spans[0].scope_spans[0].spans[0]
        attrs = {attr.key: attr.value.string_value for attr in span.attributes}
        assert attrs["session.id"] == "local-session"
        assert attrs["gen_ai.span.kind"] == "CHAIN"
        assert attrs["input.value"] == "__REDACTED__"
        assert attrs["output.value"] == "__REDACTED__"
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        server.shutdown()
        thread.join()
        server.server_close()
