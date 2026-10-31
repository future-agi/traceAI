"""auto_instrument() and init(): lazy enabling, statuses, provider setup, fail-open behaviour."""

import logging
import signal
import sys
import textwrap

import pytest
import requests
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from auto_mock_server import MockServer, decode_spans
from fi_instrumentation.auto import _manager, _registry, wire
from fi_instrumentation.auto._registry import Integration

CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}

FAKE_INSTRUMENTOR = '''
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

CALLS = []


class FakeInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        if {fail}:
            raise RuntimeError("cannot patch")
        CALLS.append(kwargs)

    def _uninstrument(self, **kwargs):
        pass
'''


def _reset_global_provider():
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()
    trace_api._TRACER_PROVIDER = None


@pytest.fixture
def fresh_state(monkeypatch, tmp_path):
    """Fake libraries and traceAI packages on sys.path, a clean manager and global provider."""
    packages = {
        "fakeailib": "",
        "orphanlib": "",
        "brokenlib": "",
        "manuallib": "",
        "traceai_fakeailib": FAKE_INSTRUMENTOR.format(fail=False),
        "traceai_brokenlib": FAKE_INSTRUMENTOR.format(fail=True),
    }
    for name, source in packages.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text(textwrap.dedent(source))
    monkeypatch.syspath_prepend(str(tmp_path))
    integrations = (
        Integration("fake", ("fakeailib",), "traceAI-fake", "traceai_fakeailib", "FakeInstrumentor"),
        Integration("orphan", ("orphanlib",), "traceAI-orphan", "traceai_orphanlib", "X", wire=("openai.chat",)),
        Integration("broken", ("brokenlib",), "traceAI-broken", "traceai_brokenlib", "FakeInstrumentor"),
        Integration("manual", ("manuallib",), "traceAI-manual", "traceai_manuallib", kind=_registry.MANUAL, note="call setup()"),
        Integration("absent", ("nosuchlib_xyz",), "traceAI-absent", "traceai_absent", "X"),
        Integration("optin", ("fakeailib",), "traceAI-optin", "traceai_fakeailib", "FakeInstrumentor", default=False, note="opt in"),
    )
    monkeypatch.setattr(_registry, "ALL", integrations)
    monkeypatch.setattr(_manager, "_state", _manager._State())
    monkeypatch.setattr(_manager, "_warned_credentials", False)
    monkeypatch.setattr(_manager, "_sigterm_chained", False)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    for key in ("FI_API_KEY", "FI_SECRET_KEY", "FI_INSTRUMENTATIONS", "FI_DISABLED_INSTRUMENTATIONS",
                "FI_WIRE_CAPTURE", "FI_CONFIG_FILE", "OTEL_SDK_DISABLED", "FI_SAMPLE_RATE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    for module in list(sys.modules):
        if module.split(".")[0] in packages:
            sys.modules.pop(module)
    _reset_global_provider()
    yield tmp_path
    wire.disable()
    signal.signal(signal.SIGTERM, previous_sigterm)
    _reset_global_provider()


def test_lazy_enable_on_first_import(fresh_state):
    provider = TracerProvider()
    statuses = _manager.auto_instrument(tracer_provider=provider)
    assert statuses["fake"] == _manager.PENDING
    assert statuses["orphan"] == _manager.MISSING_INSTRUMENTOR
    assert statuses["manual"] == _manager.STATUS_MANUAL
    assert statuses["absent"] == _manager.NOT_INSTALLED
    assert statuses["optin"] == _manager.DISABLED
    assert "fakeailib" not in sys.modules

    import fakeailib  # noqa: F401
    import traceai_fakeailib

    report = {s.name: s for s in _manager.report()}
    assert report["fake"].status == _manager.INSTRUMENTED
    assert traceai_fakeailib.CALLS[-1]["tracer_provider"] is provider
    assert "pip install traceAI-orphan" in report["orphan"].detail
    assert "openai.chat" in report["orphan"].detail
    assert report["manual"].detail == "call setup()"


def test_failures_are_reported_not_raised(fresh_state, caplog):
    import brokenlib  # noqa: F401

    with caplog.at_level(logging.WARNING):
        statuses = _manager.auto_instrument(tracer_provider=TracerProvider())
    assert statuses["broken"] == _manager.FAILED
    detail = {s.name: s.detail for s in _manager.report()}["broken"]
    assert "cannot patch" in detail
    assert "could not enable broken" in caplog.text


def test_allowlist_denylist_and_wire_switch(fresh_state):
    statuses = _manager.auto_instrument(
        tracer_provider=TracerProvider(), instrumentations=["optin", "wire"], lazy=False
    )
    assert statuses["optin"] == _manager.INSTRUMENTED
    assert statuses["fake"] == _manager.DISABLED
    assert statuses["wire.httpx"] != _manager.DISABLED
    statuses = _manager.auto_instrument(tracer_provider=TracerProvider(), disabled_instrumentations=["wire"])
    assert statuses["wire.requests"] != _manager.DISABLED


def test_plan_is_side_effect_free(fresh_state):
    statuses = {s.name: s.status for s in _manager.plan()}
    assert statuses["fake"] == _manager.PENDING
    assert "fakeailib" not in sys.modules
    assert "traceai_fakeailib" not in sys.modules
    text = _manager.format_report(_manager.plan())
    assert "fake" in text and "absent" not in text


def test_init_without_credentials_is_a_quiet_no_op(fresh_state, caplog):
    with caplog.at_level(logging.WARNING):
        assert _manager.init() is None
        assert _manager.init() is None
    assert caplog.text.count("FI_API_KEY and FI_SECRET_KEY are not set") == 1
    assert isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider)


