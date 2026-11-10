"""Offline regressions for credentials, selective privacy and native lifecycles."""

import asyncio
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import dspy
import numpy as np
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_dspy import DSPyInstrumentor

HAS_CALLBACKS = importlib.util.find_spec("dspy.utils.callback") is not None
callbacks_only = pytest.mark.skipif(not HAS_CALLBACKS, reason="DSPy predates callbacks")
async_embeddings = pytest.mark.skipif(
    not hasattr(getattr(dspy, "Embedder", None), "acall"),
    reason="DSPy has no asynchronous Embedder",
)


@pytest.fixture
def setup():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = list(dspy.settings.get("callbacks", []))
    token = context.attach(context.Context())
    instrumentor = DSPyInstrumentor()
    yield instrumentor, provider, exporter
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    if HAS_CALLBACKS:
        dspy.configure(callbacks=original)
    context.detach(token)
    provider.shutdown()


def local_completion():
    from litellm import ModelResponse

    return ModelResponse(
        model="openai/gpt-4o-mini",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "PRIVATE_ANSWER"},
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )


def assert_not_exported(exporter, *markers):
    leaked = [
        key
        for span in exporter.get_finished_spans()
        for key, value in span.attributes.items()
        if any(marker in str(value) for marker in markers)
    ]
    assert not leaked, f"Private values exported in: {leaked}"


@pytest.mark.parametrize("source", ["constructor", "call"])
def test_lm_credentials_are_not_exported(setup, source):
    instrumentor, provider, exporter = setup
    instrumentor.instrument(tracer_provider=provider)
    credentials = {
        "api_key": "SYNTHETIC_PROVIDER_CREDENTIAL",
        "extra_headers": {"Authorization": "Bearer SYNTHETIC_AUTHORIZATION"},
    }
    original = json.dumps(credentials, sort_keys=True)
    lm = dspy.LM(
        "openai/gpt-4o-mini",
        engine="litellm",
        cache=False,
        **(credentials if source == "constructor" else {}),
    )
    with patch("litellm.completion", return_value=local_completion()) as transport:
        assert lm(
            prompt="PRIVATE_QUESTION",
            temperature=0.25,
            **(credentials if source == "call" else {}),
        ) == ["PRIVATE_ANSWER"]
    assert transport.call_count == 1
    # Telemetry must not mutate or remove credentials from the actual request.
    assert transport.call_args.kwargs["api_key"] == credentials["api_key"]
    assert transport.call_args.kwargs["extra_headers"] == credentials["extra_headers"]
    assert json.dumps(credentials, sort_keys=True) == original
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert (
        json.loads(spans[0].attributes["gen_ai.request.parameters"])["temperature"]
        == 0.25
    )
    assert_not_exported(
        exporter, "SYNTHETIC_PROVIDER_CREDENTIAL", "SYNTHETIC_AUTHORIZATION"
    )


@pytest.mark.parametrize(
    "flag,marker",
    [
        ("hide_input_messages", "PRIVATE_QUESTION"),
        ("hide_input_text", "PRIVATE_QUESTION"),
        ("hide_output_messages", "PRIVATE_ANSWER"),
        ("hide_output_text", "PRIVATE_ANSWER"),
        ("hide_llm_invocation_parameters", "PRIVATE_STOP_VALUE"),
    ],
)
@pytest.mark.parametrize("via_environment", [False, True])
def test_selective_privacy_flags_do_not_leak_raw_copies(
    setup, monkeypatch, flag, marker, via_environment
):
    instrumentor, provider, exporter = setup
    if via_environment:
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        config = TraceConfig()
    else:
        config = TraceConfig(**{flag: True})
    instrumentor.instrument(tracer_provider=provider, config=config)
    with patch("litellm.completion", return_value=local_completion()):
        assert dspy.LM("openai/gpt-4o-mini", engine="litellm", cache=False)(
            prompt="PRIVATE_QUESTION", stop=["PRIVATE_STOP_VALUE"]
        ) == ["PRIVATE_ANSWER"]
    assert len(exporter.get_finished_spans()) == 1
    assert_not_exported(exporter, marker)


