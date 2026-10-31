"""Offline contract tests against real Haystack pipeline execution."""

import asyncio
import json
import logging
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from threading import Thread
from typing import List

os.environ["HAYSTACK_TELEMETRY_ENABLED"] = "false"
os.environ["HAYSTACK_AUTO_TRACE_ENABLED"] = "false"

import pytest
from fi_instrumentation import TraceConfig
from haystack import Document, Pipeline, component, tracing
from haystack.components.builders import PromptBuilder
from haystack.dataclasses import ChatMessage
from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_haystack import HaystackInstrumentor
from traceai_haystack._bindings import pipeline_context
from traceai_haystack._tracer import HaystackTracer

CURRENT = int(version("haystack-ai").split(".")[0]) >= 3
SECRET = "private-content-91a7"


@component
class Echo:
    @component.output_types(text=str)
    def run(self, text: str):
        return {"text": text}

    @component.output_types(text=str)
    async def run_async(self, text: str):
        await asyncio.sleep(0)
        return {"text": text}


@component
class SyntheticGenerator:
    @component.output_types(replies=List[str], meta=List[dict])
    def run(self, prompt: str, streaming_callback=None):
        if streaming_callback:
            streaming_callback("answer")
        return {
            "replies": ["answer"],
            "meta": [
                {
                    "model": "offline-model",
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
            ],
        }

    @component.output_types(replies=List[str], meta=List[dict])
    async def run_async(self, prompt: str, streaming_callback=None):
        return self.run(prompt, streaming_callback)


@component
class SyntheticEmbedder:
    model = "offline-embedding"

    @component.output_types(embedding=List[float])
    def run(self, text: str):
        return {"embedding": [0.25, 0.75]}


@component
class SyntheticRetriever:
    @component.output_types(documents=List[Document])
    def run(self, query: str):
        return {
            "documents": [
                Document(id="d1", content=SECRET, score=0.9, meta={"source": "local"})
            ]
        }


@component
class SyntheticRanker:
    model = "offline-ranker"

    @component.output_types(documents=List[Document])
    def run(self, query: str, documents: List[Document], top_k: int = 1):
        return {"documents": documents[:top_k]}


@component
class Failure:
    @component.output_types(text=str)
    def run(self, text: str):
        raise ValueError(text)

    @component.output_types(text=str)
    async def run_async(self, text: str):
        raise ValueError(text)


@contextmanager
def installed(config=None, capture_content=True, exporter=None):
    previous = tracing.tracer.actual_tracer
    provider = TracerProvider()
    exporter = exporter or InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = HaystackInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider, config=config, capture_content=capture_content
    )
    try:
        yield instrumentor, provider, exporter
    finally:
        instrumentor.uninstrument()
        tracing.enable_tracing(previous)
        provider.shutdown()


def pipeline_for(mode):
    if mode != "sync" and not hasattr(Pipeline, "run_async"):
        from haystack import AsyncPipeline

        return AsyncPipeline()
    return Pipeline()


def execute(pipeline, mode, data):
    if mode == "sync":
        return pipeline.run(data)
    if mode == "async":
        return asyncio.run(pipeline.run_async(data))

    async def consume():
        outputs = [output async for output in pipeline.run_async_generator(data)]
        return outputs[-1]

    return asyncio.run(consume())


@pytest.mark.parametrize("mode", ["sync", "async", "generator"])
def test_real_pipeline_one_producer_and_parenting(mode):
    original_run = Echo.run
    private_hook = Pipeline._run_component
    with installed() as (ins, provider, exporter):
        assert isinstance(tracing.tracer.actual_tracer, HaystackTracer)
        pipeline = pipeline_for(mode)
        pipeline.add_component("echo", Echo())
        with provider.get_tracer("application").start_as_current_span(
            "request"
        ) as parent:
            assert execute(pipeline, mode, {"echo": {"text": SECRET}}) == {
                "echo": {"text": SECRET}
            }
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == ["Echo.run", "Pipeline.run", "request"]
        child, root, _ = spans
        assert child.parent.span_id == root.context.span_id
        assert root.parent.span_id == parent.get_span_context().span_id
        assert child.attributes["gen_ai.span.kind"] == "CHAIN"
        assert SECRET in child.attributes["input.value"]
        assert SECRET in child.attributes["output.value"]
        assert SECRET in root.attributes["output.value"]
        assert "haystack.pipeline.input_data" not in root.attributes
        assert Echo.run is original_run
        assert Pipeline._run_component is private_hook
    assert pipeline_context.get() is None