def test_init_respects_otel_sdk_disabled(fresh_state, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert _manager.init(api_key="k", secret_key="s") is None


def test_init_exports_ai_calls_and_only_chains_sigterm(fresh_state):
    sigint = signal.getsignal(signal.SIGINT)
    exporter = InMemorySpanExporter()
    provider = _manager.init(project_name="zero-code", span_exporter=exporter, batch=False, sample_rate=1.0)
    assert provider is trace_api.get_tracer_provider()
    assert signal.getsignal(signal.SIGINT) == sigint
    assert signal.getsignal(signal.SIGTERM).__qualname__.endswith("_flush_on_sigterm.<locals>.handler")
    with MockServer() as server:
        requests.Session().post(f"{server.url}/v1/chat/completions", json=CHAT)
    (span,) = exporter.get_finished_spans()
    assert span.name == "chat gpt-4o-mini"
    assert span.resource.attributes["project_name"] == "zero-code"
    assert span.resource.attributes["project_type"] == "observe"
    assert _manager.flush() is True


def test_init_coexists_with_an_existing_otel_provider(fresh_state):
    app_exporter = InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace_api.set_tracer_provider(app_provider)
    with MockServer() as server:
        provider = _manager.init(api_key="key-1234567", secret_key="secret-1234567", base_url=server.url,
                                 project_name="coexist")
        assert provider is app_provider
        requests.Session().post(f"{server.url}/v1/chat/completions", json=CHAT)
        assert app_provider.force_flush()
        exported = decode_spans(server.collected)
        auth = [r["headers"] for r in server.requests if r["path"].endswith("/v1/traces")]
    assert [s.name for s in app_exporter.get_finished_spans()] == ["chat gpt-4o-mini"]
    assert [s["name"] for s in exported] == ["chat gpt-4o-mini"]
    assert exported[0]["resource"]["project_name"] == "coexist"
    assert auth and auth[0].get("x-api-key") == "key-1234567"
    _manager.shutdown()  # not ours: must leave the application's provider running
    assert app_provider._active_span_processor is not None


def test_sampling_and_config_errors(fresh_state):
    from fi_instrumentation.auto import ConfigError

    provider = _manager.init(span_exporter=InMemorySpanExporter(), batch=False, sample_rate=0.0)
    tracer = provider.get_tracer("t")
    assert not tracer.start_span("dropped").is_recording()
    # Once configured, even invalid later arguments are ignored without side effects.
    assert _manager.init(sample_rate=3) is provider


def test_init_reuses_a_registered_future_agi_provider(fresh_state):
    from fi_instrumentation import register

    exporter = InMemorySpanExporter()
    registered = register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                          set_global_tracer_provider=True, signal_handlers=False)
    assert _manager.init(api_key="k", secret_key="s") is registered
    with MockServer() as server:
        requests.Session().post(f"{server.url}/v1/chat/completions", json=CHAT)
        assert server.collected == []  # no second exporter was added
    assert [s.name for s in exporter.get_finished_spans()] == ["chat gpt-4o-mini"]


