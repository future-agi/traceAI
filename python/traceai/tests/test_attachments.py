from types import SimpleNamespace
from unittest.mock import Mock

import traceai
from fi_instrumentation.attachments import AttachmentConfig
from fi_instrumentation.instrumentation.config import TraceConfig


def test_provider_policy_manager_propagation_and_idempotence(monkeypatch):
    manager = SimpleNamespace(policy=object(), status=lambda: SimpleNamespace(state="disabled"))
    config = TraceConfig(hide_inputs=True)
    provider = SimpleNamespace(attachment_manager=manager, trace_config=config)
    register = Mock(return_value=provider)
    constructor = Mock()
    module = SimpleNamespace(OpenAIInstrumentor=constructor,
                             AWrongInstrumentor=Mock(side_effect=AssertionError("guessed class")))
    monkeypatch.setattr(traceai, "register", register)
    monkeypatch.setattr(traceai.importlib, "import_module", Mock(return_value=module))
    attachments = AttachmentConfig(enabled=False)
    args = dict(project_name="synthetic", attachment_config=attachments, trace_config=config, auto_instrument=["openai"], verbose=False)
    assert traceai.init(**args) is provider
    assert traceai.init(**args) is provider
    register.assert_called_once()
    constructor.assert_called_once()
    constructor.return_value.instrument.assert_called_once_with(
        tracer_provider=provider, config=config, attachment_manager=manager, attachment_policy=manager.policy)
    assert register.call_args.kwargs["attachment_config"] is attachments


def test_voice_registration_does_not_claim_recording_or_mutate_global_provider(monkeypatch):
    module = SimpleNamespace(enable_fi_attribute_mapping=Mock(side_effect=AssertionError("global mapping")))
    monkeypatch.setattr(traceai.importlib, "import_module", Mock(return_value=module))
    assert traceai._instrument_library("pipecat", verbose=False)
    assert traceai.attachment_status()["adapters"]["pipecat"] == "registered"


def test_explicit_session_binding_uses_shared_manager_without_recording_permission(monkeypatch):
    manager = SimpleNamespace(status=lambda: SimpleNamespace(state="ready"))
    monkeypatch.setattr(traceai, "_tracer_provider", SimpleNamespace(attachment_manager=manager))
    binder = Mock(return_value=object())
    monkeypatch.setattr(traceai.importlib, "import_module", Mock(return_value=SimpleNamespace(bind_session=binder)))
    session, job = object(), object()
    result = traceai.bind_livekit_session(session, job)
    assert result is binder.return_value
    binder.assert_called_once_with(session, job, manager=manager, owned_recording_files=False, finalization_timeout=30)
    assert traceai.attachment_status()["adapters"]["livekit"] == "session_bound"
    assert "recording_source_ready" not in str(traceai.attachment_status())


def test_old_optional_adapter_reports_unsupported_without_exception_body(monkeypatch, capsys):
    monkeypatch.setattr(traceai, "_tracer_provider", SimpleNamespace(attachment_manager=SimpleNamespace(status=lambda: SimpleNamespace(state="ready"))))
    monkeypatch.setattr(traceai.importlib, "import_module", Mock(return_value=SimpleNamespace()))
    assert traceai.bind_pipecat_audio_processor(object(), object()) is None
    assert traceai.attachment_status()["adapters"]["pipecat"] == "unsupported_version"
    monkeypatch.setattr(traceai.importlib, "import_module", Mock(side_effect=RuntimeError("SYNTHETIC_SECRET")))
    assert not traceai._instrument_library("openai")
    assert "SYNTHETIC_SECRET" not in capsys.readouterr().out


def test_disabled_init_is_strict_noop(monkeypatch):
    """Auto module: attachments OFF by default; the null object is shared, nothing starts."""
    import threading

    from fi_instrumentation.attachments import DisabledAttachmentManager

    before = {t.ident for t in threading.enumerate()}
    provider = traceai.init(project_name="synthetic", project_type="observe", verbose=False, set_global_tracer_provider=False)
    try:
        assert isinstance(provider.attachment_manager, DisabledAttachmentManager)
        assert traceai.attachment_status()["runtime"] == "disabled"
        assert {t.ident for t in threading.enumerate()} == before
    finally:
        provider.shutdown()
