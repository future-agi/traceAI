"""Local synthetic tests only; never disable the OTel SDK's positive controls."""

import os

import pytest

# Must precede CrewAI imports, which can initialize a telemetry singleton.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_DISABLE_TRACKING"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "false"


@pytest.fixture(autouse=True)
def native_telemetry_off(request, monkeypatch):
    from importlib.metadata import version
    from crewai.telemetry import Telemetry

    if request.node.name.startswith("test_native_telemetry_"):
        yield
        return
    if version("crewai") == "0.41.1":
        # Test-only isolation: legacy CrewAI does not honor native disable flags.
        # Production instrumentation must never patch the user's telemetry.
        def disabled(instance):
            instance.ready = False
            instance.trace_set = False

        monkeypatch.setattr(Telemetry, "__init__", disabled)
    else:
        assert Telemetry._is_telemetry_disabled()
    yield


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
                "Ocp-Apim-Subscription-Key",
                "subscription_key",
                "DD-APPLICATION-KEY",
                "application_key",
                "X-Parse-Master-Key",
                "master_key",
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
