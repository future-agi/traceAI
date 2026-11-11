"""Assert truthful fail-closed states against real AudioBufferProcessor types."""

from dataclasses import replace

import pytest
from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, CorrelationHandle
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from traceai_pipecat import bind_audio_processor, create_audio_capture
from traceai_pipecat.attachments import AudioSessionContext

CONTEXT = AudioSessionContext(CorrelationHandle("1" * 32, "2" * 16, True))


@pytest.mark.parametrize("buffer_size,turns,status", [(0, False, "unbounded_source"),
    (1024, True, "unbounded_source"), (131073, False, "unbounded_source"),
    (4096, False, "streaming_unavailable")])
def test_existing_processor_never_started_or_modified(manager, transport, buffer_size, turns, status):
    processor = AudioBufferProcessor(buffer_size=buffer_size, enable_turn_audio=turns)
    before = dict(processor.__dict__)
    binding = bind_audio_processor(processor, CONTEXT, manager=manager)
    assert binding.status == status and binding.processor is None
    assert processor.__dict__ == before
    assert not processor._recording
    assert not transport.payloads and not transport.requests


def test_creation_requires_separate_local_and_server_permission(manager, transport):
    assert create_audio_capture(CONTEXT, manager=manager).status == "recording_disabled"
    manager.config = replace(manager.config, record_audio=True)
    manager.policy.config = manager.config
    assert create_audio_capture(CONTEXT, manager=manager).status == "recording_disabled"
    manager.update_policy(replace(transport.policy, record_audio=True))
    result = create_audio_capture(CONTEXT, manager=manager)
    assert result.status == "streaming_unavailable" and result.processor is None


@pytest.mark.parametrize("flag", ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_TEXT",
                                  "FI_HIDE_OUTPUT_TEXT", "FI_PII_REDACTION"])
def test_policy_denial_precedes_processor_access(manager, monkeypatch, flag):
    class Poison:
        def __getattribute__(self, name):
            raise AssertionError("denied processor accessed")
    monkeypatch.setenv(flag, "true")
    assert bind_audio_processor(Poison(), CONTEXT, manager=manager).status in ("policy_denied", "redaction_unavailable")


def test_off_by_default_does_not_import_or_touch_upstream():
    manager = AttachmentManager(config=AttachmentConfig())
    try:
        assert bind_audio_processor(object(), object(), manager=manager).status == "disabled"
        assert create_audio_capture(object(), manager=manager).status == "disabled"
        assert manager.owned_bytes == 0
    finally:
        manager.shutdown(0)


def test_unknown_upstream_fails_closed(manager, monkeypatch):
    monkeypatch.setattr("traceai_pipecat.attachments.version", lambda _: "2.0.0")
    assert bind_audio_processor(object(), CONTEXT, manager=manager).status == "unsupported_version"


def test_no_late_ambient_context_lookup(manager):
    assert bind_audio_processor(object(), None, manager=manager).status == "correlation_unavailable"
