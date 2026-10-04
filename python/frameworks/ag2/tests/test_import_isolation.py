"""AC-08: traceai-ag2 must never import ``autogen`` (AG2 Classic / AutoGen)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_PYTHON_DIR = Path(__file__).resolve().parents[3]

_FORBIDDEN = ("autogen", "autogen_agentchat", "autogen_core", "autogen_ext")


def test_import_and_setup_do_not_import_autogen():
    code = (
        "import sys\n"
        "import traceai_ag2\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from ag2 import Agent\n"
        "from ag2.testing import TestConfig\n"
        "traceai_ag2.setup(Agent('a', config=TestConfig('hi')), tracer_provider=TracerProvider())\n"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {_FORBIDDEN!r})\n"
        "print('FORBIDDEN', bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_PACKAGE_DIR), str(_PYTHON_DIR), env.get("PYTHONPATH", "")])
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")
    assert b"FORBIDDEN []" in result.stdout


def test_package_source_has_no_autogen_import():
    for path in sorted((_PACKAGE_DIR / "traceai_ag2").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                assert root not in _FORBIDDEN, f"{path}:{node.lineno} imports {root}"


def test_dependencies_exclude_autogen():
    text = (_PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    deps = text.split("[tool.poetry.dependencies]", 1)[1].split("\n[", 1)[0]
    declared = {
        line.split("=", 1)[0].strip().lower()
        for line in deps.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert "ag2" in declared
    assert not declared & {"autogen", "autogen-agentchat", "pyautogen", "autogen-core"}
