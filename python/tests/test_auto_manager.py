"""auto_instrument() and init(): lazy enabling, statuses, provider setup and fail-open behaviour."""

import importlib
import logging
import signal
import sys

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from auto_mock_server import MockServer, decode_spans
from fi_instrumentation.auto import _manager, _registry
from fi_instrumentation.auto._registry import Integration
from fi_instrumentation.otel import HTTPSpanExporter

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

# A stand-in web server library and its OpenTelemetry instrumentor: each request is a root SERVER
# span with an internal child, under the instrumentor's own scope. Only /chat calls the AI library.
# The root carries the request target with its query string, and any request headers, as the
# OpenTelemetry web instrumentations record them.
FAKE_WEB_LIBRARY = '''
def handle(path, headers=None):
    if path.partition("?")[0] == "/chat":
        import fakeailib

        return fakeailib.complete("ping")
    return "ok"
'''

FAKE_WEB_INSTRUMENTOR = '''
import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import unwrap


class FakeWebInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        tracer = trace_api.get_tracer("opentelemetry.instrumentation.fakeweb", None, kwargs.get("tracer_provider"))

        def traced(wrapped, instance, args, call_kwargs):
            target = args[0]
            attributes = {"http.target": target, "url.full": "http://shop.example.com" + target}
            for name, value in (call_kwargs.get("headers") or {}).items():
                attributes["http.request.header." + name] = (value,)
            with tracer.start_as_current_span("GET " + target.partition("?")[0], kind=trace_api.SpanKind.SERVER,
                                              attributes=attributes):
                with tracer.start_as_current_span("http send"):
                    return wrapped(*args, **call_kwargs)

        wrapt.wrap_function_wrapper("fakeweblib", "handle", traced)

    def _uninstrument(self, **kwargs):
        unwrap(__import__("fakeweblib"), "handle")
'''

WEB_SCOPE = "opentelemetry.instrumentation.fakeweb"

# A stand-in for the voice package's export step: it records the policies its mapped copy sees.
FAKE_LIVEKIT = {
    "traceai_livekit": '''
SEEN = []


class BaseMappedSpanExporter:
    def _convert_attributes(self, attributes, *, recording_snapshot=False):
        return {**attributes, "mapped": True}

    def _mapped_copy(self, span):
        from traceai_livekit._recording_boundary import _copy_span

        SEEN.append(tuple(getattr(span.instrumentation_scope, "recording_policies", ())))
        return _copy_span(span, attributes=self._convert_attributes(dict(span.attributes or {})))


def enable_fi_attribute_mapping(*args, **kwargs):
    return False


def flush_on_job_shutdown(provider=None):
    return True
''',
    "traceai_livekit/_recording_attributes": '''
def _is_recording_snapshot(attributes, *, span_name=None):
    return "lk.recording.operation" in attributes or span_name == "livekit.recording.snapshot"
''',
    "traceai_livekit/_recording_boundary": '''
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope


class _RecordingScope(InstrumentationScope):
    def __init__(self, scope, policies):
        super().__init__(name=scope.name, version=scope.version, schema_url=scope.schema_url)
        self.recording_policies = tuple(policies)


def _copy_span(span, **changes):
    fields = {key: getattr(span, key) for key in (
        "name", "context", "parent", "resource", "attributes", "events", "links",
        "kind", "status", "start_time", "end_time", "instrumentation_scope")}
    fields.update(changes)
    return ReadableSpan(**fields)
''',
}

# A stand-in for a manual integration's exporter wrapper: the app passes it to register() around a
# Future AGI exporter, and it converts its library's spans before handing them on.
FAKE_WRAPPER = '''
from opentelemetry.sdk.trace.export import SpanExporter


class WrappedSpanExporter(SpanExporter):
    def __init__(self, exporter):
        self.exporter = exporter

    def export(self, spans):
        return self.exporter.export(spans)

    def shutdown(self):
        self.exporter.shutdown()
'''

# Spans under a traceAI instrumentation scope are AI spans, so the Future AGI route carries them.
AI_SCOPE = "traceai_test_app"


class FutureAGIExporter(HTTPSpanExporter):
    """A Future AGI exporter, of the type register() creates, that keeps what it gets in memory."""

    def __init__(self):
        super().__init__(endpoint="http://127.0.0.1:9/v1/traces")
        self._memory = InMemorySpanExporter()

    def export(self, spans):
        return self._memory.export(spans)

    def get_finished_spans(self):
        return self._memory.get_finished_spans()


_ENVIRONMENT = (
    "FI_API_KEY", "FI_SECRET_KEY", "FI_INSTRUMENTATIONS", "FI_DISABLED_INSTRUMENTATIONS",
    "FI_CONFIG_FILE", "OTEL_SDK_DISABLED", "FI_SAMPLE_RATE", "FI_PROJECT_NAME", "FI_PROJECT_TYPE",
    "FI_PROJECT_VERSION_NAME", "OTEL_SERVICE_NAME", "FI_AUTO_DEBUG", "FI_HIDE_INPUTS",
)

