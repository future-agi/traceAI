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


_OLD_CORE_PROBE = r'''
import sys
# A core release without the attachments package (e.g. fi-instrumentation-otel 1.3.1).
sys.modules["fi_instrumentation.attachments"] = None
import fi_instrumentation

calls = []

def old_register(*, project_name=None, project_type=None, project_version_name=None, batch=True,
                 set_global_tracer_provider=False, verbose=True, transport=None, semantic_convention=None):
    calls.append(project_name)
    return object()

import traceai
assert traceai.AttachmentConfig is None
traceai.register = old_register
assert traceai.attachment_status()["runtime"] == "unsupported_version"
provider = traceai.init(project_name="synthetic", verbose=False, set_global_tracer_provider=False)
assert calls == ["synthetic"], calls
assert traceai.bind_livekit_session(object(), object()) is None
assert traceai.attachment_status()["adapters"]["livekit"] == "unsupported_version"
assert traceai.get_attachment_manager() is None
assert traceai.report()["attachments"]["state"] == "unsupported_version"
assert all(row["media"] == "unqualified" for row in traceai.report()["integrations"])
from opentelemetry import context as otel_context
marked = traceai.with_attachment_ownership()
assert otel_context.get_value("traceai.auto.attachment_owner.v1", marked) is True
traceai._tracer_provider = None
try:
    traceai.init(project_name="other", verbose=False, attachment_manager=object())
except RuntimeError as error:
    assert "fi-instrumentation-otel" in str(error)
else:
    raise AssertionError("attachment_manager accepted on a core without attachments")
try:
    traceai.init(project_name="other", verbose=False, trace_config=object())
except RuntimeError as error:
    assert "fi-instrumentation-otel" in str(error)
else:
    raise AssertionError("trace_config accepted on a core without attachments")
assert calls == ["synthetic"], calls
print("OLD_CORE_OK")
'''


def test_import_and_init_on_core_without_attachments():
    """Review finding (P1): traceai must not crash on a core that predates attachments."""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    result = subprocess.run([sys.executable, "-c", _OLD_CORE_PROBE], capture_output=True, text=True,
                            timeout=120, env=env)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "OLD_CORE_OK" in result.stdout


# ----------------------------------------------------------- shared manager (A01)
import dataclasses  # noqa: E402

import pytest  # noqa: E402
from fi_instrumentation.attachments import AttachmentManager, CapturePolicy, DisabledAttachmentManager  # noqa: E402
from fi_instrumentation.attachments import media_capture  # noqa: E402
from opentelemetry import context as otel_context  # noqa: E402


def _enabled_like_manager():
    """Stands in for an enabled manager: not the null object, and it can preflight."""
    return SimpleNamespace(policy=object(), preflight=Mock(), status=lambda: SimpleNamespace(state="ready"))


def _install(monkeypatch, manager, config=None):
    provider = SimpleNamespace(attachment_manager=manager, trace_config=config or TraceConfig())
    monkeypatch.setattr(traceai, "register", Mock(return_value=provider))
    constructor = Mock()
    monkeypatch.setattr(traceai.importlib, "import_module",
                        Mock(return_value=SimpleNamespace(OpenAIInstrumentor=constructor)))
    return provider, constructor


def test_enabled_manager_reaches_every_instrumentor_as_attachments(monkeypatch):
    manager = _enabled_like_manager()
    provider, constructor = _install(monkeypatch, manager)
    assert traceai.get_attachment_manager() is None
    traceai.init(project_name="synthetic", auto_instrument=["openai"], verbose=False)
    assert traceai.get_attachment_manager() is manager
    constructor.return_value.instrument.assert_called_once_with(
        tracer_provider=provider, config=provider.trace_config, attachments=manager, attachment_manager=manager)


def test_kill_switch_keeps_the_off_path_and_reports_disabled(monkeypatch):
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    manager = _enabled_like_manager()
    provider, constructor = _install(monkeypatch, manager)
    traceai.init(project_name="synthetic", auto_instrument=["openai"], verbose=False)
    # Exactly the keyword arguments auto passed before this change, with attachments OFF.
    constructor.return_value.instrument.assert_called_once_with(
        tracer_provider=provider, config=provider.trace_config, attachment_manager=manager,
        attachment_policy=manager.policy)
    assert traceai.report()["attachments"]["state"] == "disabled"
    assert "status" not in traceai.report()["attachments"]
    assert traceai.attachment_status()["runtime"] == "disabled"


