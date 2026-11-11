"""Synthetic cancellation probe executed in a separate interpreter by pytest."""

import asyncio
import tempfile
from pathlib import Path

from conftest import TransportDouble
from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager
from test_livekit_attachments import owned_session
from traceai_livekit import bind_session


async def main():
    wire = TransportDouble()
    wire.release.clear()
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=wire)
    assert manager.ready(2)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        session, job, _, span, provider, calls = owned_session(root)
        (root / "audio.ogg").write_bytes(b"synthetic")
        binding = bind_session(session, job, manager=manager, owned_recording_files=True, finalization_timeout=30)
        task = asyncio.create_task(job._on_session_end())
        for _ in range(200):
            if wire.entered.is_set():
                break
            await asyncio.sleep(.01)
        assert wire.entered.is_set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert binding.status == "incomplete" and calls == ["original"]
        assert binding._scope is None
        wire.release.set()
        manager.shutdown(.2)
        span.end()
        provider.shutdown()
    print("bounded-cancel-exit")


if __name__ == "__main__":
    asyncio.run(main())
