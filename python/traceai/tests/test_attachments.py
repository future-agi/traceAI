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


def test_context_keys_use_the_typescript_names():
    # The TypeScript auto module names its keys createContextKey("traceai.auto.attachment_policy.v1")
    # and createContextKey("traceai.auto.attachment_owner.v1"); the Python names are the same strings.
    assert traceai._CAPTURE_POLICY_KEY == "traceai.auto.attachment_policy.v1"
    assert traceai._OWNER_KEY == "traceai.auto.attachment_owner.v1"
    assert media_capture.CAPTURE_POLICY_KEY == "traceai.auto.attachment_policy.v1"
    assert media_capture.OWNER_KEY == "traceai.auto.attachment_owner.v1"


def test_policy_set_under_the_shared_name_is_honoured():
    # An adapter that sets the per-call policy by the shared NAME (no traceai import) restricts capture.
    ctx = otel_context.set_value("traceai.auto.attachment_policy.v1", CapturePolicy(capture_content=False))
    token = otel_context.attach(ctx)
    try:
        assert media_capture.local_capture_policy(TraceConfig(), direction="input").capture_content is False
    finally:
        otel_context.detach(token)
    assert media_capture.local_capture_policy(TraceConfig(), direction="input").capture_content is True
    assert otel_context.get_value("traceai.auto.attachment_policy.v1",
                                  traceai.with_attachment_capture_policy(CapturePolicy(muted=True))).muted is True


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


# ------------------------------------------------- MCP and agent media adapters (#693)
_AGENT_INSTRUMENTORS = {  # registry name -> (module, entry point)
    "openai_agents": ("traceai_openai_agents", "OpenAIAgentsInstrumentor"),
    "claude_agent_sdk": ("traceai_claude_agent_sdk", "ClaudeAgentInstrumentor"),
    "smolagents": ("traceai_smolagents", "SmolagentsInstrumentor"),
    "firecrawl": ("traceai_firecrawl", "FirecrawlInstrumentor"),
}
_MCP_AGENT_DESCRIPTORS = {  # report row name -> (descriptor module, descriptor name)
    "mcp": ("traceai_mcp", "mcp_attachment_capabilities"),
    "mcp_use": ("traceai_mcp_use._attachments", "mcp_use_attachment_capabilities"),
    "openai_agents": ("traceai_openai_agents._attachments", "openai_agents_attachment_capabilities"),
    "claude_agent_sdk": ("traceai_claude_agent_sdk._attachments", "claude_agent_attachment_capabilities"),
    "smolagents": ("traceai_smolagents._attachments", "smolagents_attachment_capabilities"),
    "firecrawl": ("traceai_firecrawl._attachments", "firecrawl_attachment_capabilities"),
}


def test_mcp_and_agent_packages_are_registered_with_explicit_entry_points():
    for name, (module, entry) in _AGENT_INSTRUMENTORS.items():
        assert traceai._INSTRUMENTOR_REGISTRY[name] == module
        assert traceai._ENTRY_POINTS[module] == entry
    for alias, name in (("openai-agents", "openai_agents"), ("claude-agent-sdk", "claude_agent_sdk"),
                        ("mcp-use", "mcp_use")):
        assert traceai._INSTRUMENTOR_REGISTRY[alias] == traceai._INSTRUMENTOR_REGISTRY[name]
    assert traceai._INSTRUMENTOR_REGISTRY["mcp_use"] == "traceai_mcp_use"
    # mcp-use has a LangChain callback, not an instrumentor: auto must never guess a class for it.
    assert "traceai_mcp_use" not in traceai._ENTRY_POINTS
    assert "traceai_mcp_use" in traceai._EXPLICIT_ONLY


# Provider and framework instrumentors newly reachable from auto (#697 and #691). They take
# the generic keyword arguments; Agno has its own (see test_agno_*).
_PROVIDER_FRAMEWORK_INSTRUMENTORS = {
    "openrouter": ("traceai_openrouter", "OpenRouterInstrumentor"),
    "portkey": ("traceai_portkey", "PortkeyInstrumentor"),
    "vllm": ("traceai_vllm", "VLLMInstrumentor"),
    "minimax": ("traceai_minimax", "MiniMaxInstrumentor"),
    "pydantic_ai": ("traceai_pydantic_ai", "PydanticAIInstrumentor"),
}


