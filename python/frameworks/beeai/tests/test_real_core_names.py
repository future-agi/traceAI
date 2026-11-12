"""Check the core names this package uses against the real installed core.

tests/conftest.py replaces fi_instrumentation with mock modules, so the other tests here
cannot notice a core name that the declared fi-instrumentation-otel floor lacks. This test
reads the package source in a clean interpreter that imports the real core.
"""

import pathlib
import subprocess
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "traceai_beeai"

CHECK = r"""
import ast, importlib, pathlib, sys, types
missing = []
for path in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "fi_instrumentation":
            module = importlib.import_module(node.module)
            for alias in node.names:
                if hasattr(module, alias.name):
                    names[alias.asname or alias.name] = getattr(module, alias.name)
                else:
                    missing.append(f"{path.name}: {node.module}.{alias.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in names:
            owner = names[node.value.id]
            if isinstance(owner, (type, types.ModuleType)) and not hasattr(owner, node.attr):
                missing.append(f"{path.name}: {node.value.id}.{node.attr}")
print("\n".join(sorted(set(missing))))
sys.exit(1 if missing else 0)
"""


def test_every_core_name_the_package_uses_exists_in_the_installed_core():
    result = subprocess.run(
        [sys.executable, "-I", "-c", CHECK, str(PACKAGE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "missing in the installed core:\n" + result.stdout + result.stderr
