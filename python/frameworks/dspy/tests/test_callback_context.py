"""Application OTel contexts must survive DSPy's FIFO callback teardown."""

import asyncio
import importlib.util
import os

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import dspy
import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_dspy import DSPyInstrumentor

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("dspy.utils.callback") is None,
    reason="DSPy 2.5.1 predates callbacks",
)


@pytest.mark.parametrize("placement", ["before", "after", "instance"])
@pytest.mark.parametrize(
    "operation",
    [
        "module",
        "async_module",
        "tool",
        "adapter",
        "lm",
        "custom_adapter",
        "custom_lm",
        "compile",
    ],
)
@pytest.mark.parametrize("fail", [False, True])
def test_user_callback_context_is_restored(placement, operation, fail):
    from dspy.utils.callback import BaseCallback, with_callbacks
    from dspy.utils.dummies import DummyLM

    if operation == "compile" and not hasattr(BaseCallback, "on_compile_start"):
        pytest.skip("DSPy has no compile callbacks")

    if operation == "async_module" and not hasattr(dspy.Module, "acall"):
        pytest.skip("DSPy has no acall")
    if operation == "tool" and placement == "instance":
        pytest.skip("DSPy Tool has no instance-level callbacks API")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("application")
    pending = {}
    user_events = []

    class UserCallback(BaseCallback):
        def start(self, call_id, instance, inputs):
            span = tracer.start_span("user-callback")
            token = context.attach(trace.set_span_in_context(span))
            pending[call_id] = (span, token)
            user_events.append("start")

        def end(self, call_id, outputs, exception=None):
            span, token = pending.pop(call_id)
            context.detach(token)
            span.end()
            user_events.append("end")

        on_module_start = on_tool_start = on_adapter_parse_start = on_lm_start = start
        on_module_end = on_tool_end = on_adapter_parse_end = on_lm_end = end
        on_compile_start = start
        on_compile_end = end

    def local_work():
        with tracer.start_as_current_span("body") as span:
            # The context guard must not detach tracing during the actual call.
            assert span.parent is not None
        if fail:
            raise ValueError("synthetic failure")
        return "local"

    class Local(dspy.Module):
        def forward(self, question):
            return dspy.Prediction(answer=local_work())

        async def aforward(self, question):
            await asyncio.sleep(0)
            return self.forward(question)

    original_callbacks = list(dspy.settings.callbacks)
    outer_token = context.attach(context.Context())
    instrumentor = DSPyInstrumentor()
    user = UserCallback()
    try:
        dspy.configure(callbacks=[user] if placement == "before" else [])
        instrumentor.instrument(tracer_provider=provider)
        if placement == "after":
            dspy.configure(callbacks=[*dspy.settings.callbacks, user])
        if operation in ("module", "async_module"):
            instance = Local()
            invoke = lambda: instance(question="offline")
            async_contexts = []
            if operation == "async_module":

                async def run():
                    # Observe inside the task, before asyncio.run resets it.
                    before = context.get_current()
                    try:
                        return await instance.acall(question="offline")
                    finally:
                        async_contexts.append(context.get_current() is before)

                invoke = lambda: asyncio.run(run())
        elif operation == "tool":
            instance = dspy.Tool(local_work)
            invoke = lambda: instance()
        elif operation == "adapter":
            instance = dspy.ChatAdapter()
            signature = dspy.Signature("question -> answer")
            completion = (
                "invalid"
                if fail
                else "[[ ## answer ## ]]\nlocal\n[[ ## completed ## ]]"
            )
            invoke = lambda: instance.parse(signature, completion)
        elif operation == "custom_adapter":
            # Define overrides AFTER instrument(), as in customer setup snippets.
            class CustomAdapter(dspy.Adapter):
                def parse(self, signature, completion):
                    return {"answer": local_work()}

            instance = CustomAdapter()
            invoke = lambda: instance.parse(
                dspy.Signature("question -> answer"), "local"
            )
        elif operation == "custom_lm":

            class CustomLM(dspy.BaseLM):
                @with_callbacks
                def __call__(self, prompt=None, messages=None, **kwargs):
                    return [local_work()]

            instance = CustomLM("offline/model")
            invoke = lambda: instance(prompt="offline")
        elif operation == "compile":
            from dspy.teleprompt.teleprompt import Teleprompter

            class CustomOptimizer(Teleprompter):
                def compile(self, student, **kwargs):
                    local_work()
                    return student

            instance = CustomOptimizer()
            invoke = lambda: instance.compile(Local())
        else:
            instance = DummyLM([{"answer": "local"}, {"answer": "local"}])
            if fail:
                # Invalid local answers fail without contacting a provider.
                instance.answers = iter(["invalid", "invalid"])
            invoke = lambda: instance(prompt="offline")
        if placement == "instance":
            instance.callbacks = [user]

        with tracer.start_as_current_span("application") as parent:
            before = context.get_current()
            for _ in range(2):
                if fail:
                    message = {"adapter": "answer", "lm": "items"}.get(
                        operation, "synthetic failure"
                    )
                    with pytest.raises(Exception, match=message):
                        invoke()
                else:
                    invoke()
                assert not pending
                if operation == "async_module":
                    assert async_contexts and all(async_contexts)
                assert context.get_current() is before
                with tracer.start_as_current_span("unrelated") as sibling:
                    assert sibling.parent.span_id == parent.get_span_context().span_id
            assert user_events == ["start", "end", "start", "end"]
        assert not trace.get_current_span().get_span_context().is_valid
        assert not instrumentor._callback._calls
        own_spans = [
            s
            for s in exporter.get_finished_spans()
            if "gen_ai.span.kind" in s.attributes
        ]
        assert len(own_spans) == 2
    finally:
        patches = list(instrumentor._patches)
        instrumentor.uninstrument()
        dspy.configure(callbacks=original_callbacks)
        context.detach(outer_token)
        provider.shutdown()
        # Include dynamically created subclasses, not just the native classes
        # that existed at registration time. No hook or guard may stay owned.
        for cls, method, original, _, owned in patches:
            if owned:
                assert cls.__dict__[method] is original
            else:
                assert method not in cls.__dict__
