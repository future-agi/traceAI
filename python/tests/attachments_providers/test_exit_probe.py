"""Lifecycle: an instrumented process exits promptly with attachments enabled, disabled, and after fork."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

pytest.importorskip("openai")
pytest.importorskip("traceai_openai")

PROBE = textwrap.dedent("""
    import base64, os, ssl, sys, threading
    sys.path.insert(0, os.environ["PROBE_DIR"])
    from conftest import png
    import openai
    from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, create_attachment_manager
    from fi_instrumentation.attachments.protocol import HTTPTransport
    from opentelemetry.sdk.trace import TracerProvider
    from traceai_openai import OpenAIInstrumentor

    enabled = os.environ["PROBE_ENABLED"] == "1"
    before = threading.active_count()
    if enabled:
        transport = HTTPTransport(os.environ["PROBE_F01"], {"X-API-Key": "SYNTHETIC_KEY"},
                                  ssl_context=ssl.create_default_context(cafile=os.environ["PROBE_CERT"]))
        manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic",
                                    transport=transport)
        assert manager.ready(5)
    else:
        manager = create_attachment_manager(config=AttachmentConfig(enabled=False))
    OpenAIInstrumentor().instrument(tracer_provider=TracerProvider(), attachments=manager)
    client = openai.OpenAI(api_key="sk-synthetic", base_url=os.environ["PROBE_PROVIDER"] + "/v1", max_retries=0)
    image = "data:image/png;base64," + base64.b64encode(png(seed=41)).decode()
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": image}}]}])
    if not enabled:
        assert threading.active_count() == before, "disabled attachments started a thread"
    if os.environ.get("PROBE_FORK") == "1" and hasattr(os, "fork"):
        pid = os.fork()
        if pid == 0:
            client.chat.completions.create(model="m", messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image}}]}])
            os._exit(0)
        os.waitpid(pid, 0)
    print("PROBE_DONE", flush=True)
    # No explicit flush or shutdown: interpreter exit must not hang.
""")


@pytest.mark.parametrize("enabled,fork", [(True, False), (False, False), (True, True)])
def test_process_exits_promptly(f01, provider, tmp_path, enabled, fork):
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    env = dict(os.environ, PROBE_DIR=os.path.dirname(__file__), PROBE_ENABLED="1" if enabled else "0",
               PROBE_F01=f01.endpoint, PROBE_CERT=str(f01.cert), PROBE_PROVIDER=provider.url,
               PROBE_FORK="1" if fork else "0")
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr[-2000:]
    assert "PROBE_DONE" in result.stdout
    assert elapsed < 30, elapsed
    if not enabled:
        assert f01.state["requests"] == []
