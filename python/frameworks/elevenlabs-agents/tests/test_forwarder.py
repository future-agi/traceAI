import json
import time

import pytest

from conftest import (
    FI_API_KEY,
    FI_SECRET_KEY,
    MARKER,
    NEVER_NEEDLES,
    WEBHOOK_SECRET,
    RecordingExporter,
    hide_all,
    load_fixture,
    raw,
)
from traceai_elevenlabs_agents import map as map_mod
from traceai_elevenlabs_agents.exporter import ExportError
from traceai_elevenlabs_agents.store import ForwardedStore
from traceai_elevenlabs_agents.verify import sign

OK = raw(load_fixture("post_call_transcription_otel.json"))


def deliver(forwarder, body=OK, header=None):
    return forwarder.handle_webhook(body, sign(WEBHOOK_SECRET, body) if header is None else header)


def test_good_signature_is_forwarded_once(make_forwarder):
    forwarder = make_forwarder()
    status, payload = deliver(forwarder)
    assert status == 200
    assert payload["status"] == "forwarded"
    assert payload["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert payload["spans"] == 8
    assert len(forwarder.exporter.batches) == 1


@pytest.mark.parametrize(
    "header",
    [
        "",
        "t=1760000000,v0=" + "0" * 64,
        sign("wrong-secret", OK),
        sign(WEBHOOK_SECRET, OK, int(time.time()) - 3600),
    ],
)
def test_bad_signatures_return_401_and_store_nothing(make_forwarder, header):
    forwarder = make_forwarder()
    status, payload = deliver(forwarder, header=header)
    assert (status, payload) == (401, {"error": "invalid_signature"})
    assert forwarder.exporter.batches == []
    assert forwarder.store.state("conv_9001k1zph3fkeh5s8xg9z90swaqa") is None


def test_mutated_body_returns_401(make_forwarder):
    forwarder = make_forwarder()
    header = sign(WEBHOOK_SECRET, OK)
    status, _ = forwarder.handle_webhook(OK.replace(b"Goodbye", b"G00dbye"), header)
    assert status == 401
    assert forwarder.exporter.batches == []


@pytest.mark.parametrize("kind", ["post_call_transcription", "post_call_audio", "call_initiation_failure"])
def test_other_webhook_types_ack_and_store_nothing(make_forwarder, kind):
    forwarder = make_forwarder()
    body = raw(load_fixture("other_webhook_types.json")[kind])
    status, payload = deliver(forwarder, body)
    assert (status, payload) == (200, {"status": "ignored"})
    assert forwarder.exporter.batches == []


def test_duplicate_delivery_yields_one_export(make_forwarder):
    forwarder = make_forwarder()
    assert deliver(forwarder)[0] == 200
    # ElevenLabs retries with a new timestamp, so a new signature.
    status, payload = deliver(forwarder, header=sign(WEBHOOK_SECRET, OK, int(time.time()) + 1))
    assert (status, payload["status"]) == (200, "duplicate")
    roots = [s for s in forwarder.exporter.spans() if "parentSpanId" not in s]
    assert len(roots) == 1


def test_idempotency_record_survives_a_restart(tmp_path, make_forwarder):
    state = str(tmp_path / "state" / "forwarded.sqlite3")
    first = make_forwarder(state_path=state)
    assert deliver(first)[0] == 200
    first.store.close()
    second = make_forwarder(state_path=state)
    status, payload = deliver(second)
    assert (status, payload["status"]) == (200, "duplicate")
    assert second.exporter.batches == []


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda b: b["data"].pop("otlp_traces"), "missing_resource_spans"),
        (lambda b: b["data"]["otlp_traces"].update(resourceSpans=[]), "missing_resource_spans"),
        (lambda b: b.update(data="x"), "missing_data"),
    ],
)
def test_malformed_otlp_returns_400_and_stores_nothing(make_forwarder, mutate, code):
    forwarder = make_forwarder()
    body = load_fixture("post_call_transcription_otel.json")
    mutate(body)
    status, payload = deliver(forwarder, raw(body))
    assert (status, payload) == (400, {"error": code})
    assert forwarder.exporter.batches == []


def test_invalid_json_after_valid_signature_is_400(make_forwarder):
    forwarder = make_forwarder()
    status, payload = deliver(forwarder, b"{not json")
    assert (status, payload) == (400, {"error": "invalid_json"})


