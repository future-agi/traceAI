import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from harbor_atif2otel.plugin import OtelPlugin
from traceai_harbor import FutureAGIPlugin, import_job
from traceai_harbor._artifacts import ArtifactError, parse
from traceai_harbor._transport import DeliveryError, send


def test_native_plugin_requires_mlflow(job, receiver, monkeypatch):
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "FI_API_KEY=synthetic,FI_SECRET_KEY=synthetic"
    )
    receiver.responses = [(200, b'{"experiments":[{"experiment_id":"1"}]}', {})]
    callbacks = []
    native_job = SimpleNamespace(
        job_dir=job,
        config=SimpleNamespace(job_name="test"),
        on_trial_ended=callbacks.append,
    )

    async def run():
        plugin = OtelPlugin(endpoint=receiver.endpoint)
        await plugin.on_job_start(native_job)
        await callbacks[0](
            SimpleNamespace(config=SimpleNamespace(trial_name="trial-1"))
        )

    asyncio.run(run())
    assert len(receiver.requests) == 2
    assert receiver.requests[0][0].endswith("/api/2.0/mlflow/experiments/search")
    assert receiver.requests[1][0] == "/tracer/v1/traces/v1/traces"
    assert all(
        "fi_api_key" not in {key.lower() for key in headers}
        for _, headers, _ in receiver.requests
    )


def test_native_cli_cannot_forward_fi_headers(job, receiver, tmp_path):
    receiver.responses = [(200, b'{"experiments":[{"experiment_id":"1"}]}', {})]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "OTEL_EXPORTER_OTLP_HEADERS": "FI_API_KEY=synthetic,FI_SECRET_KEY=synthetic",
        "HARBOR_TELEMETRY": "false",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    run = subprocess.run(
        [
            str(Path(sys.executable).parent / "harbor"),
            "traces",
            "export",
            "--path",
            str(job),
            "--format",
            "otel",
            "--endpoint",
            receiver.endpoint,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(receiver.requests) == 2
    assert receiver.requests[0][0].endswith("/api/2.0/mlflow/experiments/search")
    assert all(
        "fi_secret_key" not in {key.lower() for key in headers}
        for _, headers, _ in receiver.requests
    )


def test_atomic_manifest_crash_after_acceptance(job, receiver, monkeypatch):
    original = os.replace

    def fail(*args, **kwargs):
        raise OSError("synthetic crash before atomic acknowledgement")

    monkeypatch.setattr(os, "replace", fail)
    assert import_job(job, settings=receiver.settings).failed == 1
    assert len(receiver.requests) == 1
    monkeypatch.setattr(os, "replace", original)
    assert import_job(job, settings=receiver.settings).sent == 1
    assert (
        len(receiver.requests) == 2
    )  # Documented crash-after-acceptance duplicate window.
    assert receiver.requests[0][2] == receiver.requests[1][2]
    assert import_job(job, settings=receiver.settings).skipped == 1


def test_corrupt_acknowledgement_fails_closed(job, receiver):
    assert import_job(job, settings=receiver.settings).sent == 1
    manifests = list((job / ".traceai-harbor").rglob("*.json"))
    assert manifests
    manifests[0].write_text("{")
    assert import_job(job, settings=receiver.settings).failed == 1
    assert len(receiver.requests) == 1


def test_content_change_conflicts_even_when_metadata_unchanged(job, receiver):
    assert import_job(job, settings=receiver.settings).sent == 1
    path = job / "trial-1" / "agent" / "trajectory.json"
    data = json.loads(path.read_text())
    data["steps"][0]["message"] = "different private content"
    path.write_text(json.dumps(data))
    assert import_job(job, settings=receiver.settings).conflicts == 1
    assert len(receiver.requests) == 1


def test_process_lock_and_resume(job, receiver, tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "FI_API_KEY": receiver.settings.api_key,
        "FI_SECRET_KEY": receiver.settings.secret_key,
        "FI_PROJECT_NAME": receiver.settings.project_name,
        "FI_HARBOR_ENDPOINT": receiver.endpoint,
    }
    command = [
        str(Path(sys.executable).parent / "traceai-harbor"),
        "import-job",
        str(job),
    ]
    processes = [
        subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(3)
    ]
    summaries = []
    for process in processes:
        out, err = process.communicate(timeout=50)
        assert process.returncode == 0, out + err
        summaries.append(json.loads(out))
    assert sum(item["sent"] for item in summaries) == 1
    assert sum(item["skipped"] for item in summaries) == 2
    assert len(receiver.requests) == 1


def test_transport_timeout_redacted(receiver, monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("PRIVATE FAILURE")

    monkeypatch.setattr(httpx.Client, "send", timeout)
    with pytest.raises(DeliveryError, match="^transport_error$"):
        send(b"", receiver.settings)


def test_numeric_overflow_and_hardlink(job, receiver, tmp_path):
    with pytest.raises(ArtifactError):
        parse(b'{"value":1e999}')
    file = job / "trial-1" / "agent" / "trajectory.json"
    file.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{"private":"NEVER_READ"}')
    os.link(outside, file)
    assert import_job(job, settings=receiver.settings).incomplete == 1
    assert b"NEVER_READ" not in receiver.requests[0][2]


def test_plugin_failure_is_observational(job, receiver, monkeypatch, caplog):
    for key, value in {
        "FI_API_KEY": receiver.settings.api_key,
        "FI_SECRET_KEY": receiver.settings.secret_key,
        "FI_HARBOR_ENDPOINT": receiver.endpoint,
    }.items():
        monkeypatch.setenv(key, value)
    before = (job / "trial-1" / "result.json").read_bytes()
    receiver.responses = [(401, b"PRIVATE FAILURE", {})] * 2
    callbacks = []

    async def run():
        plugin = FutureAGIPlugin()
        await plugin.on_job_start(
            SimpleNamespace(job_dir=job, on_trial_ended=callbacks.append)
        )
        await callbacks[0](
            SimpleNamespace(trial_name="trial-1", config=SimpleNamespace(job_id=None))
        )
        await plugin.on_job_end(None)
        return plugin

    plugin = asyncio.run(run())
    assert plugin.summary.failed == 1
    assert (job / "trial-1" / "result.json").read_bytes() == before
    assert "delivery incomplete" in caplog.text
    assert (
        "PRIVATE" not in caplog.text and receiver.settings.secret_key not in caplog.text
    )
