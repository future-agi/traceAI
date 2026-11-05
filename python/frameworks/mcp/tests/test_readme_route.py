"""Keep the package guide honest about its propagation-only contract."""
from pathlib import Path


def test_readme_distinguishes_native_spans_from_legacy_propagation():
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    assert "automatically trace all MCP requests" not in readme
    assert "does not create spans" in readme
    assert "native OpenTelemetry" in readme
    assert "Do not enable `MCPInstrumentor`" in readme
    assert "https://docs.futureagi.com/docs/integrations/traceai/mcp-python" in readme


def test_readme_and_floor_constraints_require_cancellation_safe_sdk():
    package = Path(__file__).parents[1]
    readme = (package / "README.md").read_text()
    assert '"mcp>=1.30.0,<2"' in readme
    assert '"mcp==1.8.1"' not in readme
    assert '"pydantic==2.11.10"' not in readme
    constraints = (package / "tests" / "constraints-floor.txt").read_text()
    requirements = [line for line in constraints.splitlines() if line and not line.startswith("#")]
    assert requirements == ["mcp==1.30.0"]
