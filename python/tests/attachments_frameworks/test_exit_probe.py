"""Lifecycle: a framework-instrumented process exits promptly with attachments enabled, disabled, after fork."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

PROBE = textwrap.dedent("""
    import base64, os, ssl, sys, threading
    sys.path.insert(0, os.environ["PROBE_DIR"])
    from conftest import png
    from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, create_attachment_manager
    from fi_instrumentation.attachments.protocol import HTTPTransport
    from opentelemetry.sdk.trace import TracerProvider

    enabled = os.environ["PROBE_ENABLED"] == "1"
    framework = os.environ["PROBE_FRAMEWORK"]
    def attachment_threads():
        return {t.ident for t in threading.enumerate() if t.name.startswith("fi-attachment")}
    before = attachment_threads()
    if enabled:
        transport = HTTPTransport(os.environ["PROBE_F01"], {"X-API-Key": "SYNTHETIC_KEY"},
                                  ssl_context=ssl.create_default_context(cafile=os.environ["PROBE_CERT"]))
        manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic",
                                    transport=transport)
        assert manager.ready(5)
    else:
        manager = create_attachment_manager(config=AttachmentConfig(enabled=False))
    image = png(seed=71)

    if framework == "langchain":
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.messages import HumanMessage
        from traceai_langchain import LangChainInstrumentor
        LangChainInstrumentor().instrument(tracer_provider=TracerProvider(), attachments=manager)
        model = FakeListChatModel(responses=["ok", "ok"])
        def call():
            model.invoke([HumanMessage(content=[{"type": "image", "source_type": "base64",
                                                 "data": base64.b64encode(image).decode(),
                                                 "mime_type": "image/png"}])])
    else:
        from pydantic_ai import Agent, BinaryContent
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.models.function import FunctionModel
        from traceai_pydantic_ai import PydanticAIInstrumentor
        PydanticAIInstrumentor().instrument(tracer_provider=TracerProvider(), attachments=manager,
                                            skip_dep_check=True)
        agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")])))
        def call():
            agent.run_sync(["x", BinaryContent(data=image, media_type="image/png")])

    call()
    if not enabled:
        # Frameworks may start their own threads (event loops, executors); attachments must not.
        assert attachment_threads() == before, "disabled attachments started a thread"
    if os.environ.get("PROBE_FORK") == "1" and hasattr(os, "fork"):
        pid = os.fork()
        if pid == 0:
            call()
            os._exit(0)
        os.waitpid(pid, 0)
    print("PROBE_DONE", flush=True)
    # No explicit flush or shutdown: interpreter exit must not hang.
""")


def _available(framework):
    module = {"langchain": "traceai_langchain", "pydantic_ai": "traceai_pydantic_ai"}[framework]
    try:
        __import__(module)
        __import__("langchain_core" if framework == "langchain" else "pydantic_ai")
        return True
    except ImportError:
        return False


@pytest.mark.parametrize("framework", ["langchain", "pydantic_ai"])
@pytest.mark.parametrize("enabled,fork", [(True, False), (False, False), (True, True)])
def test_process_exits_promptly(f01, tmp_path, framework, enabled, fork):
    if not _available(framework):
        pytest.skip(framework + " not installed")
    if fork and framework == "pydantic_ai":
        # run_sync in a forked child hangs with NO instrumentation at all (pydantic-ai 0.1.0 on a GCP VM);
        # the fork row is covered by LangChain.
        pytest.skip("pydantic-ai run_sync is not fork-safe on its own")
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    env = dict(os.environ, PROBE_DIR=os.path.dirname(__file__), PROBE_ENABLED="1" if enabled else "0",
               PROBE_F01=f01.endpoint, PROBE_CERT=str(f01.cert), PROBE_FORK="1" if fork else "0",
               PROBE_FRAMEWORK=framework)
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr[-2000:]
    assert "PROBE_DONE" in result.stdout
    assert elapsed < 30, elapsed
    if not enabled:
        assert f01.state["requests"] == []