# Stand-ins that single tests write; removed from sys.modules around every test.
_STAND_INS = ("fakeweblib", "fakewebinstr", "traceai_livekit", "latelib", "traceai_latelib",
              "partiallib", "traceai_partiallib", "traceai_wrapped")


def _instrumentor(target, fail):
    return FAKE_INSTRUMENTOR.replace("TARGET", target).replace("FAIL", str(fail))


def _write_packages(root, packages):
    """Write ``name`` (a package) or ``name/sub`` (a module in it) sources, visible to imports at once."""
    for name, source in packages.items():
        package, _, module = name.partition("/")
        (root / package).mkdir(exist_ok=True)
        (root / package / f"{module or '__init__'}.py").write_text(source)
    importlib.invalidate_caches()


def _forget_modules(names):
    for module in list(sys.modules):
        if module.split(".")[0] in names:
            sys.modules.pop(module)


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
    _write_packages(tmp_path, packages)
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
    _forget_modules(set(packages) | set(_STAND_INS))
    _reset_global_provider()
    yield tmp_path
    _manager.shutdown()
    signal.signal(signal.SIGTERM, previous_sigterm)
    _reset_global_provider()
    _forget_modules(_STAND_INS)


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


def test_an_incomplete_key_pair_warns_once(fresh_state, monkeypatch, caplog):
    monkeypatch.setenv("FI_API_KEY", "fi-api-PLACEHOLDER")
    with caplog.at_level(logging.WARNING):
        assert _manager.init() is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "missing FI_SECRET_KEY" in warnings[0]


def test_a_found_config_file_base_url_with_keys_from_elsewhere_keeps_the_agent_off(fresh_state, monkeypatch, caplog):
    monkeypatch.delenv("FI_BASE_URL", raising=False)
    (fresh_state / "futureagi.toml").write_text('base_url = "http://127.0.0.1:1"\n')
    monkeypatch.setenv("FI_API_KEY", "fi-api-PLACEHOLDER")
    monkeypatch.setenv("FI_SECRET_KEY", "fi-secret-PLACEHOLDER")
    with caplog.at_level(logging.WARNING):
        assert _manager.init() is None
    # One warning, saying why; nothing is sent to the default collector instead.
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "export to Future AGI is off" in warnings[0]
    assert isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider)


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

    exporter = FutureAGIExporter()
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
    with app_provider.get_tracer(AI_SCOPE).start_as_current_span("one"):
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
    with provider.get_tracer(AI_SCOPE).start_as_current_span("one"):
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
    with app_provider.get_tracer(AI_SCOPE).start_as_current_span("one"):
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
    with application.get_tracer(AI_SCOPE).start_as_current_span("one"):
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
    with app_provider.get_tracer(AI_SCOPE).start_as_current_span("one"):
        pass
    assert "mapped" not in app_exporter.get_finished_spans()[0].attributes
    assert fi_exporter.get_finished_spans()[0].attributes["mapped"] is True


def _credential_bearing_span(tracer):
    # An application HTTP client span: a credential-named query parameter and an authorization header.
    attributes = {
        "url.full": "https://api.example.com/v1/orders?api_key=sk-test-PLACEHOLDER",
        "http.request.header.authorization": ("Bearer sk-test-PLACEHOLDER",),
    }
    with tracer.start_as_current_span("GET /v1/orders", kind=trace_api.SpanKind.CLIENT, attributes=attributes):
        pass


def _future_agi_text(exporter):
    """Everything Future AGI receives for each span: name, attributes, events, links and status."""
    return repr([(s.name, dict(s.attributes or {}),
                  [(e.name, dict(e.attributes or {})) for e in s.events],
                  [dict(link.attributes or {}) for link in s.links], s.status.description)
                 for s in exporter.get_finished_spans()])


def _web_stand_ins(root, monkeypatch):
    """The stand-in web server and AI library, as the only two integrations in the registry."""
    _write_packages(root, {"fakeweblib": FAKE_WEB_LIBRARY, "fakewebinstr": FAKE_WEB_INSTRUMENTOR})
    web = Integration("fakeweb", ("fakeweblib",), "opentelemetry-instrumentation-fakeweb", "fakewebinstr",
                      "FakeWebInstrumentor", category="web", scopes=(WEB_SCOPE,))
    fake = Integration("fake", ("fakeailib",), "traceAI-fake", "traceai_fakeailib", "FakeInstrumentor")
    monkeypatch.setattr(_registry, "ALL", (web, fake))


def _application_provider():
    """A provider the application configured, with its own exporter, set as the global provider."""
    app_exporter = InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace_api.set_tracer_provider(app_provider)
    return app_provider, app_exporter


