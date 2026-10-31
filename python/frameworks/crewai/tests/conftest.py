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


@pytest.fixture
def service_credentials():
    return {
        name: f"FAKE_SERVICE_CREDENTIAL_{index}_7f9b2a"
        for index, name in enumerate(
            (
                "X-Amz-Security-Token",
                "X-Vault-Token",
                "X-Consul-Token",
                "X-Subject-Token",
                "X-Api-Secret",
                "X-Service-Token",
                "security_token",
                "X-Token",
                "PRIVATE-TOKEN",
                "private_token",
                "X-Gitlab-Token",
                "Circle-Token",
                "app_secret",
                "X-Auth",
                "Authorization",
                "X-API-Key",
                "vendor-api.key",
                "client[private][key]",
                "sessionid",
                "passphrase",
                "signature",
                "bearer",
                "jwt",
                "otp",
                "otp_code",
                "totp",
                "pin",
            )
        )
    }


@pytest.fixture
def native_tool_usage():
    """Construct the real version-specific ToolUsage around a local function."""
    from importlib.metadata import version
    from types import SimpleNamespace

    from crewai.tools.tool_usage import ToolUsage

    def create(function):
        if version("crewai") == "0.41.1":
            from crewai.agents.tools_handler import ToolsHandler
            from langchain_core.tools import StructuredTool

            tool = StructuredTool.from_function(function)
            usage = ToolUsage(
                tools_handler=ToolsHandler(),
                tools=[tool],
                original_tools=[],
                tools_description=function.__name__,
                tools_names=function.__name__,
                task=SimpleNamespace(used_tools=0),
                function_calling_llm=None,
                agent=SimpleNamespace(tools_results=[]),
                action=None,
            )
            usage._printer.print = lambda **kwargs: None
            return usage
        from crewai.tools.structured_tool import CrewStructuredTool

        return ToolUsage(
            tools_handler=None,
            tools=[CrewStructuredTool.from_function(function)],
            task=None,
            function_calling_llm=None,
        )

    return create
