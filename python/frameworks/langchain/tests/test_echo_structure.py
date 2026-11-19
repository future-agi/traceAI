"""A chain's output counts as an echo of its input by structure (identity, pass-through or assign shape) or by
string content, never by a boolean or number that merely shares a field name with the input."""
import json

import pytest
from fi_instrumentation import TraceConfig
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from pydantic import BaseModel
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_langchain import LangChainInstrumentor

SETTINGS = ["hide_inputs", "hide_input_messages", "hide_input_text", "hide_input_images"]
REPLY = "FRESH_MODEL_REPLY_4471"


def _spans(config, run):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        run()
        return exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def _outputs(spans):
    return [dict(s.attributes).get("output.value") for s in spans]


def _dump(spans):
    return json.dumps([dict(s.attributes) for s in spans], default=str)


# --- LangGraph-style message state --------------------------------------------------------------


def _graph_node(state):
    return {"messages": [AIMessage(content=REPLY)]}


@pytest.mark.parametrize("setting", [None, *SETTINGS])
def test_a_node_that_returns_a_new_message_keeps_its_output(setting):
    config = TraceConfig(**{setting: True}) if setting else TraceConfig()
    spans = _spans(config, lambda: RunnableLambda(_graph_node).invoke({"messages": [HumanMessage(content="SENTINEL_USER_TURN_3318")]}))
    assert any(REPLY in str(out) for out in _outputs(spans)), (setting, _outputs(spans))
    if setting in ("hide_inputs", "hide_input_messages", "hide_input_text"):
        assert "SENTINEL_USER_TURN_3318" not in _dump(spans)


class _Turn(BaseModel):
    """A message-like schema object: every message carries the same schema fields."""
    role: str
    content: str
    example: bool = False


@pytest.mark.parametrize("setting", [None, *SETTINGS])
def test_schema_fields_shared_by_message_objects_do_not_make_a_node_output_an_echo(setting):
    config = TraceConfig(**{setting: True}) if setting else TraceConfig()

    def node(state):
        return {"turns": [_Turn(role="ai", content=REPLY)]}

    spans = _spans(config, lambda: RunnableLambda(node).invoke({"turns": [_Turn(role="human", content="SENTINEL_USER_TURN_4402")]}))
    assert any(REPLY in str(out) for out in _outputs(spans)), (setting, _outputs(spans))


# --- bare scalar pass-through (the shape LangChain uses for non-dict input and output) ----------


@pytest.mark.parametrize("setting", SETTINGS)
@pytest.mark.parametrize("value", [True, 7, [1, 2, 3], "Qz", 0.5])
def test_a_bare_scalar_passthrough_is_withheld(value, setting):
    spans = _spans(TraceConfig(**{setting: True}), lambda: RunnableLambda(lambda x: x).invoke(value))
    assert all(out is None for out in _outputs(spans)), (value, setting, _outputs(spans))
    assert "raw.output" not in _dump(spans)


@pytest.mark.parametrize("value", [True, 7, [1, 2, 3]])
def test_a_bare_scalar_passthrough_is_recorded_with_no_setting(value):
    spans = _spans(TraceConfig(), lambda: RunnableLambda(lambda x: x).invoke(value))
    assert any(out is not None for out in _outputs(spans))


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_scalar_result_different_from_its_input_is_kept(setting):
    spans = _spans(TraceConfig(**{setting: True}), lambda: RunnableLambda(lambda x: False).invoke(True))
    assert any(out is not None for out in _outputs(spans))


# --- assign shape --------------------------------------------------------------------------------


@pytest.mark.parametrize("setting", SETTINGS)
def test_an_assign_passthrough_of_booleans_and_numbers_is_withheld(setting):
    chain = RunnablePassthrough.assign(answer=RunnableLambda(lambda _: REPLY))
    spans = _spans(TraceConfig(**{setting: True}), lambda: chain.invoke({"flag": True, "count": 5}))
    chain_outputs = [out for out in _outputs(spans) if out is not None]
    assert all('"flag"' not in out for out in chain_outputs), chain_outputs


# --- non-echo outputs ----------------------------------------------------------------------------


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_result_that_shares_a_field_name_and_value_with_the_input_is_kept(setting):
    def work(x):
        # shares "count" (equal) with the input but is not a pass-through: the input's other key is gone
        return {"count": 5, "ok": True, "note": REPLY}

    spans = _spans(TraceConfig(**{setting: True}), lambda: RunnableLambda(work).invoke({"count": 5, "flag": True, "q": "SENTINEL_Q_5512"}))
    assert any(REPLY in str(out) for out in _outputs(spans))
    assert "SENTINEL_Q_5512" not in _dump(spans)


# --- string content ------------------------------------------------------------------------------


@pytest.mark.parametrize("setting", SETTINGS)
def test_an_output_that_contains_an_input_string_is_withheld(setting):
    spans = _spans(TraceConfig(**{setting: True}),
                   lambda: RunnableLambda(lambda x: {"summary": "see: " + x["q"]}).invoke({"q": "SENTINEL_STRING_ECHO_2290"}))
    assert "SENTINEL_STRING_ECHO_2290" not in _dump(spans)


def test_controls_record_everything_with_no_setting():
    spans = _spans(TraceConfig(), lambda: RunnableLambda(lambda x: {"summary": "see: " + x["q"]}).invoke({"q": "SENTINEL_STRING_ECHO_2290"}))
    assert "SENTINEL_STRING_ECHO_2290" in _dump(spans)
