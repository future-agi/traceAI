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

from ._core_version import assert_hidden, dump as _dump_all

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
    return _dump_all(spans)


# --- LangGraph-style message state --------------------------------------------------------------


def _graph_node(state):
    return {"messages": [AIMessage(content=REPLY)]}


@pytest.mark.parametrize("setting", [None, *SETTINGS])
def test_a_node_that_returns_a_new_message_keeps_its_output(setting):
    config = TraceConfig(**{setting: True}) if setting else TraceConfig()
    spans = _spans(config, lambda: RunnableLambda(_graph_node).invoke({"messages": [HumanMessage(content="SENTINEL_USER_TURN_3318")]}))
    assert any(REPLY in str(out) for out in _outputs(spans)), (setting, _outputs(spans))
    if setting in ("hide_inputs", "hide_input_messages", "hide_input_text"):
        assert_hidden(spans, "SENTINEL_USER_TURN_3318", setting=setting)


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
    assert not any("output.value" in dict(s.attributes) for s in spans), _outputs(spans)


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
    assert_hidden(spans, "SENTINEL_Q_5512", setting=setting)


# --- string content ------------------------------------------------------------------------------


@pytest.mark.parametrize("setting", SETTINGS)
def test_an_output_that_contains_an_input_string_is_withheld(setting):
    spans = _spans(TraceConfig(**{setting: True}),
                   lambda: RunnableLambda(lambda x: {"summary": "see: " + x["q"]}).invoke({"q": "SENTINEL_STRING_ECHO_2290"}))
    assert_hidden(spans, "SENTINEL_STRING_ECHO_2290", setting=setting)


def test_controls_record_everything_with_no_setting():
    spans = _spans(TraceConfig(), lambda: RunnableLambda(lambda x: {"summary": "see: " + x["q"]}).invoke({"q": "SENTINEL_STRING_ECHO_2290"}))
    assert "SENTINEL_STRING_ECHO_2290" in _dump(spans)


# --- multi-turn agent state: a new reply is kept even though the state already holds earlier turns ---------------

from langchain_core.messages import ToolMessage  # noqa: E402
from langchain_core.runnables import RunnableParallel  # noqa: E402

HUMAN = "SENTINEL_USER_TURN_3318"
PRIOR = "PRIOR_ASSISTANT_NOTE_7741"
TOOL_RESULT = "TOOL_RESULT_5520"
ALL_SETTINGS = [None, *SETTINGS]


def _config(setting):
    return TraceConfig(**{setting: True}) if setting else TraceConfig()


def _prior_ai():
    return AIMessage(
        content=PRIOR,
        response_metadata={"model_name": "gpt-4o-mini", "finish_reason": "tool_calls"},
        tool_calls=[{"name": "lookup", "args": {"topic": "weather"}, "id": "call_1", "type": "tool_call"}],
    )


def _kept(spans):
    return any(REPLY in str(out) for out in _outputs(spans))


@pytest.mark.parametrize("setting", ALL_SETTINGS)
def test_a_new_reply_after_a_tool_round_trip_is_kept(setting):
    state = {"messages": [HumanMessage(content=HUMAN), _prior_ai(), ToolMessage(content=TOOL_RESULT, tool_call_id="call_1")]}
    spans = _spans(_config(setting), lambda: RunnableLambda(_graph_node).invoke(state))
    assert _kept(spans), (setting, _outputs(spans))
    if setting in ("hide_inputs", "hide_input_messages", "hide_input_text"):
        assert_hidden(spans, HUMAN, setting=setting)
        assert_hidden(spans, TOOL_RESULT, setting=setting)


@pytest.mark.parametrize("setting", ALL_SETTINGS)
def test_a_new_reply_after_human_ai_human_is_kept(setting):
    state = {"messages": [HumanMessage(content=HUMAN), AIMessage(content=PRIOR, response_metadata={"model_name": "gpt-4o-mini"}), HumanMessage(content="a second question about tides")]}
    spans = _spans(_config(setting), lambda: RunnableLambda(_graph_node).invoke(state))
    assert _kept(spans), (setting, _outputs(spans))


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_node_that_returns_its_input_messages_unchanged_is_withheld(setting):
    state = {"messages": [HumanMessage(content=HUMAN), _prior_ai()]}
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda s: {"messages": s["messages"]}).invoke(state))
    assert_hidden(spans, HUMAN, setting=setting)
    assert_hidden(spans, PRIOR, setting=setting)


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_new_reply_that_contains_an_input_text_is_withheld(setting):
    state = {"messages": [HumanMessage(content=HUMAN), _prior_ai()]}
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda s: {"messages": [AIMessage(content="you wrote: " + HUMAN)]}).invoke(state))
    assert_hidden(spans, HUMAN, setting=setting)


class _Wrapper(BaseModel):
    text: str


@pytest.mark.parametrize("setting", SETTINGS)
def test_an_input_text_with_a_newline_and_a_quote_inside_a_nested_model_is_withheld(setting):
    secret = 'SENTINEL_LINE_ONE\nthen "quoted" text \\ and a backslash'
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda x: {"wrapped": _Wrapper(text="prefix " + secret + " suffix")}).invoke({"q": secret}))
    assert_hidden(spans, "SENTINEL_LINE_ONE", setting=setting)


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_number_passed_through_below_the_top_level_is_withheld(setting):
    chain = RunnableParallel(orig=RunnablePassthrough(), x=RunnableLambda(lambda _: REPLY))
    spans = _spans(_config(setting), lambda: chain.invoke(12345678))
    assert all("12345678" not in str(out) for out in _outputs(spans)), _outputs(spans)


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_bool_passed_through_below_the_top_level_is_withheld(setting):
    chain = RunnableParallel(orig=RunnablePassthrough(), x=RunnableLambda(lambda _: REPLY))
    spans = _spans(_config(setting), lambda: chain.invoke(True))
    assert not any('"orig": true' in str(out) for out in _outputs(spans)), _outputs(spans)


