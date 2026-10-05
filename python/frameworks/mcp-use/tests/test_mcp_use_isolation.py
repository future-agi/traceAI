"""The package only acts where the callback is passed.

Importing traceai_mcp_use, building FutureAGICallback and running an agent
with it change no attribute of mcp-use, LangChain or mcp classes and
modules and no environment variable. The test suite itself never sends
mcp-use usage telemetry.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest
from _mcp_use_support import ANSWER, add_script, new_provider, run_agent

from traceai_mcp_use import FutureAGICallback

pytest.importorskip("uvicorn", reason="the loopback MCP server runs on uvicorn")

SNAPSHOT = textwrap.dedent(
    """
    import importlib, json, os, sys
    import _mcp_use_support  # telemetry off, as in every test
    TARGETS = {
        "mcp_use.agents.mcpagent": ["MCPAgent"],
        "mcp_use.agents.observability.callbacks_manager": ["ObservabilityManager"],
        "mcp_use.agents.adapters.langchain_adapter": ["LangChainAdapter"],
        "mcp_use.client.task_managers.stdio": [],
        "mcp.client.stdio": [],
        "langchain_core.callbacks.manager": ["CallbackManager", "AsyncCallbackManager"],
        "langchain_core.callbacks.base": ["BaseCallbackHandler", "BaseCallbackManager"],
        "langchain_core.language_models.chat_models": ["BaseChatModel"],
        "langchain_core.tools.base": ["BaseTool"],
    }

    def snapshot():
        found = {}
        for module_name, classes in TARGETS.items():
            module = importlib.import_module(module_name)
            for key, value in vars(module).items():
                found[module_name + ":" + key] = id(value)
            for class_name in classes:
                for key, value in vars(getattr(module, class_name)).items():
                    found[module_name + "." + class_name + ":" + key] = id(value)
        return found

    before, env_before = snapshot(), dict(os.environ)
    import traceai_mcp_use
    from uuid import uuid4
    from _mcp_use_support import new_provider
    handler = traceai_mcp_use.FutureAGICallback(tracer_provider=new_provider()[1], capture_content=True)
    root = uuid4()
    handler.on_chain_start({}, {"messages": []}, run_id=root)
    handler.on_chain_end({"messages": []}, run_id=root)
    after, env_after = snapshot(), dict(os.environ)
    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    print(json.dumps({
        "changed": changed,
        "env": sorted(set(env_before.items()) ^ set(env_after.items())),
        "modules": sorted(name for name in sys.modules if name.split(".")[0] in ("langfuse", "lmnr", "traceai_mcp")),
    }))
    """
)


def test_import_and_use_patch_nothing_and_set_no_environment_variable():
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, "-c", SNAPSHOT], env=env, capture_output=True, timeout=600
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-4000:]
    report = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert report == {"changed": [], "env": [], "modules": []}


def test_a_traced_run_leaves_the_agent_callbacks_as_passed():
    from mcp_use import MCPAgent

    exporter, provider = new_provider()
    handler = FutureAGICallback(tracer_provider=provider)
    before = {key: id(value) for key, value in vars(MCPAgent).items()}
    assert run_agent(add_script(), [handler]) == ANSWER
    assert {key: id(value) for key, value in vars(MCPAgent).items()} == before
    assert len(exporter.get_finished_spans()) == 4


def test_mcp_use_usage_telemetry_is_off_in_this_suite():
    from mcp_use.telemetry.telemetry import Telemetry

    telemetry = Telemetry()
    assert telemetry._posthog_client is None
    assert telemetry._scarf_client is None
