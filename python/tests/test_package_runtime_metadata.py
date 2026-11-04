"""Keep the advertised runtime range honest for every optional extra."""
from importlib.metadata import metadata

from packaging.specifiers import SpecifierSet


def test_requires_python_covers_only_qualified_runtimes():
    package = metadata("fi-instrumentation-otel")
    supported = SpecifierSet(package["Requires-Python"])
    for minor in range(10, 15):
        assert f"3.{minor}.0" in supported
    # Core alone may work on 3.15, but evaluation's native dependency does not.
    assert "3.15.0" not in supported
    assert "3.9.0" not in supported
    assert "Programming Language :: Python :: 3.15" not in package.get_all("Classifier", [])
    assert set(package.get_all("Provides-Extra", [])) == {"grpc", "evaluation"}
