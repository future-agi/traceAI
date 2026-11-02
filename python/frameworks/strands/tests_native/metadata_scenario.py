"""Metadata regressions with real offline Agents, isolated from legacy mocks."""

import json
import sys
from importlib.metadata import version

from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands import Agent
from strands.models.model import Model
from traceai_strands import StrandsCallbackHandler, StrandsSpanExporter

check = sys.argv[1]
stop_reason = sys.argv[2] if len(sys.argv) > 2 else "end_turn"
SYSTEM = "SYSTEM-INSTRUCTION-SENTINEL"
FINISH = "gen_ai.response.finish_reasons"
PRIVATE = "PRIVATE-GENERATED-OUTPUT-SENTINEL"
OUTPUT = (
    json.dumps([{"finish_reason": PRIVATE, "description": "generated user content"}])
    if check == "completion_privacy"
    else "OUTPUT-SENTINEL"
)
provider = TracerProvider()
raw = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(raw))
trace.set_tracer_provider(provider)


class OfflineModel(Model):
    def __init__(self):
        self.config = {"model_id": "synthetic-model"}

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield

    async def stream(self, messages, *args, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockDelta": {
                "delta": {"text": OUTPUT},
                "contentBlockIndex": 0,
            }
        }
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
                "metrics": {"latencyMs": 1},
            }
        }


agent = Agent(model=OfflineModel(), callback_handler=None, system_prompt=SYSTEM)
if check == "completion_privacy":
    callback = StrandsCallbackHandler(tracer_provider=provider)
    callback.on_agent_start(agent, "INPUT-SENTINEL")
try:
    result = agent("INPUT-SENTINEL")
    if check == "completion_privacy":
        assert str(result).strip() == OUTPUT
        callback.on_agent_end(agent, result)
except Exception as error:  # noqa: BLE001 - floor lacks MaxTokensReachedException
    # The floor returns a truncated result; newer SDKs raise after ending chat.
    assert (
        stop_reason == "max_tokens"
        and type(error).__name__ == "MaxTokensReachedException"
    ), error

original = next(
    span
    for span in raw.get_finished_spans()
    if span.attributes.get("gen_ai.operation.name") == "chat"
)
snapshot = original.to_json()


def project(span, capture=True, **flags):
    sink = InMemorySpanExporter()
    StrandsSpanExporter(
        sink, capture_content=capture, config=TraceConfig(**flags)
    ).export([span])
    (converted,) = sink.get_finished_spans()
    assert converted.context == span.context
    assert converted.parent == span.parent
    assert converted.start_time == span.start_time
    assert converted.end_time == span.end_time
    assert converted.status.status_code == span.status.status_code
    assert not converted.events
    return converted


def synthetic(attributes=None, events=()):
    with provider.get_tracer("strands.telemetry.tracer").start_as_current_span(
        "chat", attributes={"gen_ai.operation.name": "chat", **(attributes or {})}
    ) as span:
        for name, attrs in events:
            span.add_event(name, attrs)
    return raw.get_finished_spans()[-1]


if check == "finish":
    assert stop_reason in snapshot, "the native SDK must emit the stop reason"
    for capture, flags in [
        (False, {}),
        (True, {}),
        (True, {"hide_outputs": True}),
        (True, {"hide_output_messages": True}),
        (True, {"hide_output_text": True}),
        (True, {"hide_error_details": True}),
    ]:
        converted = project(original, capture, **flags)
        assert list(converted.attributes.get(FINISH, ())) == [stop_reason], (
            converted.to_json()
        )
        if not capture or any(
            flags.get(key)
            for key in ("hide_outputs", "hide_output_messages", "hide_output_text")
        ):
            assert "OUTPUT-SENTINEL" not in converted.to_json()
    # Choice metadata is independent of the presence of message content, and
    # multiple choices must not collapse into a single reason.
    choices = synthetic(
        events=[
            ("gen_ai.choice", {"finish_reason": "max_tokens"}),
            (
                "gen_ai.choice",
                {"finish_reason": "end_turn", "message": "OUTPUT-SENTINEL"},
            ),
            ("gen_ai.choice", {"finish_reason": 123}),
            ("gen_ai.choice", {"finish_reason": ""}),
        ]
    )
    assert list(project(choices, False).attributes[FINISH]) == [
        "max_tokens",
        "end_turn",
    ]
    native = synthetic(
        {FINISH: ["stop"]}, [("gen_ai.choice", {"finish_reason": "end_turn"})]
    )
    assert list(project(native, False).attributes[FINISH]) == ["stop"]
    assert FINISH not in project(synthetic(), False).attributes
    output_key = "gen_ai.output.messages"
    details = "gen_ai.client.inference.operation.details"
    envelope = json.dumps(
        [
            {"finish_reason": "end_turn", "parts": [{"text": PRIVATE}]},
            {"finish_reason": "max_tokens"},
        ]
    )
    for span in (
        synthetic({output_key: envelope}),
        synthetic(events=[(details, {output_key: envelope})]),
    ):
        converted = project(span, False)
        assert list(converted.attributes[FINISH]) == ["end_turn", "max_tokens"]
        assert PRIVATE not in converted.to_json()
    # Invalid native envelopes must not fall back to arbitrary completion text.
    for value in ("invalid JSON", json.dumps({"finish_reason": PRIVATE})):
        span = synthetic(
            {
                output_key: value,
                "gen_ai.completion": json.dumps([{"finish_reason": PRIVATE}]),
            },
            [(details, {output_key: envelope})],
        )
        assert FINISH not in project(span, False).attributes
    unrelated = synthetic(events=[("unrelated", {output_key: envelope})])
    assert FINISH not in project(unrelated, False).attributes