def test_future_agi_route_carries_only_ai_spans_on_an_application_provider(fresh_state):
    from fi_instrumentation import TraceConfig
    from fi_instrumentation.instrumentation.config import REDACTED_VALUE

    app_exporter, fi_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace_api.set_tracer_provider(app_provider)
    _manager.init(span_exporter=fi_exporter, batch=False, config=TraceConfig(hide_inputs=True))
    _credential_bearing_span(app_provider.get_tracer("checkout.http"))
    # A traceAI span that did not mask its own content is masked on the Future AGI copy.
    with app_provider.get_tracer(AI_SCOPE).start_as_current_span("raw", attributes={"input.value": "a prompt"}):
        pass
    import fakeailib

    fakeailib.complete("ping")
    # The application's own exporter still gets every span, unchanged.
    assert [s.name for s in app_exporter.get_finished_spans()] == ["GET /v1/orders", "raw", "chat fake"]
    assert app_exporter.get_finished_spans()[1].attributes["input.value"] == "a prompt"
    assert [s.name for s in fi_exporter.get_finished_spans()] == ["raw", "chat fake"]
    assert fi_exporter.get_finished_spans()[0].attributes["input.value"] == REDACTED_VALUE
    assert "PLACEHOLDER" not in _future_agi_text(fi_exporter)


def test_future_agi_route_carries_only_ai_spans_on_the_agent_provider(fresh_state):
    from fi_instrumentation import TraceConfig
    from fi_instrumentation.instrumentation.config import REDACTED_VALUE

    fi_exporter, app_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    provider = _manager.init(span_exporter=fi_exporter, batch=False, config=TraceConfig(hide_inputs=True))
    assert provider is trace_api.get_tracer_provider()
    # A processor the application adds to the agent's provider, for its own backend.
    provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    _credential_bearing_span(trace_api.get_tracer("checkout.http"))
    with trace_api.get_tracer(AI_SCOPE).start_as_current_span("raw", attributes={"input.value": "a prompt"}):
        pass
    assert [s.name for s in app_exporter.get_finished_spans()] == ["GET /v1/orders", "raw"]
    assert app_exporter.get_finished_spans()[1].attributes["input.value"] == "a prompt"
    assert [s.name for s in fi_exporter.get_finished_spans()] == ["raw"]
    assert fi_exporter.get_finished_spans()[0].attributes["input.value"] == REDACTED_VALUE
    assert "PLACEHOLDER" not in _future_agi_text(fi_exporter)


def test_future_agi_route_keeps_only_request_roots_that_led_to_an_ai_call(fresh_state, monkeypatch):
    _web_stand_ins(fresh_state, monkeypatch)
    app_provider, app_exporter = _application_provider()
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False)
    statuses = {s.name: s.status for s in _manager.report()}
    assert statuses == {"fakeweb": _manager.INSTRUMENTED, "fake": _manager.INSTRUMENTED}
    import fakeweblib

    # One route calls the AI library, one does not (a health check). The AI route's request
    # carries a credential in its query string and in a header.
    assert fakeweblib.handle("/chat?token=sk-test-PLACEHOLDER",
                             headers={"authorization": "Bearer sk-test-PLACEHOLDER"}) == "pong"
    assert fakeweblib.handle("/health") == "ok"
    # A request root from an instrumentation the agent did not enable stays with the application too.
    with app_provider.get_tracer("checkout.server").start_as_current_span(
            "POST /login", kind=trace_api.SpanKind.SERVER, attributes={"http.target": "/login?token=fi-secret-PLACEHOLDER"}):
        fakeailib = sys.modules["fakeailib"]
        fakeailib.complete("ping")
    app_spans = app_exporter.get_finished_spans()
    assert sorted(s.name for s in app_spans) == [
        "GET /chat", "GET /health", "POST /login", "chat fake", "chat fake", "http send", "http send"]
    # The application's own exporter keeps the request as it was.
    (app_root,) = [s for s in app_spans if s.name == "GET /chat"]
    assert app_root.attributes["http.target"] == "/chat?token=sk-test-PLACEHOLDER"
    # Only the AI route's request root and its AI child reach Future AGI, plus the other AI call.
    fi_spans = fi_exporter.get_finished_spans()
    assert [s.name for s in fi_spans] == ["chat fake", "GET /chat", "chat fake"]
    # Exactly what Future AGI receives for that request: its path, without the query string or headers.
    assert dict(fi_spans[1].attributes) == {"http.target": "/chat", "url.full": "http://shop.example.com/chat"}
    assert fi_spans[1].kind == trace_api.SpanKind.SERVER
    assert fi_spans[0].context.trace_id == fi_spans[1].context.trace_id
    assert "PLACEHOLDER" not in _future_agi_text(fi_exporter)


