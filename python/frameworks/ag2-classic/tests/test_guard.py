"""AC-05: version guard with fake module layouts (no other package installed)."""

from __future__ import annotations

import types

import pytest

from traceai_ag2_classic import AG2ClassicCompatibilityError, check_autogen_classic


def _layout(
    *,
    owners=("autogen",),
    versions=None,
    version="0.14.1",
    has_otel=True,
    importable=True,
    agentchat=False,
    ag2_module=False,
):
    versions = dict(versions or {"autogen": "0.14.1"})
    modules = {}
    if importable:
        modules["autogen"] = types.ModuleType("autogen")
        if version is not None:
            vm = types.ModuleType("autogen.version")
            setattr(vm, "__version__", version)
            modules["autogen.version"] = vm

    def import_module(name):
        if name in modules:
            return modules[name]
        raise ImportError("No module named {0!r}".format(name))

    def find_spec(name):
        if name == "autogen.opentelemetry":
            return object() if (importable and has_otel) else None
        if name == "autogen_agentchat":
            return object() if agentchat else None
        if name == "ag2":
            return object() if ag2_module else None
        return None

    return {
        "distributions_for": lambda top: list(owners) if top == "autogen" else [],
        "distribution_version": versions.get,
        "import_module": import_module,
        "find_spec": find_spec,
    }


def _assert_names_three_packages(error):
    text = str(error)
    for name in ("`autogen` 0.14.x", "`ag2` 1.x", "`autogen-agentchat`"):
        assert name in text
    assert "pyautogen" in text


@pytest.mark.parametrize("version", ["0.14.0", "0.14.1", "0.14"])
def test_supported_versions_pass(version):
    assert check_autogen_classic(**_layout(version=version, versions={"autogen": version})) == version


@pytest.mark.parametrize("version", ["0.13.9", "0.15.0", "0.140.0", "1.1.2", "0.2.35"])
def test_unsupported_versions_raise(version):
    with pytest.raises(AG2ClassicCompatibilityError) as info:
        check_autogen_classic(**_layout(version=version, versions={"autogen": version}))
    _assert_names_three_packages(info.value)
    assert version in str(info.value)


def test_autogen_0_14_0_alias_layout_backed_by_ag2_0_14_0_passes():
    # PyPI autogen==0.14.0 requires ag2==0.14.0, which ships the `autogen` module.
    layout = _layout(owners=("ag2",), versions={"autogen": "0.14.0", "ag2": "0.14.0"}, version="0.14.0")
    assert check_autogen_classic(**layout) == "0.14.0"


@pytest.mark.parametrize(
    "versions",
    [
        {"ag2": "0.14.0"},  # ag2 installed directly, no `autogen` distribution
        {"ag2": "0.9.0", "autogen": "0.9.0"},  # older ag2 alias line
        {"ag2": "1.1.2", "autogen": "0.14.1"},  # mismatched pairing
    ],
)
def test_autogen_module_owned_by_ag2_outside_the_alias_layout_raises(versions):
    with pytest.raises(AG2ClassicCompatibilityError) as info:
        check_autogen_classic(**_layout(owners=("ag2",), versions=versions))
    assert "installed by `ag2`" in str(info.value)
    _assert_names_three_packages(info.value)


@pytest.mark.parametrize("owners", [("pyautogen",), ("autogen", "pyautogen"), ("PyAutoGen",)])
def test_pyautogen_owner_raises(owners):
    with pytest.raises(AG2ClassicCompatibilityError) as info:
        check_autogen_classic(**_layout(owners=owners))
    assert "pyautogen" in info.value.reason
    _assert_names_three_packages(info.value)


def test_missing_opentelemetry_module_raises():
    with pytest.raises(AG2ClassicCompatibilityError) as info:
        check_autogen_classic(**_layout(has_otel=False))
    assert "autogen.opentelemetry" in str(info.value)
    _assert_names_three_packages(info.value)


def test_microsoft_autogen_layout_raises():
    with pytest.raises(AG2ClassicCompatibilityError) as info:
        check_autogen_classic(**_layout(owners=(), importable=False, agentchat=True))
    assert "autogen_agentchat" in str(info.value)
    _assert_names_three_packages(info.value)


def test_ag2_1x_only_layout_raises():
    # ag2 1.x ships `ag2`, not `autogen`, so `import autogen` fails.
    with pytest.raises(AG2ClassicCompatibilityError) as info:
        check_autogen_classic(**_layout(owners=(), importable=False, ag2_module=True))
    assert "`ag2` (AG2 1.x, `import ag2`) is installed" in str(info.value)
    _assert_names_three_packages(info.value)


def test_missing_version_module_raises():
    with pytest.raises(AG2ClassicCompatibilityError) as info:
        check_autogen_classic(**_layout(version=None))
    assert "autogen.version" in str(info.value)


def test_installed_autogen_passes_guard():
    pytest.importorskip("autogen.opentelemetry")
    from autogen.version import __version__

    assert check_autogen_classic() == __version__
    assert __version__.startswith("0.14.")
