"""Each Messages call reaches the Future AGI collector path once, with what Observe reads.

Real path: Anthropic SDK native tracing (or AnthropicInstrumentor) -> fi_instrumentation
register() provider -> OTLP/HTTP exporter -> loopback harness Receiver. Synthetic
transport and credentials; nothing leaves 127.0.0.1.
"""

import inspect
import json
import sys
from pathlib import Path

import anthropic
import pytest

try:
    import httpx2 as httpx
except ImportError:
    import httpx

_TESTS_DIR = Path(__file__).resolve().parents[3] / "tests"
if not (_TESTS_DIR / "harness" / "__init__.py").exists():
    pytest.skip("needs the repository's OTLP test harness", allow_module_level=True)
sys.path.insert(0, str(_TESTS_DIR))

from harness import Receiver  # noqa: E402

NATIVE = "open_telemetry" in inspect.signature(anthropic.Anthropic).parameters
pytestmark = pytest.mark.skipif(not NATIVE, reason="SDK predates native OpenTelemetry")

PROMPT = "synthetic prompt"
ANSWER = "synthetic answer"
PARAMS = dict(model="local-test", max_tokens=16, messages=[{"role": "user", "content": PROMPT}])
MESSAGE = {
    "id": "msg_local",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": ANSWER}],
    "model": "local-test",
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 3,
        "output_tokens": 2,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 5,
    },
}


def respond(request):
    return httpx.Response(200, json=MESSAGE)


def attributes(span):
    values = {}
    for item in span.get("attributes", []):
        value = item["value"]
        values[item["key"]] = next(iter(value.values())) if value else None
    return values


@pytest.fixture
def collector(monkeypatch):
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv("FI_API_KEY", "synthetic-api-key")
        monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret-key")
        yield receiver


def register_provider():
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    return register(project_type=ProjectType.OBSERVE, project_name="anthropic_native_contract", verbose=False)


def send(provider, content_mode=None):
    traces = {"content_mode": content_mode} if content_mode else {}
    with anthropic.Anthropic(
        api_key="synthetic",
        base_url="http://localhost",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        open_telemetry={"tracer_provider": provider, "traces": traces},
    ) as client:
        assert client.messages.create(**PARAMS).content[0].text == ANSWER


def assert_collector_auth(collector):
    requests = collector.requests()
    assert requests and {r["path"] for r in requests} == {"/tracer/v1/traces"}
    assert requests[0]["headers"]["x-api-key"] == "synthetic-api-key"
    assert requests[0]["headers"]["x-secret-key"] == "synthetic-secret-key"


def test_native_span_reaches_the_collector_once_with_model_usage_and_content(collector):
    provider = register_provider()
    try:
        send(provider, content_mode="content")
        provider.force_flush()
    finally:
        provider.shutdown()

    assert_collector_auth(collector)
    spans = collector.spans()
    assert [span["name"] for span in spans] == ["anthropic.messages.create"]
    values = attributes(spans[0])
    assert values["gen_ai.provider.name"] == "anthropic"
    assert values["gen_ai.operation.name"] == "chat"
    assert values["gen_ai.request.model"] == "local-test"
    assert values["gen_ai.response.model"] == "local-test"
    # Native input usage already includes cache reads and writes.
    assert int(values["gen_ai.usage.input_tokens"]) == 12
    assert int(values["gen_ai.usage.output_tokens"]) == 2
    assert PROMPT in json.loads(values["gen_ai.input.messages"])[0]["parts"][0]["content"]
    assert ANSWER in json.loads(values["gen_ai.output.messages"])[0]["parts"][0]["content"]


def test_instrumented_client_reaches_the_collector_once_with_future_agi_context(collector):
    from fi_instrumentation import using_attributes
    from traceai_anthropic import AnthropicInstrumentor

    provider = register_provider()
    instrumentor = AnthropicInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with using_attributes(session_id="sess-1", user_id="user-1"):
            send(provider, content_mode="content")
        provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()

    assert_collector_auth(collector)
    spans = collector.spans()
    assert [span["name"] for span in spans] == ["Messages"]
    values = attributes(spans[0])
    assert values["session.id"] == "sess-1"
    assert values["user.id"] == "user-1"
    assert values["gen_ai.span.kind"] == "LLM"
    assert PROMPT in values["input.value"]
    assert ANSWER in values["output.value"]


def test_native_default_exports_metadata_without_content(collector):
    provider = register_provider()
    try:
        send(provider)
        provider.force_flush()
    finally:
        provider.shutdown()
    spans = collector.spans()
    assert [span["name"] for span in spans] == ["anthropic.messages.create"]
    payload = json.dumps(spans)
    assert PROMPT not in payload and ANSWER not in payload
    assert attributes(spans[0])["gen_ai.request.model"] == "local-test"
