import copy
import json
import shutil
from dataclasses import replace
from uuid import uuid4

from traceai_harbor import import_job
from traceai_harbor._privacy import Privacy


def test_bounded_structured_redaction(receiver):
    privacy = Privacy(receiver.settings)
    secret = "PRIVATE-PASSWORD"
    for value in [
        json.dumps({"password": secret, "padding": "x" * 20000}),
        json.dumps({"nested": json.dumps({"api_key": secret})}),
        '{"password":"' + secret,
        json.dumps({"nested": {"authorization": secret}}),
    ]:
        assert secret not in privacy.tree(value)
    assert len(privacy.text("é" * 10000).encode()) <= 2048


def test_expansion_budget(job, receiver, trajectory):
    template = copy.deepcopy(trajectory["steps"][1])
    trajectory["steps"] = [{**template, "step_id": i} for i in range(2100)]
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    result = import_job(job, settings=receiver.settings)
    assert result.sent == 1 and result.incomplete == 1


def test_many_trials_streamed_and_sharded(job, receiver):
    source = job / "trial-1"
    # Enough trials to exercise linear per-record manifest IO and reuse of the
    # same bounded conversion budget; no job-sized protobuf or ack dict exists.
    for i in range(128):
        name = f"trial-{i + 2}"
        target = job / name
        shutil.copytree(source, target)
        file = target / "result.json"
        data = json.loads(file.read_text())
        data.update(id=str(uuid4()), trial_name=name)
        data["config"]["trial_name"] = name
        file.write_text(json.dumps(data))
    result = import_job(job, settings=receiver.settings)
    assert result.ok and result.sent == 129
    manifests = list((job / ".traceai-harbor").rglob("*.json"))
    assert len(manifests) == 129
    assert max(p.stat().st_size for p in manifests) < 256
    assert import_job(job, settings=receiver.settings).skipped == 129
    assert len(receiver.requests) == 129


def test_manifest_symlink_fails_closed(job, receiver, tmp_path):
    outside = tmp_path / "private-state"
    outside.mkdir()
    (job / ".traceai-harbor").symlink_to(outside, target_is_directory=True)
    assert not import_job(job, settings=receiver.settings).ok
    assert receiver.requests == []
    assert list(outside.iterdir()) == []


def test_destination_endpoint_and_credentials_isolation(job, receiver):
    assert import_job(job, settings=receiver.settings).sent == 1
    for settings in [
        replace(receiver.settings, endpoint=receiver.endpoint + "/alternate"),
        replace(receiver.settings, api_key="different-synthetic-account"),
        replace(receiver.settings, capture_content=True),
    ]:
        assert import_job(job, settings=settings).sent == 1
    assert len(receiver.requests) == 4


def test_partial_job_resumes_only_failed_trials(job, receiver):
    second = job / "trial-2"
    shutil.copytree(job / "trial-1", second)
    file = second / "result.json"
    data = json.loads(file.read_text())
    data.update(id=str(uuid4()), trial_name="trial-2")
    data["config"]["trial_name"] = "trial-2"
    file.write_text(json.dumps(data))
    receiver.responses = [(401, b"", {}), (200, b"", {})]
    result = import_job(job, settings=receiver.settings)
    assert result.sent == 1 and result.failed == 1
    result = import_job(job, settings=receiver.settings)
    assert result.sent == 1 and result.skipped == 1
    assert len(receiver.requests) == 3
