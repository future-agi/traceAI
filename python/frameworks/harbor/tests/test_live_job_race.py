import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

from traceai_harbor import FutureAGIPlugin


def test_live_hook_during_job_result_rewrite(job, receiver, monkeypatch):
    for key, value in {
        "FI_API_KEY": receiver.settings.api_key,
        "FI_SECRET_KEY": receiver.settings.secret_key,
        "FI_HARBOR_ENDPOINT": receiver.endpoint,
    }.items():
        monkeypatch.setenv(key, value)
    result_file = job / "result.json"
    original = result_file.read_bytes()
    job_id = UUID(json.loads(original)["id"])
    callbacks = []

    async def run():
        plugin = FutureAGIPlugin()
        await plugin.on_job_start(
            SimpleNamespace(job_dir=job, on_trial_ended=callbacks.append)
        )
        # Harbor _write_job_result uses write_text while other trial hooks run.
        # Model the truncate/write window deterministically, without sleeps.
        result_file.write_text("")
        await callbacks[0](
            SimpleNamespace(trial_name="trial-1", config=SimpleNamespace(job_id=job_id))
        )
        assert len(receiver.requests) == 1
        result_file.write_bytes(original)
        await plugin.on_job_end(None)
        return plugin

    plugin = asyncio.run(run())
    assert plugin.summary.ok and plugin.summary.skipped == 1
    assert len(receiver.requests) == 1