def test_mapper_bug_returns_500_and_exports_nothing(make_forwarder, monkeypatch):
    forwarder = make_forwarder()

    def boom(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr("traceai_elevenlabs_agents.forwarder.map_otlp_traces", boom)
    status, payload = deliver(forwarder)
    assert (status, payload) == (500, {"error": "mapping_failed"})
    assert forwarder.exporter.batches == []
    assert forwarder.store.state("conv_9001k1zph3fkeh5s8xg9z90swaqa") is None


def test_export_failure_returns_502_releases_claim_and_retry_succeeds(make_forwarder):
    exporter = RecordingExporter(fail=ExportError("collector returned HTTP 503", 503))
    forwarder = make_forwarder(exporter=exporter)
    status, payload = deliver(forwarder)
    assert (status, payload["error"]) == (502, "export_failed")
    assert forwarder.store.state("conv_9001k1zph3fkeh5s8xg9z90swaqa") is None
    exporter.fail = None
    status, payload = deliver(forwarder)
    assert (status, payload["status"]) == (200, "forwarded")
    assert len(exporter.batches) == 1


def test_ack_only_after_export(make_forwarder):
    # AC-13 at the unit boundary: 2xx is returned only after the exporter accepted the batch.
    order = []

    class Exporter(RecordingExporter):
        def export(self, request):
            order.append("export")
            return super().export(request)

    forwarder = make_forwarder(exporter=Exporter())
    status, _ = deliver(forwarder)
    order.append("ack:%d" % status)
    assert order == ["export", "ack:200"]
    assert forwarder.store.state("conv_9001k1zph3fkeh5s8xg9z90swaqa") == "done"


def test_concurrent_delivery_of_same_conversation_is_deferred(make_forwarder):
    forwarder = make_forwarder()
    assert forwarder.store.claim("conv_9001k1zph3fkeh5s8xg9z90swaqa", "post_call_webhook") == "claimed"
    status, payload = deliver(forwarder)
    assert (status, payload["error"]) == (503, "in_flight")
    assert forwarder.exporter.batches == []


def test_stale_pending_claim_is_taken_over(tmp_path):
    store = ForwardedStore(str(tmp_path / "s.sqlite3"), stale_after_secs=0)
    assert store.claim("c1", "post_call_webhook") == "claimed"
    time.sleep(0.01)
    assert store.claim("c1", "post_call_webhook") == "claimed"
    store.complete("c1", "t")
    assert store.claim("c1", "backfill") == "duplicate"
    assert store.claim("c1", "backfill", force=True) == "claimed"
    store.close()


def test_paused_forwarder_returns_503(make_forwarder):
    forwarder = make_forwarder(paused=True)
    assert deliver(forwarder) == (503, {"error": "paused"})
    assert forwarder.exporter.batches == []


def test_logs_and_responses_carry_no_secret_signature_or_content(make_forwarder, log_capture):
    forwarder = make_forwarder()
    header = sign(WEBHOOK_SECRET, OK)
    responses = [
        forwarder.handle_webhook(OK, header),
        forwarder.handle_webhook(OK, header),
        forwarder.handle_webhook(OK, sign("wrong", OK)),
        forwarder.handle_webhook(OK.replace(b"Goodbye", b"Bye"), header),
        forwarder.handle_webhook(raw(load_fixture("other_webhook_types.json")["post_call_audio"]),
                                 sign(WEBHOOK_SECRET, raw(load_fixture("other_webhook_types.json")["post_call_audio"]))),
    ]
    # Logs and HTTP responses never carry content, in any mode.
    text = log_capture.text + json.dumps(responses)
    for needle in (WEBHOOK_SECRET, FI_API_KEY, FI_SECRET_KEY, header.split("v0=")[1], MARKER, "UklGRg"):
        assert needle not in text
    # Spans never carry a credential or the signature.
    exported = json.dumps(forwarder.exporter.batches)
    for needle in (WEBHOOK_SECRET, FI_API_KEY, FI_SECRET_KEY, header.split("v0=")[1], "UklGRg"):
        assert needle not in exported
    assert "conv_9001k1zph3fkeh5s8xg9z90swaqa" in log_capture.text


def test_content_by_default_and_the_standard_hide_flags_remove_it(make_forwarder):
    default = make_forwarder()
    hidden = make_forwarder(trace_config=hide_all())
    deliver(default)
    deliver(hidden)
    assert MARKER in json.dumps(default.exporter.batches)
    assert MARKER not in json.dumps(hidden.exporter.batches)
    for forwarder in (default, hidden):
        blob = json.dumps(forwarder.exporter.batches)
        for needle in (WEBHOOK_SECRET, FI_API_KEY, FI_SECRET_KEY) + NEVER_NEEDLES:
            assert needle not in blob


def test_webhook_user_id_is_written_when_present(make_forwarder):
    body = load_fixture("post_call_transcription_otel.json")
    body["data"]["user_id"] = "caller-17"
    forwarder = make_forwarder()
    assert deliver(forwarder, raw(body))[0] == 200
    root = [s for s in forwarder.exporter.spans() if "parentSpanId" not in s][0]
    assert map_mod.attributes_to_dict(root["attributes"])["user.id"] == "caller-17"


def test_two_forwarders_bind_two_projects(make_forwarder):
    a = make_forwarder(project_name="project-a")
    b = make_forwarder(project_name="project-b")
    deliver(a)
    deliver(b)
    names = []
    for forwarder in (a, b):
        for batch in forwarder.exporter.batches:
            resources = [map_mod.attributes_to_dict(rs["resource"]["attributes"]) for rs in batch["resourceSpans"]]
            names.append({r["project_name"] for r in resources})
    assert names == [{"project-a"}, {"project-b"}]


# --- bodies captured from a live ElevenLabs post-call webhook, 2026-10-19 ---------------------------

LIVE = "live_webhook_post_call_transcription_otel.json"
LIVE_TIMEOUT = "live_webhook_post_call_transcription_otel_timeout.json"


def test_live_webhook_body_is_forwarded_with_hierarchy_model_tokens_and_user(make_forwarder):
    forwarder = make_forwarder()
    status, payload = deliver(forwarder, raw(load_fixture(LIVE)))
    assert (status, payload["status"]) == (200, "forwarded")
    spans = forwarder.exporter.spans()
    attrs = {s["spanId"]: map_mod.attributes_to_dict(s["attributes"]) for s in spans}
    root = [s for s in spans if "parentSpanId" not in s]
    assert len(root) == 1 and attrs[root[0]["spanId"]]["fi.span.kind"] == "CONVERSATION"
    r = attrs[root[0]["spanId"]]
    # the live body has no data.user_id: the id is the root attribute elevenlabs.user_id
    assert r["user.id"] == "live-user-42"
    assert r["session.id"].startswith("conv_LIVE")
    kinds = [a.get("fi.span.kind") for a in attrs.values()]
    assert kinds.count("TOOL") == 1 and kinds.count("LLM") >= 1
    llm = [a for a in attrs.values() if a.get("fi.span.kind") == "LLM"]
    assert all(a["gen_ai.request.model"] == "gpt-4o-mini" for a in llm)
    assert any(int(a.get("gen_ai.usage.input_tokens", 0)) > 0 for a in llm)


def test_live_webhook_without_user_has_no_user_id(make_forwarder):
    forwarder = make_forwarder()
    assert deliver(forwarder, raw(load_fixture(LIVE_TIMEOUT)))[0] == 200
    root = [s for s in forwarder.exporter.spans() if "parentSpanId" not in s][0]
    assert "user.id" not in map_mod.attributes_to_dict(root["attributes"])


def test_live_timeout_call_is_ok_status_and_two_minutes_long(make_forwarder):
    # ElevenLabs reports status OK even when the call hit the maximum duration (no ERROR in 3 live cases)
    forwarder = make_forwarder()
    assert deliver(forwarder, raw(load_fixture(LIVE_TIMEOUT)))[0] == 200
    root = [s for s in forwarder.exporter.spans() if "parentSpanId" not in s][0]
    a = map_mod.attributes_to_dict(root["attributes"])
    assert root["status"]["code"] == 1 and a["call.duration"] == 121.0


def test_live_webhook_body_hides_user_id_with_hide_inputs(make_forwarder):
    forwarder = make_forwarder(trace_config=hide_all())
    assert deliver(forwarder, raw(load_fixture(LIVE)))[0] == 200
    blob = json.dumps(forwarder.exporter.batches)
    assert "live-user-42" not in blob and "Encourage flow" not in blob
