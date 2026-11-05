"""Typed leaf privacy and independently permitted invocation metadata."""
import copy
import json

import pytest
from openai import _types as sdk_types
from fi_instrumentation import TraceConfig
from traceai_fireworks._wrappers import _get_input_attributes

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TEXT = "visible-text"
IMAGE = "https://example.invalid/image"
MESSAGES = [{"role": "user", "content": [
    {"type": "text", "text": TEXT}, {"type": "image_url", "image_url": {"url": IMAGE}},
]}]


@pytest.fixture(autouse=True)
def new_core():
    pytest.importorskip("fi_instrumentation.instrumentation._chat_input")


@pytest.mark.parametrize("flag", ["hide_input_images", "hide_input_text"])
@pytest.mark.parametrize("sentinel", [getattr(sdk_types, name)() for name in ("NotGiven", "Omit") if hasattr(sdk_types, name)])
def test_exact_sdk_omission_types_do_not_invalidate_permitted_content(flag, sentinel):
    config = TraceConfig(**{flag: True})
    attrs = _get_input_attributes("model", MESSAGES, config=config, temperature=sentinel)
    parsed = json.loads(config.mask("input.value", attrs["input.value"]))
    assert "temperature" not in parsed
    assert (TEXT if flag == "hide_input_images" else IMAGE) in json.dumps(parsed)


@pytest.mark.parametrize("hide_inputs", [False, True])
@pytest.mark.parametrize("hide_messages", [False, True])
@pytest.mark.parametrize("hide_parameters", [False, True])
@pytest.mark.parametrize("hide_text,hide_images", [(False, False), (True, False), (False, True), (True, True)])
def test_invocation_policy_matrix(hide_inputs, hide_messages, hide_parameters, hide_text, hide_images):
    config = TraceConfig(hide_inputs=hide_inputs, hide_input_messages=hide_messages,
                         hide_llm_invocation_parameters=hide_parameters,
                         hide_input_text=hide_text, hide_input_images=hide_images)
    original = copy.deepcopy(MESSAGES)
    attrs = _get_input_attributes("model", MESSAGES, config=config, temperature=0.7, max_tokens=100,
                                  tools=[{"hidden": TEXT}], extra_body={"credential": "SYNTHETIC-SECRET"})
    masked = {k: config.mask(k, v) for k, v in attrs.items()}
    if hide_inputs or hide_parameters:
        assert "gen_ai.request.parameters" not in attrs
    else:
        params = json.loads(masked["gen_ai.request.parameters"])
        assert params["temperature"] == 0.7 and params["max_tokens"] == 100
        if hide_messages or hide_text or hide_images:
            assert params["tools"] == "__REDACTED__"
    if hide_inputs or hide_messages:
        assert masked["input.value"] == "__REDACTED__"
        assert not any(k.startswith("gen_ai.input.messages") for k in attrs)
    assert "SYNTHETIC-SECRET" not in json.dumps(masked)
    assert MESSAGES == original


@pytest.mark.parametrize("separator", ["\t", "\n", "\r"])
@pytest.mark.parametrize("hide_images", [False, True])
def test_pii_flattened_and_raw_copies_share_leaf_redaction(separator, hide_images):
    pii = separator.join(["123", "45", "6789"])
    messages = [{"role": "user", "content": [{"type": "text", "text": pii}]}]
    original = copy.deepcopy(messages)
    config = TraceConfig(pii_redaction=True, hide_input_images=hide_images)
    attrs = _get_input_attributes("model", messages, config=config, stop=[pii])
    parsed = json.loads(config.mask("input.value", attrs["input.value"]))
    assert parsed["messages"][0]["content"][0]["text"] == "<SSN>"
    assert attrs["gen_ai.input.messages.0.message.content"] == "[{'type': 'text', 'text': '<SSN>'}]"
    assert json.loads(attrs["gen_ai.request.parameters"])["stop"] == ["<SSN>"]
    assert messages == original