@pytest.mark.parametrize("name", sorted({**_AGENT_INSTRUMENTORS, **_PROVIDER_FRAMEWORK_INSTRUMENTORS}))
def test_agent_instrumentors_get_the_shared_manager_and_the_off_kwargs_when_off(monkeypatch, name):
    module_name, entry = {**_AGENT_INSTRUMENTORS, **_PROVIDER_FRAMEWORK_INSTRUMENTORS}[name]
    for env, expected in ((None, "on"), ("false", "off")):
        if env is None:
            monkeypatch.delenv("FI_ATTACHMENTS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", env)
        monkeypatch.setattr(traceai, "_tracer_provider", None)
        monkeypatch.setattr(traceai, "_init_options", None)
        manager = _enabled_like_manager()
        provider = SimpleNamespace(attachment_manager=manager, trace_config=TraceConfig())
        monkeypatch.setattr(traceai, "register", Mock(return_value=provider))
        constructor = Mock()
        imported = []

        def import_module(requested, constructor=constructor, imported=imported):
            imported.append(requested)
            return SimpleNamespace(**{entry: constructor})

        monkeypatch.setattr(traceai.importlib, "import_module", import_module)
        traceai.init(project_name="synthetic", auto_instrument=[name], verbose=False)
        assert imported == [module_name]
        if expected == "on":
            constructor.return_value.instrument.assert_called_once_with(
                tracer_provider=provider, config=provider.trace_config, attachments=manager, attachment_manager=manager)
        else:  # attachments OFF: exactly the keyword arguments every other instrumentor gets
            constructor.return_value.instrument.assert_called_once_with(
                tracer_provider=provider, config=provider.trace_config, attachment_manager=manager,
                attachment_policy=manager.policy)
        assert traceai.attachment_status()["adapters"][name] == "registered"


def test_enabling_mcp_use_records_it_and_constructs_nothing(monkeypatch):
    module = SimpleNamespace(FutureAGICallback=Mock(side_effect=AssertionError("constructed a callback")),
                             SomethingInstrumentor=Mock(side_effect=AssertionError("guessed a class")))
    monkeypatch.setattr(traceai.importlib, "import_module", Mock(return_value=module))
    assert traceai._instrument_library("mcp-use", verbose=False)
    assert traceai.attachment_status()["adapters"]["mcp-use"] == "registered"


def test_mcp_and_agent_report_rows_read_each_installed_descriptor(monkeypatch):
    modules = {}
    for name, (path, attr) in _MCP_AGENT_DESCRIPTORS.items():
        modules[path] = SimpleNamespace(**{attr: {"operations": (name + ".output",)}})
        modules.setdefault(traceai._INSTRUMENTOR_REGISTRY[name], SimpleNamespace())

    def import_module(requested):
        if requested in modules:
            return modules[requested]
        raise ImportError(requested)

    monkeypatch.setattr(traceai, "_installed", lambda module: module in modules)
    monkeypatch.setattr(traceai.importlib, "import_module", import_module)
    rows = {row["name"]: row for row in traceai.report()["integrations"]}
    for name in _MCP_AGENT_DESCRIPTORS:
        assert rows[name]["media"] == "provider_adapter", name
        assert rows[name]["media_operations"] == [name + ".output"], name
        assert rows[name]["status"] == "not_enabled", name
    for alias in ("mcp-use", "openai-agents", "claude-agent-sdk"):
        assert alias not in rows  # aliases share one row
    # An installed package version without the media adapter (no descriptor) is unqualified.
    for name, (path, _attr) in _MCP_AGENT_DESCRIPTORS.items():
        saved = modules[path]
        if path == traceai._INSTRUMENTOR_REGISTRY[name]:
            modules[path] = SimpleNamespace()  # the package is installed, its descriptor is not
        else:
            del modules[path]  # this package version has no _attachments module
        rows = {row["name"]: row for row in traceai.report()["integrations"]}
        assert rows[name] == {"name": name, "package": traceai._INSTRUMENTOR_REGISTRY[name],
                              "status": "not_enabled", "media": "unqualified"}, name
        modules[path] = saved


def _explicit_setup(monkeypatch, manager, config=None, module=None, adapter=True):
    provider = SimpleNamespace(attachment_manager=manager, trace_config=config or TraceConfig(hide_outputs=True))
    monkeypatch.setattr(traceai, "_tracer_provider", provider)
    monkeypatch.setattr(traceai, "_media_support", lambda m: {"media": "provider_adapter" if adapter else "unqualified"})
    monkeypatch.setattr(traceai.importlib, "import_module", Mock(return_value=module))
    return provider


