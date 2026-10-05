"""examples/search_and_answer.py runs end to end into harness.Receiver.

The example's ``main()`` takes optional clients so the test can hand it real
clients on the loopback fake's channel; everything else (register(),
instrument(), the calls and the flush) is the example's own code. Without
clients it builds them with Application Default Credentials, which this test
never does. The architecture asks for no child process here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402

from _discoveryengine_support import (  # noqa: E402
    CONTENT_MARKERS,
    SEARCH_RESULTS,
    SERVING_CONFIG,
    FakeDiscoveryEngine,
    answer_client,
    search_client,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "search_and_answer.py"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"


def _load_example():
    spec = importlib.util.spec_from_file_location("discoveryengine_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_exports_one_search_and_one_answer_span(monkeypatch, capsys):
    from traceai_discoveryengine import DiscoveryEngineInstrumentor

    example = _load_example()
    with FakeDiscoveryEngine() as fake, Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
        monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
        monkeypatch.setenv("DISCOVERY_ENGINE_SERVING_CONFIG", SERVING_CONFIG)
        try:
            example.main(search_client=search_client(fake), answer_client=answer_client(fake))
        finally:
            # instrument() is a process-wide singleton; leave nothing wrapped.
            DiscoveryEngineInstrumentor().uninstrument()
        spans = receiver.spans()
        exports = receiver.requests()

    assert fake.methods() == ["Search", "AnswerQuery"]
    assert capsys.readouterr().out.splitlines() == [
        "{0} results on the first page".format(SEARCH_RESULTS),
        "answer state: SUCCEEDED",
    ]
    assert [span["name"] for span in spans] == [
        "discoveryengine.search",
        "discoveryengine.answer_query",
    ]
    search, answer = (_flatten_attributes(span["attributes"]) for span in spans)
    assert search["fi.span.kind"] == answer["fi.span.kind"] == "RETRIEVER"
    assert search["discoveryengine.serving_config"] == SERVING_CONFIG
    assert int(search["discoveryengine.result_count"]) == SEARCH_RESULTS
    assert answer["discoveryengine.answer.state"] == "SUCCEEDED"
    assert all(span["status"]["code"] == "STATUS_CODE_OK" for span in spans)

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "discoveryengine-example"
            assert resource["project_type"] == "observe"

    wire = json.dumps(spans)
    for marker in CONTENT_MARKERS + (example.QUERY, example.QUESTION):
        assert marker not in wire, marker


@pytest.mark.parametrize(
    "serving_config, endpoint",
    [
        (SERVING_CONFIG, None),
        (SERVING_CONFIG.replace("/locations/global/", "/locations/eu/"), "eu-discoveryengine.googleapis.com"),
        (SERVING_CONFIG.replace("/locations/global/", "/locations/us/"), "us-discoveryengine.googleapis.com"),
    ],
    ids=["global", "eu", "us"],
)
def test_example_picks_the_regional_endpoint_from_the_serving_config(serving_config, endpoint):
    options = _load_example().client_options(serving_config)
    assert options.get("api_endpoint") == endpoint
