"""Provider discovery and optional SDK processor installation (no network)."""

import atexit
import builtins
import gc
import sys
import types
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from unittest.mock import Mock

import pytest
from fi_instrumentation import otel
from fi_instrumentation.fi_types import ProjectType
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(otel, "_registered_tracer_providers", weakref.WeakSet())
    monkeypatch.setattr(otel, "_provider_registration_order", weakref.WeakKeyDictionary())
    monkeypatch.setattr(otel, "_skills_processor_failure_logged", False)
    created = []

    def make(**kwargs):
        exporter = InMemorySpanExporter()
        provider = otel.register(**{
            "project_name": "runtime-tests", "project_type": ProjectType.OBSERVE,
            "verbose": False, "batch": False, "span_exporter": exporter, **kwargs,
        })
        created.append(provider)
        return provider, exporter

    yield make
    for provider in created:
        provider.shutdown()
        # Both upstream construction and traceAI register() use exit hooks.
        atexit.unregister(provider.shutdown)


def _sdk_processor(monkeypatch, factory, mode="on"):
    """Supply only the optional bridge module, without needing the SDK installed."""
    fi = types.ModuleType("fi")
    fi.__path__ = []
    telemetry = types.ModuleType("fi.telemetry")
    telemetry.__path__ = []
    processor = types.ModuleType("fi.telemetry.processor")
    processor.FutureAGISkillsSpanProcessor = factory
    processor.INSTALL_LOCK = RLock()
    bridge = types.ModuleType("fi.telemetry._otel")
    bridge.mode = lambda: mode
    telemetry._otel = bridge
    monkeypatch.setitem(sys.modules, "fi.telemetry._otel", bridge)
    fi.telemetry = telemetry
    telemetry.processor = processor
    monkeypatch.setitem(sys.modules, "fi", fi)
    monkeypatch.setitem(sys.modules, "fi.telemetry", telemetry)
    monkeypatch.setitem(sys.modules, "fi.telemetry.processor", processor)


def test_non_global_providers_are_returned_in_registration_order(providers):
    global_provider = trace.get_tracer_provider()
    first, _ = providers()
    second, _ = providers()
    assert otel.get_registered_tracer_providers() == (first, second)
    assert trace.get_tracer_provider() is global_provider
    assert "service.name" not in second.resource.attributes
    assert not hasattr(second, "_futureagi_skills_processor")


def test_registry_is_an_immutable_snapshot(providers):
    first, _ = providers()
    snapshot = otel.get_registered_tracer_providers()
    second, _ = providers()
    assert snapshot == (first,)
    assert otel.get_registered_tracer_providers() == (first, second)


def test_global_registration_remains_explicit(monkeypatch, providers):
    set_global = Mock()
    monkeypatch.setattr(trace, "set_tracer_provider", set_global)
    provider, _ = providers(set_global_tracer_provider=True)
    set_global.assert_called_once_with(provider)
    assert otel.get_registered_tracer_providers() == (provider,)


def test_registry_does_not_keep_providers_alive(monkeypatch):
    monkeypatch.setattr(otel, "_registered_tracer_providers", weakref.WeakSet())
    monkeypatch.setattr(otel, "_provider_registration_order", weakref.WeakKeyDictionary())
    provider = otel.register(
        project_name="weak-provider", project_type=ProjectType.OBSERVE,
        verbose=False, batch=False, span_exporter=InMemorySpanExporter(),
    )
    reference = weakref.ref(provider)
    assert otel.get_registered_tracer_providers() == (provider,)
    provider.shutdown()
    # The registry must not introduce retention beyond OTel's existing exit hooks.
    atexit.unregister(provider.shutdown)
    del provider
    gc.collect()
    assert reference() is None
    assert otel.get_registered_tracer_providers() == ()
    assert not otel._provider_registration_order


def test_direct_provider_is_not_registered(providers):
    direct = otel.TracerProvider(verbose=False, shutdown_on_exit=False)
    try:
        assert direct not in otel.get_registered_tracer_providers()
    finally:
        direct.shutdown()


def test_default_never_imports_the_optional_sdk(monkeypatch, providers):
    real_import = builtins.__import__
    attempted = []

    def guard(name, *args, **kwargs):
        if name == "fi" or name.startswith("fi."):
            attempted.append(name)
            raise AssertionError("default registration imported the optional SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    providers()
    assert attempted == []


@pytest.mark.parametrize("batch", [False, True])
def test_opt_in_keeps_exporter_and_installs_only_once(monkeypatch, providers, batch):
    class StampingProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            span.set_attribute("gen_ai.agent.name", "test-agent")

    factory = Mock(side_effect=StampingProcessor)
    _sdk_processor(monkeypatch, factory)
    provider, exporter = providers(skills_processor=True, batch=batch)
    processor = provider._futureagi_skills_processor
    assert isinstance(processor, StampingProcessor)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(otel._install_skills_processor, [provider] * 8))
    factory.assert_called_once_with()
    assert provider._active_span_processor._span_processors[-1] is processor
    assert len(provider._active_span_processor._span_processors) == 2
    with provider.get_tracer(__name__).start_as_current_span("after-install"):
        pass
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.agent.name"] == "test-agent"


def test_existing_sdk_marker_prevents_duplicate_install(monkeypatch, providers):
    provider, _ = providers()
    existing = SpanProcessor()
    provider.add_span_processor(existing)
    provider._futureagi_skills_processor = existing
    factory = Mock(side_effect=AssertionError("duplicate installation"))
    _sdk_processor(monkeypatch, factory)
    otel._install_skills_processor(provider)
    factory.assert_not_called()
    assert len(provider._active_span_processor._span_processors) == 2


