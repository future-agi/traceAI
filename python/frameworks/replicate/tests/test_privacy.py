"""Token redaction and TraceConfig content controls (PRD R-06, R-07, AC-05, AC-08)."""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import replicate  # noqa: E402
import traceai_replicate  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402

from _support import (  # noqa: E402
    API_TOKEN,
    DATA_MODEL,
    DATA_PAYLOAD,
    ECHO_MODEL,
    PROMPT,
    TEXT_MODEL,
    TEXT_OUTPUT,
    FakeReplicate,
    RecordingTransport,
    attrs,
    instrumented,
)


def _client(fake, **options):
    client = replicate.Client(base_url=fake.origin, transport=RecordingTransport(), **options)
    client.poll_interval = 0.01
    return client


def test_token_passed_to_the_client_is_redacted_from_input_and_output():
    prompt = "my key is {0}".format(API_TOKEN)
    with FakeReplicate() as fake, instrumented() as traced:
        client = _client(fake, api_token=API_TOKEN)
        output = client.run(ECHO_MODEL, input={"prompt": prompt})

    assert output == prompt  # the caller still gets the vendor's output
    values = attrs(traced.one())
    assert json.loads(values["input.value"]) == {"prompt": "my key is [redacted]"}
    assert values["output.value"] == "my key is [redacted]"
    assert API_TOKEN not in traced.wire()


def test_token_read_from_the_environment_by_the_client_is_redacted(monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", API_TOKEN)
    with FakeReplicate() as fake, instrumented() as traced:
        client = _client(fake)  # no api_token: the SDK reads the environment
        client.run(ECHO_MODEL, input={"prompt": API_TOKEN})

    assert fake.request_headers("POST", "/v1/models/acme/echo-model/predictions")[
        "authorization"
    ] == "Bearer " + API_TOKEN
    assert API_TOKEN not in traced.wire()
    assert attrs(traced.one())["output.value"] == "[redacted]"


def test_token_in_a_client_authorization_header_is_redacted():
    with FakeReplicate() as fake, instrumented() as traced:
        client = _client(fake, headers={"Authorization": "Bearer " + API_TOKEN})
        client.run(ECHO_MODEL, input={"prompt": API_TOKEN})

    assert API_TOKEN not in traced.wire()


def test_content_is_recorded_by_default_and_hidden_by_trace_config():
    # Control run: with the default TraceConfig the markers do reach the span.
    with FakeReplicate() as fake, instrumented() as shown:
        _client(fake, api_token=API_TOKEN).run(TEXT_MODEL, input={"prompt": PROMPT})
    assert PROMPT in shown.wire()
    assert TEXT_OUTPUT in shown.wire()

    config = TraceConfig(hide_inputs=True, hide_outputs=True)
    with FakeReplicate() as fake, instrumented(config=config) as hidden:
        _client(fake, api_token=API_TOKEN).run(TEXT_MODEL, input={"prompt": PROMPT})
    values = attrs(hidden.one())
    assert values["input.value"] == "__REDACTED__"
    assert values["output.value"] == "__REDACTED__"
    assert "input.mime_type" not in values
    assert "output.mime_type" not in values
    # Metadata that is not content stays.
    assert values["gen_ai.request.model"] == TEXT_MODEL
    assert values["replicate.output.type"] == "text"
    assert PROMPT not in hidden.wire()
    assert TEXT_OUTPUT not in hidden.wire()


def test_hide_flags_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    with FakeReplicate() as fake, instrumented() as traced:
        _client(fake, api_token=API_TOKEN).run(TEXT_MODEL, input={"prompt": PROMPT})

    assert PROMPT not in traced.wire()
    assert TEXT_OUTPUT not in traced.wire()


def test_data_uri_output_records_only_its_media_type():
    with FakeReplicate() as fake, instrumented() as traced:
        _client(fake, api_token=API_TOKEN).run(DATA_MODEL, input={})

    values = attrs(traced.one())
    assert values["replicate.output.type"] == "url"
    assert values["output.value"].startswith("data:image/png;base64,")
    assert DATA_PAYLOAD not in traced.wire()


def test_file_inputs_are_described_not_read():
    image = io.BytesIO(b"FILE-BYTES-MARKER" * 4)
    with FakeReplicate() as fake, instrumented() as traced:
        client = _client(fake, api_token=API_TOKEN)
        client.run(
            TEXT_MODEL,
            input={"prompt": PROMPT, "image": image},
            file_encoding_strategy="base64",
        )

    # The client itself read and inlined the file; the span did not.
    (_, _, _, body) = [call for call in fake.calls if call[0] == "POST"][0]
    assert base64.b64encode(b"FILE-BYTES-MARKER" * 4).decode() in body["input"]["image"]
    values = attrs(traced.one())
    assert json.loads(values["input.value"]) == {"prompt": PROMPT, "image": "<BytesIO>"}
    assert "FILE-BYTES-MARKER" not in traced.wire()
    assert json.loads(values["gen_ai.request.parameters"])["file_encoding_strategy"] == "base64"


def test_the_package_never_reads_the_environment():
    # PRD R-06: the instrumentor never reads REPLICATE_API_TOKEN; it only
    # redacts the copies the client itself stored.
    package = Path(traceai_replicate.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text()
        assert not re.search(r"\benviron\b|\bgetenv\b", text), source.name
