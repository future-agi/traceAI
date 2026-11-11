"""Actual TLS loopback transfers; only synthetic credentials and audio."""

import io
import json
import ssl
import subprocess
import threading
import uuid
import wave
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value

from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, Bytes
from fi_instrumentation.attachments.protocol import HTTPTransport, TransferError
from test_policy_runtime import CONTEXT


def expiry(seconds=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def server(tmp_path):
    key, cert = tmp_path / "synthetic.key", tmp_path / "synthetic.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
    state = {"requests": [], "payloads": [], "fail": None, "policy": {}, "expires": 60}
    scope = {k: str(uuid.uuid4()) for k in ("organization_id", "workspace_id", "project_id")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body, headers=None):
            data = json.dumps(body).encode()
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body, dict(self.headers)))
            if self.path.endswith("attachment-sessions"):
                self.respond(200, {"schema_version": 1, "scope": scope,
                    "capture_session_id": body["capture_session_id"], "expires_at": expiry(state["expires"]),
                    "transfer_modes": ["authenticated_stream"],
                    "policy": {"version": "v1", "enabled": True, "allowed_sources": ["Bytes", "OwnedRecordingFile"],
                               "allowed_modalities": ["audio"], "require_media_redaction": False,
                               "require_metadata_removal": False, "allow_unredacted_media_with_text_pii": False,
                               "record_audio": False, "record_video": False, **state["policy"]},
                    "limits": {"max_upload_bytes": 8388608, "segment_bytes": 8388608,
                               "segment_duration_ms": 30000, "recording_bytes": 1073741824,
                               "recording_duration_ms": 7200000, "active_transfers": 16,
                               "active_recordings": 4, "reserved_bytes": 1073741824,
                               "validation_jobs": 1024, "initializations_per_minute": 120,
                               "links_per_minute": 1000, "upload_bytes_per_second": 1048576,
                               "playback_bytes_per_second": 1048576, "transfer_timeout_seconds": 30,
                               "grant_ttl_seconds": 60, "revocation_check_seconds": 5}})
            elif self.path.endswith("/uploads"):
                upload_id = str(uuid.uuid4())
                state["upload_id"] = upload_id
                self.respond(201, {"upload_id": upload_id, "attachment_id": body["attachment_id"],
                    "occurrence_id": body["occurrence_id"], "state": "pending_upload", "reserved_bytes": body["declared_bytes"],
                    "expires_at": expiry(), "transfer_mode": "authenticated_stream",
                    "write_capability": "SYNTHETIC_UPLOAD_ONLY"})
            elif self.path.endswith("/complete"):
                self.respond(202, {"upload_id": state["upload_id"], "state": "validating", "reason": "binding_pending"})
            elif self.path.endswith("/abort"):
                self.respond(200, {})
            else:
                self.respond(404, {})

        def do_PUT(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"].append((self.path, None, dict(self.headers)))
            if state["fail"]:
                self.respond(state["fail"], {"error": "CANARY_BODY_SIGNED_URL"}, {"Location": "https://never-follow.invalid/secret"})
                return
            state["payloads"].append(payload)
            self.respond(200, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(cert))
    transport = HTTPTransport("https://localhost:" + str(server.server_port),
                              {"X-API-Key": "SYNTHETIC_KEY", "X-Secret-Key": "SYNTHETIC_SECRET"}, ssl_context=client_context)
    yield transport, state
    server.shutdown()
    server.server_close()
    thread.join(2)


def new_manager(transport):
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=transport)
    assert manager.ready(2)
    return manager


def test_real_wav_transfer_and_only_safe_projection(server, monkeypatch):
    transport, state = server
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NETRC", "/not-used")
    import http.client
    original = http.client.HTTPConnection.endheaders
    def suppressed(connection, *args, **kwargs):
        assert get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        return original(connection, *args, **kwargs)
    with patch.object(http.client.HTTPConnection, "endheaders", suppressed):
        manager = new_manager(transport)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 800)
        raw = buffer.getvalue()
        handle = manager.attach(CONTEXT, Bytes(raw), mime_type="audio/wav", direction="mixed", conversation_id="session_1", turn_id="turn_1")
        assert handle.result(2).status == "validating"
        assert state["payloads"] == [raw]
        with wave.open(io.BytesIO(state["payloads"][0]), "rb") as decoded:
            assert decoded.getnframes() == 800 and decoded.getframerate() == 8000
        puts = [request for request in state["requests"] if request[0].endswith("/content")]
        assert puts[0][2]["authorization"] == "Bearer SYNTHETIC_UPLOAD_ONLY"
        assert "x-api-key" not in puts[0][2]
        init = state["requests"][1][1]
        assert init["association"]["parent"]["trace_id"] == CONTEXT.trace_uuid
        assert init["conversation_id"] == "session_1"
        assert len(json.dumps(init)) < 2048
        manager.shutdown(1)


@pytest.mark.parametrize("flag", ["enabled", "require_media_redaction", "require_metadata_removal"])
def test_server_denial_has_no_upload_network(server, flag):
    transport, state = server
    state["policy"][flag] = flag != "enabled"
    manager = new_manager(transport)
    result = manager.attach(CONTEXT, Bytes(b"secret"), mime_type="audio/wav", direction="mixed")
    assert result.admission in ("policy_denied", "redaction_unavailable")
    assert len(state["requests"]) == 1 and not state["payloads"]
    manager.shutdown(1)


@pytest.mark.parametrize("status", [302, 401, 403, 409, 410, 413, 422])
def test_no_redirect_or_security_retry_or_body_leak(server, status, caplog):
    transport, state = server
    state["fail"] = status
    manager = new_manager(transport)
    result = manager.attach(CONTEXT, Bytes(b"secret"), mime_type="audio/wav", direction="mixed")
    assert result.result(2).status != "validating"
    assert len([request for request in state["requests"] if request[0].endswith("/content")]) == 1
    assert "CANARY" not in caplog.text + repr(vars(result))
    manager.shutdown(1)


def test_expired_bootstrap_has_no_capture(server):
    transport, state = server
    state["expires"] = -1
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=transport)
    assert not manager.ready(2)
    assert manager.status == "expired"
    assert manager.attach(CONTEXT, Bytes(b"secret"), mime_type="audio/wav", direction="mixed").admission == "expired"
    assert len(state["requests"]) == 1
    manager.shutdown(1)


@pytest.mark.parametrize("endpoint", ["http://localhost", "https://user:pass@example.test", "https://example.test/path", "https://example.test/?secret=canary", "file:///tmp/private"])
def test_control_endpoint_rejects_unsafe_shapes(endpoint):
    with pytest.raises(TransferError):
        HTTPTransport(endpoint, {"authorization": "synthetic"})
