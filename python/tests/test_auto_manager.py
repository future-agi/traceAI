"""auto_instrument() and init(): lazy enabling, statuses, provider setup and fail-open behaviour."""

import logging
import signal
import sys

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from auto_mock_server import MockServer, decode_spans
from fi_instrumentation.auto import _manager, _registry
from fi_instrumentation.auto._registry import Integration

# A stand-in AI library and its traceAI package, written to a temporary directory per test.
FAKE_LIBRARY = '''
def complete(prompt):
    return "pong"
'''

FAKE_INSTRUMENTOR = '''
import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import unwrap

from fi_instrumentation import FITracer, TraceConfig

CALLS = []


class FakeInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        if FAIL:
            raise RuntimeError("cannot patch")
        CALLS.append(kwargs)
        tracer = FITracer(trace_api.get_tracer(__name__, None, kwargs.get("tracer_provider")),
                          config=kwargs.get("config") or TraceConfig())

        def traced(wrapped, instance, args, call_kwargs):
            attributes = {"gen_ai.span.kind": "LLM", "input.value": args[0]}
            with tracer.start_as_current_span("chat fake", attributes=attributes) as span:
                result = wrapped(*args, **call_kwargs)
                span.set_attribute("output.value", result)
                return result

        wrapt.wrap_function_wrapper("TARGET", "complete", traced)

    def _uninstrument(self, **kwargs):
        unwrap(__import__("TARGET"), "complete")
'''

FAKE_MAPPER = '''
class BaseMappedSpanExporter:
    def _convert_attributes(self, attributes):
        return {**attributes, "mapped": True}


def enable_fi_attribute_mapping(*args, **kwargs):
    return False
'''

_ENVIRONMENT = (
    "FI_API_KEY", "FI_SECRET_KEY", "FI_INSTRUMENTATIONS", "FI_DISABLED_INSTRUMENTATIONS",
    "FI_CONFIG_FILE", "OTEL_SDK_DISABLED", "FI_SAMPLE_RATE", "FI_PROJECT_NAME", "FI_PROJECT_TYPE",
    "FI_PROJECT_VERSION_NAME", "OTEL_SERVICE_NAME", "FI_AUTO_DEBUG", "FI_HIDE_INPUTS",
)


def _instrumentor(target, fail):
    return FAKE_INSTRUMENTOR.replace("TARGET", target).replace("FAIL", str(fail))


def _reset_global_provider():
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()
    trace_api._TRACER_PROVIDER = None


@pytest.fixture
def fresh_state(monkeypatch, tmp_path):
    """Fake libraries and traceAI packages on sys.path, a clean manager and global provider."""
    packages = {
        "fakeailib": FAKE_LIBRARY,
        "orphanlib": "",
        "brokenlib": FAKE_LIBRARY,
        "manuallib": "",
        "traceai_fakeailib": _instrumentor("fakeailib", fail=False),
        "traceai_brokenlib": _instrumentor("brokenlib", fail=True),
        "traceai_fakemapped": FAKE_MAPPER,
    }
    for name, source in packages.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    integrations = (
        Integration("fake", ("fakeailib",), "traceAI-fake", "traceai_fakeailib", "FakeInstrumentor"),
        Integration("orphan", ("orphanlib",), "traceAI-orphan", "traceai_orphanlib", "X"),
        Integration("broken", ("brokenlib",), "traceAI-broken", "traceai_brokenlib", "FakeInstrumentor"),
        Integration("manual", ("manuallib",), "traceAI-manual", "traceai_manuallib", kind=_registry.MANUAL,
                    note="call setup()"),
        Integration("absent", ("nosuchlib_xyz",), "traceAI-absent", "traceai_absent", "X"),
        Integration("optin", ("fakeailib",), "traceAI-optin", "traceai_fakeailib", "FakeInstrumentor",
                    default=False, note="opt in"),
    )
    monkeypatch.setattr(_registry, "ALL", integrations)
    monkeypatch.setattr(_manager, "_state", _manager._State())
    monkeypatch.setattr(_manager, "_warned_credentials", False)
    monkeypatch.setattr(_manager, "_sigterm_chained", False)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    for key in _ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    for module in list(sys.modules):
        if module.split(".")[0] in packages:
            sys.modules.pop(module)
    _reset_global_provider()
    yield tmp_path
    _manager.shutdown()
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
    assert "traceai_fakeailib" not in sys.modules

    import fakeailib  # noqa: F401
    import traceai_fakeailib

    report = {s.name: s for s in _manager.report()}
    assert report["fake"].status == _manager.INSTRUMENTED
    assert traceai_fakeailib.CALLS[-1]["tracer_provider"] is provider
    # Without its traceAI package a library is not traced at all in this release, and the report says so.
    assert "not traced" in report["orphan"].detail
    assert "pip install traceAI-orphan" in report["orphan"].detail
    assert report["manual"].detail == "call setup()"