@pytest.mark.parametrize("mode", ["sync", "async", "generator"])
def test_model_tokens_messages_prompt_template_and_streaming(mode):
    chunks = []
    with installed() as (_, _, exporter):
        pipeline = pipeline_for(mode)
        pipeline.add_component(
            "prompt",
            PromptBuilder(template="Hello {{name}}", required_variables=["name"]),
        )
        pipeline.add_component("generator", SyntheticGenerator())
        pipeline.connect("prompt", "generator")
        execute(
            pipeline,
            mode,
            {
                "prompt": {"name": SECRET},
                "generator": {"streaming_callback": chunks.append},
            },
        )
        spans = exporter.get_finished_spans()
        assert len(spans) == 3
        prompt = next(s for s in spans if s.name == "PromptBuilder.run")
        generator = next(s for s in spans if s.name == "SyntheticGenerator.run")
        assert prompt.attributes["gen_ai.prompt.template.name"] == "Hello {{name}}"
        assert SECRET in prompt.attributes["gen_ai.prompt.template.variables"]
        assert generator.attributes["gen_ai.span.kind"] == "LLM"
        assert generator.attributes["gen_ai.request.model"] == "offline-model"
        assert generator.attributes["gen_ai.usage.input_tokens"] == 3
        assert generator.attributes["gen_ai.usage.output_tokens"] == 2
        assert generator.attributes["gen_ai.usage.total_tokens"] == 5
        assert SECRET in generator.attributes["gen_ai.input.messages.0.message.content"]
        assert (
            generator.attributes["gen_ai.output.messages.0.message.content"] == "answer"
        )
        assert chunks == ["answer"]


@pytest.mark.parametrize("mode", ["sync", "async", "generator"])
def test_retrieval_reranking_embedding_semantics(mode):
    with installed() as (_, _, exporter):
        pipeline = pipeline_for(mode)
        pipeline.add_component("retriever", SyntheticRetriever())
        pipeline.add_component("ranker", SyntheticRanker())
        pipeline.add_component("embedder", SyntheticEmbedder())
        pipeline.connect("retriever.documents", "ranker.documents")
        execute(
            pipeline,
            mode,
            {
                "retriever": {"query": SECRET},
                "ranker": {"query": SECRET},
                "embedder": {"text": SECRET},
            },
        )
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert len(spans) == 4
        assert (
            spans["SyntheticEmbedder.run"].attributes["embedding.model_name"]
            == "offline-embedding"
        )
        assert spans["SyntheticEmbedder.run"].attributes[
            "embedding.embeddings.0.embedding.vector"
        ] == (0.25, 0.75)
        assert (
            spans["SyntheticRetriever.run"].attributes[
                "retrieval.documents.0.document.content"
            ]
            == SECRET
        )
        assert (
            spans["SyntheticRanker.run"].attributes["reranker.model_name"]
            == "offline-ranker"
        )
        assert (
            spans["SyntheticRanker.run"].attributes[
                "reranker.output_documents.0.document.id"
            ]
            == "d1"
        )


@pytest.mark.parametrize("mode", ["sync", "async", "generator"])
def test_errors_propagate_with_private_error_spans(mode):
    with installed(config=TraceConfig(hide_inputs=True, hide_outputs=True)) as (
        _,
        _,
        exporter,
    ):
        pipeline = pipeline_for(mode)
        pipeline.add_component("failure", Failure())
        with pytest.raises(Exception, match=SECRET):
            execute(pipeline, mode, {"failure": {"text": SECRET}})
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert all(s.status.status_code == trace.StatusCode.ERROR for s in spans)
        assert SECRET not in str(
            [(dict(s.attributes), s.status.description, s.events) for s in spans]
        )
    assert pipeline_context.get() is None


def test_default_content_opt_in_and_override(monkeypatch):
    old = tracing.tracer.is_content_tracing_enabled
    tracing.tracer.is_content_tracing_enabled = False
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    ins = HaystackInstrumentor()
    try:
        ins.instrument(tracer_provider=provider)
        pipeline = Pipeline()
        pipeline.add_component("generator", SyntheticGenerator())
        pipeline.run({"generator": {"prompt": SECRET}})
        spans = exporter.get_finished_spans()
        assert SECRET not in str([dict(s.attributes) for s in spans])
        assert spans[0].attributes["gen_ai.request.model"] == "offline-model"
        assert spans[0].attributes["gen_ai.usage.total_tokens"] == 5
        assert tracing.tracer.is_content_tracing_enabled is False
    finally:
        ins.uninstrument()
        tracing.tracer.is_content_tracing_enabled = old
        provider.shutdown()


