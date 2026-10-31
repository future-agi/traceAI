"""Serialize expensive children and respect the shared laptop's capacity gate."""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path


class HostSafety:
    def __init__(self, work: Path):
        self.work = work
        self.script = Path.home() / ".futureagi-local/bin/host-status.sh"
        shared = Path("/Volumes/nikm4/futureagi/tmp/traceai-auto-review")
        self.lock_path = shared / "heavy.lock" if shared.is_dir() else work.parent / "heavy.lock"
        self.observations = []

    def check(self):
        while True:
            if self.script.is_file():
                text = subprocess.check_output([str(self.script)], text=True, timeout=20).strip()
                free, load = re.search(r"free=([\d.]+)%", text), re.search(r"load=([\d.]+)", text)
                if not free or not load:
                    raise RuntimeError("Cannot parse host-status; refusing to run a child: " + text)
                free, load = float(free[1]), float(load[1])
            elif sys.platform.startswith("linux"):
                values = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line and line.split()[1].isdigit()}
                free = 100 * values["MemAvailable"] / values["MemTotal"]
                load = os.getloadavg()[0]
                text = f"linux free={free:.1f}% load={load:.2f}"
            else:
                raise RuntimeError("host-status.sh is absent; refusing laptop execution")
            print("host " + text, file=sys.stderr, flush=True)
            self.observations.append({"unix_time": time.time(), "free_percent": free, "load": load})
            if free >= 20 and load <= 30:
                return
            print("Capacity gate closed; waiting 60 seconds", file=sys.stderr, flush=True)
            time.sleep(60)

    @contextlib.contextmanager
    def step(self, heavy=False):
        acquired = False
        try:
            # Every child, including Python/Node and probes, shares the owner's lock.
            # `heavy` is retained as adapter metadata, not a bypass of serialization.
            while True:
                try:
                    self.lock_path.mkdir()
                    acquired = True
                    break
                except FileExistsError:
                    # Same stale-empty-directory policy as the owner's shell lock.
                    try:
                        if time.time() - self.lock_path.stat().st_mtime > 25 * 60:
                            self.lock_path.rmdir()
                    except (FileNotFoundError, OSError):
                        pass
                    time.sleep(10)
            self.check()
            yield
        finally:
            if acquired:
                self.lock_path.rmdir()


def clean_environment(work: Path) -> dict[str, str]:
    strip_prefixes = ("FI_", "OTEL_", "TRACEAI_", "OPENAI_", "ANTHROPIC_", "BRAINTRUST_", "BENCH_")
    strip_keys = {"NODE_OPTIONS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "DOTNET_STARTUP_HOOKS",
                  "RUBYOPT", "RUBYLIB", "GEM_HOME", "GEM_PATH", "BUNDLE_GEMFILE", "BUNDLE_PATH", "BUNDLE_BIN_PATH",
                  "PYTHONPATH", "PYTHONHOME", "GOFLAGS", "GOWORK", "GOTOOLCHAIN", "GOENV", "GOTELEMETRY", "NODE_PATH",
                  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
    env = {k: v for k, v in os.environ.items() if not k.startswith(strip_prefixes) and k not in strip_keys}
    cache = work / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    tmp = work / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update({"TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp), "XDG_CACHE_HOME": str(cache),
                "npm_config_cache": str(cache / "npm"), "PIP_CACHE_DIR": str(cache / "pip"),
                "UV_CACHE_DIR": str(cache / "uv"), "NODE_COMPILE_CACHE": str(cache / "node-compile"),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "GEM_HOME": str(cache / "gems"), "BUNDLE_PATH": str(cache / "bundle"),
                "GOCACHE": str(cache / "go-build"), "GOMODCACHE": os.environ.get("TRACEAI_BENCH_GO_MOD_CACHE", str(cache / "go-mod")),
                # Cached Go toolchains can be selected, but network downloads cannot.
                "GOTOOLCHAIN": "auto", "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off", "GOMAXPROCS": "2",
                "GOTELEMETRY": "off", "GOENV": "off", "GOTMPDIR": str(tmp),
                "NUGET_PACKAGES": str(cache / "nuget"), "DOTNET_CLI_HOME": str(cache / "dotnet"),
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
                "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "true", "DOTNET_NOLOGO": "1",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                "FI_CONFIG_FILE": str(work / "empty.toml"), "FI_PROJECT_TYPE": "observe",
                "FI_PROJECT_NAME": "local-auto-release-gate", "FI_API_KEY": "local-dummy-api-key",
                "FI_SECRET_KEY": "local-dummy-secret-key", "FI_SAMPLE_RATE": "1", "FI_WIRE_CAPTURE": "true",
                "OTEL_TRACES_SAMPLER": "always_on", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
                "OTEL_BSP_SCHEDULE_DELAY": "200", "OTEL_BSP_MAX_QUEUE_SIZE": "65536",
                "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "512", "OTEL_BSP_EXPORT_TIMEOUT": "3000",
                "TRACEAI_CACHE_DIR": str(cache / "launcher"), "FI_AUTO_DEBUG": "false"})
    (work / "empty.toml").write_text("# No inherited user configuration.\n")
    return env