elif check == "completion_privacy":
    privacy = sys.argv[3]
    capture = privacy != "capture_off"
    flags = {privacy: True} if privacy.startswith("hide_") else {}
    legacy = next(
        span for span in raw.get_finished_spans() if span.name == "strands.agent"
    )
    assert legacy.attributes["gen_ai.completion"].strip() == OUTPUT
    legacy_snapshot = legacy.to_json()
    converted = project(legacy, capture, **flags)
    assert FINISH not in converted.attributes, converted.to_json()
    native = project(original, capture, **flags)
    assert list(native.attributes[FINISH]) == ["end_turn"], native.to_json()
    for span in (converted, native):
        if privacy == "visible":
            assert PRIVATE in span.attributes["output.value"]
        else:
            assert PRIVATE not in span.to_json(), span.to_json()
    assert legacy.to_json() == legacy_snapshot
elif check == "system":
    key = "gen_ai.system_instructions"
    event = ("gen_ai.client.inference.operation.details", {key: SYSTEM})
    spans = [synthetic(events=[event]), synthetic({key: SYSTEM})]
    if version("strands-agents") == "1.0.0":
        # This SDK predates system-prompt capture; do not fabricate a prompt.
        assert SYSTEM not in snapshot
        assert SYSTEM not in project(original).to_json()
    else:
        assert SYSTEM in snapshot, "the native SDK must capture system instructions"
        spans.insert(0, original)
    for span in spans:
        assert SYSTEM in project(span).to_json()
        for flag in (
            "hide_inputs",
            "hide_input_messages",
            "hide_input_text",
            "hide_input_images",
        ):
            assert SYSTEM not in project(span, **{flag: True}).to_json()
        assert SYSTEM not in project(span, False).to_json()
        assert SYSTEM in project(span, hide_outputs=True).to_json()
    assert project(synthetic(events=[event])).attributes[key] == SYSTEM
    preferred = synthetic({key: "attribute-wins"}, [event])
    assert project(preferred).attributes[key] == "attribute-wins"
    assert (
        key not in project(synthetic(events=[("unrelated", {key: SYSTEM})])).attributes
    )
    oversized = synthetic(events=[(event[0], {key: "x" * 1_000_001})])
    assert key not in project(oversized).attributes
elif check == "provider":
    assert "strands-agents" in snapshot, "the native SDK must emit its framework tag"
    for capture in (False, True):
        converted = project(original, capture)
        for key in ("gen_ai.system", "gen_ai.provider.name"):
            assert key not in converted.attributes, converted.to_json()
        explicit = synthetic(
            {
                "gen_ai.system": "strands-agents",
                "gen_ai.provider.name": "custom-provider",
            }
        )
        assert (
            project(explicit, capture).attributes["gen_ai.provider.name"]
            == "custom-provider"
        )
        legacy = synthetic(
            {
                "gen_ai.system": "custom-provider",
                "gen_ai.provider.name": "strands-agents",
            }
        )
        assert project(legacy, capture).attributes["gen_ai.system"] == "custom-provider"
        assert "gen_ai.provider.name" not in project(legacy, capture).attributes
else:
    raise AssertionError(check)

assert original.to_json() == snapshot, "projection must not mutate native spans"
provider.shutdown()
print(json.dumps({"status": "PASS", "check": check, "stop_reason": stop_reason}))
