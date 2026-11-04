"""Synthetic-only Harbor test backend. Not packaged or customer integration code."""

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities


class LocalFixtureEnvironment(BaseEnvironment):
    @staticmethod
    def type():
        return "traceai-local-fixture"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(disable_internet=True)

    def _validate_definition(self):
        pass

    async def start(self, force_build=False):
        self.root = Path(tempfile.mkdtemp(prefix="harbor-synthetic-")).resolve()
        for folder in [
            "logs/agent",
            "logs/verifier",
            "logs/artifacts",
            "tests",
            "solution",
        ]:
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    async def stop(self, delete=True):
        if delete:
            shutil.rmtree(self.root)

    def _path(self, path):
        result = self.root / str(path).lstrip("/")
        if not result.resolve().is_relative_to(self.root):
            raise ValueError("fixture path escape")
        return result

    def _command(self, command):
        for path in ["/logs", "/tests", "/solution"]:
            command = command.replace(path, str(self.root / path[1:]))
        return command

    async def upload_file(self, source_path, target_path):
        target = self._path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = Path(source_path).read_bytes()
        if target.suffix == ".sh":
            data = self._command(data.decode()).encode()
        target.write_bytes(data)

    async def upload_dir(self, source_dir, target_dir):
        for source in Path(source_dir).rglob("*"):
            if source.is_file():
                await self.upload_file(
                    source, str(Path(target_dir) / source.relative_to(source_dir))
                )

    async def download_file(self, source_path, target_path):
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(source_path), target)

    async def download_dir(self, source_dir, target_dir):
        source = self._path(source_dir)
        if source.exists():
            shutil.copytree(source, target_dir, dirs_exist_ok=True)

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        # No host environment inheritance, including FI credentials.
        assert not any(key.startswith("FI_") for key in (env or {}))
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            self._command(command),
            cwd=self.root if not cwd else self._path(cwd),
            env={"PATH": "/usr/bin:/bin", **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_sec or 30
        )
        return ExecResult(
            return_code=process.returncode,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )


class DeterministicAgent(BaseAgent):
    capabilities = AgentCapabilities(atif=True)

    @staticmethod
    def name():
        return "synthetic-atif"

    def version(self):
        return "1"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        def now():
            return datetime.now(timezone.utc).isoformat()

        start = now()
        await environment.exec(
            "printf 'synthetic task output' > /logs/artifacts/output.txt"
        )
        trajectory = {
            "schema_version": "ATIF-v1.7",
            "session_id": str(self.context_id),
            "agent": {
                "name": self.name(),
                "version": self.version(),
                "model_name": "synthetic-model",
            },
            "steps": [
                {
                    "step_id": 1,
                    "source": "user",
                    "timestamp": start,
                    "message": instruction,
                },
                {
                    "step_id": 2,
                    "source": "agent",
                    "timestamp": now(),
                    "message": "hello",
                    "metrics": {"prompt_tokens": 5, "completion_tokens": 2},
                    "tool_calls": [
                        {
                            "tool_call_id": "call",
                            "function_name": "echo",
                            "arguments": {"text": "hello"},
                        }
                    ],
                },
                {
                    "step_id": 3,
                    "source": "agent",
                    "timestamp": now(),
                    "message": "done",
                    "llm_call_count": 0,
                    "observation": {
                        "results": [{"source_call_id": "call", "content": "hello"}]
                    },
                },
            ],
        }
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "trajectory.json").write_text(json.dumps(trajectory))
        context.n_input_tokens = 5
        context.n_output_tokens = 2