@pytest.mark.parametrize("setting", ALL_SETTINGS)
def test_a_multi_turn_reply_that_shares_small_numbers_and_flags_with_the_state_is_kept(setting):
    state = {"messages": [HumanMessage(content=HUMAN), _prior_ai()], "count": 3, "done": False}
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda s: {"messages": [AIMessage(content=REPLY)], "count": 3, "done": False}).invoke(state))
    assert _kept(spans), (setting, _outputs(spans))


# --- content parts, output-side fields, and booleans vs numbers ------------------------------------------------

from langchain_core.documents import Document  # noqa: E402

from ._core_version import output_side  # noqa: E402

IMAGE_URL = "https://images.example.com/SENTINELIMAGE9931/photo.png"
TOOL_PART = "SENTINEL_TOOL_RESULT_PART_6604"
COPIED = 'SENTINEL_COPIED_LINE\nthen "quoted" text \\ and a backslash'


@pytest.mark.parametrize("setting", SETTINGS)
def test_an_image_only_message_passed_through_in_a_sub_list_is_withheld(setting):
    image = HumanMessage(content=[{"type": "image_url", "image_url": {"url": IMAGE_URL}}])
    state = {"messages": [image, AIMessage(content="ok"), HumanMessage(content="a later question about tides")]}
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda s: {"messages": s["messages"][:1]}).invoke(state))
    assert "SENTINELIMAGE9931" not in output_side(spans), (setting, _outputs(spans))


@pytest.mark.parametrize("content", ["string", "list"])
@pytest.mark.parametrize("setting", SETTINGS)
def test_a_tool_result_content_part_passed_through_is_withheld(setting, content):
    inner = TOOL_PART if content == "string" else [{"type": "text", "text": TOOL_PART}]
    part = HumanMessage(content=[{"type": "tool_result", "tool_use_id": "toolu_1", "content": inner}])
    state = {"messages": [part, AIMessage(content="ok"), HumanMessage(content="and then?")]}
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda s: {"messages": s["messages"][:1]}).invoke(state))
    assert TOOL_PART not in output_side(spans), (setting, _outputs(spans))


@pytest.mark.parametrize("setting", SETTINGS)
def test_input_text_copied_into_an_output_document_metadata_is_withheld(setting):
    spans = _spans(_config(setting), lambda: RunnableLambda(
        lambda x: {"docs": [Document(page_content="an unrelated page", metadata={"source": "prefix " + x["q"] + " suffix"})]}).invoke({"q": COPIED}))
    assert "SENTINEL_COPIED_LINE" not in output_side(spans), (setting, _outputs(spans))


@pytest.mark.parametrize("setting", SETTINGS)
def test_input_text_copied_into_an_output_message_additional_kwargs_is_withheld(setting):
    spans = _spans(_config(setting), lambda: RunnableLambda(
        lambda x: {"messages": [AIMessage(content=REPLY, additional_kwargs={"note": "prefix " + x["q"] + " suffix"})]}).invoke({"q": COPIED}))
    assert "SENTINEL_COPIED_LINE" not in output_side(spans), (setting, _outputs(spans))


class _Note(BaseModel):
    """Not a LangChain message or document: every field of it is input text to look for."""
    content: str
    label: str


@pytest.mark.parametrize("setting", SETTINGS)
def test_any_field_of_a_non_langchain_object_with_content_is_input_text(setting):
    secret = "SENTINEL_NOTE_LABEL_8120"
    spans = _spans(_config(setting), lambda: RunnableLambda(
        lambda s: {"summary": "the label was " + s["notes"][0].label}).invoke({"notes": [_Note(content="hi", label=secret)]}))
    assert secret not in output_side(spans), (setting, _outputs(spans))


@pytest.mark.parametrize("setting", ALL_SETTINGS)
def test_a_boolean_input_is_not_matched_by_an_equal_number_in_the_output(setting):
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda _: {"count": 1}).invoke(True))
    assert any('"count": 1' in str(out) for out in _outputs(spans)), (setting, _outputs(spans))


ORDER = "SENTINEL_ORDER_ID_55821"


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_nested_id_or_type_value_inside_a_content_part_is_input_text(setting):
    part = HumanMessage(content=[{"type": "json", "json": {"id": ORDER, "meta": {"type": ORDER + "_T"}}}])
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda s: {"out": "order " + ORDER + " and " + ORDER + "_T"}).invoke({"messages": [part]}))
    assert ORDER not in output_side(spans), (setting, _outputs(spans))


@pytest.mark.parametrize("setting", SETTINGS)
def test_invalid_tool_call_arguments_are_input_text(setting):
    prior = AIMessage(content="", invalid_tool_calls=[{"name": "f", "args": '{"q": "' + ORDER + '"', "id": "1", "error": None, "type": "invalid_tool_call"}])
    spans = _spans(_config(setting), lambda: RunnableLambda(lambda s: {"out": "looked up " + ORDER}).invoke({"messages": [prior]}))
    assert ORDER not in output_side(spans), (setting, _outputs(spans))