def test_a_registered_future_agi_provider_gets_the_same_route(fresh_state, monkeypatch):
    from fi_instrumentation import TraceConfig, register
    from fi_instrumentation.instrumentation.config import REDACTED_VALUE

    _web_stand_ins(fresh_state, monkeypatch)
    exporter = FutureAGIExporter()
    registered = register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                          set_global_tracer_provider=True)
    assert _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER",
                         config=TraceConfig(hide_inputs=True), lazy=False) is registered
    assert {s.name: s.status for s in _manager.report()} == {
        "fakeweb": _manager.INSTRUMENTED, "fake": _manager.INSTRUMENTED}
    # The route takes the place of the application's Future AGI export processor, in front of it.
    assert len(registered._active_span_processor._span_processors) == 1
    import fakeweblib

    assert fakeweblib.handle("/chat") == "pong"
    assert fakeweblib.handle("/health") == "ok"
    _credential_bearing_span(registered.get_tracer("checkout.http"))
    with registered.get_tracer(AI_SCOPE).start_as_current_span("raw", attributes={"input.value": "a prompt"}):
        pass
    # /health, the internal web span and the application's HTTP client span are not exported.
    assert [s.name for s in exporter.get_finished_spans()] == ["chat fake", "GET /chat", "raw"]
    assert exporter.get_finished_spans()[2].attributes["input.value"] == REDACTED_VALUE
    assert "PLACEHOLDER" not in _future_agi_text(exporter)


def test_agent_shutdown_leaves_a_registered_provider_exporting(fresh_state):
    from fi_instrumentation import register

    exporter = FutureAGIExporter()
    registered = register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                          set_global_tracer_provider=True)
    _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER")
    _manager.shutdown()
    # The provider and its exporter belong to the application: they keep working.
    with registered.get_tracer(AI_SCOPE).start_as_current_span("after"):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == ["after"]


def _replaced_export_processor(exporter):
    from fi_instrumentation import TracerProvider as FiTracerProvider

    provider = FiTracerProvider(verbose=False)
    # The application replaced the provider's Future AGI processor with one of its own, around a
    # Future AGI exporter.
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace_api.set_tracer_provider(provider)
    return provider, {}


def _agent_exporter_too(exporter):
    from fi_instrumentation import register

    provider = register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                        set_global_tracer_provider=True)
    # The agent is given an exporter of its own, so the provider's export stays as the app set it up.
    return provider, {"span_exporter": InMemorySpanExporter(), "batch": False}


@pytest.mark.parametrize("setup", [_replaced_export_processor, _agent_exporter_too])
def test_web_servers_stay_off_on_a_registered_provider_without_a_route(fresh_state, monkeypatch, caplog, setup):
    _web_stand_ins(fresh_state, monkeypatch)
    exporter = FutureAGIExporter()
    provider, options = setup(exporter)
    with caplog.at_level(logging.WARNING):
        assert _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER",
                             lazy=False, **options) is provider
    report = {s.name: s for s in _manager.report()}
    assert report["fakeweb"].status == _manager.DISABLED
    assert "Future AGI" in report["fakeweb"].detail
    assert report["fake"].status == _manager.INSTRUMENTED
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "web server" in warnings[0]
    import fakeweblib

    # No request spans are made, so none can reach the application's Future AGI export unfiltered.
    assert fakeweblib.handle("/chat") == "pong"
    assert [s.name for s in exporter.get_finished_spans()] == ["chat fake"]


@pytest.mark.parametrize("routable", [True, False])
def test_auto_instrument_on_a_registered_provider_filters_it_as_init_does(fresh_state, monkeypatch, caplog, routable):
    from fi_instrumentation import register

    _web_stand_ins(fresh_state, monkeypatch)
    exporter = FutureAGIExporter()
    if routable:
        register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                 set_global_tracer_provider=True)
    else:
        _replaced_export_processor(exporter)
    # Called on its own, without init(): it traces with the global provider.
    with caplog.at_level(logging.WARNING):
        statuses = _manager.auto_instrument(lazy=False)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    import fakeweblib

    assert fakeweblib.handle("/chat") == "pong"
    assert fakeweblib.handle("/health") == "ok"
    names = [s.name for s in exporter.get_finished_spans()]
    if routable:
        assert statuses == {"fakeweb": _manager.INSTRUMENTED, "fake": _manager.INSTRUMENTED}
        assert not [w for w in warnings if "web server" in w]
        # /health and the internal web spans are not exported; the AI request's root is.
        assert names == ["chat fake", "GET /chat"]
    else:
        assert statuses == {"fakeweb": _manager.DISABLED, "fake": _manager.INSTRUMENTED}
        assert len(warnings) == 1, warnings
        assert "web server" in warnings[0]
        assert names == ["chat fake"]