@pytest.mark.parametrize("failure", [ImportError, RuntimeError])
def test_optional_bridge_failure_does_not_break_export(monkeypatch, providers, caplog, failure):
    _sdk_processor(monkeypatch, Mock(side_effect=failure("do not log app data")))
    caplog.set_level("DEBUG", logger=otel.__name__)
    first, exporter = providers(skills_processor=True)
    second, _ = providers(skills_processor=True)
    first.get_tracer(__name__).start_span("still-exported").end()
    assert [span.name for span in exporter.get_finished_spans()] == ["still-exported"]
    assert not hasattr(first, "_futureagi_skills_processor")
    assert otel.get_registered_tracer_providers() == (first, second)
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("Optional Future AGI skills processor unavailable") == 1
    assert "do not log app data" not in caplog.text


@pytest.mark.parametrize("mode", ["off", "load_only"])
def test_opt_in_respects_sdk_effective_mode(monkeypatch, providers, mode):
    factory = Mock(side_effect=AssertionError("processor must not be created"))
    _sdk_processor(monkeypatch, factory, mode=mode)
    # The SDK's effective mode, not this environment value, is authoritative.
    monkeypatch.setenv("FI_SKILLS_TRACING", "on")
    provider, exporter = providers(skills_processor=True)
    factory.assert_not_called()
    assert not hasattr(provider, "_futureagi_skills_processor")
    assert len(provider._active_span_processor._span_processors) == 1
    provider.get_tracer(__name__).start_span("ordinary-tracing").end()
    assert len(exporter.get_finished_spans()) == 1


def test_missing_sdk_is_optional(monkeypatch, providers):
    monkeypatch.setitem(sys.modules, "fi.telemetry.processor", None)
    provider, exporter = providers(skills_processor=True)
    provider.get_tracer(__name__).start_span("no-sdk").end()
    assert [span.name for span in exporter.get_finished_spans()] == ["no-sdk"]
    assert not hasattr(provider, "_futureagi_skills_processor")


def test_real_sdk_processor_consumes_carrier_when_available(monkeypatch, providers):
    pytest.importorskip("fi.telemetry.processor")
    from fi.telemetry import _otel as sdk_otel
    from fi_instrumentation.instrumentation.runtime_attributes import (
        using_runtime_attributes,
    )

    monkeypatch.setattr(sdk_otel, "_configured_mode", "on")
    provider, exporter = providers(skills_processor=True)
    assert getattr(provider, "_futureagi_skills_processor", None)
    # Both real installers race on the same provider; only one processor remains.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sdk_otel.install, provider),
                   pool.submit(otel._install_skills_processor, provider)]
        for future in futures:
            future.result()
    assert len(provider._active_span_processor._span_processors) == 2
    with using_runtime_attributes({"gen_ai.agent.name": "carrier-agent"}):
        with provider.get_tracer(__name__).start_as_current_span("carrier-root"):
            pass
        with provider.get_tracer(__name__).start_as_current_span(
            "explicit", attributes={"gen_ai.agent.name": "framework-agent"},
        ):
            pass
    assert [span.attributes["gen_ai.agent.name"] for span in exporter.get_finished_spans()] == [
        "carrier-agent", "framework-agent",
    ]


def test_shutdown_unregisters_while_provider_is_still_referenced(providers):
    first, _ = providers()
    latest, _ = providers()
    latest.shutdown()
    assert otel.get_registered_tracer_providers() == (first,)
    first.shutdown()
    assert otel.get_registered_tracer_providers() == ()


def test_real_sdk_load_falls_back_to_live_provider(providers):
    pytest.importorskip("fi.telemetry.processor")
    from fi.context.materialize import LockedLink
    from fi.telemetry import configure, declare_skill

    first, live = providers()
    closed, dead = providers()
    closed.shutdown()  # Keep a strong reference: selection must not wait for GC.
    try:
        configure(skills_tracing="on")
        link = LockedLink(mount="skills/refund/", skill_id="11111111-1111-4111-8111-111111111111",
                          requested={"version": 1}, version=1, version_id="22222222-2222-4222-8222-222222222222",
                          digest="sha256:" + "a" * 64)
        with first.get_tracer(__name__).start_as_current_span("request") as parent:
            declare_skill(link)
        loads = [span for span in live.get_finished_spans() if span.name == "skill.load"]
        assert [span.attributes["fi.skill.version_id"] for span in loads] == [link.version_id]
        assert loads[0].parent == parent.get_span_context()
        assert dead.get_finished_spans() == ()
    finally:
        configure()


def test_shutdown_between_global_exposure_and_register_return(monkeypatch, providers):
    from threading import Event

    # The OTel API normally allows one global registration per process. Isolate
    # that slot, but use its real setter/getter, provider, processors and shutdown.
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", type(trace._TRACER_PROVIDER_SET_ONCE)())
    exposed, resume = Event(), Event()
    publish = trace.set_tracer_provider

    def pause_after_publish(provider):
        publish(provider)
        exposed.set()
        assert resume.wait(10)

    monkeypatch.setattr(trace, "set_tracer_provider", pause_after_publish)
    first, live = providers()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(providers, set_global_tracer_provider=True)
        try:
            assert exposed.wait(10)
            second = trace.get_tracer_provider()
            assert second is not first
            second.shutdown()
        finally:
            resume.set()
        assert pending.result(timeout=10)[0] is second
    assert trace.get_tracer_provider() is second  # OTel retains even the closed global.
    assert otel.get_registered_tracer_providers() == (first,)
    first.get_tracer(__name__).start_span("still-exported").end()
    assert [span.name for span in live.get_finished_spans()] == ["still-exported"]
