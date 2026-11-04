"""Qualify core runtimes independently of optional evaluation dependencies."""
from importlib.metadata import metadata

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet


def test_requires_python_covers_qualified_core_runtimes():
    package = metadata("fi-instrumentation-otel")
    supported = SpecifierSet(package["Requires-Python"])
    for minor in range(10, 16):
        assert f"3.{minor}.0" in supported
        assert f"Programming Language :: Python :: 3.{minor}" in package.get_all(
            "Classifier", []
        )
    assert "3.16.0" not in supported
    assert "3.9.0" not in supported
    assert set(package.get_all("Provides-Extra", [])) == {"grpc", "evaluation"}


def test_evaluation_dependencies_are_opt_in_not_silently_omitted_on_315():
    requirements = {
        requirement.name: requirement
        for requirement in map(
            Requirement, metadata("fi-instrumentation-otel").get_all("Requires-Dist", [])
        )
    }
    for name in ("futureagi", "ai-evaluation"):
        requirement = requirements[name]
        assert requirement.marker is not None
        for minor in range(10, 16):
            environment = {
                "python_version": f"3.{minor}",
                "python_full_version": f"3.{minor}.0",
            }
            for extra in ("", "grpc"):
                assert not requirement.marker.evaluate({**environment, "extra": extra})
            # An unsupported evaluation install must fail in its own dependency
            # chain, not appear successful with its libraries missing.
            assert requirement.marker.evaluate({**environment, "extra": "evaluation"})
