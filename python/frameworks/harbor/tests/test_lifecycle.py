import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import attrs, spans
from harbor.cli.job_plugins import attach_job_plugin, finalize_job_plugins
from harbor.job import Job
from harbor.models.job.config import JobConfig
from traceai_harbor import import_job


def make_task(tmp_path):
    task = tmp_path / "hello-world"
    for name in ["environment", "tests", "solution"]:
        (task / name).mkdir(parents=True, exist_ok=True)
    (task / "instruction.md").write_text("Say hello to the world.")
    (task / "task.toml").write_text(
        'version = "1.0"\n[environment]\nnetwork_mode = "no-network"\n[verifier]\ntimeout_sec = 30\n[agent]\ntimeout_sec = 30\n'
    )
    (task / "tests" / "test.sh").write_text(
        "#!/bin/bash\nprintf '1' > /logs/verifier/reward.txt\n"
    )
    (task / "solution" / "solve.sh").write_text(
        "#!/bin/bash\nprintf 'hello' > /logs/agent/output.txt\n"
    )
    return task


def test_real_job_trial_lifecycle_and_backfill(tmp_path, receiver, monkeypatch):
    for key, value in {
        "FI_API_KEY": receiver.settings.api_key,
        "FI_SECRET_KEY": receiver.settings.secret_key,
        "FI_PROJECT_NAME": receiver.settings.project_name,
        "FI_HARBOR_ENDPOINT": receiver.endpoint,
        "HARBOR_TELEMETRY": "false",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }.items():
        monkeypatch.setenv(key, value)
    task = make_task(tmp_path.resolve())
    config = JobConfig.model_validate(
        {
            "job_name": "real-lifecycle",
            "jobs_dir": tmp_path.resolve() / "jobs",
            "tasks": [{"path": task}],
            "agents": [{"import_path": "harbor_fixture:DeterministicAgent"}],
            "environment": {"import_path": "harbor_fixture:LocalFixtureEnvironment"},
            "n_attempts": 3,
            "n_concurrent_trials": 3,
            "quiet": True,
        }
    )

    async def run():
        job = await Job.create(config)
        plugin = await attach_job_plugin(job, "futureagi")
        result = await job.run()
        await finalize_job_plugins([plugin], result)
        return job, result, plugin

    job, result, plugin = asyncio.run(run())
    assert len(result.trial_results) == 3
    assert all(t.exception_info is None for t in result.trial_results), [
        t.exception_info for t in result.trial_results
    ]
    assert all(
        t.verifier_result.rewards == {"reward": 1.0} for t in result.trial_results
    )
    assert plugin.summary.ok and plugin.summary.skipped == 3
    assert len(receiver.requests) == 3
    assert {attrs(s)["fi.span.kind"] for s in spans(receiver)} >= {
        "CHAIN",
        "AGENT",
        "LLM",
        "TOOL",
    }
    original = [r[2] for r in receiver.requests]
    assert import_job(job.job_dir, settings=receiver.settings).skipped == 3
    # Re-import pristine copies as if never exported; equality is byte-for-byte,
    # including IDs, timestamps, parentage, rewards and statuses.
    pristine = tmp_path / "pristine"
    shutil.copytree(
        job.job_dir, pristine, ignore=shutil.ignore_patterns(".traceai-harbor")
    )
    assert import_job(pristine, settings=receiver.settings).sent == 3
    assert sorted(original) == sorted(r[2] for r in receiver.requests[3:])
    persisted = (job.job_dir / "config.json").read_text()
    assert (
        receiver.settings.api_key not in persisted
        and receiver.settings.secret_key not in persisted
    )


@pytest.mark.parametrize("capture_content", [False, True])
def test_installed_cli_customer_command(tmp_path, receiver, capture_content):
    task = make_task(tmp_path.resolve())
    bindir = Path(sys.executable).parent
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path.resolve()),
        "PYTHONPATH": str(Path(__file__).parent),
        "HARBOR_TELEMETRY": "false",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "FI_API_KEY": receiver.settings.api_key,
        "FI_SECRET_KEY": receiver.settings.secret_key,
        "FI_PROJECT_NAME": receiver.settings.project_name,
        "FI_HARBOR_ENDPOINT": receiver.endpoint,
    }
    # Same command as the customer page; job.json is the customer's existing
    # configuration. This test's task, agent and execution backend are synthetic.
    config = {
        "job_name": "my-harbor-job",
        "jobs_dir": "jobs",
        "tasks": [{"path": str(task)}],
        "agents": [{"import_path": "harbor_fixture:DeterministicAgent"}],
        "environment": {"import_path": "harbor_fixture:LocalFixtureEnvironment"},
        "quiet": True,
    }
    (tmp_path / "job.json").write_text(json.dumps(config))
    command = [
        str(bindir / "harbor"),
        "run",
        "--config",
        "./job.json",
        "--plugin",
        "futureagi",
    ]
    if capture_content:
        command.extend(["--plugin-kwarg", "futureagi.capture_content=true"])
    run = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(receiver.requests) == 1
    assert any("input.value" in attrs(s) for s in spans(receiver)) is capture_content
    result_files = list((tmp_path / "jobs" / "my-harbor-job").glob("*/result.json"))
    assert len(result_files) == 1
    trial = json.loads(result_files[0].read_text())
    assert trial["exception_info"] is None
    assert trial["verifier_result"]["rewards"] == {"reward": 1.0}
    assert {attrs(s)["fi.span.kind"] for s in spans(receiver)} >= {
        "CHAIN",
        "AGENT",
        "LLM",
        "TOOL",
    }
    imported = subprocess.run(
        [
            str(bindir / "traceai-harbor"),
            "import-job",
            "./jobs/my-harbor-job",
            *(["--capture-content"] if capture_content else []),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert json.loads(imported.stdout)["skipped"] == 1
    plugins = subprocess.run(
        [str(bindir / "harbor"), "plugins", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plugins.returncode == 0 and "futureagi" in plugins.stdout
