import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
import traceai_harbor._importer as importer
from conftest import attrs, spans
from traceai_harbor import FutureAGIPlugin, import_job


def test_cancelled_hook_flushes_without_duplicate(job, receiver, monkeypatch):
    for key, value in {
        "FI_API_KEY": receiver.settings.api_key,
        "FI_SECRET_KEY": receiver.settings.secret_key,
        "FI_HARBOR_ENDPOINT": receiver.endpoint,
    }.items():
        monkeypatch.setenv(key, value)
    entered, release = threading.Event(), threading.Event()
    original = importer.send

    def blocked(payload, settings):
        entered.set()
        assert release.wait(5)
        original(payload, settings)

    monkeypatch.setattr(importer, "send", blocked)

    async def run():
        callbacks = []
        plugin = FutureAGIPlugin()
        await plugin.on_job_start(
            SimpleNamespace(job_dir=job, on_trial_ended=callbacks.append)
        )
        callback = asyncio.create_task(
            callbacks[0](SimpleNamespace(trial_name="trial-1"))
        )
        assert await asyncio.to_thread(entered.wait, 5)
        callback.cancel()
        with pytest.raises(asyncio.CancelledError):
            await callback
        # Cancelling an await cannot cancel synchronous HTTP already in flight.
        # Final reconciliation waits on its file lock, then sees the saved ACK.
        release.set()
        await plugin.on_job_end(None)
        await callbacks[0](SimpleNamespace(trial_name="trial-1"))
        return plugin

    plugin = asyncio.run(run())
    assert plugin.summary.ok and plugin.summary.skipped == 1
    assert len(receiver.requests) == 1


def test_copied_context_only_does_not_account_twice(job, receiver, trajectory):
    for step in trajectory["steps"]:
        step["is_copied_context"] = True
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    result = import_job(job, settings=receiver.settings)
    assert result.sent == 1 and result.incomplete == 1
    assert not any(attrs(s)["fi.span.kind"] == "LLM" for s in spans(receiver))
    assert sum(attrs(s).get("llm.token_count.prompt", 0) for s in spans(receiver)) == 0
