import importlib
import json
import sys
from pathlib import Path

import pytest

from conftest import MARKER, WEBHOOK_SECRET, raw
from traceai_elevenlabs_agents.backfill import BackfillError, backfill, fetch_conversation
from traceai_elevenlabs_agents.verify import sign

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import standin  # noqa: E402

API_KEY = "xi_test_key_6e2d9a0c4b8f1e3d7a5c"


@pytest.fixture
def get_server():
    server = standin.GetStandin(API_KEY)
    yield server
    server.close()


def test_backfill_forwards_a_conversation(make_forwarder, get_server):
    delivery = standin.build_delivery("post_call_transcription_otel.json", MARKER)
    conv = get_server.add_from_delivery(delivery, user_id="user-7")
    forwarder = make_forwarder(elevenlabs_api_key=API_KEY, elevenlabs_base_url=get_server.base_url)
    status, payload = backfill(forwarder, conv)
    assert (status, payload["status"]) == (200, "forwarded")
    assert get_server.requests[0]["query"]["format"] == ["opentelemetry"]
    assert get_server.requests[0]["path"] == "/v1/convai/conversations/" + conv
    root = [s for s in forwarder.exporter.spans() if "parentSpanId" not in s][0]
    attrs = {a["key"]: a["value"] for a in root["attributes"]}
    assert attrs["user.id"] == {"stringValue": "user-7"}  # GET carries user_id; inputs are captured by default


def test_backfill_after_webhook_converges(make_forwarder, get_server):
    delivery = standin.build_delivery("post_call_transcription_otel.json", MARKER)
    conv = get_server.add_from_delivery(delivery)
    forwarder = make_forwarder(elevenlabs_api_key=API_KEY, elevenlabs_base_url=get_server.base_url)
    body = raw(delivery)
    assert forwarder.handle_webhook(body, sign(WEBHOOK_SECRET, body))[0] == 200
    status, payload = backfill(forwarder, conv)
    assert (status, payload["status"]) == (200, "duplicate")
    assert len(forwarder.exporter.batches) == 1


def test_force_reexports_with_fresh_span_ids_in_the_same_trace(make_forwarder, get_server):
    delivery = standin.build_delivery("post_call_transcription_otel.json", MARKER)
    conv = get_server.add_from_delivery(delivery)
    forwarder = make_forwarder(elevenlabs_api_key=API_KEY, elevenlabs_base_url=get_server.base_url)
    assert backfill(forwarder, conv)[0] == 200
    assert backfill(forwarder, conv, force=True)[0] == 200
    first, second = forwarder.exporter.batches

    def ids(batch):
        return {s["spanId"] for rs in batch["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]}

    def traces(batch):
        return {s["traceId"] for rs in batch["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]}

    assert traces(first) == traces(second)
    assert not ids(first) & ids(second)  # why --force duplicates spans: ElevenLabs re-mints span ids


def test_backfill_errors(make_forwarder, get_server):
    forwarder = make_forwarder(elevenlabs_api_key="wrong", elevenlabs_base_url=get_server.base_url)
    with pytest.raises(BackfillError) as info:
        backfill(forwarder, "conv_missing")
    assert "401" in str(info.value)
    forwarder = make_forwarder(elevenlabs_api_key=API_KEY, elevenlabs_base_url=get_server.base_url)
    with pytest.raises(BackfillError) as info:
        backfill(forwarder, "conv_missing")
    assert "404" in str(info.value)
    with pytest.raises(BackfillError):
        fetch_conversation("c", "", get_server.base_url)


def test_get_without_otlp_traces_is_400(make_forwarder, get_server, monkeypatch):
    forwarder = make_forwarder(elevenlabs_api_key=API_KEY, elevenlabs_base_url=get_server.base_url)
    module = importlib.import_module("traceai_elevenlabs_agents.backfill")
    monkeypatch.setattr(module, "fetch_conversation", lambda *a, **k: {"conversation_id": "c", "agent_id": "a"})
    assert backfill(forwarder, "c") == (400, {"error": "missing_otlp_traces", "conversation_id": "c"})


def test_standin_get_body_parses_with_the_real_sdk(get_server):
    """The stand-in GET body is accepted by the real SDK client (``conversations.get(format=...)``)."""
    pytest.importorskip("elevenlabs")
    from elevenlabs.client import ElevenLabs

    delivery = standin.build_delivery("post_call_transcription_otel.json", MARKER)
    conv = get_server.add_from_delivery(delivery, user_id="user-7")
    client = ElevenLabs(api_key=API_KEY, base_url=get_server.base_url)
    conversation = client.conversational_ai.conversations.get(conversation_id=conv, format="opentelemetry")
    assert conversation.conversation_id == conv
    assert conversation.user_id == "user-7"
    names = [s["name"] for s in conversation.otlp_traces["resourceSpans"][0]["scopeSpans"][0]["spans"]]
    assert names[0] == "elevenlabs.conversation"
    plain = client.conversational_ai.conversations.get(conversation_id=conv)
    assert plain.otlp_traces is None
    assert json.dumps(conversation.otlp_traces).count(MARKER) > 0
