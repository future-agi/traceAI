import importlib.util
from pathlib import Path

import pytest
from pydantic import SecretStr

_SPEC = importlib.util.spec_from_file_location(
    "futureagi_filter", Path(__file__).resolve().parents[1] / "futureagi_filter.py"
)
assert _SPEC and _SPEC.loader
_FILTER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FILTER)


def _valves(**overrides):
    values = dict(api_key="key", secret_key="secret", project="proj")
    values.update(overrides)
    return _FILTER.Valves(**values)


def test_valves_hide_secrets():
    valves = _valves()
    assert isinstance(valves.api_key, SecretStr)
    assert isinstance(valves.secret_key, SecretStr)
    assert "key" not in repr(valves.api_key)
    assert valves.redact is False
    assert valves.include_email_hash is False


def test_span_has_model_messages_and_usage():
    attributes = _FILTER.span_attributes(
        {"model": "gpt", "messages": [{"role": "user", "content": "hi"}], "usage": {"prompt_tokens": 2, "completion_tokens": 4}, "user_id": "u1", "chat_id": "c1", "output": "ok"},
        _valves(),
    )
    assert attributes["gen_ai.request.model"] == "gpt"
    assert attributes["gen_ai.usage.input_tokens"] == 2
    assert attributes["session.id"] == "c1"
    assert "input.value" in attributes
    assert "email" not in attributes


def test_email_hash_is_opt_in_and_not_raw():
    form = {"email": "a@b.c", "user_id": "u1"}
    assert "user.email_hash" not in _FILTER.span_attributes(form, _valves())
    hashed = _FILTER.span_attributes(form, _valves(include_email_hash=True))
    assert hashed["user.email_hash"] == _FILTER.email_hash("a@b.c")
    assert "a@b.c" not in hashed.values()


def test_outlet_returns_the_body_when_export_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("collector down")

    monkeypatch.setattr(_FILTER, "span_attributes", boom)
    body = {"chat_id": "c1", "output": "ok"}
    filt = _FILTER.Filter()
    filt.valves = _valves()
    assert filt.outlet(body) is body


def test_redact_strips_content():
    attributes = _FILTER.span_attributes(
        {"messages": [{"role": "user", "content": "secret"}], "output": "secret"},
        _valves(redact=True),
    )
    assert "input.value" not in attributes
    assert "output.value" not in attributes


def test_missing_usage_is_flagged():
    attributes = _FILTER.span_attributes({"model": "gpt"}, _valves())
    assert attributes["gen_ai.usage.unavailable"] is True
    assert "gen_ai.usage.input_tokens" not in attributes


def test_collector_path():
    assert _FILTER.collector_endpoint("https://api.futureagi.com") == "https://api.futureagi.com/tracer/v1/traces"
    assert (
        _FILTER.collector_endpoint("https://api.futureagi.com/tracer/v1/traces")
        == "https://api.futureagi.com/tracer/v1/traces"
    )