def test_failures_are_reported_not_raised(fresh_state, caplog):
    import brokenlib  # noqa: F401

    with caplog.at_level(logging.WARNING):
        statuses = _manager.auto_instrument(tracer_provider=TracerProvider())
    assert statuses["broken"] == _manager.FAILED
    detail = {s.name: s.detail for s in _manager.report()}["broken"]
    assert "cannot patch" in detail
    assert "could not enable broken" in caplog.text


def test_allowlist_enables_only_the_named_integrations(fresh_state):
    statuses = _manager.auto_instrument(tracer_provider=TracerProvider(), instrumentations=["optin"], lazy=False)
    assert statuses["optin"] == _manager.INSTRUMENTED
    assert statuses["fake"] == _manager.DISABLED
    assert statuses["broken"] == _manager.DISABLED


def test_denylist_from_the_environment(fresh_state, monkeypatch):
    monkeypatch.setenv("FI_DISABLED_INSTRUMENTATIONS", " Fake, broken ")
    statuses = _manager.auto_instrument(tracer_provider=TracerProvider(), lazy=False)
    assert statuses["fake"] == _manager.DISABLED
    assert statuses["broken"] == _manager.DISABLED
    assert statuses["manual"] == _manager.STATUS_MANUAL


def test_plan_is_side_effect_free(fresh_state):
    statuses = {s.name: s.status for s in _manager.plan()}
    assert statuses["fake"] == _manager.PENDING
    assert "fakeailib" not in sys.modules
    assert "traceai_fakeailib" not in sys.modules
    text = _manager.format_report(_manager.plan())
    assert "fake" in text and "absent" not in text
    assert "absent" in _manager.format_report(_manager.plan(), include_absent=True)


def test_init_without_credentials_is_a_quiet_no_op(fresh_state, caplog):
    with caplog.at_level(logging.WARNING):
        assert _manager.init() is None
        assert _manager.init() is None
    assert caplog.text.count("FI_API_KEY and FI_SECRET_KEY are not set") == 1
    assert isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider)
    import fakeailib

    assert fakeailib.complete("ping") == "pong"
    assert "traceai_fakeailib" not in sys.modules


def test_init_respects_otel_sdk_disabled(fresh_state, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER") is None
    assert isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider)


def test_init_traces_library_calls_and_only_chains_sigterm(fresh_state):
    sigint = signal.getsignal(signal.SIGINT)
    exporter = InMemorySpanExporter()
    provider = _manager.init(project_name="zero-code", span_exporter=exporter, batch=False, sample_rate=1.0)
    assert provider is trace_api.get_tracer_provider()
    assert signal.getsignal(signal.SIGINT) == sigint
    assert signal.getsignal(signal.SIGTERM).__qualname__.endswith("_flush_on_sigterm.<locals>.handler")
    import fakeailib

    assert fakeailib.complete("ping") == "pong"
    (span,) = exporter.get_finished_spans()
    assert span.name == "chat fake"
    assert span.attributes["gen_ai.span.kind"] == "LLM"
    # Content capture is on by default.
    assert span.attributes["input.value"] == "ping"
    assert span.attributes["output.value"] == "pong"
    assert span.resource.attributes["project_name"] == "zero-code"
    assert span.resource.attributes["project_type"] == "observe"
    assert _manager.flush() is True