@callbacks_only
@pytest.mark.parametrize(
    "operation",
    ["python_interpreter", "local_interpreter", "typesafe", "async_typesafe"],
)
@pytest.mark.parametrize("placement", ["before", "after", "instance"])
def test_all_native_callback_contexts_restore(setup, operation, placement):
    from dspy.utils.callback import BaseCallback

    if not hasattr(BaseCallback, "on_interpreter_shutdown_start"):
        pytest.skip("DSPy predates interpreter/TypeSafe callbacks")
    instrumentor, provider, exporter = setup
    tracer = provider.get_tracer("application")
    pending = {}

    class UserCallback(BaseCallback):
        def start(self, call_id, instance, inputs):
            span = tracer.start_span("user-callback")
            pending[call_id] = (span, context.attach(trace.set_span_in_context(span)))

        def end(self, call_id, outputs, exception=None):
            span, token = pending.pop(call_id)
            context.detach(token)
            span.end()

        on_interpreter_shutdown_start = on_lm_start = start
        on_interpreter_shutdown_end = on_lm_end = end

    user = UserCallback()
    dspy.configure(callbacks=[user] if placement == "before" else [])
    instrumentor.instrument(tracer_provider=provider)
    if placement == "after":
        dspy.configure(callbacks=[*dspy.settings.callbacks, user])
    if operation == "python_interpreter":
        instance = dspy.PythonInterpreter(deno_command=["/usr/bin/false"])
        invoke = instance.shutdown  # Unstarted instance: no subprocess is launched.
    elif operation == "local_interpreter":
        instance = dspy.LocalInterpreter()
        invoke = instance.shutdown
    else:
        from dspy.clients.typesafe import TypeSafe

        instance = TypeSafe(model="synthetic-local", api_key="synthetic")
        response = {
            "model": "synthetic-local",
            "usage": {},
            "answers": {"local": {"choice": "yes"}},
        }

        async def ainvoke():
            before = context.get_current()
            result = await instance.acall(state="local", questions={})
            assert context.get_current() is before
            return result

        def invoke():
            with patch.object(dspy.cache, "get", return_value=response):
                if operation == "async_typesafe":
                    return asyncio.run(ainvoke())
                return instance(state="local", questions={})

    if placement == "instance":
        instance.callbacks = [user]
    with tracer.start_as_current_span("application") as parent:
        before = context.get_current()
        invoke()
        assert not pending
        assert not instrumentor._callback._calls
        assert any(
            "gen_ai.span.kind" in s.attributes for s in exporter.get_finished_spans()
        )
        assert context.get_current() is before
        with tracer.start_as_current_span("unrelated") as sibling:
            assert sibling.parent.span_id == parent.get_span_context().span_id


@callbacks_only
def test_registration_failure_rolls_back_guards(setup):
    settings_module = importlib.import_module(type(dspy.settings).__module__)
    if not hasattr(settings_module, "config_owner_thread_id"):
        pytest.skip("DSPy predates configure thread ownership")
    instrumentor, provider, _ = setup
    dspy.configure(callbacks=[])
    originals = {
        (cls, method): vars(cls).get(method)
        for cls in (dspy.Module, dspy.Adapter, dspy.BaseLM)
        for method in ("__call__", "__init_subclass__")
    }
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(RuntimeError, match="thread|task"):
                pool.submit(instrumentor.instrument, tracer_provider=provider).result()
        assert not instrumentor.is_instrumented_by_opentelemetry
        for (cls, method), original in originals.items():
            assert vars(cls).get(method) is original
        instrumentor.instrument(tracer_provider=provider)
        instrumentor.uninstrument()
        for (cls, method), original in originals.items():
            assert vars(cls).get(method) is original

        class LateAdapter(dspy.Adapter):
            def format(self, signature, demos, inputs):
                return []

            def parse(self, signature, completion):
                return {}

        assert not instrumentor._patches
    finally:
        # Also restore a broken pre-fix attempt so it cannot poison other tests.
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor._uninstrument()


