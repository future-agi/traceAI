"""The installed package and OpenTelemetry instrumentor have the same floor."""

from importlib.metadata import requires

from packaging.requirements import Requirement
from traceai_lancedb import LanceDBInstrumentor


def test_supported_lancedb_floor_matches_installed_metadata():
    (gate,) = LanceDBInstrumentor().instrumentation_dependencies()
    gate = Requirement(gate)
    dependency = next(
        requirement
        for text in requires("traceAI-lancedb")
        if (requirement := Requirement(text)).name == "lancedb"
    )
    assert str(gate.specifier) == str(dependency.specifier)
    assert "0.14.0" not in gate.specifier
    assert "0.24.3" not in gate.specifier
    assert "0.25.0" in gate.specifier
    assert "0.41.0" in gate.specifier
