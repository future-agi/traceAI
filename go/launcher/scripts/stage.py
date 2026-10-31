"""Stage already-built agents; never build an SDK or invoke its runtime."""
import json
import os
import pathlib
import re
import shutil
import sys
import zipfile

root = pathlib.Path(sys.argv[1]).resolve()
target = pathlib.Path(sys.argv[2]).resolve()
versions = {}


def stage(name, source):
    dest = target / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    return dest


jar = root / "java/traceai-javaagent/target/traceai-javaagent.jar"
# A startup hook is loaded before its fail-open Initialize() can run. Always
# ship the lowest supported framework: net8 works on 8 and 10, not vice versa.
# Missing net8 must fail staging, never silently fall back to a newer assembly.
dotnet = root / "csharp/src/TraceAI.AutoInstrumentation/bin/Release/net8.0"
node = root / "typescript/packages/traceai_auto/dist/agent"
for path in (jar, dotnet / "TraceAI.AutoInstrumentation.dll", node / "preload.cjs", root / "go/auto/go.mod"):
    if not path.is_file():
        raise SystemExit("Missing prebuilt agent: " + str(path))

deps = json.loads((dotnet / "TraceAI.AutoInstrumentation.deps.json").read_text())
if deps.get("runtimeTarget", {}).get("name") != ".NETCoreApp,Version=v8.0":
    raise SystemExit("The shared .NET 8/10 payload must target net8.0, not a renamed net10.0 build")

shutil.copy2(jar, stage("java", jar) / jar.name)
with zipfile.ZipFile(jar) as archive:
    key = "META-INF/maven/ai.traceai/traceai-javaagent/pom.properties"
    metadata = archive.read(key).decode() if key in archive.namelist() else ""
    match = re.search(r"(?m)^version=(.+)$", metadata)
    versions["java"] = match.group(1) if match else "unversioned jar"

dest = stage("dotnet", dotnet)
for path in dotnet.iterdir():
    if path.is_file():
        shutil.copy2(path, dest / path.name)
versions["dotnet"] = next((name.split("/", 1)[1] for name in deps.get("libraries", {}) if name.startswith("TraceAI.AutoInstrumentation/")), "unversioned assembly")

dest = stage("node", node)
shutil.copytree(node, dest, dirs_exist_ok=True)
versions["node"] = json.loads((root / "typescript/packages/traceai_auto/package.json").read_text())["version"]

go = root / "go/auto"
dest = stage("go", go)
# Prune before traversal: SDK test-output directories can contain entire module
# caches. They are not agent source and can exceed extraction integrity limits.
for directory, dirs, names in os.walk(go):
    base = pathlib.Path(directory)
    dirs[:] = [name for name in dirs
               if not name.startswith(".")
               and name not in ("cmd", "testdata", "vendor", "node_modules")
               and not (base / name / "go.mod").is_file()]
    for name in names:
        path = base / name
        if path.is_symlink() or not path.is_file() or name.endswith("_test.go"):
            continue
        if not (path.suffix == ".go" or name in ("go.mod", "go.sum", "wire-protocols.json")):
            continue
        relative = path.relative_to(go)
        # go:embed omits nested modules, even with all:. Keep these sources out
        # of the launcher's ./... package pattern; restore names on extraction.
        to = dest / (str(relative) + ".source")
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, to)
versions["go"] = "unversioned source snapshot"

ruby = root / "ruby/lib"
# Ruby may still be under development. Never publish a stale Ruby payload.
if (target / "ruby").exists():
    shutil.rmtree(target / "ruby")
if (ruby / "traceai/auto.rb").is_file():
    shutil.copytree(ruby, target / "ruby")
    match = re.search(r"s\.version\s*=\s*['\"]([^'\"]+)", (root / "ruby/traceai-auto.gemspec").read_text())
    versions["ruby"] = match.group(1) if match else "unversioned source snapshot"
else:
    print("Ruby entry point unavailable; skipping Ruby payload (the fail-open shim is always embedded).")

(target / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
print("Staged existing agents: " + ", ".join(versions))
