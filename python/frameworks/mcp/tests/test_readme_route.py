"""Keep the package guide honest about its propagation-only contract."""
from pathlib import Path


def test_readme_distinguishes_native_spans_from_legacy_propagation():
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    assert "automatically trace all MCP requests" not in readme
    assert "does not create spans" in readme
    assert "native OpenTelemetry" in readme
    assert "Do not enable `MCPInstrumentor`" in readme
    assert "https://docs.futureagi.com/docs/integrations/traceai/mcp-python" in readme
