import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, ServerPolicy
from fi_instrumentation.attachments.errors import TransferError
from fi_instrumentation.attachments.protocol import ImportJob, Limits, Session, Upload, strict_json

sys.path.insert(0, str(Path(__file__).parent))

F01 = Path(__file__).parent / "f01"


def f01(name):
    return json.loads((F01 / name).read_text())


@pytest.fixture(autouse=True)
def clean_attachment_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("FI_ATTACHMENTS_", "FI_HIDE_")) or key == "FI_PII_REDACTION":
            monkeypatch.delenv(key, raising=False)


def capability():
    return "cap_" + uuid.uuid4().hex


class TransportDouble:
    """Section 9 AttachmentTransport double. Honors check() and deadlines."""

    def __init__(self, policy=None, limits=None):
        self.policy = policy or ServerPolicy(
            enabled=True, credential_write=True,
            allowed_sources=frozenset(("Bytes", "OwnedRecordingFile", "ProviderFile", "RemoteUrl")),
            allowed_modalities=frozenset(("audio", "image", "video", "document", "file")),
            require_media_redaction=False, require_metadata_removal=False, record_audio=True, version="v1")
        self.limits = limits or Limits()
        self.requests, self.payloads, self.aborts, self.keys = [], [], [], []
        self.manifests, self.imports, self.statuses = [], [], []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failures, self.init_failures, self.complete_failures, self.manifest_failures = [], [], [], []
        self.calls = 0
        self.bootstraps = 0
        self.bootstrap_bodies = []
        self.session_ttl = 600
        self.renew_result = "fresh"
        self.renewals = []
        self.session_renewals = []
        self.renew_scope = None
        self.renew_policy = None
        self.uploads = {}
        self.capability_used = []
        self.content_thread = []
        self.block_forever = False

    def _session(self, body, ttl=None):
        scope = ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002",
                 "00000000-0000-4000-8000-000000000003")
        now = time.time()
        return Session(scope[2], body["capture_session_id"], self.policy, now + (ttl or self.session_ttl),
                       self.limits, scope, now, json.dumps(body).encode())

    def bootstrap(self, body, *, key, deadline):
        self.bootstraps += 1
        self.bootstrap_bodies.append(dict(body))
        self.keys.append(key)
        return self._session(body)

    def renew_session(self, current, *, sequence, deadline):
        self.session_renewals.append(sequence)
        if self.renew_result == "fail":
            raise TransferError("incomplete", True)
        body = json.loads(current.request)
        session = self._session(body)
        if self.renew_scope is not None:
            session = Session(self.renew_scope, session.capture_session_id, session.policy, session.expires_at,
                              session.limits, (session.scope[0], session.scope[1], self.renew_scope),
                              session.issued_at, session.request)
        if self.renew_policy is not None:
            session = Session(session.project_id, session.capture_session_id, self.renew_policy,
                              session.expires_at, session.limits, session.scope, session.issued_at, session.request)
        if session.scope != current.scope:
            raise TransferError("scope_unresolved")
        return session

    def initialize(self, session, body, *, key, deadline, check):
        check()
        request = strict_json(body)
        self.keys.append(key)
        self.requests.append(request)
        if self.init_failures:
            raise self.init_failures.pop(0)
        upload = self.uploads.get(key)
        if upload is None:
            upload = Upload(str(uuid.uuid4()), time.time() + 60, capability(), request["declared_bytes"])
            self.uploads[key] = upload
        return upload

    def renew_upload(self, session, upload, body, *, deadline):
        self.renewals.append(body)
        if self.renew_result == "same":
            raise TransferError("expired")
        fresh = Upload(upload.upload_id, time.time() + 120, capability(), upload.reserved_bytes)
        return fresh

    def put_content(self, session, upload, source, *, deadline, check, pacer, chunk_bytes=256 * 1024):
        self.calls += 1
        self.content_thread.append(threading.current_thread().name)
        self.entered.set()
        while not self.release.wait(0.05):
            if self.block_forever:
                continue
            check()
            if time.monotonic() > deadline:
                raise TransferError("incomplete", True)
        check()
        self.capability_used.append(("content", upload.capability))
        if self.failures:
            raise self.failures.pop(0)
        data = b"".join(bytes(source.read(o, min(chunk_bytes, len(source) - o)))
                        for o in range(0, len(source), chunk_bytes))
        source.check()
        self.payloads.append(data)

    def complete(self, session, upload, *, key, deadline, check):
        check()
        self.keys.append(key)
        self.capability_used.append(("complete", upload.capability))
        if self.complete_failures:
            raise self.complete_failures.pop(0)
        return "validating"

    def abort(self, session, upload, *, key, deadline):
        self.keys.append(key)
        self.aborts.append(upload.upload_id)

    def upload_status(self, session, upload, *, deadline):
        return ("validating", "binding_pending")

    def put_manifest(self, session, capability_token, recording_id, body, *, key, deadline, check):
        check()
        self.keys.append(key)
        self.capability_used.append(("manifest", capability_token))
        if self.manifest_failures:
            raise self.manifest_failures.pop(0)
        self.manifests.append((recording_id, json.loads(body), key, capability_token))

    def submit_import(self, session, body, *, key, deadline, check):
        check()
        self.keys.append(key)
        request = json.loads(body)
        self.imports.append((request, key))
        if self.failures:
            raise self.failures.pop(0)
        return ImportJob(str(uuid.uuid4()), request["recording"]["recording_id"])

    def import_status(self, session, job_id, *, deadline):
        self.statuses.append(job_id)
        return ("importing", "binding_pending")


@pytest.fixture
def transport():
    return TransportDouble()


def make_manager(transport, **config):
    manager = AttachmentManager(config=AttachmentConfig(enabled=True, **config), project_name="synthetic",
                                transport=transport)
    assert manager.ready(2)
    return manager


@pytest.fixture
def manager(transport):
    manager = make_manager(transport)
    yield manager
    transport.release.set()
    transport.block_forever = False
    manager.shutdown(2)