def test_restore_previous_native_tracer_without_overwriting_new_owner():
    original = tracing.tracer.actual_tracer
    original_run = Pipeline.run
    with installed() as (ins, _, _):
        adapter = tracing.tracer.actual_tracer
        ins.instrument()  # BaseInstrumentor's idempotence guard.
        assert tracing.tracer.actual_tracer is adapter
    assert tracing.tracer.actual_tracer is original
    assert Pipeline.run is original_run
    with installed() as (ins, _, _):
        tracing.enable_tracing(original)
        ins.uninstrument()
        assert tracing.tracer.actual_tracer is original


def test_suppression_does_not_export():
    with installed() as (_, _, exporter):
        token = context_api.attach(
            context_api.set_value(context_api._SUPPRESS_INSTRUMENTATION_KEY, True)
        )
        try:
            pipeline = Pipeline()
            pipeline.add_component("echo", Echo())
            pipeline.run({"echo": {"text": SECRET}})
        finally:
            context_api.detach(token)
        assert exporter.get_finished_spans() == ()


def test_concurrent_async_pipelines_do_not_share_parent_or_metadata():
    with installed() as (_, _, exporter):

        async def run():
            first, second = pipeline_for("async"), pipeline_for("async")
            first.add_component("echo", Echo())
            second.add_component("echo", Echo())
            await asyncio.gather(
                first.run_async({"echo": {"text": "first"}}),
                second.run_async({"echo": {"text": "second"}}),
            )

        asyncio.run(run())
        spans = exporter.get_finished_spans()
        roots = {s.context.span_id: s for s in spans if s.name == "Pipeline.run"}
        assert len(roots) == 2
        for child in (s for s in spans if s.name == "Echo.run"):
            root = roots[child.parent.span_id]
            assert (
                json.loads(child.attributes["input.value"])["text"]
                in root.attributes["input.value"]
            )
            assert child.context.trace_id == root.context.trace_id


def test_public_protocol_explicit_parent_and_unknown_content():
    with installed() as (_, provider, exporter):
        with provider.get_tracer("application").start_as_current_span(
            "parent"
        ) as parent:
            external_parent = tracing.tracer.current_span()
        with tracing.tracer.trace(
            "haystack.agent.step.tool",
            {"haystack.tool.name": "local", "unknown": SECRET},
            parent_span=external_parent,
        ) as span:
            span.set_content_tag("unknown.content", SECRET)
            span.set_tag("haystack.tool.error", True)
        tool = exporter.get_finished_spans()[-1]
        assert tool.parent.span_id == parent.get_span_context().span_id
        assert tool.attributes["gen_ai.span.kind"] == "TOOL"
        assert tool.status.status_code == trace.StatusCode.ERROR
        assert SECRET not in str(tool.attributes)


@contextmanager
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            requests.append(request)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "privacy", ["off", "inputs", "outputs", "messages", "text", "vectors", "default"]
)
def test_otlp_wire_privacy_and_semantics(privacy):
    config = {
        "inputs": TraceConfig(hide_inputs=True),
        "outputs": TraceConfig(hide_outputs=True),
        "messages": TraceConfig(hide_input_messages=True, hide_output_messages=True),
        "text": TraceConfig(hide_input_text=True, hide_output_text=True),
        "vectors": TraceConfig(hide_embedding_vectors=True),
    }.get(privacy, TraceConfig())
    with receiver() as (endpoint, requests):
        exporter = OTLPSpanExporter(endpoint=endpoint)
        with installed(
            config=config, capture_content=privacy != "default", exporter=exporter
        ) as (_, provider, _):
            pipeline = Pipeline()
            pipeline.add_component("generator", SyntheticGenerator())
            pipeline.run({"generator": {"prompt": SECRET}})
            provider.force_flush()
        spans = [
            s
            for r in requests
            for rs in r.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]
        assert len(spans) == 2
        child = next(s for s in spans if s.name == "SyntheticGenerator.run")
        root = next(s for s in spans if s.name == "Pipeline.run")
        assert child.parent_span_id == root.span_id
        assert child.trace_id == root.trace_id
        attrs = {a.key: a.value for a in child.attributes}
        assert attrs["gen_ai.request.model"].string_value == "offline-model"
        assert attrs["gen_ai.usage.total_tokens"].int_value == 5
        wire = b"".join(r.SerializeToString() for r in requests)
        if privacy in ("inputs", "messages", "text", "default"):
            assert SECRET.encode() not in wire
        if privacy in ("outputs", "messages", "text", "default"):
            assert b"answer" not in wire
        assert b"haystack.pipeline.input_data" not in wire


