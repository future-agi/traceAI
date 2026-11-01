"""Configuration/ownership contract; real framework coverage is in test_real_agno."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from openinference.instrumentation import TraceConfig
from openinference.instrumentation.agno import AgnoInstrumentor
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from traceai_agno import (
    AgnoInstrumentorWrapper,
    configure_agno_tracing,
    setup_traceai_exporter,
)


@pytest.fixture
def state():
    AgnoInstrumentorWrapper._instance = None
    delegate = AgnoInstrumentor()
    if delegate.is_instrumented_by_opentelemetry:
        delegate.uninstrument()
    provider = TracerProvider()
    yield provider
    AgnoInstrumentorWrapper().uninstrument()
    if delegate.is_instrumented_by_opentelemetry:
        delegate.uninstrument()
    provider.shutdown()
    AgnoInstrumentorWrapper._instance = None


def test_initial_singleton_state(state):
    wrapper = AgnoInstrumentorWrapper()
    assert AgnoInstrumentorWrapper() is wrapper
    assert not wrapper.is_instrumented
    assert wrapper.tracer_provider is None
    wrapper.uninstrument()


def test_explicit_provider_is_forwarded_and_not_closed(state):
    config = TraceConfig(hide_inputs=True)
    wrapper = configure_agno_tracing(tracer_provider=state, config=config)
    assert wrapper.tracer_provider is state
    assert wrapper.is_instrumented
    assert configure_agno_tracing(tracer_provider=state, config=config) is wrapper
    assert configure_agno_tracing() is wrapper
    assert wrapper.instrument() is wrapper
    with patch.object(state, "shutdown") as shutdown:
        wrapper.uninstrument()
        shutdown.assert_not_called()
    assert not wrapper.is_instrumented
    assert wrapper.tracer_provider is None
    assert configure_agno_tracing(tracer_provider=state).is_instrumented


def test_concurrent_setup_keeps_one_delegate(state):
    from concurrent.futures import ThreadPoolExecutor

    delegate = AgnoInstrumentor()
    with patch.object(delegate, "instrument", wraps=delegate.instrument) as activate:
        with ThreadPoolExecutor(max_workers=4) as pool:
            wrappers = list(
                pool.map(
                    lambda _: configure_agno_tracing(tracer_provider=state), range(8)
                )
            )
        activate.assert_called_once()
    assert all(wrapper is wrappers[0] for wrapper in wrappers)
    assert wrappers[0].is_instrumented


def test_uses_existing_global_without_replacing_it(state, monkeypatch):
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", state)
    assert configure_agno_tracing().tracer_provider is state


@pytest.mark.parametrize(
    "provider", [trace.ProxyTracerProvider(), trace.NoOpTracerProvider()]
)
def test_unconfigured_global_fails(state, monkeypatch, provider):
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    with pytest.raises(ValueError, match="configured tracer_provider"):
        configure_agno_tracing()
    assert not AgnoInstrumentorWrapper().is_instrumented


def test_reconfiguration_is_not_silently_ignored(state):
    configure_agno_tracing(tracer_provider=state)
    with pytest.raises(ValueError, match="already configured"):
        configure_agno_tracing(otlp_endpoint="http://localhost:4318/v1/traces")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"otlp_endpoint": "http://localhost:4318/v1/traces"},
        {"otlp_headers": {}},
        {"project_name": "test"},
    ],
)
def test_rejects_conflicting_provider_export_settings(state, kwargs):
    with pytest.raises(ValueError, match="not both"):
        configure_agno_tracing(tracer_provider=state, **kwargs)


@pytest.mark.parametrize("kwargs", [{"otlp_headers": {}}, {"project_name": "test"}])
def test_export_settings_need_endpoint(state, kwargs):
    with pytest.raises(ValueError, match="require otlp_endpoint"):
        configure_agno_tracing(**kwargs)


def test_external_instrumentation_is_not_taken_over(state):
    delegate = AgnoInstrumentor()
    delegate.instrument(tracer_provider=state)
    with pytest.raises(RuntimeError, match="already instrumented outside"):
        configure_agno_tracing(tracer_provider=state)
    AgnoInstrumentorWrapper().uninstrument()
    assert delegate.is_instrumented_by_opentelemetry


def test_external_teardown_requires_cleanup_before_reconfiguration(state):
    wrapper = configure_agno_tracing(tracer_provider=state)
    AgnoInstrumentor().uninstrument()
    assert not wrapper.is_instrumented
    with pytest.raises(RuntimeError, match="removed externally"):
        configure_agno_tracing(tracer_provider=state)
    wrapper.uninstrument()
    assert configure_agno_tracing(tracer_provider=state).is_instrumented


def test_dependency_conflict_raises_and_does_not_activate(state):
    from opentelemetry.instrumentation.dependencies import (
        DependencyConflict,
        DependencyConflictError,
    )

    with patch.object(
        AgnoInstrumentor,
        "_check_dependency_conflicts",
        return_value=DependencyConflict("agno>=2.5.0", "2.0.0"),
    ):
        with pytest.raises(DependencyConflictError):
            configure_agno_tracing(tracer_provider=state)
    assert not AgnoInstrumentorWrapper().is_instrumented


def test_missing_instrumentor_can_be_retried(state, monkeypatch):
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "openinference.instrumentation.agno", None)
        with pytest.raises(ImportError, match="traceai-agno"):
            configure_agno_tracing(tracer_provider=state)
    assert configure_agno_tracing(tracer_provider=state).is_instrumented


def test_failed_activation_closes_only_owned_provider(state):
    with (
        patch("traceai_agno._instrumentor.setup_traceai_exporter", return_value=state),
        patch.object(
            AgnoInstrumentor,
            "instrument",
            side_effect=RuntimeError("activation failed"),
        ),
        patch.object(state, "shutdown") as shutdown,
    ):
        with pytest.raises(RuntimeError, match="activation failed"):
            configure_agno_tracing(otlp_endpoint="http://localhost:4318/v1/traces")
        shutdown.assert_called_once()
    assert not AgnoInstrumentorWrapper().is_instrumented
    assert AgnoInstrumentorWrapper().tracer_provider is None


def test_silent_upstream_nonactivation_is_rejected(state):
    with patch.object(AgnoInstrumentor, "instrument"):
        with pytest.raises(RuntimeError, match="did not activate"):
            configure_agno_tracing(tracer_provider=state)
    assert not AgnoInstrumentorWrapper().is_instrumented


def test_uninstrument_failure_remains_visible_and_retryable(state):
    wrapper = configure_agno_tracing(tracer_provider=state)
    with patch.object(
        AgnoInstrumentor, "uninstrument", side_effect=RuntimeError("teardown failed")
    ):
        with pytest.raises(RuntimeError, match="teardown failed"):
            wrapper.uninstrument()
    assert wrapper.is_instrumented
    wrapper.uninstrument()
    assert not wrapper.is_instrumented


@pytest.mark.parametrize("batch", [True, False])
def test_exporter_resource_headers_and_global_compatibility(batch, monkeypatch):
    headers = {"X-Api-Key": "synthetic-api", "X-Secret-Key": "synthetic-secret"}
    exporter = MagicMock()
    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            return_value=exporter,
        ) as factory,
        patch.object(trace, "set_tracer_provider") as set_global,
    ):
        provider = setup_traceai_exporter(
            "http://localhost:4318/v1/traces",
            headers,
            batch,
            "service",
            project_name="project",
        )
        try:
            factory.assert_called_once_with(
                endpoint="http://localhost:4318/v1/traces", headers=headers
            )
            assert factory.call_args.kwargs["headers"] is not headers
            assert provider.resource.attributes["service.name"] == "service"
            assert provider.resource.attributes["project_name"] == "project"
            assert provider.resource.attributes["project_type"] == "observe"
            set_global.assert_called_once_with(provider)
        finally:
            provider.shutdown()
        exporter.shutdown.assert_called_once()


def test_endpoint_setup_never_changes_global_environment_or_headers(state):
    before = dict(os.environ)
    headers = {"X-Api-Key": "synthetic-api"}
    with patch.object(trace, "set_tracer_provider") as set_global:
        wrapper = configure_agno_tracing(
            otlp_endpoint="http://localhost:4318/v1/traces",
            otlp_headers=headers,
            project_name="project",
        )
        provider = wrapper.tracer_provider
        with patch.object(provider, "shutdown", wraps=provider.shutdown) as shutdown:
            wrapper.uninstrument()
            shutdown.assert_called_once()
        set_global.assert_not_called()
    assert headers == {"X-Api-Key": "synthetic-api"}
    assert dict(os.environ) == before


def test_exporter_default_project_and_private_provider():
    with patch.object(trace, "set_tracer_provider") as set_global:
        provider = setup_traceai_exporter(
            "http://localhost:4318/v1/traces", set_global=False
        )
        try:
            assert provider.resource.attributes["project_name"] == "agno-agent"
            set_global.assert_not_called()
        finally:
            provider.shutdown()


def test_empty_endpoint_rejected():
    with pytest.raises(ValueError, match="complete OTLP"):
        setup_traceai_exporter("")