def test_mcp_use_callback_uses_the_shared_manager_only_while_capturing(monkeypatch):
    callback_class = Mock()
    module = SimpleNamespace(FutureAGICallback=callback_class)
    manager = _enabled_like_manager()
    provider = _explicit_setup(monkeypatch, manager, module=module)
    assert traceai.mcp_use_callback() is callback_class.return_value
    callback_class.assert_called_once_with(tracer_provider=provider, config=provider.trace_config, attachments=manager)
    assert traceai.attachment_status()["adapters"]["mcp_use"] == "registered"
    # Caller arguments win over auto's defaults.
    callback_class.reset_mock()
    own = TraceConfig()
    traceai.mcp_use_callback(config=own, redact=["x"])
    assert callback_class.call_args.kwargs["config"] is own and callback_class.call_args.kwargs["redact"] == ["x"]
    # Attachments OFF: the callback auto built before (no attachments argument at all).
    for off in ("env", "disabled", "no_adapter"):
        callback_class.reset_mock()
        monkeypatch.delenv("FI_ATTACHMENTS_ENABLED", raising=False)
        if off == "env":
            monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
            provider = _explicit_setup(monkeypatch, manager, module=module)
        elif off == "disabled":
            provider = _explicit_setup(monkeypatch, DisabledAttachmentManager(), module=module)
        else:
            provider = _explicit_setup(monkeypatch, manager, module=module, adapter=False)
        traceai.mcp_use_callback()
        callback_class.assert_called_once_with(tracer_provider=provider, config=provider.trace_config)


def test_wrap_mcp_client_uses_the_shared_manager_and_tolerates_old_packages(monkeypatch):
    wrap = Mock()
    manager = _enabled_like_manager()
    provider = _explicit_setup(monkeypatch, manager, module=SimpleNamespace(wrap_mcp_client=wrap))
    session = object()
    assert traceai.wrap_mcp_client(session) is wrap.return_value
    wrap.assert_called_once_with(session, tracer_provider=provider, trace_config=provider.trace_config,
                                 attachments=manager)
    wrap.reset_mock()
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    traceai.wrap_mcp_client(session)
    wrap.assert_called_once_with(session, tracer_provider=provider, trace_config=provider.trace_config)
    # An installed traceAI-mcp without wrap_mcp_client: the session comes back untouched.
    _explicit_setup(monkeypatch, manager, module=SimpleNamespace())
    assert traceai.wrap_mcp_client(session) is session
    assert traceai.attachment_status()["adapters"]["mcp"] == "unsupported_version"


def test_explicit_entry_points_need_init(monkeypatch):
    monkeypatch.setattr(traceai, "_tracer_provider", None)
    monkeypatch.setattr(traceai.importlib, "import_module",
                        Mock(return_value=SimpleNamespace(FutureAGICallback=Mock(), wrap_mcp_client=Mock())))
    with pytest.raises(RuntimeError):
        traceai.mcp_use_callback()
    with pytest.raises(RuntimeError):
        traceai.wrap_mcp_client(object())


# ------------------------------------- provider (#697) and framework (#691) media adapters
# report row name -> (descriptor module, descriptor name). The provider packages define the
# descriptor as a lazy module attribute; the framework packages in their _attachments module.
_PROVIDER_FRAMEWORK_DESCRIPTORS = {
    "google_genai": ("traceai_google_genai", "google_genai_attachment_capabilities"),
    "vertexai": ("traceai_vertexai", "vertexai_attachment_capabilities"),
    "bedrock": ("traceai_bedrock", "bedrock_attachment_capabilities"),
    "litellm": ("traceai_litellm", "litellm_attachment_capabilities"),
    "groq": ("traceai_groq._audio", "groq_attachment_capabilities"),
    "fireworks": ("traceai_fireworks", "fireworks_attachment_capabilities"),
    "xai": ("traceai_xai", "xai_attachment_capabilities"),
    "mistralai": ("traceai_mistralai", "mistralai_attachment_capabilities"),
    "openrouter": ("traceai_openrouter", "openrouter_attachment_capabilities"),
    "portkey": ("traceai_portkey", "portkey_attachment_capabilities"),
    "ollama": ("traceai_ollama", "ollama_attachment_capabilities"),
    "vllm": ("traceai_vllm", "vllm_attachment_capabilities"),
    "minimax": ("traceai_minimax", "minimax_attachment_capabilities"),
    "langchain": ("traceai_langchain._attachments", "langchain_attachment_capabilities"),
    "llamaindex": ("traceai_llamaindex._attachments", "llamaindex_attachment_capabilities"),
    "agno": ("traceai_agno._attachments", "agno_attachment_capabilities"),
    "pydantic_ai": ("traceai_pydantic_ai._attachments", "pydantic_ai_attachment_capabilities"),
}
# Instrumented, but their descriptors claim no media operation: never a media adapter.
_UNCLAIMED = {"together": "traceai_together", "deepseek": "traceai_deepseek", "cerebras": "traceai_cerebras"}


