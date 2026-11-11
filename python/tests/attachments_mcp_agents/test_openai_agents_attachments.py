"""Real OpenAI Agents SDK Runner (scripted local Model, real ComputerTool and function tools) -> loopback F01."""

import asyncio
import json
from importlib.metadata import version

import pytest

agents = pytest.importorskip("agents")
pytest.importorskip("traceai_openai_agents")

from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from packaging.version import Version  # noqa: E402
from traceai_openai_agents import OpenAIAgentsInstrumentor  # noqa: E402

from _harness import data_uri, flush, png, refs  # noqa: E402

SDK = Version(version("openai-agents"))
COMPUTER_SPAN = SDK >= Version("0.10.0")  # the SDK records computer calls on a function span from 0.10.0
TYPED_IMAGE = SDK >= Version("0.4.0")  # ToolOutputImage exists from 0.4.0
SHOT = png(seed=41)
TOOL_IMAGE = png(seed=42)


class ScriptedModel(agents.Model):
    """Replays model outputs; no network."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        from agents.usage import Usage

        self.inputs.append(kwargs.get("input", args[1] if len(args) > 1 else None))
        output = self.turns.pop(0)
        fields = dict(output=output, usage=Usage(), response_id=None)
        try:
            return agents.ModelResponse(**fields)
        except TypeError:
            return agents.ModelResponse(output=output, usage=Usage(), response_id=None, request_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def message(text="done"):
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    return ResponseOutputMessage(id="m1", type="message", role="assistant", status="completed",
                                 content=[ResponseOutputText(type="output_text", text=text, annotations=[])])


def computer_call(call_id="call_shot"):
    from openai.types.responses import ResponseComputerToolCall

    return ResponseComputerToolCall.model_validate({
        "id": "cc1", "type": "computer_call", "call_id": call_id, "status": "completed",
        "pending_safety_checks": [], "action": {"type": "screenshot"}})


def function_call(name, call_id="call_fn"):
    from openai.types.responses import ResponseFunctionToolCall

    return ResponseFunctionToolCall(id="fc1", type="function_call", call_id=call_id, name=name, arguments="{}")


class Screen(agents.AsyncComputer):
    shots = 0

    @property
    def environment(self):
        return "browser"

    @property
    def dimensions(self):
        return (1024, 768)

    async def screenshot(self):
        Screen.shots += 1
        return data_uri("image/png", SHOT).split(",", 1)[1]

    async def click(self, x, y, button):
        return None

    async def double_click(self, x, y):
        return None

    async def scroll(self, x, y, scroll_x, scroll_y):
        return None

    async def type(self, text):
        return None

    async def wait(self):
        return None

    async def move(self, x, y):
        return None

    async def keypress(self, keys):
        return None

    async def drag(self, path):
        return None


@pytest.fixture
def traced():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    state = {}

    def install(manager, config=None):
        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider, attachments=manager,
                                              config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    agents.set_trace_processors([])
    provider.shutdown()


def run_computer(manager, install, config=None):
    exporter = install(manager, config)
    model = ScriptedModel([[computer_call()], [message()]])
    agent = agents.Agent(name="operator", model=model, tools=[agents.ComputerTool(computer=Screen())])
    result = asyncio.run(agents.Runner.run(agent, "take a screenshot"))
    return exporter, model, result


def function_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.attributes.get("gen_ai.span.kind") == "TOOL"]


def test_computer_screenshot_uploads_on_the_computer_tool_span(f01, traced):
    manager = f01.manager()
    exporter, model, result = run_computer(manager, traced)
    flush(manager)
    assert result.final_output == "done"
    if not COMPUTER_SPAN:
        assert f01.uploads == []  # no function span records the computer call before 0.10.0
        return
    assert [(u["mime_type"], u["direction"], u["purpose"]) for u in f01.uploads] == [
        ("image/png", "output", "computer_screenshot")]
    assert f01.payloads == [SHOT]
    tool = [s for s in function_spans(exporter) if refs(s)]
    assert len(tool) == 1
    output = tool[0].attributes["output.value"]
    assert output.startswith("fi-attachment:") and output[len("fi-attachment:"):] == refs(tool[0])[0]["occurrence_id"]
    # The SDK still sent the original screenshot to the model.
    sent = json.dumps(model.inputs[-1], default=str)
    assert data_uri("image/png", SHOT) in sent
    manager.shutdown(5)


@pytest.mark.skipif(not TYPED_IMAGE, reason="ToolOutputImage needs openai-agents 0.4.0")
def test_function_tool_typed_image_uploads_and_url_or_file_id_never_fetch(f01, traced):
    manager = f01.manager()
    exporter = traced(manager)

    @agents.function_tool
    def chart() -> list:
        """Draw a chart."""
        return [agents.ToolOutputImage(image_url=data_uri("image/png", TOOL_IMAGE)),
                agents.ToolOutputImage(image_url="https://example.invalid/chart.png"),
                agents.ToolOutputImage(file_id="file-123")]

    model = ScriptedModel([[function_call("chart")], [message()]])
    agent = agents.Agent(name="analyst", model=model, tools=[chart])
    asyncio.run(agents.Runner.run(agent, "chart it"))
    flush(manager)
    assert [(u["mime_type"], u["purpose"]) for u in f01.uploads] == [("image/png", "tool_result")]
    assert f01.payloads == [TOOL_IMAGE]
    span = [s for s in function_spans(exporter) if refs(s)][0]
    assert "https://example.invalid/chart.png" in span.attributes["output.value"]
    assert data_uri("image/png", TOOL_IMAGE) not in span.attributes["output.value"]
    manager.shutdown(5)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs", "hide_input_images", "pii_redaction"])
def test_local_restrictions_deny_the_screenshot(f01, traced, flag):
    manager = f01.manager()
    before = len(f01.after_bootstrap())
    run_computer(manager, traced, TraceConfig(**{flag: True}))
    flush(manager)
    assert f01.uploads == [] and len(f01.after_bootstrap()) == before
    manager.shutdown(5)


def test_disabled_manager_keeps_the_old_attributes(f01, traced):
    exporter, _, result = run_computer(create_attachment_manager(config=AttachmentConfig()), traced)
    assert result.final_output == "done" and f01.uploads == []
    if COMPUTER_SPAN:
        outputs = [s.attributes.get("output.value") for s in function_spans(exporter)]
        assert data_uri("image/png", SHOT) in outputs


def test_sensitive_data_off_records_and_uploads_nothing(f01, traced):
    manager = f01.manager()
    traced(manager)
    model = ScriptedModel([[computer_call()], [message()]])
    agent = agents.Agent(name="operator", model=model, tools=[agents.ComputerTool(computer=Screen())])
    asyncio.run(agents.Runner.run(agent, "shot", run_config=agents.RunConfig(trace_include_sensitive_data=False)))
    flush(manager)
    assert f01.uploads == []
    manager.shutdown(5)