def test_a_registered_provider_exporting_elsewhere_keeps_its_exporter(fresh_state, monkeypatch):
    import io

    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    from fi_instrumentation import register

    _web_stand_ins(fresh_state, monkeypatch)
    printed = io.StringIO()
    # The application sends its register() provider's spans to the console, not to Future AGI.
    registered = register(project_name="mine", span_exporter=ConsoleSpanExporter(out=printed), batch=False,
                          verbose=False, set_global_tracer_provider=True)
    (app_processor,) = registered._active_span_processor._span_processors
    with MockServer() as server:
        assert _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER",
                             base_url=server.url, lazy=False) is registered
        # That processor stays first and as it was; the agent's Future AGI route is added next to it.
        processors = registered._active_span_processor._span_processors
        assert len(processors) == 2
        assert processors[0] is app_processor
        assert {s.name: s.status for s in _manager.report()} == {
            "fakeweb": _manager.INSTRUMENTED, "fake": _manager.INSTRUMENTED}
        import fakeweblib

        assert fakeweblib.handle("/chat") == "pong"
        assert fakeweblib.handle("/health") == "ok"
        assert registered.force_flush()
        exported = decode_spans(server.collected)
    # The console still prints every span, unfiltered.
    for name in ("GET /chat", "GET /health", "http send", "chat fake"):
        assert f'"{name}"' in printed.getvalue()
    # Future AGI receives the AI span and its request root only.
    assert sorted(s["name"] for s in exported) == ["GET /chat", "chat fake"]


def test_other_processors_on_a_registered_provider_still_see_every_span(fresh_state, monkeypatch):
    from fi_instrumentation import register

    _web_stand_ins(fresh_state, monkeypatch)
    exporter, app_exporter = FutureAGIExporter(), InMemorySpanExporter()
    registered = register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                          set_global_tracer_provider=True)
    # A processor the application added for its own backend.
    registered.add_span_processor(SimpleSpanProcessor(app_exporter))
    _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER", lazy=False)
    import fakeweblib

    fakeweblib.handle("/chat?token=sk-test-PLACEHOLDER")
    fakeweblib.handle("/health")
    _credential_bearing_span(registered.get_tracer("checkout.http"))
    app_spans = app_exporter.get_finished_spans()
    assert sorted(s.name for s in app_spans) == [
        "GET /chat", "GET /health", "GET /v1/orders", "chat fake", "http send", "http send"]
    # It gets the original spans, unchanged.
    (app_root,) = [s for s in app_spans if s.name == "GET /chat"]
    assert app_root.attributes["http.target"] == "/chat?token=sk-test-PLACEHOLDER"
    assert [s.name for s in exporter.get_finished_spans()] == ["chat fake", "GET /chat"]


def test_ai_spans_still_open_when_a_registered_provider_shuts_down_are_exported(fresh_state):
    from fi_instrumentation import register

    exporter = FutureAGIExporter()
    registered = register(project_name="mine", span_exporter=exporter, batch=False, verbose=False,
                          set_global_tracer_provider=True)
    _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER")
    # A long agent span and a span of the application's own, both still open at exit.
    registered.get_tracer(AI_SCOPE).start_span("long agent")
    request = registered.get_tracer("checkout.http").start_span("GET /orders")
    registered.shutdown()
    # The provider's Future AGI export ends the open AI span and exports it, marked as leaked.
    (span,) = exporter.get_finished_spans()
    assert span.name == "long agent"
    assert span.attributes["gen_ai.span.leaked"] is True
    # The application's own span is not ended for it.
    assert request.is_recording()
    request.end()


@pytest.mark.parametrize("setup", ["package exporter", "plain exporter", "agent"])
def test_a_manual_integration_on_a_registered_provider_keeps_its_library_spans(fresh_state, monkeypatch, setup):
    from fi_instrumentation import register

    # An integration whose package converts its library's spans in an exporter wrapper: the app passes
    # that exporter to register() and then calls init(), as the integration's setup line says.
    _write_packages(fresh_state, {"wrappedlib": "", "traceai_wrapped": FAKE_WRAPPER})
    wrapped = Integration("wrapped", ("wrappedlib",), "traceai-wrapped", "traceai_wrapped", kind=_registry.MANUAL,
                          category="framework", note="pass WrappedSpanExporter to register()",
                          scopes=("wrappedlib.telemetry",))
    monkeypatch.setattr(_registry, "ALL", (wrapped,))
    exporter = FutureAGIExporter()
    if setup == "agent":
        # Started by the agent, the integration is not set up, so its raw library spans stay out.
        provider = _manager.init(span_exporter=exporter, batch=False)
    else:
        from traceai_wrapped import WrappedSpanExporter

        # Without the package's exporter, nothing converts the library's raw spans, so they stay out.
        chosen = WrappedSpanExporter(exporter) if setup == "package exporter" else exporter
        provider = register(project_name="mine", span_exporter=chosen, batch=False, verbose=False,
                            set_global_tracer_provider=True)
        _manager.init(api_key="fi-api-PLACEHOLDER", secret_key="fi-secret-PLACEHOLDER")
    assert {s.name: s.status for s in _manager.report()} == {"wrapped": _manager.STATUS_MANUAL}
    with provider.get_tracer("wrappedlib.telemetry").start_as_current_span("invoke_agent"):
        pass
    with provider.get_tracer("checkout.http").start_as_current_span("GET /orders"):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == (
        ["invoke_agent"] if setup == "package exporter" else [])