def test_provider_and_framework_packages_are_registered_with_explicit_entry_points():
    for name, (module, entry) in _PROVIDER_FRAMEWORK_INSTRUMENTORS.items():
        assert traceai._INSTRUMENTOR_REGISTRY[name] == module
        assert traceai._ENTRY_POINTS[module] == entry
    assert traceai._INSTRUMENTOR_REGISTRY["pydantic-ai"] == "traceai_pydantic_ai"
    assert traceai._INSTRUMENTOR_REGISTRY["agno"] == "traceai_agno"
    assert traceai._ENTRY_POINTS["traceai_agno"] == "AgnoInstrumentorWrapper"
    for name, (path, attr) in _PROVIDER_FRAMEWORK_DESCRIPTORS.items():
        module = traceai._INSTRUMENTOR_REGISTRY[name]
        assert traceai._MEDIA_ADAPTERS[module] == (path, attr), name
    for module in _UNCLAIMED.values():
        assert module not in traceai._MEDIA_ADAPTERS
        assert module in traceai._ENTRY_POINTS  # still traced


def test_provider_and_framework_report_rows_read_each_installed_descriptor(monkeypatch):
    modules = {}
    for name, (path, attr) in _PROVIDER_FRAMEWORK_DESCRIPTORS.items():
        modules[path] = SimpleNamespace(**{attr: {"operations": (name + ".input",)}})
        modules.setdefault(traceai._INSTRUMENTOR_REGISTRY[name], SimpleNamespace())
    for name, module in _UNCLAIMED.items():  # installed, with a descriptor that claims nothing
        modules[module] = SimpleNamespace(**{name + "_attachment_capabilities": {"operations": ()}})

    def import_module(requested):
        if requested in modules:
            return modules[requested]
        raise ImportError(requested)

    monkeypatch.setattr(traceai, "_installed", lambda module: module in modules)
    monkeypatch.setattr(traceai.importlib, "import_module", import_module)
    rows = {row["name"]: row for row in traceai.report()["integrations"]}
    for name in _PROVIDER_FRAMEWORK_DESCRIPTORS:
        assert rows[name]["media"] == "provider_adapter", name
        assert rows[name]["media_operations"] == [name + ".input"], name
        assert rows[name]["status"] == "not_enabled", name
    for name, module in _UNCLAIMED.items():
        assert rows[name] == {"name": name, "package": module, "status": "not_enabled", "media": "unqualified"}
    for alias in ("pydantic-ai", "google-genai", "vertex", "mistral"):
        assert alias not in rows  # aliases share one row
    # An installed version without the descriptor: the lazy attribute raises AttributeError (old
    # package) or ImportError (a core without provider_media); both are unqualified.
    for name, (path, attr) in _PROVIDER_FRAMEWORK_DESCRIPTORS.items():
        saved = modules[path]
        package = traceai._INSTRUMENTOR_REGISTRY[name]
        for broken in ("attribute", "import"):
            if path != package:
                del modules[path]
            elif broken == "attribute":
                modules[path] = SimpleNamespace()
            else:
                modules[path] = _RaisingModule(ImportError("fi_instrumentation.attachments.provider_media"))
            rows = {row["name"]: row for row in traceai.report()["integrations"]}
            assert rows[name] == {"name": name, "package": package, "status": "not_enabled",
                                  "media": "unqualified"}, (name, broken)
            modules[path] = saved
    # A descriptor that lists no operation is never reported as a media adapter.
    modules["traceai_litellm"] = SimpleNamespace(litellm_attachment_capabilities={"operations": []})
    assert {row["name"]: row for row in traceai.report()["integrations"]}["litellm"]["media"] == "unqualified"