def test_borrowed_manager_gains_only_restrictions(monkeypatch):
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "true")
    app_config = TraceConfig(hide_inputs=True, pii_redaction=False)
    borrowed = AttachmentManager(config=AttachmentConfig(enabled=True), trace_config=app_config)  # no scope: no thread
    register = Mock(return_value=SimpleNamespace(attachment_manager=DisabledAttachmentManager(),
                                                 trace_config=TraceConfig(hide_inputs=False, pii_redaction=True)))
    monkeypatch.setattr(traceai, "register", register)
    try:
        traceai.init(project_name="synthetic", verbose=False, attachment_manager=borrowed,
                     trace_config=TraceConfig(hide_inputs=False, pii_redaction=True))
        # The provider does not build a second (env-enabled) manager.
        assert register.call_args.kwargs["attachment_config"].enabled is False
        assert traceai.get_attachment_manager() is borrowed
        assert traceai.report()["attachments"]["borrowed"] is True
        restricted = borrowed.policy.trace_config
        assert restricted.pii_redaction is True       # auto's restriction reached the borrowed manager
        assert restricted.hide_inputs is True         # the application's own flag survived auto's False
        assert app_config.pii_redaction is False      # the application's object is not mutated
    finally:
        borrowed.shutdown(0)


def test_borrowed_manager_and_attachment_config_are_exclusive(monkeypatch):
    monkeypatch.setattr(traceai, "register", Mock(side_effect=AssertionError("registered")))
    with pytest.raises(ValueError):
        traceai.init(project_name="synthetic", verbose=False, attachment_manager=_enabled_like_manager(),
                     attachment_config=AttachmentConfig(enabled=False))


def test_context_keys_match_the_shared_helper():
    assert traceai._CAPTURE_POLICY_KEY == media_capture.CAPTURE_POLICY_KEY
    assert traceai._OWNER_KEY == media_capture.OWNER_KEY


def test_capture_policy_helper_only_restricts():
    denied = traceai.with_attachment_capture_policy(CapturePolicy(capture_content=False, muted=True))
    # A later, permissive policy in the same context cannot relax the earlier denial.
    relaxed = traceai.with_attachment_capture_policy(CapturePolicy(), denied)
    token = otel_context.attach(relaxed)
    try:
        folded = media_capture.local_capture_policy(TraceConfig(), direction="input")
        assert folded.capture_content is False and folded.muted is True
    finally:
        otel_context.detach(token)
    assert media_capture.local_capture_policy(TraceConfig(), direction="input").capture_content is True
    token = otel_context.attach(traceai.with_attachment_capture_policy("not a policy"))
    try:
        folded = media_capture.local_capture_policy(TraceConfig(), direction="output")
        assert folded.capture_content is False and folded.requested is False
    finally:
        otel_context.detach(token)


def test_ownership_helper_turns_the_shared_helper_off():
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

    manager = AttachmentManager(config=AttachmentConfig(enabled=True))
    sdk = SdkTracerProvider()
    try:
        with sdk.get_tracer("synthetic").start_as_current_span("call") as span:
            assert media_capture.Observation(manager, span, "openai").usable is True
            token = otel_context.attach(traceai.with_attachment_ownership())
            try:
                assert media_capture.local_capture_policy(None, direction="input").requested is False
                assert media_capture.Observation(manager, span, "openai").usable is False
            finally:
                otel_context.detach(token)
            assert media_capture.local_capture_policy(None, direction="input").requested is True
            assert media_capture.Observation(manager, span, "openai").usable is True
    finally:
        manager.shutdown(0)
        sdk.shutdown()


def test_merge_is_deny_wins_for_every_fact():
    for item in dataclasses.fields(CapturePolicy):
        grant = item.name in ("capture_content", "record_inputs", "record_outputs", "requested")
        tight = CapturePolicy(**{item.name: not grant})
        assert getattr(media_capture.merge_capture_policies(CapturePolicy(), tight), item.name) is (not grant)
        assert getattr(media_capture.merge_capture_policies(tight, CapturePolicy()), item.name) is (not grant)


def test_report_media_rows_are_version_aware_and_content_free(monkeypatch):
    descriptor = {"operations": ("messages.create.input", "tool_result.input")}
    modules = {"traceai_anthropic": SimpleNamespace(anthropic_attachment_capabilities=descriptor),
               "traceai_openai": SimpleNamespace(),  # an older instrumentor without the adapter
               "traceai_livekit": SimpleNamespace(bind_session=lambda *a, **k: None)}

    def import_module(name):
        if name in modules:
            return modules[name]
        raise ImportError(name)

    monkeypatch.setattr(traceai, "_installed", lambda module: module in modules)
    monkeypatch.setattr(traceai.importlib, "import_module", import_module)
    rows = {row["name"]: row for row in traceai.report()["integrations"]}
    assert rows["anthropic"]["media"] == "provider_adapter"
    assert rows["anthropic"]["media_operations"] == ["messages.create.input", "tool_result.input"]
    assert rows["openai"]["media"] == "unqualified"
    assert rows["livekit"]["media"] == "session_binding"
    assert rows["groq"] == {"name": "groq", "package": "traceai_groq", "status": "not_installed",
                           "media": "unqualified"}
    assert rows["anthropic"]["status"] == "not_enabled"
    assert "mistral" not in rows  # aliases share one row
    assert traceai.report()["attachments"]["state"] == "not_initialized"