def test_hide_settings_apply_to_enabled_libraries(fresh_state, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    exporter = InMemorySpanExporter()
    _manager.init(span_exporter=exporter, batch=False)
    import fakeailib
    import traceai_fakeailib

    fakeailib.complete("ping")
    assert traceai_fakeailib.CALLS[-1]["config"].hide_inputs is True
    (span,) = exporter.get_finished_spans()
    assert span.attributes.get("input.value") != "ping"
    assert span.attributes["output.value"] == "pong"


def test_no_wire_layer_is_loaded(fresh_state):
    _manager.init(span_exporter=InMemorySpanExporter(), batch=False)
    import fakeailib

    fakeailib.complete("ping")
    assert not [name for name in sys.modules if name.startswith("fi_instrumentation.auto.wire")]


def test_init_coexists_with_an_existing_otel_provider(fresh_state):
    app_exporter = InMemorySpanExporter()
    app_provider = TracerProvider(resource=Resource.create({"service.name": "checkout"}))
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace_api.set_tracer_provider(app_provider)
    with MockServer() as server:
        provider = _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER",
                                 base_url=server.url, project_name="coexist")
        assert provider is app_provider
        import fakeailib

        fakeailib.complete("ping")
        assert app_provider.force_flush()
        exported = decode_spans(server.collected)
        headers = [{k.lower(): v for k, v in r["headers"].items()} for r in server.trace_requests()]
    (app_span,) = app_exporter.get_finished_spans()
    assert app_span.name == "chat fake"
    # The application's own view keeps its resource.
    assert "project_name" not in app_span.resource.attributes
    assert [s["name"] for s in exported] == ["chat fake"]
    assert exported[0]["resource"]["project_name"] == "coexist"
    assert exported[0]["resource"]["service.name"] == "checkout"
    assert headers and headers[0].get("x-api-key") == "fi-api-PLACEHOLDER"
    _manager.shutdown()  # not ours: must leave the application's provider running
    with app_provider.get_tracer("app").start_as_current_span("after"):
        pass
    assert [s.name for s in app_exporter.get_finished_spans()] == ["chat fake", "after"]


def test_sample_rate_applies_to_the_agent_provider(fresh_state):
    provider = _manager.init(span_exporter=InMemorySpanExporter(), batch=False, sample_rate=0.0)
    tracer = provider.get_tracer("t")
    assert not tracer.start_span("dropped").is_recording()
    # Once configured, even invalid later arguments are ignored without side effects.
    assert _manager.init(sample_rate=3) is provider


def test_sample_rate_on_an_application_provider_thins_only_the_future_agi_route(fresh_state):
    app_exporter, fi_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace_api.set_tracer_provider(app_provider)
    _manager.init(span_exporter=fi_exporter, batch=False, sample_rate=0.0)
    with app_provider.get_tracer("app").start_as_current_span("one"):
        pass
    assert [s.name for s in app_exporter.get_finished_spans()] == ["one"]
    assert not fi_exporter.get_finished_spans()


def test_init_reuses_a_registered_future_agi_provider(fresh_state):
    from fi_instrumentation import register

    exporter = InMemorySpanExporter()
    registered = register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                          set_global_tracer_provider=True)
    assert _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER") is registered
    assert len(registered._active_span_processor._span_processors) == 1  # no second Future AGI route
    import fakeailib

    fakeailib.complete("ping")
    assert [s.name for s in exporter.get_finished_spans()] == ["chat fake"]


def test_project_name_defaults_to_the_service(fresh_state, monkeypatch):
    from fi_instrumentation.auto import resolve

    assert resolve().project_name == fresh_state.name
    assert resolve().sources["project_name"] == "working directory name"
    monkeypatch.setenv("OTEL_SERVICE_NAME", "checkout")
    assert resolve().project_name == "checkout"
    monkeypatch.setenv("FI_PROJECT_NAME", "explicit")
    assert resolve().project_name == "explicit"


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
                             verbose=False)
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


def test_attribute_mappers_change_only_the_future_agi_route(fresh_state, monkeypatch):
    mapped = Integration("mapped", ("fakeailib",), "traceAI-mapped", "traceai_fakemapped", "pipecat",
                         kind=_registry.SETUP, category="voice")
    monkeypatch.setattr(_registry, "ALL", (mapped,))
    app_exporter, fi_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace_api.set_tracer_provider(app_provider)
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False)
    assert {s.name: s.status for s in _manager.report()}["mapped"] == _manager.INSTRUMENTED
    with app_provider.get_tracer("app").start_as_current_span("one"):
        pass
    assert "mapped" not in app_exporter.get_finished_spans()[0].attributes
    assert fi_exporter.get_finished_spans()[0].attributes["mapped"] is True


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


def test_exit_flush_runs_inline_when_python_refuses_new_threads(fresh_state, monkeypatch):
    # Python 3.12+ raises this from Thread.start() inside atexit handlers.
    import threading

    def refuse(self):
        raise RuntimeError("can't create new thread at interpreter shutdown")

    calls = []
    lock = threading.Lock()
    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert _manager._bounded_call(lambda: calls.append("flushed"), 100, lock) is True
    assert calls == ["flushed"]
    assert not lock.locked()


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