@pytest.mark.skipif(
    not CURRENT, reason="Agent's public tracing events were added after Haystack 2.10"
)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_real_agent_and_tool_native_spans(mode):
    from haystack.components.agents import Agent
    from haystack.dataclasses import ToolCall
    from haystack.tools import Tool

    @component
    class LocalChatGenerator:
        @component.output_types(replies=List[ChatMessage])
        def run(self, messages: List[ChatMessage], tools=None, **kwargs):
            if len(messages) == 1:
                return {
                    "replies": [
                        ChatMessage.from_assistant(
                            tool_calls=[
                                ToolCall(
                                    tool_name="echo",
                                    arguments={"text": SECRET},
                                    id="call-1",
                                )
                            ],
                            meta={
                                "finish_reason": "tool_calls",
                                "model": "offline-model",
                            },
                        )
                    ]
                }
            return {
                "replies": [
                    ChatMessage.from_assistant(
                        "done",
                        meta={
                            "finish_reason": "stop",
                            "model": "offline-model",
                            "usage": {
                                "prompt_tokens": 3,
                                "completion_tokens": 1,
                                "total_tokens": 4,
                            },
                        },
                    )
                ]
            }

        @component.output_types(replies=List[ChatMessage])
        async def run_async(self, messages: List[ChatMessage], tools=None, **kwargs):
            return self.run(messages, tools, **kwargs)

    tool = Tool(
        name="echo",
        description="Return supplied text",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        function=lambda text: text,
    )
    agent = Agent(chat_generator=LocalChatGenerator(), tools=[tool])
    with installed() as (_, _, exporter):
        if mode == "sync":
            result = agent.run(messages=[ChatMessage.from_user("call echo")])
        else:
            result = asyncio.run(
                agent.run_async(messages=[ChatMessage.from_user("call echo")])
            )
        assert result["messages"][-1].text == "done"
        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.name == "haystack.agent.run")
        assert root.attributes["gen_ai.span.kind"] == "AGENT"
        tools = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "TOOL"]
        assert len(tools) == 1
        assert tools[0].attributes["gen_ai.tool.name"] == "echo"
        assert SECRET in tools[0].attributes["input.value"]
        llms = [s for s in spans if s.name == "haystack.agent.step.llm"]
        assert len(llms) == 2
        assert llms[-1].attributes["gen_ai.usage.total_tokens"] == 4
        assert len({s.context.trace_id for s in spans}) == 1


@pytest.mark.parametrize("privacy", ["inputs", "outputs", "vectors", "default"])
def test_otlp_documents_embeddings_templates_and_errors(privacy):
    config = {
        "inputs": TraceConfig(hide_inputs=True),
        "outputs": TraceConfig(hide_outputs=True),
        "vectors": TraceConfig(hide_embedding_vectors=True),
    }.get(privacy, TraceConfig())
    with receiver() as (endpoint, requests):
        with installed(
            config=config,
            capture_content=privacy != "default",
            exporter=OTLPSpanExporter(endpoint=endpoint),
        ) as (_, provider, _):
            pipeline = Pipeline()
            pipeline.add_component("embedder", SyntheticEmbedder())
            pipeline.add_component(
                "prompt",
                PromptBuilder(
                    template=SECRET + " {{text}}", required_variables=["text"]
                ),
            )
            pipeline.add_component("retriever", SyntheticRetriever())
            pipeline.run(
                {
                    "embedder": {"text": SECRET},
                    "prompt": {"text": SECRET},
                    "retriever": {"query": SECRET},
                }
            )
            broken = Pipeline()
            broken.add_component("failure", Failure())
            with pytest.raises(Exception):
                broken.run({"failure": {"text": SECRET}})
            provider.force_flush()
        wire = b"".join(r.SerializeToString() for r in requests)
        spans = [
            s
            for r in requests
            for rs in r.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]
        attrs = {s.name: {a.key: a.value for a in s.attributes} for s in spans}
        if privacy in ("vectors", "default"):
            assert (
                "embedding.embeddings.0.embedding.vector"
                not in attrs["SyntheticEmbedder.run"]
            )
            assert b"0.25" not in wire and b"0.75" not in wire
        if privacy in ("inputs", "default"):
            assert (
                SECRET
                not in attrs["SyntheticEmbedder.run"][
                    "embedding.embeddings.0.embedding.text"
                ].string_value
            )
            assert (
                SECRET
                not in attrs["PromptBuilder.run"][
                    "gen_ai.prompt.template.name"
                ].string_value
            )
        if privacy in ("outputs", "default"):
            assert (
                SECRET
                not in attrs["SyntheticRetriever.run"][
                    "retrieval.documents.0.document.content"
                ].string_value
            )
        if privacy == "default":
            assert SECRET.encode() not in wire
        if privacy != "vectors":
            errors = [s for s in spans if s.status.code == 2]
            assert len(errors) == 2
            assert SECRET not in str([(s.status, s.events) for s in errors])


