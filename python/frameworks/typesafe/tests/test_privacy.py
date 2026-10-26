"""traceAI TraceConfig convention: content on by default, FI_HIDE_* turn it off."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("typesafe_sdk")

from fi_instrumentation import TraceConfig  # noqa: E402

from tests._support import (  # noqa: E402
    CRITERIA_MARKER,
    INSTRUCTION_MARKER,
    NOUL,
    SCORE,
    STATE,
    STATE_MARKER,
    FakeSystemOne,
    client,
    everything,
    instrumented,
    only_span,
    questions,
)

CONTENT = (STATE_MARKER, INSTRUCTION_MARKER, CRITERIA_MARKER)
Q = "typesafe.question."


def _span(config=None, **kwargs):
    fake = FakeSystemOne()
    with instrumented(config) as exporter:
        with client(fake) as typesafe:
            typesafe.system_one(STATE, questions(), **kwargs)
    return only_span(exporter)


def test_default_captures_state_questions_and_decisions():
    span = _span()
    a = dict(span.attributes)
    assert a["input.mime_type"] == "application/json"
    payload = json.loads(a["input.value"])
    assert payload["state"] == STATE
    assert set(payload["questions"]) == set(questions())
    for marker in CONTENT:
        assert marker in a["input.value"]
    output = json.loads(a["output.value"])
    assert output["billing"] == {"type": "noul", "noul": NOUL}
    assert output["tone"]["choice"] == "angry"
    assert "probabilities" not in a["output.value"]


def test_hide_inputs_drops_state_and_questions():
    span = _span(TraceConfig(hide_inputs=True))
    dump = everything(span)
    for marker in CONTENT:
        assert marker not in dump
    assert "input.value" not in span.attributes
    # Decisions are still there.
    assert span.attributes[Q + "tone.choice"] == "angry"
    assert "output.value" in span.attributes


def test_hide_outputs_drops_labels_keeps_numbers():
    span = _span(TraceConfig(hide_outputs=True))
    a = dict(span.attributes)
    assert "output.value" not in a
    assert Q + "tone.choice" not in a
    assert a[Q + "billing.noul"] == NOUL
    assert a[Q + "urgency.score"] == SCORE
    assert a[Q + "tone.type"] == "choice"
    assert "input.value" in a


@pytest.mark.parametrize(
    "flags",
    [
        {"FI_HIDE_INPUTS": "true", "FI_HIDE_OUTPUTS": "true"},
        {"FI_HIDE_INPUT_TEXT": "true", "FI_HIDE_OUTPUT_TEXT": "true"},
    ],
)
def test_environment_flags_hide_all_content(monkeypatch, flags):
    for name, value in flags.items():
        monkeypatch.setenv(name, value)
    span = _span()
    dump = everything(span)
    for marker in CONTENT + ("angry",):
        assert marker not in dump
    a = dict(span.attributes)
    assert "input.value" not in a and "output.value" not in a
    assert a[Q + "billing.noul"] == NOUL
    assert a["gen_ai.request.model"] == "jev-latest"


def test_idempotency_key_only_when_inputs_are_shown():
    headers = {"Idempotency-Key": "idem-th8322"}
    assert _span(extra_headers=headers).attributes["typesafe.idempotency_key"] == "idem-th8322"
    hidden = _span(TraceConfig(hide_inputs=True), extra_headers=headers)
    assert "typesafe.idempotency_key" not in hidden.attributes


def test_image_assets_are_references_never_bytes_or_paths(tmp_path):
    import typesafe_sdk

    if not hasattr(typesafe_sdk, "Image"):
        pytest.skip("image assets exist from typesafe-sdk 0.7.5")
    from typesafe_sdk import Image, Noul

    photo = tmp_path / "private-user-photo.png"
    data = b"\x89PNG\r\n\x1a\n" + b"PIXELS-th8322" * 4
    photo.write_bytes(data)
    image = Image(photo)
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            with typesafe.attach_assets(image):
                typesafe.system_one(
                    "Product photo: {0}".format(image),
                    {"damaged": Noul(instructions="Is it damaged?")},
                )
    dump = everything(only_span(exporter))
    assert str(image) in dump  # the opaque <image:...> reference
    assert "PIXELS-th8322" not in dump
    assert "private-user-photo" not in dump
    assert str(tmp_path) not in dump


def test_pii_redaction_applies_to_captured_state():
    fake = FakeSystemOne()
    with instrumented(TraceConfig(pii_redaction=True)) as exporter:
        with client(fake) as typesafe:
            typesafe.system_one("Reach me at jane.doe@example.com", questions())
    assert "jane.doe@example.com" not in everything(only_span(exporter))