def test_project_name_defaults_to_the_service(fresh_state, monkeypatch):
    from fi_instrumentation.auto import resolve

    assert resolve().project_name == fresh_state.name
    assert resolve().sources["project_name"] == "working directory name"
    monkeypatch.setenv("OTEL_SERVICE_NAME", "checkout")
    assert resolve().project_name == "checkout"
    monkeypatch.setenv("FI_PROJECT_NAME", "explicit")
    assert resolve().project_name == "explicit"


def test_explicit_exporter_headers_beat_environment(fresh_state, monkeypatch):
    from fi_instrumentation.otel import HTTPSpanExporter

    monkeypatch.setenv("FI_API_KEY", "account-A-key")
    monkeypatch.setenv("FI_SECRET_KEY", "account-A-secret")
    exporter = HTTPSpanExporter(headers={"X-API-Key": "account-B-key", "x-secret-key": "account-B-secret"})
    headers = getattr(exporter, "_headers", None) or exporter._client._headers
    assert headers["x-api-key"] == "account-B-key"
    assert headers["x-secret-key"] == "account-B-secret"
    exporter.shutdown()


def test_repeated_init_reuses_app_processor_and_exporter(fresh_state):
    app_provider = TracerProvider()
    trace_api.set_tracer_provider(app_provider)
    exporter = InMemorySpanExporter()
    for _ in range(3):
        assert _manager.init(span_exporter=exporter, batch=False, project_name="coexist") is app_provider
    assert len(app_provider._active_span_processor._span_processors) == 1
    with app_provider.get_tracer("app").start_as_current_span("one"):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == ["one"]
    _manager.shutdown()


def test_repeated_init_preserves_original_project_and_exporter(fresh_state, monkeypatch):
    exporter = InMemorySpanExporter()
    calls = []
    monkeypatch.setattr(exporter, "shutdown", lambda: calls.append("shutdown"))
    provider = _manager.init(span_exporter=exporter, batch=False, project_name="first")
    assert _manager.init(span_exporter=exporter, batch=False, project_name="second") is provider
    assert calls == []
    with provider.get_tracer("app").start_as_current_span("one"):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.resource.attributes["project_name"] == "first"
    assert len(provider._active_span_processor._span_processors) == 1
    _manager.shutdown()
    assert calls == ["shutdown"]


def test_provider_replacement_never_transfers_ownership(fresh_state, monkeypatch):
    _manager.init(span_exporter=InMemorySpanExporter(), batch=False)
    application = TracerProvider()
    called = []
    monkeypatch.setattr(application, "shutdown", lambda: called.append(True))
    _manager.init(tracer_provider=application)
    _manager.shutdown()
    assert called == []


def test_second_init_cannot_replace_app_export_route(fresh_state):
    app_provider = TracerProvider()
    trace_api.set_tracer_provider(app_provider)
    first, second = InMemorySpanExporter(), InMemorySpanExporter()
    _manager.init(span_exporter=first, batch=False)
    _manager.init(span_exporter=second, batch=False)
    assert len(app_provider._active_span_processor._span_processors) == 1
    with app_provider.get_tracer("app").start_as_current_span("one"):
        pass
    assert len(first.get_finished_spans()) == 1
    assert not second.get_finished_spans()
    _manager.shutdown()