def test_the_wrapper_based_integration_names_its_library_scope():
    # Its library's own spans carry the content; its exporter wrapper converts them.
    assert _registry.find("strands").scopes == ("strands.telemetry.tracer",)


@pytest.mark.parametrize("app_provider", [False, True])
def test_spans_marked_with_an_ai_span_kind_reach_future_agi(fresh_state, app_provider):
    from fi_instrumentation import FITracer, TraceConfig
    from fi_instrumentation.fi_types import FiSpanKindValues
    from fi_instrumentation.instrumentation.config import REDACTED_VALUE

    fi_exporter = InMemorySpanExporter()
    config = TraceConfig(hide_inputs=True)
    if app_provider:
        provider, _ = _application_provider()
        _manager.init(span_exporter=fi_exporter, batch=False, config=config)
    else:
        provider = _manager.init(span_exporter=fi_exporter, batch=False, config=config)
    # The manual pattern from the core docs, under the application's own module scope.
    tracer = FITracer(provider.get_tracer(__name__))
    import fakeailib

    with tracer.start_as_current_span("support agent", fi_span_kind=FiSpanKindValues.AGENT) as agent:
        agent.set_attribute("input.value", "a prompt")
        fakeailib.complete("ping")
    # A span from another library that carries the Future AGI span kind attribute.
    with provider.get_tracer("checkout.workflow").start_as_current_span("plan", attributes={"fi.span.kind": "CHAIN"}):
        pass
    with provider.get_tracer("checkout.http").start_as_current_span("GET /orders"):
        pass
    spans = fi_exporter.get_finished_spans()
    assert [s.name for s in spans] == ["chat fake", "support agent", "plan"]
    # The traceAI child arrives with its parent, and the parent is masked like every other span.
    assert spans[0].parent.span_id == spans[1].context.span_id
    assert spans[1].attributes["input.value"] == REDACTED_VALUE


def test_a_span_marked_with_an_ai_span_kind_lets_its_request_root_through(fresh_state, monkeypatch):
    from fi_instrumentation import FITracer
    from fi_instrumentation.fi_types import FiSpanKindValues

    _web_stand_ins(fresh_state, monkeypatch)
    app_provider, _ = _application_provider()
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False)
    web = app_provider.get_tracer(WEB_SCOPE)
    # The only AI span in the request is the application's own, under its own module scope.
    agent = FITracer(app_provider.get_tracer("checkout.agent"))
    with web.start_as_current_span("POST /support", kind=trace_api.SpanKind.SERVER):
        with agent.start_as_current_span("support agent", fi_span_kind=FiSpanKindValues.AGENT):
            pass
    with web.start_as_current_span("GET /health", kind=trace_api.SpanKind.SERVER):
        with app_provider.get_tracer("checkout.agent").start_as_current_span("check"):
            pass
    assert [s.name for s in fi_exporter.get_finished_spans()] == ["support agent", "POST /support"]


def test_future_agi_copy_masks_events_links_and_error_details(fresh_state, monkeypatch):
    from fi_instrumentation import TraceConfig
    from fi_instrumentation.instrumentation.config import REDACTED_VALUE

    _web_stand_ins(fresh_state, monkeypatch)
    app_provider, app_exporter = _application_provider()
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False,
                  config=TraceConfig(hide_inputs=True, hide_error_details=True))
    web, ai = app_provider.get_tracer(WEB_SCOPE), app_provider.get_tracer(AI_SCOPE)
    # A request that makes an AI call and then fails.
    with pytest.raises(ValueError):
        with web.start_as_current_span("POST /chat", kind=trace_api.SpanKind.SERVER) as root:
            root.add_event("request received", {"input.value": "a prompt"})
            link = trace_api.Link(root.get_span_context(), {"input.value": "a linked prompt"})
            with ai.start_as_current_span("raw call", links=[link]):
                pass
            raise ValueError("the model refused: a prompt")
    # The application's exporter keeps the error details and the content.
    app_root = app_exporter.get_finished_spans()[-1]
    assert [e.name for e in app_root.events] == ["request received", "exception"]
    assert "a prompt" in app_root.status.description
    call, root = fi_exporter.get_finished_spans()
    assert (call.name, root.name) == ("raw call", "POST /chat")
    assert call.links[0].attributes["input.value"] == REDACTED_VALUE
    # No exception event and no error description reach Future AGI; the status stays ERROR.
    assert [(e.name, dict(e.attributes)) for e in root.events] == [("request received", {"input.value": REDACTED_VALUE})]
    assert root.status.status_code == trace_api.StatusCode.ERROR
    assert root.status.description is None
    assert "a prompt" not in _future_agi_text(fi_exporter)
    assert "a linked prompt" not in _future_agi_text(fi_exporter)


