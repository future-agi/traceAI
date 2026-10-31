"""Local synthetic tests only; never disable the OTel SDK's positive controls."""

import os

import pytest

# Must precede CrewAI imports, which can initialize a telemetry singleton.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_DISABLE_TRACKING"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "false"


@pytest.fixture(autouse=True)
def native_telemetry_off(request):
    from crewai.telemetry import Telemetry
    from traceai_crewai._telemetry import _NativeTelemetryGuard

    if request.node.name.startswith("test_guard_"):
        yield
        return
    # Use the packaged adapter for baseline native calls too. Floor CrewAI
    # ignores the environment flags; maintained versions use their own checks.
    guard = _NativeTelemetryGuard()
    guard.enable()
    try:
        telemetry = Telemetry()
        assert not telemetry.ready
        assert not getattr(telemetry, "provider", None)
        yield
    finally:
        guard.disable()
