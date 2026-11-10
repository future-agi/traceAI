"""AC-06 / R-13 / D15: package boundaries, dependency range, no base_url from us."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytest.importorskip("typesafe_sdk")

from traceai_typesafe import TypeSafeInstrumentor  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE = PACKAGE / "traceai_typesafe"


def _source() -> str:
    return "\n".join(path.read_text() for path in sorted(SOURCE.rglob("*.py")))


def test_dependency_range_matches_pyproject():
    (requirement,) = TypeSafeInstrumentor().instrumentation_dependencies()
    instruments = Requirement(requirement)
    assert instruments.name == "typesafe-sdk"
    match = re.search(r'^typesafe-sdk\s*=\s*"([^"]+)"', (PACKAGE / "pyproject.toml").read_text(), re.M)
    assert match and Requirement("typesafe-sdk" + match.group(1)).specifier == instruments.specifier
    for version in ("0.7.0", "0.7.4", "0.7.5", "0.9.9"):
        assert instruments.specifier.contains(version), version
    for version in ("0.6.0", "1.0.0"):
        assert not instruments.specifier.contains(version), version


def test_distribution_name_and_license():
    text = (PACKAGE / "pyproject.toml").read_text()
    assert 'name = "traceAI-typesafe"' in text
    assert 'license = "Apache-2.0"' in text
    assert "jev-evals" not in text.lower()


def test_no_eval_registry_gateway_or_http_library_imports():
    imports = "\n".join(
        line.strip()
        for line in _source().splitlines()
        if line.strip().startswith(("import ", "from "))
    )
    for forbidden in ("fi.evals", "eval", "gateway", "agentcc", "httpx", "requests", "urllib.request"):
        assert forbidden not in imports, forbidden


def test_instrumentor_sets_no_base_url_or_host():
    source = _source()
    assert "api.typesafe.ai" not in source
    assert "system-one.dev" not in source
    assert "base_url=" not in source
    assert "TYPESAFE_BASE_URL" not in source


def test_no_pass_fail_mapping():
    source = _source()
    assert '"Pass"' not in source and '"Fail"' not in source