def test_a_kept_error_description_is_redacted_for_future_agi(fresh_state):
    from fi_instrumentation import TraceConfig

    app_provider, app_exporter = _application_provider()
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False,
                  config=TraceConfig(pii_redaction=True, hide_error_details=False))
    with pytest.raises(ValueError):
        with app_provider.get_tracer(AI_SCOPE).start_as_current_span("call"):
            raise ValueError("refused for jane.doe@example.com")
    assert "jane.doe@example.com" in app_exporter.get_finished_spans()[0].status.description
    (span,) = fi_exporter.get_finished_spans()
    # Error details are kept, with the same PII redaction as attribute values.
    assert span.status.status_code == trace_api.StatusCode.ERROR
    assert "refused for" in span.status.description
    assert "<EMAIL_ADDRESS>" in span.status.description
    assert "jane.doe@example.com" not in span.status.description


def test_request_roots_of_a_web_server_the_app_instrumented_reach_future_agi(fresh_state, monkeypatch):
    _web_stand_ins(fresh_state, monkeypatch)
    app_provider, _ = _application_provider()
    import fakewebinstr

    # The application instrumented its web server itself, before the agent started.
    fakewebinstr.FakeWebInstrumentor().instrument(tracer_provider=app_provider)
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False)
    report = {s.name: s for s in _manager.report()}
    assert (report["fakeweb"].status, report["fakeweb"].detail) == (_manager.INSTRUMENTED, "already instrumented")
    import fakeweblib

    fakeweblib.handle("/chat")
    fakeweblib.handle("/health")
    # The request-root rule is the same: only the request that made an AI call.
    assert [s.name for s in fi_exporter.get_finished_spans()] == ["chat fake", "GET /chat"]


def test_a_request_root_may_have_a_remote_parent(fresh_state, monkeypatch):
    _web_stand_ins(fresh_state, monkeypatch)
    app_provider, _ = _application_provider()
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False)
    web, ai = app_provider.get_tracer(WEB_SCOPE), app_provider.get_tracer(AI_SCOPE)
    # The caller sent its trace context, so the request root's parent is a remote span.
    remote = trace_api.SpanContext(trace_id=0x5E5E, span_id=0x1E1E, is_remote=True,
                                   trace_flags=trace_api.TraceFlags(trace_api.TraceFlags.SAMPLED))
    with web.start_as_current_span("GET /chat", context=trace_api.set_span_in_context(trace_api.NonRecordingSpan(remote)),
                                   kind=trace_api.SpanKind.SERVER):
        # A server span under a local parent is not a request root.
        with web.start_as_current_span("GET /nested", kind=trace_api.SpanKind.SERVER):
            with ai.start_as_current_span("call"):
                pass
    assert [s.name for s in fi_exporter.get_finished_spans()] == ["call", "GET /chat"]


def test_ai_traces_waiting_for_their_request_root_are_bounded(fresh_state, monkeypatch):
    _web_stand_ins(fresh_state, monkeypatch)
    monkeypatch.setattr(_manager, "_AI_TRACES_LIMIT", 1)
    app_provider, _ = _application_provider()
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False)
    web, ai = app_provider.get_tracer(WEB_SCOPE), app_provider.get_tracer(AI_SCOPE)
    first = web.start_span("GET /first", kind=trace_api.SpanKind.SERVER)
    second = web.start_span("GET /second", kind=trace_api.SpanKind.SERVER)
    ai.start_span("first call", context=trace_api.set_span_in_context(first)).end()
    # The second trace's AI span pushes the first trace out of the bounded set.
    ai.start_span("second call", context=trace_api.set_span_in_context(second)).end()
    first.end()
    second.end()
    assert [s.name for s in fi_exporter.get_finished_spans()] == ["first call", "second call", "GET /second"]


def test_a_span_that_cannot_be_prepared_is_dropped_with_one_warning(fresh_state, caplog):
    app_provider, app_exporter = _application_provider()
    fi_exporter = InMemorySpanExporter()
    _manager.init(span_exporter=fi_exporter, batch=False)

    def broken(attributes):
        raise RuntimeError("mapping failed")

    _manager._state.overlays[app_provider].mappers.append(broken)
    with caplog.at_level(logging.WARNING):
        for name in ("one", "two"):
            with app_provider.get_tracer(AI_SCOPE).start_as_current_span(name):
                pass
    # span.end() never fails in the application, and its own exporter still gets every span.
    assert [s.name for s in app_exporter.get_finished_spans()] == ["one", "two"]
    assert not fi_exporter.get_finished_spans()
    assert caplog.text.count("could not be prepared for Future AGI") == 1


