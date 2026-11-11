import os
import threading
import time
import uuid
from dataclasses import replace

import pytest
from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, ServerPolicy
from fi_instrumentation.attachments.protocol import Session, Upload


@pytest.fixture(autouse=True)
def clean_attachment_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("FI_ATTACHMENTS_", "FI_HIDE_")) or key == "FI_PII_REDACTION":
            monkeypatch.delenv(key, raising=False)


class TransportDouble:
    def __init__(self, policy=None):
        self.policy = policy or ServerPolicy(
            enabled=True, credential_write=True, allowed_sources=frozenset(("Bytes", "OwnedRecordingFile")),
            allowed_directions=frozenset(("input", "output", "mixed")), require_media_redaction=False,
            require_metadata_removal=False, version="v1")
        self.requests, self.payloads, self.aborts = [], [], []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failures = []
        self.calls = 0
        self.bootstraps = 0

    def bootstrap(self, project_name, project_type, session_id, nonce):
        self.bootstraps += 1
        return Session(str(uuid.uuid4()), session_id, self.policy, time.time() + 60, 8 * 1024 * 1024)

    def initialize(self, session, request, check):
        check()
        self.requests.append(dict(request))
        return Upload(str(uuid.uuid4()), time.time() + 60)

    def transfer(self, session, upload, payload, check, chunk_bytes):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        check()
        if self.failures:
            raise self.failures.pop(0)
        if not isinstance(payload, bytes):
            payload = payload.read(0, len(payload))
        self.payloads.append(payload)

    def complete(self, session, upload, check):
        check()

    def abort(self, session, upload):
        self.aborts.append(upload.upload_id)


@pytest.fixture
def transport():
    return TransportDouble()


@pytest.fixture
def manager(transport):
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=transport)
    assert manager.ready(2)
    yield manager
    transport.release.set()
    manager.shutdown(2)
