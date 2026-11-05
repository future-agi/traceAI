"""Regressions for validation before representation, and shared copy bounds."""
import copy
import json

import pytest
from fi_instrumentation import REDACTED_VALUE, TraceConfig
from fi_instrumentation.instrumentation.config import ChatInput
from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string


@pytest.mark.parametrize("separator", ["\t", "\n", "\r"])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("key", ["input.value", "raw.input"])
def test_pii_leaves_are_scanned_before_escaping(separator, structured, key):
    pii = separator.join(["123", "45", "6789"])
    assert redact_pii_in_string(pii) == "<SSN>"
    content = [{"type": "text", "text": pii}] if structured else pii
    payload = {"model": "model", "messages": [{"role": "user", "content": content}],
               "stop": [pii], "user": pii}
    original = copy.deepcopy(payload)
    config = TraceConfig(hide_input_images=True, pii_redaction=True)
    projection = ChatInput(payload).masked(config)
    assert projection["messages"][0]["content"] == (
        [{"type": "text", "text": "<SSN>"}] if structured else "<SSN>"
    )
    assert projection["stop"] == ["<SSN>"] and projection["user"] == "<SSN>"
    assert json.loads(config.mask(key, ChatInput(payload))) == projection
    assert payload == original


@pytest.mark.parametrize("url", ["HTTPS://example.invalid/image", "hTtPs://example.invalid/image", "HtTp://example.invalid/image"])
@pytest.mark.parametrize("hide", ["hide_input_images", "hide_input_text"])
def test_case_insensitive_scheme_preserves_original_reference(url, hide):
    payload = {"model": "model", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "visible"}, {"type": "image_url", "image_url": {"url": url}},
    ]}]}
    result = json.loads(TraceConfig(**{hide: True}).mask("input.value", ChatInput(payload)))
    text, image = result["messages"][0]["content"]
    assert text["text"] == (REDACTED_VALUE if hide == "hide_input_text" else "visible")
    assert image["image_url"] == (REDACTED_VALUE if hide == "hide_input_images" else {"url": url})


@pytest.mark.parametrize("url", ["ftp://example.invalid/image", "javascript:alert(1)", "relative.png"])
def test_unsupported_image_schemes_still_fail_closed(url):
    payload = {"model": "model", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": url},
    ]}]}
    assert TraceConfig(hide_input_images=True).mask("input.value", ChatInput(payload)) == REDACTED_VALUE


@pytest.mark.parametrize("limit", [31, 32])
def test_mixed_case_data_scheme_keeps_base64_limit(limit):
    url = "DaTa:image/png;base64," + "x" * 10
    assert len(url) == 32
    payload = {"model": "model", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": url},
    ]}]}
    result = ChatInput(payload).masked(TraceConfig(base64_image_max_length=limit))
    assert result["messages"][0]["content"][0]["image_url"] == (url if limit == 32 else REDACTED_VALUE)


@pytest.mark.parametrize("content_nodes,count,allowed", [
    (0, 9999, True), (0, 10000, False), (0, 10001, False),
    (1, 9998, True), (1, 9999, False),
])
def test_stop_list_shares_content_copy_budget(content_nodes, count, allowed):
    payload = {"model": "model", "messages": ([{"role": "user", "content": "text"}] if content_nodes else []),
               "stop": ["x"] * count}
    result = TraceConfig(hide_input_images=True).mask("input.value", ChatInput(payload))
    if allowed:
        assert json.loads(result)["stop"] == payload["stop"]
    else:
        assert result == REDACTED_VALUE
    assert len(payload["stop"]) == count


def test_over_budget_list_rejected_before_scanning_or_copying(monkeypatch):
    from fi_instrumentation.instrumentation import _chat_input

    def forbidden(*args, **kwargs):
        raise AssertionError("over-budget list was traversed")

    monkeypatch.setattr(_chat_input, "_passive", forbidden)
    monkeypatch.setattr(_chat_input, "redact_pii_in_string", forbidden)
    payload = {"model": "model", "messages": [], "stop": ["x"] * 10001}
    assert TraceConfig(hide_input_images=True, pii_redaction=True).mask("input.value", ChatInput(payload)) == REDACTED_VALUE


class Active:
    def touch(self, *args, **kwargs):
        raise AssertionError("hidden or invalid protocol evaluated")
    __str__ = __iter__ = __call__ = model_dump = touch


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_messages"])
def test_pii_does_not_evaluate_hidden_payload(flag):
    config = TraceConfig(pii_redaction=True, **{flag: True})
    assert config.mask("input.value", ChatInput(Active())) == REDACTED_VALUE


def test_independent_invocations_validate_without_hidden_message_access():
    payload = {"model": "model", "messages": Active(), "temperature": 0.7, "max_tokens": 100,
               "tools": Active(), "unknown": Active(), "stop": ["123\t45\t6789"]}
    config = TraceConfig(hide_input_messages=True, pii_redaction=True)
    value = ChatInput(payload)
    assert value.masked(config) is None
    assert value.masked_invocation_parameters(config) == {
        "temperature": 0.7, "max_tokens": 100, "tools": REDACTED_VALUE, "stop": ["<SSN>"],
    }
    payload["temperature"] = Active()
    assert value.masked_invocation_parameters(config) is None
    assert ChatInput(Active()).masked_invocation_parameters(TraceConfig(hide_inputs=True)) is None


def test_pii_never_scans_unvalidated_string_subclasses():
    class CustomString(str):
        pass
    payload = {"model": "model", "messages": [{"role": "user", "content": CustomString("123\t45\t6789")}]}
    assert TraceConfig(hide_input_images=True, pii_redaction=True).mask("input.value", ChatInput(payload)) == REDACTED_VALUE


@pytest.mark.parametrize("separator", ["\t", "\n", "\r", "\x0b", "\x0c"])
def test_pii_dict_keys_are_scanned_before_escaping(separator):
    pii = separator.join(["531", "72", "4086"])
    payload = {"model": "model", "messages": [{"role": "user", "content": "x"}],
               "response_format": {"type": "json_schema", "json_schema": {
                   "name": "s", "schema": {"type": "object", "properties": {pii: {"type": "string"}}}}}}
    config = TraceConfig(pii_redaction=True)
    exported = config.mask("input.value", ChatInput(payload))
    assert "531" not in exported and "<SSN>" in exported
    assert pii in payload["response_format"]["json_schema"]["schema"]["properties"]


@pytest.mark.parametrize("prefix", ["data:image/png;BASE64,", "DATA:IMAGE/PNG;BASE64,", "data:image/png;Base64,"])
def test_base64_length_limit_marker_is_case_insensitive(prefix):
    url = prefix + "A" * 230
    payload = {"model": "model", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "visible"}, {"type": "image_url", "image_url": {"url": url}}]}]}
    config = TraceConfig(hide_input_text=True, base64_image_max_length=64)
    exported = config.mask("input.value", ChatInput(payload))
    assert "A" * 230 not in exported
    from fi_instrumentation.instrumentation.config import is_base64_url
    assert is_base64_url(url)