@pytest.mark.parametrize("batch", [False, True])
def test_the_route_shows_its_exporter_to_duplicate_checks(fresh_state, batch):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=batch)
    # How integrations look for an exporter that already sends to the same place.
    found = []
    for processor in provider._active_span_processor._span_processors:
        candidate = getattr(processor, "span_exporter", None)
        if candidate is None:
            candidate = getattr(getattr(processor, "_batch_processor", None), "_exporter", None)
        found.append(candidate)
    assert found == [exporter]


def test_voice_route_rechecks_recordings_with_the_agent_config(fresh_state, monkeypatch):
    from fi_instrumentation import TraceConfig

    _write_packages(fresh_state, FAKE_LIVEKIT)
    voice = Integration("livekit", ("fakeailib",), "traceAI-livekit", "traceai_livekit", "livekit",
                        kind=_registry.SETUP, category="voice")
    monkeypatch.setattr(_registry, "ALL", (voice,))
    app_exporter, fi_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    trace_api.set_tracer_provider(app_provider)
    config = TraceConfig(hide_inputs=True)
    _manager.init(span_exporter=fi_exporter, batch=False, lazy=False, config=config)
    assert {s.name: s.status for s in _manager.report()}["livekit"] == _manager.INSTRUMENTED
    with app_provider.get_tracer("traceai_livekit.recording").start_as_current_span(
            "livekit.recording.snapshot", attributes={"lk.recording.operation": "snapshot"}):
        pass
    import traceai_livekit

    # The package's export step ran on the Future AGI copy, with the agent's TraceConfig among its policies.
    assert any(config in policies for policies in traceai_livekit.SEEN)
    (span,) = fi_exporter.get_finished_spans()
    assert span.attributes["mapped"] is True
    assert "mapped" not in app_exporter.get_finished_spans()[0].attributes


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


def test_shutdown_flushes_and_closes_processors_added_to_the_agent_provider(fresh_state):
    calls = []

    class SecondBackend(SpanProcessor):
        def on_end(self, span):
            calls.append(span.name)

        def force_flush(self, timeout_millis=30000):
            calls.append("flush")
            return True

        def shutdown(self):
            calls.append("shutdown")

    provider = _manager.init(span_exporter=InMemorySpanExporter(), batch=False)
    provider.add_span_processor(SecondBackend())
    with provider.get_tracer("app").start_as_current_span("one"):
        pass
    _manager.shutdown()
    assert calls == ["one", "flush", "shutdown"]


def test_libraries_and_packages_that_arrive_after_startup_are_enabled(fresh_state, monkeypatch, tmp_path_factory):
    late = Integration("late", ("latelib",), "traceAI-late", "traceai_latelib", "FakeInstrumentor")
    partial = Integration("partial", ("partiallib",), "traceAI-partial", "traceai_partiallib", "FakeInstrumentor")
    monkeypatch.setattr(_registry, "ALL", (late, partial))
    # At startup one library is missing entirely, the other has no traceAI package yet.
    _write_packages(fresh_state, {"partiallib": FAKE_LIBRARY})
    statuses = _manager.auto_instrument(tracer_provider=TracerProvider())
    assert statuses == {"late": _manager.NOT_INSTALLED, "partial": _manager.MISSING_INSTRUMENTOR}
    # Their path entries arrive later, as from a later-sorting .pth file or a sys.path change.
    later = tmp_path_factory.mktemp("later")
    _write_packages(later, {
        "latelib": FAKE_LIBRARY,
        "traceai_latelib": _instrumentor("latelib", fail=False),
        "traceai_partiallib": _instrumentor("partiallib", fail=False),
    })
    monkeypatch.syspath_prepend(str(later))
    import latelib
    import partiallib
    import traceai_latelib
    import traceai_partiallib

    assert {s.name: s.status for s in _manager.report()} == {"late": _manager.INSTRUMENTED,
                                                            "partial": _manager.INSTRUMENTED}
    assert latelib.complete("ping") == partiallib.complete("ping") == "pong"
    assert traceai_latelib.CALLS and traceai_partiallib.CALLS


def test_auto_instrument_cannot_rebind_enabled_instrumentor(fresh_state):
    import traceai_fakeailib

    first, second = TracerProvider(), TracerProvider()
    _manager.auto_instrument(tracer_provider=first, lazy=False)
    _manager.auto_instrument(tracer_provider=second, lazy=False)
    assert traceai_fakeailib.CALLS[-1]["tracer_provider"] is first