def test_register_argument_passthrough_and_resource_parity(fresh_state):
    provider = _manager.init(project_type="experiment", project_version_name="release-1",
                             project_name="project", span_exporter=InMemorySpanExporter(), batch=False,
                             verbose=False, signal_handlers=False)
    attrs = provider.resource.attributes
    assert attrs["project_name"] == "project"
    assert attrs["project_type"] == "experiment"
    assert attrs["project_version_name"] == "release-1"
    assert attrs["project_version_id"]
    assert attrs["eval_tags"] == "[]"
    assert attrs["metadata"] == "{}"
    assert attrs["semantic_convention"] == "fi"
    _manager.shutdown()


def test_app_resource_defaults_match_register(fresh_state):
    application = TracerProvider()
    trace_api.set_tracer_provider(application)
    exporter = InMemorySpanExporter()
    _manager.init(span_exporter=exporter, batch=False)
    with application.get_tracer("app").start_as_current_span("one"):
        pass
    attrs = exporter.get_finished_spans()[0].resource.attributes
    assert attrs["project_name"] == fresh_state.name
    assert attrs["project_version_name"] == "DEFAULT_PROJECT_VERSION_NAME"
    assert attrs["project_type"] == "observe"
    assert attrs["eval_tags"] == "[]"
    assert attrs["metadata"] == "{}"
    assert attrs["semantic_convention"] == "fi"
    _manager.shutdown()


def test_flush_and_shutdown_honor_deadlines(fresh_state, monkeypatch):
    import threading
    import time

    release = threading.Event()

    class StuckProvider:
        def force_flush(self, timeout_millis):
            release.wait(2)
            return True

        def shutdown(self):
            release.wait(2)

    monkeypatch.setattr(_manager, "_lifecycle_lock", threading.Lock())
    _manager._state.provider = StuckProvider()
    _manager._state.owned_provider = True
    start = time.monotonic()
    try:
        assert _manager.flush(20) is False
        # A timed-out operation must not spawn more blocked workers.
        assert _manager.flush(20) is False
        _manager.shutdown(timeout_millis=20)
        assert time.monotonic() - start < 0.5
    finally:
        release.set()


def test_sigterm_reentrant_flush_is_bounded_and_chains(fresh_state, monkeypatch):
    import threading
    import time

    called, entered = [], threading.Event()
    export_lock = threading.Lock()

    class Provider:
        def force_flush(self, timeout_millis):
            entered.set()
            with export_lock:
                return True

    monkeypatch.setattr(_manager, "_lifecycle_lock", threading.Lock())
    signal.signal(signal.SIGTERM, lambda *args: called.append(True))
    _manager._state.provider = provider = Provider()
    _manager._flush_on_sigterm(provider, timeout_millis=20)
    with export_lock:
        start = time.monotonic()
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert time.monotonic() - start < 0.5
        assert entered.is_set()
        assert called == [True]


def test_auto_instrument_cannot_disable_enabled_library(fresh_state, monkeypatch):
    import traceai_fakeailib

    calls = []
    instrumentor = traceai_fakeailib.FakeInstrumentor()
    monkeypatch.setattr(instrumentor, "uninstrument", lambda: calls.append("disabled"))
    _manager.auto_instrument(tracer_provider=TracerProvider(), lazy=False)
    statuses = _manager.auto_instrument(disabled_instrumentations=["fake"])
    assert calls == []
    assert statuses["fake"] == _manager.INSTRUMENTED
    _manager.shutdown()
    assert calls == ["disabled"]


def test_agent_shutdown_leaves_application_span_open(fresh_state):
    application = TracerProvider()
    trace_api.set_tracer_provider(application)
    _manager.init(span_exporter=InMemorySpanExporter(), batch=False)
    active = application.get_tracer("app").start_span("still working")
    _manager.shutdown()
    assert active.is_recording()
    active.end()


def test_auto_instrument_cannot_rebind_enabled_instrumentor(fresh_state):
    import traceai_fakeailib

    first, second = TracerProvider(), TracerProvider()
    _manager.auto_instrument(tracer_provider=first, lazy=False)
    _manager.auto_instrument(tracer_provider=second, lazy=False)
    assert traceai_fakeailib.CALLS[-1]["tracer_provider"] is first