def test_fake_omission_objects_and_sdk_subclasses_are_not_trusted():
    class NotGiven:
        def touch(self, *args, **kwargs):
            raise AssertionError("fake omission protocol invoked")
        __str__ = __bool__ = __iter__ = model_dump = touch

    class Subclass(sdk_types.NotGiven):
        pass

    for sentinel in (NotGiven(), Subclass()):
        config = TraceConfig(hide_input_images=True)
        attrs = _get_input_attributes("model", MESSAGES, config=config, temperature=sentinel)
        assert config.mask("input.value", attrs["input.value"]) == "__REDACTED__"
        assert not any(k.startswith("gen_ai.input.messages") for k in attrs)


def test_omission_detection_does_not_call_custom_type_equality():
    class ActiveType(type):
        def __eq__(cls, other):
            raise AssertionError("omission detection called custom equality")

    class Active(metaclass=ActiveType):
        pass

    attrs = _get_input_attributes("model", MESSAGES, config=TraceConfig(hide_input_images=True),
                                  temperature=0.7, unknown=Active())
    assert json.loads(attrs["gen_ai.request.parameters"])["temperature"] == 0.7


def test_message_hiding_does_not_inspect_hidden_messages_or_tools():
    class Active:
        def touch(self, *args, **kwargs):
            raise AssertionError("hidden protocol invoked")
        __str__ = __iter__ = model_dump = touch

    config = TraceConfig(hide_input_messages=True)
    attrs = _get_input_attributes("model", [{"role": "user", "content": Active()}], config=config,
                                  temperature=0.7, tools=Active(), unknown=Active())
    assert json.loads(attrs["gen_ai.request.parameters"]) == {"temperature": 0.7, "tools": "__REDACTED__"}
    attrs = _get_input_attributes("model", MESSAGES, config=config, temperature=Active())
    assert "gen_ai.request.parameters" not in attrs


def _assistant_tool_call_message():
    from openai.types.chat import ChatCompletionMessage
    return ChatCompletionMessage.construct(
        role="assistant", content=None,
        tool_calls=[{"id": "call-1", "type": "function",
                     "function": {"name": "lookup", "arguments": '{"q": "tool-args"}'}}],
    )


@pytest.mark.parametrize("flag", ["pii_redaction", "hide_input_images", "hide_input_text"])
def test_sdk_message_objects_in_a_tool_loop_keep_permitted_telemetry(flag):
    sdk_message = _assistant_tool_call_message()
    messages = [{"role": "user", "content": TEXT}, sdk_message,
                {"role": "tool", "tool_call_id": "call-1", "content": "tool-result"}]
    config = TraceConfig(**{flag: True})
    attrs = _get_input_attributes("model", messages, config=config, temperature=0.7, max_tokens=100)
    masked = config.mask("input.value", attrs["input.value"])
    assert masked != "__REDACTED__"
    assert attrs["gen_ai.input.messages.1.message.role"] == "assistant"
    assert attrs["gen_ai.input.messages.2.message.role"] == "tool"
    if flag == "hide_input_text":
        assert TEXT not in json.dumps(masked) and "tool-args" not in masked
    else:
        assert TEXT in masked
    params = json.loads(attrs["gen_ai.request.parameters"])
    assert params["temperature"] == 0.7 and params["max_tokens"] == 100
    assert messages[1] is sdk_message and sdk_message.tool_calls is not None


@pytest.mark.parametrize("flag", ["hide_input_images", "hide_input_text", "pii_redaction"])
@pytest.mark.parametrize("invalid", [{"url": "ftp://example.invalid/i.png"},
                                     {"url": "https://example.invalid/i.png", "detail": "ultra"}])
def test_unprojectable_messages_keep_independent_parameters(flag, invalid):
    messages = [{"role": "user", "content": [{"type": "text", "text": TEXT},
                                             {"type": "image_url", "image_url": invalid}]}]
    config = TraceConfig(**{flag: True})
    attrs = _get_input_attributes("model", messages, config=config, temperature=0.7, max_tokens=100)
    params = json.loads(attrs["gen_ai.request.parameters"])
    assert params["temperature"] == 0.7 and params["max_tokens"] == 100
    if flag != "pii_redaction":
        assert config.mask("input.value", attrs["input.value"]) == "__REDACTED__"