class _RaisingModule:
    def __init__(self, error):
        self._error = error

    def __getattr__(self, name):
        raise self._error


def _stub_openinference(monkeypatch):
    """OpenInference's TraceConfig shape (hide_* flags plus an image cap), without the package."""
    import dataclasses
    import sys
    from types import ModuleType

    @dataclasses.dataclass(frozen=True)
    class OpenInferenceTraceConfig:
        hide_llm_invocation_parameters: bool = False
        hide_inputs: bool = False
        hide_outputs: bool = False
        hide_input_messages: bool = False
        hide_output_messages: bool = False
        hide_input_images: bool = False
        hide_input_text: bool = False
        hide_output_text: bool = False
        hide_embedding_vectors: bool = False
        base64_image_max_length: int = 32000

    package, module = ModuleType("openinference"), ModuleType("openinference.instrumentation")
    module.TraceConfig = OpenInferenceTraceConfig
    package.instrumentation = module
    monkeypatch.setitem(sys.modules, "openinference", package)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", module)
    return OpenInferenceTraceConfig


def _agno_init(monkeypatch, manager, config):
    monkeypatch.setattr(traceai, "_tracer_provider", None)
    monkeypatch.setattr(traceai, "_init_options", None)
    monkeypatch.setattr(traceai, "_instrumented", set())
    provider = SimpleNamespace(attachment_manager=manager, trace_config=config)
    monkeypatch.setattr(traceai, "register", Mock(return_value=provider))
    wrapper = Mock()
    monkeypatch.setattr(traceai.importlib, "import_module",
                        Mock(return_value=SimpleNamespace(AgnoInstrumentorWrapper=wrapper)))
    traceai.init(project_name="synthetic", auto_instrument=["agno"], verbose=False)
    wrapper.assert_called_once_with()
    wrapper.return_value.instrument.assert_called_once()
    return provider, wrapper.return_value.instrument.call_args


def test_agno_gets_its_own_keyword_arguments_on_and_off(monkeypatch):
    monkeypatch.delenv("FI_ATTACHMENTS_ENABLED", raising=False)
    monkeypatch.setattr(traceai, "_media_support", lambda module: {"media": "provider_adapter"})
    manager = _enabled_like_manager()
    provider, call = _agno_init(monkeypatch, manager, TraceConfig())
    # No fi TraceConfig and no attachment_* alias: Agno's wrapper accepts neither.
    assert call.args == () and call.kwargs == {"tracer_provider": provider, "attachments": manager}
    assert traceai.attachment_status()["adapters"]["agno"] == "registered"
    for off in ("env", "disabled"):
        if off == "env":
            monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
            manager = _enabled_like_manager()
        else:
            monkeypatch.delenv("FI_ATTACHMENTS_ENABLED", raising=False)
            manager = traceai.DisabledAttachmentManager()
        provider, call = _agno_init(monkeypatch, manager, TraceConfig())
        # Attachments OFF: exactly what configure_agno_tracing(tracer_provider=...) gets.
        assert call.kwargs == {"tracer_provider": provider}, off


def test_agno_carries_auto_hide_flags_as_an_openinference_config(monkeypatch):
    oi_config = _stub_openinference(monkeypatch)
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    config = TraceConfig(hide_inputs=True, hide_output_text=True, hide_input_images=True)
    provider, call = _agno_init(monkeypatch, _enabled_like_manager(), config)
    passed = call.kwargs["config"]
    assert isinstance(passed, oi_config)
    assert (passed.hide_inputs, passed.hide_output_text, passed.hide_input_images) == (True, True, True)
    assert (passed.hide_outputs, passed.hide_input_text, passed.base64_image_max_length) == (False, False, 32000)
    assert set(call.kwargs) == {"tracer_provider", "config"}


def test_a_dependency_conflict_is_never_reported_registered(monkeypatch):
    provider = SimpleNamespace(attachment_manager=traceai.DisabledAttachmentManager(), trace_config=TraceConfig())
    monkeypatch.setattr(traceai, "register", Mock(return_value=provider))
    instrumentor = Mock(is_instrumented_by_opentelemetry=False)  # BaseInstrumentor skipped _instrument
    monkeypatch.setattr(traceai.importlib, "import_module",
                        Mock(return_value=SimpleNamespace(PydanticAIInstrumentor=Mock(return_value=instrumentor))))
    traceai.init(project_name="synthetic", auto_instrument=["pydantic-ai"], verbose=False)
    instrumentor.instrument.assert_called_once()
    assert traceai.attachment_status()["adapters"]["pydantic-ai"] == "dependency_unavailable"
    assert "traceai_pydantic_ai" not in traceai._instrumented
    rows = {row["name"]: row for row in traceai.report()["integrations"]}
    assert rows["pydantic_ai"]["status"] == "dependency_unavailable"


