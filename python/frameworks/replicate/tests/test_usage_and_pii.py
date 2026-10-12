"""Token usage from prediction metrics, and PII redaction of error text."""

from __future__ import annotations

import pytest
from opentelemetry.trace import StatusCode

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from _support import (  # noqa: E402
    FAIL_MODEL,
    IMAGE_MODEL,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    PROMPT,
    TEXT_VERSION,
    FakeReplicate,
    attrs,
    instrumented,
    make_client,
)
from fi_instrumentation import TraceConfig  # noqa: E402
from replicate.exceptions import ModelError  # noqa: E402

USAGE = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens")


def test_create_then_wait_records_the_reported_token_counts():
    with FakeReplicate() as fake, instrumented() as traced:
        prediction = make_client(fake).predictions.create(version=TEXT_VERSION, input={"prompt": PROMPT})
        prediction.wait()

    values = attrs(traced.one())
    assert values["gen_ai.span.kind"] == "LLM"
    assert [values[key] for key in USAGE] == [INPUT_TOKENS, OUTPUT_TOKENS, INPUT_TOKENS + OUTPUT_TOKENS]


def test_a_prediction_without_token_metrics_records_no_usage():
    with FakeReplicate() as fake, instrumented() as traced:
        make_client(fake).run(IMAGE_MODEL, input={"prompt": PROMPT})

    values = attrs(traced.one())
    assert not [key for key in USAGE if key in values]


def test_pii_redaction_also_covers_a_failed_predictions_error_text():
    with FakeReplicate() as fake, instrumented(config=TraceConfig(pii_redaction=True)) as traced:
        with pytest.raises(ModelError):
            make_client(fake).run(FAIL_MODEL, input={"prompt": PROMPT}, wait=False)

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    (event,) = [event for event in span.events if event.name == "exception"]
    for text in (span.status.description, event.attributes["exception.message"],
                 event.attributes["exception.stacktrace"]):
        assert "ops@example.com" not in text
    assert "<EMAIL_ADDRESS>" in span.status.description
