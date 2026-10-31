"""Offline adapters. Build only tiny fixture apps, never rebuild agent source trees."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class Unavailable(Exception):
    pass


@dataclass
class Runtime:
    name: str
    off: list[str]
    on: list[str]
    off_env: dict = field(default_factory=dict)
    on_env: dict = field(default_factory=dict)
    heavy: bool = False
    metadata: dict = field(default_factory=dict)


def fingerprint(path: Path) -> dict:
    if path.is_file():
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    digest = hashlib.sha256()
    count = 0
    for item in sorted(path.rglob("*")):
        if item.is_file() and "__pycache__" not in item.parts and ".git" not in item.parts:
            digest.update(str(item.relative_to(path)).encode()); digest.update(item.read_bytes()); count += 1
    return {"path": str(path), "source_sha256": digest.hexdigest(), "source_files": count}


def prepare(name, args, root: Path, work: Path, env: dict, guard) -> Runtime:
    apps = Path(__file__).resolve().parent / "apps"
    logs = work / "build-logs"; logs.mkdir(exist_ok=True)

    def tool(command):
        resolved = shutil.which(str(command), path=env.get("PATH"))
        if not resolved: raise Unavailable(f"toolchain not found: {command}")
        return resolved
    def require(path):
        if not path.is_file(): raise Unavailable(f"agent missing: {path}; prebuild/provision offline")
        return path
    def check(command, label, heavy=False, cwd=None, child_env=None):
        with guard.step(heavy):
            result = subprocess.run(command, env=child_env or env, cwd=cwd or work, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=240)
        (logs / (label + ".log")).write_text(result.stdout)
        if result.returncode:
            raise Unavailable(f"{label} unavailable (exit {result.returncode}); see {logs / (label + '.log')}: " + result.stdout[-1200:])
        return result.stdout.strip()

    if name in ("python", "launcher", "launcher-node"):
        if name != "launcher-node":
            py = tool(args.python)
            source = root / "python/fi_instrumentation"
            require(source / "auto/_cli.py")
            snapshot = work / "snapshots/python/fi_instrumentation"
            shutil.rmtree(snapshot, ignore_errors=True)
            shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            py_env = {"PYTHONPATH": str(snapshot.parent)}
            version = check([py, "-c", "import sys,json,httpx,fi_instrumentation,opentelemetry.sdk.trace; import importlib.metadata as m; print(json.dumps({'python':sys.version,'httpx':httpx.__version__,'agent_file':fi_instrumentation.__file__,'installed_distributions':{d.metadata['Name']:d.version for d in m.distributions()}}))"],
                            name + "-python-probe", child_env={**env, **py_env})
            version = json.loads(version)
            if not version["python"].startswith("3.12."):
                raise Unavailable("Python fixture requires Python 3.12 (select --python)")
            base = [py, str(apps / "python_app.py")]
            metadata = {"runtime": version, "agent": fingerprint(snapshot), "provisioned_agent_source": str(source),
                        "agent_snapshot": "immutable per invocation; includes provider/exporter/core helpers, not only auto/"}
        if name == "python":
            return Runtime(name, base, [py, "-m", "fi_instrumentation.auto", *base],
                           py_env, py_env, metadata={**metadata, "entry": "fi-instrument equivalent: python -m fi_instrumentation.auto python app.py",
                                                   "batch_delay_ms": 200, "batch_delay_source": "OTEL_BSP_SCHEDULE_DELAY=200"})
        provisioned_launcher = require(Path(args.launcher or root / "go/launcher/bin/traceai"))
        launcher = work / "snapshots/launcher/traceai"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(provisioned_launcher, launcher)
        node_agent = str(work / "not-provisioned-node")
        if name == "launcher-node":
            node_runtime = prepare("node", args, root, work, env, guard)
            base, py_env, metadata = node_runtime.off, {}, node_runtime.metadata
            node_agent = node_runtime.on[2]
        return Runtime(name, base, [str(launcher), "run", "--", *base], py_env,
                       {**py_env, "TRACEAI_NODE_AGENT": node_agent,
                        "TRACEAI_JAVA_AGENT": str(work / "not-provisioned.jar"),
                        "TRACEAI_DOTNET_AGENT": str(work / "not-provisioned.dll"),
                        "TRACEAI_GO_AGENT": str(work / "not-provisioned-go"),
                        "TRACEAI_RUBY_AGENT": str(work / "not-provisioned-ruby")},
                       metadata={**metadata, "launcher": fingerprint(launcher), "entry": "traceai run -- <ordinary app>",
                                 "launcher_cache": "persistent within run; startup samples are warm-cache after workload"})
    if name == "node":
        node = tool(args.node)
        provisioned_agent = require(Path(args.node_agent or root / "typescript/packages/traceai_auto/dist/agent/preload.cjs"))
        snapshot = work / "snapshots/node"
        shutil.rmtree(snapshot, ignore_errors=True)
        shutil.copytree(provisioned_agent.parent, snapshot)
        agent = snapshot / provisioned_agent.name
        version = check([node, "--version"], "node-version")
        parts = tuple(int(x) for x in version.lstrip("v").split('.')[:2])
        if parts < (20, 6): raise Unavailable("Node 20.6+ required")
        app = str(apps / "node_app.cjs")
        return Runtime(name, [node, app], [node, "--require", str(agent), app], metadata={
            "runtime": version, "agent": fingerprint(snapshot), "provisioned_agent_entry": str(provisioned_agent),
            "agent_snapshot": "entire preload directory, including runtime.cjs and module hooks, frozen per invocation",
            "entry": "node --require dist/agent/preload.cjs app.cjs",
            "batch_delay_ms": 0, "batch_delay_source": "agent uses SimpleSpanProcessor; export is asynchronous, not awaited by app"})
    if name == "java":
        java, javac = tool(args.java), tool(args.javac)
        agent = require(Path(args.java_agent or root / "java/traceai-javaagent/target/traceai-javaagent.jar"))
        build = work / "build/java"; build.mkdir(parents=True, exist_ok=True)
        version = check([java, "-version"], "java-version", True)
        check([javac, "--release", "17", "-d", str(build), str(apps / "java/Bench.java")], "java-build", True)
        base = [java, "-cp", str(build), "Bench"]
        return Runtime(name, base, [java, "-javaagent:" + str(agent), *base[1:]], heavy=True, metadata={
            "runtime": version, "agent": fingerprint(agent), "entry": "java -javaagent:<jar> -cp <app> Bench",
            "batch_delay_ms": 200, "batch_delay_source": "agent hard-coded 200 ms (OTEL_BSP setting does not override)"})
    if name == "dotnet":
        dotnet = tool(args.dotnet)
        agent = require(Path(args.dotnet_agent or root / "csharp/artifacts/auto/TraceAI.AutoInstrumentation.dll"))
        build = work / "build/dotnet"; build.mkdir(parents=True, exist_ok=True)
        for file in ("Bench.csproj", "Program.cs"):
            shutil.copy2(apps / "dotnet" / file, build / file)
        config = build / "NuGet.Config"
        config.write_text('<configuration><packageSources><clear /></packageSources></configuration>\n')
        version = check([dotnet, "--version"], "dotnet-version", True)
        check([dotnet, "build", str(build / "Bench.csproj"), "-c", "Release", "-o", str(build / "out"),
               "--configfile", str(config), "-p:NuGetAudit=false", "--disable-build-servers"], "dotnet-build", True)
        base = [dotnet, str(build / "out/Bench.dll")]
        return Runtime(name, base, base, on_env={"DOTNET_STARTUP_HOOKS": str(agent)}, heavy=True, metadata={
            "runtime": version, "agent": fingerprint(agent), "entry": "DOTNET_STARTUP_HOOKS=<published DLL> dotnet Bench.dll",
            "batch_delay_ms": 1000, "batch_delay_source": "PrivacyBatchProcessor hard-coded 1000 ms"})
    if name == "go":
        go = tool(args.go)
        module = root / "go/auto"
        require(module / "go.mod")
        version = check([go, "version"], "go-version")
        binaries = []
        for mode in ("off", "on"):
            build = work / "build/go" / mode; build.mkdir(parents=True, exist_ok=True)
            shutil.copy2(apps / "go/main.go", build / "main.go")
            go_version = re.search(r"^go (\S+)", (module / "go.mod").read_text(), re.M)[1]
            mod = f"module benchfixture\n\ngo {go_version}\n"
            if mode == "on":
                mod += f"\nrequire github.com/future-agi/traceAI/go/auto v0.0.0\nreplace github.com/future-agi/traceAI/go/auto => {json.dumps(str(module))}\n"
                (build / "agent_import.go").write_text('package main\nimport _ "github.com/future-agi/traceAI/go/auto"\n')
                if (module / "go.sum").is_file(): shutil.copy2(module / "go.sum", build / "go.sum")
            (build / "go.mod").write_text(mod)
            binary = build / "bench-app"
            check([go, "build", "-p=1", "-mod=mod", "-o", str(binary), "."], "go-build-" + mode, True, build)
            binaries.append([str(binary)])
        return Runtime(name, *binaries, metadata={"runtime": version, "agent": fingerprint(module),
            "entry": "separately compiled app with a blank import; off binary contains no agent",
            "batch_delay_ms": 200, "batch_delay_source": "agent hard-coded 200 ms; fixture waits idle, no explicit tracing flush"})
    if name == "ruby":
        ruby = tool(args.ruby)
        lib = Path(args.ruby_lib or root / "ruby/lib")
        agent = require(lib / "traceai/auto.rb")
        ruby_env = {} if not args.ruby_gem_home else {"GEM_HOME": str(Path(args.ruby_gem_home).resolve()), "GEM_PATH": str(Path(args.ruby_gem_home).resolve())}
        version = check([ruby, "--version"], "ruby-version", child_env={**env, **ruby_env})
        check([ruby, "-I", str(lib), "-e", "require 'opentelemetry/sdk'; require 'opentelemetry/exporter/otlp'"], "ruby-deps", child_env={**env, **ruby_env})
        base = [ruby, str(apps / "ruby_app.rb")]
        return Runtime(name, base, base, off_env=ruby_env, on_env={**ruby_env, "RUBYLIB": str(lib), "RUBYOPT": "-rtraceai/auto"}, metadata={
            "runtime": version, "agent": fingerprint(lib), "entry": "RUBYOPT=-rtraceai/auto ruby app.rb",
            "batch_delay_ms": None, "batch_delay_source": "OTEL_BSP_SCHEDULE_DELAY=200 requested; verify Ruby agent README when provisioned"})
    raise ValueError("unknown runtime: " + name)