def test_generator_close_finalizes_native_root_and_clears_metadata_context():
    with installed() as (_, _, exporter):

        async def consume_one():
            pipeline = pipeline_for("generator")
            pipeline.add_component("echo", Echo())
            stream = pipeline.run_async_generator({"echo": {"text": "close"}})
            await anext(stream)
            await stream.aclose()
            assert pipeline_context.get() is None

        asyncio.run(consume_one())
        assert len(exporter.get_finished_spans()) == 2


def test_async_cancellation_finalizes_spans_and_propagates():
    @component
    class Wait:
        @component.output_types(text=str)
        def run(self, text: str):
            return {"text": text}

        @component.output_types(text=str)
        async def run_async(self, text: str):
            await asyncio.Event().wait()
            return {"text": text}

    with installed() as (_, _, exporter):

        async def cancel():
            pipeline = pipeline_for("async")
            pipeline.add_component("wait", Wait())
            task = asyncio.create_task(pipeline.run_async({"wait": {"text": SECRET}}))
            for _ in range(20):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel())
        assert len(exporter.get_finished_spans()) == 2
        assert pipeline_context.get() is None


def test_nested_pipeline_retains_provider_span_parent():
    with installed() as (_, provider, exporter):

        @component
        class Nested:
            @component.output_types(text=str)
            def run(self, text: str):
                inner = Pipeline()
                inner.add_component("echo", Echo())
                with provider.get_tracer("application").start_as_current_span("work"):
                    return inner.run({"echo": {"text": text}})["echo"]

        pipeline = Pipeline()
        pipeline.add_component("nested", Nested())
        pipeline.run({"nested": {"text": "nested"}})
        spans = exporter.get_finished_spans()
        work = next(s for s in spans if s.name == "work")
        inner = next(s for s in spans if s.name == "Pipeline.run")
        assert inner.parent.span_id == work.context.span_id
        assert len(spans) == 5


def test_repeated_instrumentation_restores_component_and_public_entrypoints():
    original_run = Pipeline.run
    original_component = Echo.run
    for _ in range(3):
        with installed() as (_, _, exporter):
            pipeline = Pipeline()
            pipeline.add_component("echo", Echo())
            pipeline.run({"echo": {"text": "cycle"}})
            assert len(exporter.get_finished_spans()) == 2
        assert Pipeline.run is original_run
        assert Echo.run is original_component


def test_semantic_extraction_errors_do_not_break_application(caplog):
    caplog.set_level(logging.DEBUG, logger="traceai_haystack._tracer")

    @component
    class BrokenMetadataGenerator:
        @component.output_types(replies=List[str], meta=List[str])
        def run(self, prompt: str):
            return {"replies": ["still works"], "meta": ["not-a-dict"]}

    with installed() as (_, _, exporter):
        pipeline = Pipeline()
        pipeline.add_component("generator", BrokenMetadataGenerator())
        assert pipeline.run({"generator": {"prompt": "question"}})["generator"][
            "replies"
        ] == ["still works"]
        assert len(exporter.get_finished_spans()) == 2

    assert "Unable to extract Haystack span attributes" in caplog.text
    assert "AttributeError; frames=" in caplog.text
    assert "not-a-dict" not in caplog.text


def test_chat_replies_without_finish_reason_and_multiple_text_replies():
    from traceai_haystack._semantics import _get_llm_output_message_attributes

    attrs = dict(
        _get_llm_output_message_attributes(
            {"replies": [ChatMessage.from_assistant("answer")]}
        )
    )
    assert attrs["gen_ai.output.messages.0.message.content"] == "answer"
    attrs = dict(_get_llm_output_message_attributes({"replies": ["one", "two"]}))
    assert attrs["gen_ai.output.messages.1.message.content"] == "two"


def test_document_vectors_cannot_leak_through_aggregate_io():
    from traceai_haystack._semantics import _get_input_attributes

    doc = Document(content="local", embedding=[0.1234567, 0.9876543])
    attrs = dict(_get_input_attributes({"pipeline": {"documents": [doc]}}))
    assert "0.1234567" not in attrs["input.value"]
    assert "0.9876543" not in attrs["input.value"]
