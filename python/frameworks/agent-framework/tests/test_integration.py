"""Lifecycle tests for enable_fi_attribute_mapping()."""

import os

import pytest
from opentelemetry import trace as trace_api

# Dummy creds so register() doesn't complain.
os.environ.setdefault("FI_API_KEY", "test-key")
os.environ.setdefault("FI_SECRET_KEY", "test-secret")

from traceai_agent_framework import (  # noqa: E402
    AgentFrameworkSpanProcessor,
    enable_fi_attribute_mapping,
)


def _processors_on(provider):
    """Return the list of installed span processors on the given provider."""
    active = getattr(provider, "_active_span_processor", None)
    if active is None:
        return []
    return list(getattr(active, "_span_processors", ()))


def _count_our_processors(provider):
    return sum(
        1 for p in _processors_on(provider)
        if isinstance(p, AgentFrameworkSpanProcessor)
    )


@pytest.fixture
def fresh_global_provider(monkeypatch):
    """Reset the global tracer provider between tests so state doesn't leak."""
    original = trace_api._TRACER_PROVIDER
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(
        trace_api,
        "_TRACER_PROVIDER_SET_ONCE",
        type(trace_api._TRACER_PROVIDER_SET_ONCE)(),
    )
    yield
    trace_api._TRACER_PROVIDER = original


def _register(set_global=True):
    """Helper to call register()."""
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    return register(
        project_type=ProjectType.OBSERVE,
        project_name="test-project",
        set_global_tracer_provider=set_global,
        verbose=False,
    )


# ---------------------------------------------------------------------------
# Global-provider path
# ---------------------------------------------------------------------------


def test_returns_false_when_no_provider_is_installed(fresh_global_provider):
    """No register() called → no real provider on the global → cannot add a processor."""
    assert enable_fi_attribute_mapping() is False


def test_installs_processor_on_global_provider_after_register(fresh_global_provider):
    provider = _register(set_global=True)
    assert _count_our_processors(provider) == 0

    assert enable_fi_attribute_mapping() is True
    assert _count_our_processors(provider) == 1


def test_idempotent_install_on_same_provider(fresh_global_provider):
    """Calling enable twice should not install the processor twice."""
    provider = _register(set_global=True)
    assert enable_fi_attribute_mapping() is True
    assert enable_fi_attribute_mapping() is False
    assert _count_our_processors(provider) == 1


# ---------------------------------------------------------------------------
# Explicit-provider path
# ---------------------------------------------------------------------------


def test_install_on_explicit_provider_does_not_require_global(fresh_global_provider):
    """User who keeps set_global_tracer_provider=False can still pass the provider."""
    provider = _register(set_global=False)
    assert enable_fi_attribute_mapping(tracer_provider=provider) is True
    assert _count_our_processors(provider) == 1


def test_idempotent_install_on_explicit_provider(fresh_global_provider):
    provider = _register(set_global=False)
    assert enable_fi_attribute_mapping(tracer_provider=provider) is True
    assert enable_fi_attribute_mapping(tracer_provider=provider) is False
    assert _count_our_processors(provider) == 1


# ---------------------------------------------------------------------------
# Native-instrumentation handling
# ---------------------------------------------------------------------------


def test_enable_turns_on_native_instrumentation(fresh_global_provider):
    """If the framework's instrumentation flag is off, our helper turns it on."""
    from agent_framework.observability import OBSERVABILITY_SETTINGS

    _register(set_global=True)
    enable_fi_attribute_mapping()
    assert OBSERVABILITY_SETTINGS.enable_instrumentation is True


def test_user_explicit_disable_is_respected(fresh_global_provider):
    """If user explicitly disabled the framework's instrumentation, we must not re-enable it."""
    from agent_framework.observability import (
        OBSERVABILITY_SETTINGS,
        disable_instrumentation,
        enable_instrumentation as _af_enable,
    )

    disable_instrumentation()
    assert OBSERVABILITY_SETTINGS.enable_instrumentation is False

    _register(set_global=True)
    enable_fi_attribute_mapping()

    # Disable wins because our helper does not pass force=True.
    assert OBSERVABILITY_SETTINGS.enable_instrumentation is False

    # Clean up so the explicit-disable doesn't bleed into other tests.
    _af_enable(force=True)

