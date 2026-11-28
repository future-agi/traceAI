"""Optional RAG reads use the hosted API contract, checked by the real SDK."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import WEBHOOK_SECRET, load_fixture, raw
from test_server import Collector
from traceai_elevenlabs_agents.config import ForwarderConfig
from traceai_elevenlabs_agents.forwarder import Forwarder
from traceai_elevenlabs_agents.map import attributes_to_dict, iter_spans
from traceai_elevenlabs_agents.verify import sign

API_KEY = "xi_synthetic_rag_key"
CONTENT = "Synthetic retrieved reference: USD to EUR is 0.92."
MODEL = "multilingual_e5_large_instruct"
TURN_ID = "a000000000000004"


class RagAPI:
    def __init__(self):
        self.delivery = load_fixture("post_call_transcription_otel.json")
        self.conversation_id = self.delivery["data"]["conversation_id"]
        self.info = {
            "chunks": [{"document_id": "doc_test", "chunk_id": "chunk_test", "vector_distance": 0.12}],
            "embedding_model": MODEL,
            "retrieval_query": "usd eur rate zqx-th8231-privacy-marker-7f3a",
        }
        self.conversation = {
            "agent_id": "agent_test", "conversation_id": self.conversation_id,
            "status": "done", "metadata": {"start_time_unix_secs": 1, "call_duration_secs": 5},
            "analysis": None,
            "transcript": [
                {"role": "agent", "message": "Hello", "time_in_call_secs": 0},
                {"role": "user", "message": "Rate?", "time_in_call_secs": 1},
                {"role": "agent", "message": "0.92", "time_in_call_secs": 2, "rag_retrieval_info": self.info},
            ],
        }
        self.content = CONTENT
        self.requests = []
        self.status = 200
        self.redirect = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                owner.requests.append((parts.path, query, self.headers.get("xi-api-key")))
                status = owner.status
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", owner.redirect)
                    self.end_headers()
                    return
                if self.headers.get("xi-api-key") != API_KEY:
                    status = 401
                if parts.path.startswith("/v1/convai/conversations/"):
                    payload = dict(owner.conversation)
                    if query.get("format") == ["opentelemetry"]:
                        payload["otlp_traces"] = owner.delivery["data"]["otlp_traces"]
                else:
                    payload = {"id": "chunk_test", "name": "Synthetic document", "content": owner.content}
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def rag_api():
    api = RagAPI()
    yield api
    api.close()


def configured_forwarder(api, collector, **env):
    values = {
        "FI_API_KEY": "fi_test_api", "FI_SECRET_KEY": "fi_test_secret",
        "FI_PROJECT_NAME": "rag-test", "FI_BASE_URL": collector.base_url,
        "ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET, "ELEVENLABS_API_KEY": API_KEY,
        "FI_ELEVENLABS_STATE_PATH": ":memory:", "FI_ELEVENLABS_API_BASE_URL": api.base_url,
        "FI_ELEVENLABS_ENRICH_RAG": "true",
    }
    values.update(env)
    return Forwarder(ForwarderConfig.from_env(values))


def deliver(forwarder, api):
    body = raw(api.delivery)
    return forwarder.handle_webhook(body, sign(WEBHOOK_SECRET, body))


def test_real_sdk_rag_contract_and_opt_in_forwarding(rag_api):
    from elevenlabs.client import ElevenLabs

    # The real framework parses both hosted responses; no vendor/model request is made.
    client = ElevenLabs(api_key=API_KEY, base_url=rag_api.base_url)
    conversation = client.conversational_ai.conversations.get(conversation_id=rag_api.conversation_id)
    info = conversation.transcript[2].rag_retrieval_info
    assert info.retrieval_query == rag_api.info["retrieval_query"]
    chunk = client.conversational_ai.knowledge_base.documents.chunk.get(
        info.chunks[0].document_id, info.chunks[0].chunk_id, embedding_model=info.embedding_model,
    )
    assert chunk.content == CONTENT
    sdk_requests = list(rag_api.requests)
    rag_api.requests.clear()
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector)
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        payload = json.loads(collector.requests[0]["body"])
        turn = next(s for _, s in iter_spans(payload) if s["spanId"] == TURN_ID)
        attrs = attributes_to_dict(turn["attributes"])
        assert attrs.get("retrieval.documents.0.document.content") == chunk.content
        assert rag_api.requests == sdk_requests
        assert attrs["fi.span.kind"] == "LLM"  # retrieval did not replace the generation
        original = list(iter_spans(rag_api.delivery["data"]["otlp_traces"]))
        exported = list(iter_spans(payload))
        assert len(exported) == len(original)
        for (_, before), (_, after) in zip(original, exported):
            for key in ("traceId", "spanId", "parentSpanId", "startTimeUnixNano", "endTimeUnixNano"):
                assert before.get(key) == after.get(key)
        assert deliver(forwarder, rag_api)[1]["status"] == "duplicate"
        assert len(collector.requests) == 1
        assert rag_api.requests == sdk_requests  # no reads on a retry
    finally:
        forwarder.store.close()
        collector.close()


@pytest.mark.parametrize("flag", [
    "FI_HIDE_INPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_INPUT_TEXT",
    "FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_OUTPUT_TEXT",
])
def test_hidden_content_does_not_read_rag_api(rag_api, flag):
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector, **{flag: "true"})
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        assert rag_api.requests == []
        assert CONTENT.encode() not in collector.requests[0]["body"]
        assert b"retrieval.documents" not in collector.requests[0]["body"]
    finally:
        forwarder.store.close()
        collector.close()


def test_disabled_by_default(rag_api):
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector, FI_ELEVENLABS_ENRICH_RAG="false", ELEVENLABS_API_KEY="")
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        assert rag_api.requests == []
    finally:
        forwarder.store.close()
        collector.close()


@pytest.mark.parametrize("case", ["index", "query", "model", "role", "conversation", "no_rag"])
def test_never_guess_a_join(rag_api, case):
    if case == "index":
        span = next(s for _, s in iter_spans(rag_api.delivery["data"]["otlp_traces"]) if s["spanId"] == TURN_ID)
        span["attributes"] = [a for a in span["attributes"] if a["key"] != "elevenlabs.turn.index"]
    elif case == "query":
        rag_api.info["retrieval_query"] = "different query"
    elif case == "model":
        rag_api.info["embedding_model"] = "different model"
    elif case == "role":
        rag_api.conversation["transcript"][2]["role"] = "user"
    elif case == "conversation":
        rag_api.conversation["conversation_id"] = "different conversation"
    else:
        for _, span in iter_spans(rag_api.delivery["data"]["otlp_traces"]):
            span["attributes"] = [a for a in span["attributes"] if not a["key"].startswith("elevenlabs.rag.")]
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector)
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        assert b"retrieval.documents" not in collector.requests[0]["body"]
        assert len(rag_api.requests) == (0 if case == "no_rag" else 1)
    finally:
        forwarder.store.close()
        collector.close()


def test_reads_and_exported_content_are_bounded_and_cached(rag_api):
    from traceai_elevenlabs_agents.rag import MAX_CONTENT_BYTES, MAX_DOCUMENTS

    rag_api.info["chunks"] *= MAX_DOCUMENTS + 3
    rag_api.content = "界" * MAX_CONTENT_BYTES
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector)
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        assert len(rag_api.requests) == 2  # the same (document, chunk, model) fetched once
        exported = json.loads(collector.requests[0]["body"])
        turn = next(s for _, s in iter_spans(exported) if s["spanId"] == TURN_ID)
        attrs = attributes_to_dict(turn["attributes"])
        contents = [v for k, v in attrs.items() if k.startswith("retrieval.documents.") and k.endswith(".content")]
        assert len(contents) == MAX_DOCUMENTS
        assert all(len(c.encode()) <= MAX_CONTENT_BYTES for c in contents)
        assert attrs["elevenlabs.rag.enrichment.status"] == "limited"
        metadata = json.loads(attrs["retrieval.documents.0.document.metadata"])
        assert metadata["content_truncated"] is True
        assert metadata["snapshot"] == "current"
        assert metadata["source"] == "elevenlabs.knowledge_base_api"
    finally:
        forwarder.store.close()
        collector.close()


@pytest.mark.parametrize("case", ["http", "redirect", "oversized", "timeout"])
def test_optional_api_failures_preserve_trace_without_error_text(rag_api, monkeypatch, caplog, case):
    from traceai_elevenlabs_agents import rag

    if case == "http":
        rag_api.status = 403
    elif case == "redirect":
        rag_api.redirect = rag_api.base_url + "/must-not-receive-key"
    elif case == "oversized":
        monkeypatch.setattr(rag, "MAX_RESPONSE_BYTES", 32)
    else:
        def timeout(*args, **kwargs):
            raise TimeoutError("private-url-with-synthetic-key")
        monkeypatch.setattr(rag, "_get_json", timeout)
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector)
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        assert len(rag_api.requests) == (0 if case == "timeout" else 1)
        body = collector.requests[0]["body"]
        assert b"unavailable" in body
        assert "RAG enrichment unavailable" in caplog.text
        assert "private-url-with-synthetic-key" not in caplog.text
        assert API_KEY not in caplog.text
        assert API_KEY.encode() not in body
        assert b"must-not-receive-key" not in body
        assert CONTENT.encode() not in body
    finally:
        forwarder.store.close()
        collector.close()


def test_rag_privacy_and_pii_mask(rag_api):
    from dataclasses import fields
    from fi_instrumentation import TraceConfig

    if "pii_redaction" not in {f.name for f in fields(TraceConfig)}:
        pytest.skip("The minimum fi-instrumentation-otel predates the PII switch")
    rag_api.content = "Contact fake@example.com from private.internal at /home/test/file or +1-415-555-2671"
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector, FI_PII_REDACTION="true")
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        exported = json.loads(collector.requests[0]["body"])
        turn = next(s for _, s in iter_spans(exported) if s["spanId"] == TURN_ID)
        content = attributes_to_dict(turn["attributes"])["retrieval.documents.0.document.content"]
        for needle in ("fake@example.com", "private.internal", "/home/test/file", "415-555-2671"):
            assert needle not in content
    finally:
        forwarder.store.close()
        collector.close()


@pytest.mark.parametrize("content", [
    "Download https://example.invalid/file?signature=synthetic-token now",
    "Traceback (most recent call last):\n  File \"/home/test/private.py\", line 3\nValueError: invalid reference",
])
def test_signed_urls_and_stacks_never_leave(rag_api, content):
    rag_api.content = content
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector)
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        body = collector.requests[0]["body"]
        assert b"synthetic-token" not in body
        assert b"private.py" not in body
        assert b"Traceback" not in body
    finally:
        forwarder.store.close()
        collector.close()


def test_invalid_signature_never_triggers_reads(rag_api):
    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector)
    try:
        body = raw(rag_api.delivery)
        assert forwarder.handle_webhook(body, sign("wrong", body))[0] == 401
        assert rag_api.requests == []
        assert collector.requests == []
    finally:
        forwarder.store.close()
        collector.close()


def test_opt_in_requires_api_key():
    from traceai_elevenlabs_agents.config import ConfigError

    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY"):
        ForwarderConfig(fi_api_key="fake", fi_secret_key="fake", project_name="test", enrich_rag=True)


def test_backfill_enrichment_uses_the_same_claim(rag_api):
    from traceai_elevenlabs_agents.backfill import backfill

    collector = Collector()
    forwarder = configured_forwarder(rag_api, collector)
    try:
        assert backfill(forwarder, rag_api.conversation_id)[0] == 200
        assert CONTENT.encode() in collector.requests[0]["body"]
        assert len(rag_api.requests) == 3  # OTLP, regular conversation, chunk
        assert deliver(forwarder, rag_api)[1]["status"] == "duplicate"
        assert len(rag_api.requests) == 3
        assert len(collector.requests) == 1
    finally:
        forwarder.store.close()
        collector.close()