def test_agno_without_the_media_adapter_keeps_the_off_arguments(monkeypatch):
    """A traceai-agno release before the adapter has no ``attachments`` parameter at all."""
    monkeypatch.delenv("FI_ATTACHMENTS_ENABLED", raising=False)
    monkeypatch.setattr(traceai, "_media_support", lambda module: {"media": "unqualified"})
    provider, call = _agno_init(monkeypatch, _enabled_like_manager(), TraceConfig())
    assert call.kwargs == {"tracer_provider": provider}
    assert traceai.attachment_status()["adapters"]["agno"] == "registered"


def _agno_on(monkeypatch):
    for name in ("FI_ATTACHMENTS_ENABLED", "FI_PII_REDACTION", "FI_HIDE_ERROR_DETAILS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(traceai, "_media_support", lambda module: {"media": "provider_adapter"})


def _agno_row():
    return {row["name"]: row for row in traceai.report()["integrations"]}["agno"]["status"]


def test_agno_fails_closed_on_a_pii_or_error_flag_it_cannot_apply(monkeypatch):
    """Agno reads pii_redaction / hide_error_details only from the environment: no media, own status."""
    _stub_openinference(monkeypatch)
    for config in (TraceConfig(pii_redaction=True), TraceConfig(hide_error_details=True),
                   TraceConfig(pii_redaction=True, hide_error_details=True)):
        _agno_on(monkeypatch)
        monkeypatch.setattr(traceai, "_adapter_status", {})
        provider, call = _agno_init(monkeypatch, _enabled_like_manager(), config)
        assert call.kwargs == {"tracer_provider": provider}, config
        assert traceai.attachment_status()["adapters"]["agno"] == "config_unsupported"
        assert _agno_row() == "config_unsupported"
    # A hide flag Agno does receive keeps its config, and the error flag it derives is not lost.
    _agno_on(monkeypatch)
    monkeypatch.setattr(traceai, "_adapter_status", {})
    provider, call = _agno_init(monkeypatch, _enabled_like_manager(),
                                TraceConfig(hide_inputs=True, pii_redaction=True))
    assert set(call.kwargs) == {"tracer_provider", "config"} and call.kwargs["config"].hide_inputs is True
    assert _agno_row() == "config_unsupported"


def test_agno_keeps_media_when_the_environment_carries_the_flag(monkeypatch):
    _stub_openinference(monkeypatch)
    for env, config in (("FI_PII_REDACTION", TraceConfig(pii_redaction=True)),
                        ("FI_HIDE_ERROR_DETAILS", TraceConfig(hide_error_details=True))):
        _agno_on(monkeypatch)
        monkeypatch.setenv(env, "true")
        monkeypatch.setattr(traceai, "_adapter_status", {})
        manager = _enabled_like_manager()
        provider, call = _agno_init(monkeypatch, manager, config)
        assert call.kwargs == {"tracer_provider": provider, "attachments": manager}, env
        assert _agno_row() == "registered"
    # hide_error_details derived from a forwarded content hide flag is applied by Agno too.
    _agno_on(monkeypatch)
    monkeypatch.setattr(traceai, "_adapter_status", {})
    manager = _enabled_like_manager()
    config = TraceConfig(hide_inputs=True)
    assert config.hide_error_details is True
    provider, call = _agno_init(monkeypatch, manager, config)
    assert set(call.kwargs) == {"tracer_provider", "config", "attachments"}
    assert _agno_row() == "registered"


def test_agno_off_arguments_are_unchanged_by_an_unreachable_flag(monkeypatch):
    _stub_openinference(monkeypatch)
    for off in ("env", "disabled"):
        _agno_on(monkeypatch)
        monkeypatch.setattr(traceai, "_adapter_status", {})
        if off == "env":
            monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
            manager = _enabled_like_manager()
        else:
            manager = traceai.DisabledAttachmentManager()
        provider, call = _agno_init(monkeypatch, manager, TraceConfig(pii_redaction=True))
        assert call.kwargs == {"tracer_provider": provider}, off
        assert _agno_row() == "config_unsupported", off
