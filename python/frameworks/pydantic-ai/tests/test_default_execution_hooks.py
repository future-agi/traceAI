"""T5-PY-004: default registration must trace execution, not just Agent.run.

These SDK-shaped fakes exercise the public Model request methods and the
FunctionToolset.call_tool (1.0) / Tool.run (historical) dispatch boundaries.
No provider, credentials, native instrument_all setting, or network is used.
"""

import asyncio
import inspect
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_pydantic_ai import PydanticAIInstrumentor


@pytest.fixture(params=["modern", "legacy"])
def sdk(request, monkeypatch):
    modern = request.param == "modern"

    class ModelRetry(Exception):
        pass

    class Model:
        model_name = "local-test-model"
        system = "test"

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            cls.subclass_initialized = True

        async def request(self, messages, model_settings, model_request_parameters):
            raise NotImplementedError

    class LocalModel(Model):
        def __init__(self):
            self.calls = []
            self.responses = []
            self.failure = None

        async def request(self, messages, model_settings, model_request_parameters):
            self.calls.append((messages, model_settings, model_request_parameters))
            if self.failure is not None:
                raise self.failure
            response = SimpleNamespace(
                kind="response", output="done",
                usage=SimpleNamespace(input_tokens=2, output_tokens=1, total_tokens=3),
            )
            self.responses.append(response)
            return response

        @asynccontextmanager
        async def request_stream(self, messages, model_settings, model_request_parameters):
            self.stream_entered = True
            stream = SimpleNamespace(usage=lambda: SimpleNamespace(
                input_tokens=2, output_tokens=1, total_tokens=3))
            self.stream = stream
            try:
                yield stream
            finally:
                self.stream_exited = True

    class FunctionToolset:
        async def call_tool(self, name, tool_args, ctx, tool):
            result = tool.function(**tool_args)
            return await result if inspect.isawaitable(result) else result

    class Tool:
        def __init__(self, function):
            self.function = function
            self.name = "local_tool"
            self.description = "Return the supplied value."
            self.current_retry = 0

        async def run(self, message, run_context):
            try:
                result = self.function(**message.args)
                if inspect.isawaitable(result):
                    result = await result
                return SimpleNamespace(part_kind="tool-return", content=result)
            except ModelRetry as exc:
                self.current_retry += 1
                return SimpleNamespace(part_kind="retry-prompt", content=str(exc))

    class Agent:
        def __init__(self, model, function=None):
            self.model = model
            self.tool = Tool(function) if function else None
            self.result = SimpleNamespace(output="done", new_messages=lambda: model.responses)

        async def run(self, user_prompt):
            # Disabled native tracing may temporarily install an invalid span.
            # Child hooks must still use the traceAI run as their parent.
            with trace.use_span(trace.INVALID_SPAN):
                await self.model.request([user_prompt], None, None)
                if self.tool:
                    for retry in range(2):
                        ctx = SimpleNamespace(retry=retry)
                        try:
                            if modern:
                                self.tool_result = await FunctionToolset().call_tool(
                                    name="local_tool", tool_args={"value": 7},
                                    ctx=ctx, tool=self.tool,
                                )
                            else:
                                part = await self.tool.run(
                                    SimpleNamespace(args={"value": 7}), ctx)
                                if part.part_kind == "retry-prompt":
                                    raise ModelRetry(part.content)
                                self.tool_result = part.content
                            break
                        except ModelRetry:
                            await self.model.request(["retry"], None, None)
                    await self.model.request(["tool result"], None, None)
            return self.result

        def run_sync(self, user_prompt):
            return asyncio.run(self.run(user_prompt))

    package = ModuleType("pydantic_ai")
    package.__path__ = []
    package.Agent = Agent
    models = ModuleType("pydantic_ai.models")
    models.Model = Model
    usage = ModuleType("pydantic_ai.usage")
    # Keep these tests on response-local accounting in both dispatch variants.
    usage.RequestUsage = SimpleNamespace
    package.models, package.usage = models, usage
    toolsets = ModuleType("pydantic_ai.toolsets")
    toolsets.__path__ = []
    function = ModuleType("pydantic_ai.toolsets.function")
    function.FunctionToolset = FunctionToolset
    tools = ModuleType("pydantic_ai.tools")
    tools.Tool = Tool
    for name, module in {
        "pydantic_ai": package, "pydantic_ai.models": models,
        "pydantic_ai.usage": usage, "pydantic_ai.toolsets": toolsets,
        "pydantic_ai.toolsets.function": function if modern else None,
        "pydantic_ai.tools": tools,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    # Isolate BaseInstrumentor's singleton and enable through the public API.
    monkeypatch.setattr(PydanticAIInstrumentor, "_instance", None)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented", False)
    instrumentor = PydanticAIInstrumentor()
    monkeypatch.setattr(instrumentor, "instrumentation_dependencies", lambda: [])
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def enable():
        instrumentor.instrument(tracer_provider=provider)

    try:
        yield SimpleNamespace(
            Agent=Agent, Model=Model, LocalModel=LocalModel, Tool=Tool,
            FunctionToolset=FunctionToolset, ModelRetry=ModelRetry,
            instrumentor=instrumentor, exporter=exporter, enable=enable,
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def execution_spans(sdk):
    spans = sdk.exporter.get_finished_spans()
    runs = [span for span in spans if span.name == "pydantic_ai.agent.run"]
    tools = [span for span in spans if span.name.startswith("pydantic_ai.tool.call.")]
    models = [span for span in spans if span.name == "pydantic_ai.model.request"]
    return runs, tools, models


@pytest.mark.parametrize("method", ["run", "run_sync"])
@pytest.mark.parametrize("async_tool", [False, True])
def test_default_registration_traces_actual_tools_and_each_request(sdk, method, async_tool):
    calls = []
    returned = {"answer": 7}

    def local_tool(value):
        calls.append(value)
        return returned

    async def async_local_tool(value):
        return local_tool(value)

    # Existing models and tools must be covered without re-registration.
    model = sdk.LocalModel()
    agent = sdk.Agent(model, async_local_tool if async_tool else local_tool)
    sdk.enable()
    result = (asyncio.run(agent.run("use tool")) if method == "run"
              else agent.run_sync("use tool"))
    assert result is agent.result
    assert agent.tool_result is returned
    assert calls == [7]
    assert model.calls == [(["use tool"], None, None), (["tool result"], None, None)]
    runs, tools, models = execution_spans(sdk)
    assert len(runs) == 1
    assert len(tools) == 1  # RED: the default instrumentor installs no tool hook.
    assert len(models) == 2
    for child in tools + models:
        assert child.parent.span_id == runs[0].context.span_id
        assert child.context.trace_id == runs[0].context.trace_id
        assert child.status.status_code.name == "OK"
    assert tools[0].attributes["pydantic_ai.tool.name"] == "local_tool"
    assert tools[0].attributes["pydantic_ai.tool.args"] == '{"value": 7}'
    assert tools[0].attributes["pydantic_ai.tool.result"] == '{"answer": 7}'
    assert all(span.attributes["gen_ai.request.model"] == "local-test-model" for span in models)
    assert all(span.attributes["gen_ai.usage.input_tokens"] == 2 for span in models)


def test_caught_model_retry_keeps_success_and_records_each_attempt(sdk):
    calls = []
    retry = sdk.ModelRetry("try once more")

    def local_tool(value):
        calls.append(value)
        if len(calls) == 1:
            raise retry
        return value

    agent = sdk.Agent(sdk.LocalModel(), local_tool)
    sdk.enable()
    assert agent.run_sync("retry") is agent.result
    assert calls == [7, 7]
    runs, tools, models = execution_spans(sdk)
    assert len(runs) == 1
    assert runs[0].status.status_code.name == "OK"
    assert len(models) == 3
    assert len(tools) == 2
    assert tools[0].attributes["pydantic_ai.tool.is_error"] is True
    assert tools[0].attributes["pydantic_ai.tool.error_message"] == str(retry)
    assert tools[0].status.status_code.name == "ERROR"
    assert tools[1].attributes["pydantic_ai.tool.is_error"] is False
    assert tools[1].attributes["pydantic_ai.tool.retry_count"] == 1
    assert tools[1].status.status_code.name == "OK"
    assert all(span.parent.span_id == runs[0].context.span_id for span in tools + models)


@pytest.mark.parametrize("failure_site", ["tool", "model"])
def test_execution_errors_preserve_exception_identity(sdk, failure_site):
    error = ValueError("local execution failed")

    def fail(value):
        raise error

    model = sdk.LocalModel()
    if failure_site == "model":
        model.failure = error
    agent = sdk.Agent(model, fail if failure_site == "tool" else None)
    sdk.enable()
    with pytest.raises(ValueError) as caught:
        agent.run_sync("fail")
    assert caught.value is error
    runs, tools, models = execution_spans(sdk)
    child = tools if failure_site == "tool" else models
    assert len(child) == 1
    assert child[0].status.status_code.name == "ERROR"
    assert child[0].events[0].name == "exception"
    assert child[0].parent.span_id == runs[0].context.span_id
    assert runs[0].status.status_code.name == "ERROR"


def test_late_model_classes_and_teardown_are_not_missed(sdk):
    original_request = sdk.LocalModel.request
    original_stream = sdk.LocalModel.request_stream
    original_init_subclass = sdk.Model.__dict__["__init_subclass__"]
    original_tool = sdk.FunctionToolset.call_tool
    original_legacy_tool = sdk.Tool.run
    sdk.enable()

    async def request(self, messages, model_settings, model_request_parameters):
        # Super delegation is still one model request, not two child spans.
        return await super(LateModel, self).request(
            messages, model_settings, model_request_parameters)

    LateModel = type("LateModel", (sdk.LocalModel,), {"request": request})
    assert LateModel.subclass_initialized
    agent = sdk.Agent(LateModel())
    assert agent.run_sync("late") is agent.result
    assert len(execution_spans(sdk)[2]) == 1
    sdk.instrumentor.uninstrument()
    assert sdk.LocalModel.request is original_request
    assert sdk.LocalModel.request_stream is original_stream
    assert sdk.Model.__dict__["__init_subclass__"] is original_init_subclass
    assert LateModel.request is request
    assert sdk.FunctionToolset.call_tool is original_tool
    assert sdk.Tool.run is original_legacy_tool
    sdk.exporter.clear()
    agent.run_sync("not traced")
    assert sdk.exporter.get_finished_spans() == ()
    sdk.enable()
    agent.run_sync("traced again")
    assert len(execution_spans(sdk)[2]) == 1


def test_request_preserves_arguments_and_response_identity(sdk):
    sdk.enable()
    model = sdk.LocalModel()
    messages, options, parameters = [], {}, object()
    response = asyncio.run(model.request(
        messages, model_settings=options, model_request_parameters=parameters))
    assert response is model.responses[0]
    observed = model.calls[0]
    assert observed[0] is messages
    assert observed[1] is options
    assert observed[2] is parameters
    assert len(execution_spans(sdk)[2]) == 1


def test_model_without_own_subclass_hook_is_restored(sdk):
    del sdk.Model.__init_subclass__
    sdk.enable()

    class LateModel(sdk.LocalModel):
        async def request(self, *args, **kwargs):
            return await super().request(*args, **kwargs)

    agent = sdk.Agent(LateModel())
    assert agent.run_sync("late") is agent.result
    assert len(execution_spans(sdk)[2]) == 1
    sdk.instrumentor.uninstrument()
    assert "__init_subclass__" not in sdk.Model.__dict__


def test_stream_request_keeps_context_manager_and_result_identity(sdk):
    model = sdk.LocalModel()
    sdk.enable()

    async def execute():
        async with model.request_stream([], None, None) as stream:
            assert stream is model.stream
            assert model.stream_entered
            assert not sdk.exporter.get_finished_spans()
        assert model.stream_exited

    asyncio.run(execute())
    models = execution_spans(sdk)[2]
    assert len(models) == 1
    assert models[0].attributes["gen_ai.usage.total_tokens"] == 3
    assert models[0].status.status_code.name == "OK"


@pytest.mark.parametrize("failure", [False, True])
def test_stream_manager_can_enter_and_exit_in_different_tasks(sdk, failure):
    model = sdk.LocalModel()
    sdk.enable()
    error = ValueError("consumer failed") if failure else None

    async def execute():
        previous = trace.get_current_span()
        manager = model.request_stream([], None, None)
        stream = await asyncio.create_task(manager.__aenter__())
        assert stream is model.stream
        assert model.stream_entered
        assert trace.get_current_span() is previous
        assert sdk.exporter.get_finished_spans() == ()
        suppressed = await asyncio.create_task(manager.__aexit__(
            type(error) if error else None, error, None))
        assert not suppressed
        assert model.stream_exited
        assert trace.get_current_span() is previous

    asyncio.run(execute())
    span, = execution_spans(sdk)[2]
    assert span.status.status_code.name == ("ERROR" if failure else "OK")
    if error:
        assert span.attributes["pydantic_ai.error.message"] == str(error)
    else:
        assert span.attributes["gen_ai.usage.total_tokens"] == 3