def test_partial_install_failure_rolls_back_guards(setup):
    instrumentor, provider, _ = setup
    original_patch = instrumentor._patch
    originals = []

    def fail_after_patching(cls, method, wrapper):
        originals.append((cls, method, vars(cls).get(method)))
        original_patch(cls, method, wrapper)
        raise RuntimeError("synthetic installation failure")

    original_callbacks = list(dspy.settings.get("callbacks", []))
    with patch.object(instrumentor, "_patch", side_effect=fail_after_patching):
        with pytest.raises(RuntimeError, match="synthetic installation failure"):
            instrumentor.instrument(tracer_provider=provider)
    assert not instrumentor.is_instrumented_by_opentelemetry
    assert not instrumentor._patches
    assert not instrumentor._wrappers
    assert instrumentor._callback is None
    assert dspy.settings.get("callbacks", []) == original_callbacks
    for cls, method, original in originals:
        assert vars(cls).get(method) is original
    instrumentor.instrument(tracer_provider=provider)
    instrumentor.uninstrument()


@async_embeddings
def test_async_embeddings_have_a_span_and_restore(setup):
    instrumentor, provider, exporter = setup
    original = dspy.Embedder.acall
    instrumentor.instrument(tracer_provider=provider)
    embedder = dspy.Embedder(
        lambda inputs: np.array([[1.0, 2.0] for _ in inputs]), caching=False
    )
    embedder(inputs="synthetic")
    assert len(exporter.get_finished_spans()) == 1
    exporter.clear()
    captured = embedder.acall
    with provider.get_tracer("application").start_as_current_span("parent") as parent:
        result = asyncio.run(captured(inputs="synthetic"))
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].parent.span_id == parent.get_span_context().span_id
    assert result.tolist() == [1.0, 2.0]
    assert spans[0].attributes["gen_ai.span.kind"] == "EMBEDDING"
    instrumentor.uninstrument()
    assert dspy.Embedder.acall is original
    exporter.clear()
    asyncio.run(captured(inputs="after removal"))
    assert not exporter.get_finished_spans()


@async_embeddings
@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_outputs", "hide_input_text", "hide_embedding_vectors"]
)
def test_async_embeddings_privacy(setup, flag):
    instrumentor, provider, exporter = setup
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(**{flag: True})
    )
    embedder = dspy.Embedder(
        lambda inputs, **kwargs: np.array([[0.123456, 0.234567] for _ in inputs]),
        caching=False,
    )
    asyncio.run(
        embedder.acall(inputs="PRIVATE_EMBEDDING_TEXT", api_key="SYNTHETIC_EMBED_KEY")
    )
    assert len(exporter.get_finished_spans()) == 1
    assert_not_exported(exporter, "SYNTHETIC_EMBED_KEY")
    assert_not_exported(
        exporter,
        "PRIVATE_EMBEDDING_TEXT"
        if flag in ("hide_inputs", "hide_input_text")
        else "0.123456",
    )


@async_embeddings
def test_async_embeddings_suppression_error_and_cancellation(setup):
    instrumentor, provider, exporter = setup
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(hide_error_details=True)
    )
    embedder = dspy.Embedder(lambda inputs: [[1.0]], caching=False)
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        asyncio.run(embedder.acall(inputs="suppressed"))
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()

    async def failing(*args, **kwargs):
        raise ValueError("PRIVATE_ERROR")

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError("PRIVATE_ERROR")

    for error, function in ((ValueError, failing), (asyncio.CancelledError, cancelled)):

        async def run():
            before = context.get_current()
            with patch("dspy.clients.embedding._acompute_embeddings", function):
                with pytest.raises(error):
                    await embedder.acall(inputs="local")
            assert context.get_current() is before

        exporter.clear()
        asyncio.run(run())
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        assert "PRIVATE_ERROR" not in str(spans[0].events)
