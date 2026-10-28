"""The plugin inside the Hermes process: signals, profile secrets, flush bounds."""

import signal
import sys
import time
import types

import pytest

from conftest import FakeContext, run_tool_turn
from traceai_hermes_agent import FutureAGIHermesPlugin, build_tracer_provider, register


@pytest.fixture
def fi_env(monkeypatch):
    monkeypatch.setenv("FI_API_KEY", "key-a")
    monkeypatch.setenv("FI_SECRET_KEY", "secret-a")
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setenv("FI_PROJECT_NAME", "project-a")


def _exporter(provider):
    processor = provider._active_span_processor._span_processors[0]
    return getattr(processor, "span_exporter", None) or processor._batch_processor._exporter


def _headers(exporter):
    # opentelemetry-exporter-otlp-proto-http keeps them on its client (1.40+) or itself.
    client = getattr(exporter, "_client", exporter)
    headers = getattr(client, "_headers", None) or getattr(exporter, "_headers")
    return {k.lower(): v for k, v in headers.items()}


def test_building_the_provider_leaves_signal_handlers_alone(fi_env):
    def mine(signum, frame):  # pragma: no cover - never called
        pass

    before = {sig: signal.signal(sig, mine) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        ctx = FakeContext()
        register(ctx)
        run_tool_turn(ctx)  # first turn hook builds the provider on this (main) thread
        assert signal.getsignal(signal.SIGINT) is mine
        assert signal.getsignal(signal.SIGTERM) is mine
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def test_provider_exports_uncompressed_with_explicit_keys():
    provider = build_tracer_provider(api_key="k", secret_key="s", base_url="https://fi.example/",
                                     project_name="p")
    exporter = _exporter(provider)
    assert exporter._endpoint == "https://fi.example/tracer/v1/traces"
    assert _headers(exporter)["x-api-key"] == "k" and _headers(exporter)["x-secret-key"] == "s"
    assert exporter._compression.value == "none"
    assert provider.resource.attributes["project_name"] == "p"
    assert provider.resource.attributes["project_type"] == "observe"
    provider.shutdown()


def test_session_finalize_does_not_start_tracing(fi_env):
    # A CLI session that ends with no turn calls on_session_finalize first, on
    # the main thread at exit: it must not build anything.
    plugin = FutureAGIHermesPlugin()
    plugin.callback("on_session_finalize")(session_id="s")
    assert plugin._provider is None and plugin._recorder is None


def test_exit_with_unreachable_collector_is_bounded(fi_env):
    plugin = FutureAGIHermesPlugin()
    ctx = FakeContext()
    for hook in ("pre_llm_call", "pre_api_request", "post_api_request", "on_session_end"):
        ctx.register_hook(hook, plugin.callback(hook))
    run_tool_turn(ctx)
    assert plugin._provider is not None
    t0 = time.monotonic()
    plugin.callback("on_session_finalize")(session_id="20261014_120000_abc123")
    plugin.shutdown()
    assert time.monotonic() - t0 < 8


class _Scope:
    """A stand-in for agent.secret_scope with Hermes' multiplexing semantics."""

    class UnscopedSecretError(Exception):
        pass

    def __init__(self):
        self.current = None

    def get_secret(self, name, default=None):
        if self.current is None:
            raise self.UnscopedSecretError(name)
        return self.current.get(name, default)


@pytest.fixture
def multiplexed(monkeypatch):
    scope = _Scope()
    module = types.ModuleType("agent.secret_scope")
    module.UnscopedSecretError = _Scope.UnscopedSecretError
    module.get_secret = scope.get_secret
    package = types.ModuleType("agent")
    package.secret_scope = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    # The launch profile's keys are in os.environ; served profiles must not use them.
    monkeypatch.setenv("FI_API_KEY", "launch-key")
    monkeypatch.setenv("FI_SECRET_KEY", "launch-secret")
    monkeypatch.setenv("FI_PROJECT_NAME", "launch-project")
    return scope


def test_multi_profile_gateway_uses_each_profiles_own_keys(multiplexed):
    # Hermes builds one PluginManager, hence one plugin instance, per profile.
    profiles = {
        "a": {"FI_API_KEY": "key-a", "FI_SECRET_KEY": "secret-a", "FI_PROJECT_NAME": "proj-a",
              "FI_BASE_URL": "http://127.0.0.1:9"},
        "b": {"FI_API_KEY": "key-b", "FI_SECRET_KEY": "secret-b", "FI_PROJECT_NAME": "proj-b",
              "FI_BASE_URL": "http://127.0.0.1:9"},
    }
    plugins = {}
    for name, secrets in profiles.items():
        plugin = FutureAGIHermesPlugin()
        # A hook outside any profile scope (e.g. gateway shutdown) builds nothing.
        multiplexed.current = None
        plugin.callback("pre_llm_call")(session_id="x", turn_id="t0")
        assert plugin._provider is None and not plugin._disabled
        multiplexed.current = secrets
        plugin.callback("pre_llm_call")(session_id=name, turn_id="t1")
        plugins[name] = plugin
    for name, plugin in plugins.items():
        exporter = _exporter(plugin._provider)
        assert _headers(exporter)["x-api-key"] == "key-" + name
        assert plugin._provider.resource.attributes["project_name"] == "proj-" + name
        plugin._provider.shutdown()


def test_profile_without_keys_does_not_borrow_the_launch_profiles(multiplexed):
    plugin = FutureAGIHermesPlugin()
    multiplexed.current = {"FI_PROJECT_NAME": "proj-c"}
    plugin.callback("pre_llm_call")(session_id="c", turn_id="t1")
    assert plugin._provider is None and plugin._disabled
