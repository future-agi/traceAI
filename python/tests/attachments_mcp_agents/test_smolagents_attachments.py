"""Real smolagents (OpenAIServerModel on a loopback OpenAI double, real Tool and agent loop) -> loopback F01."""

import io

import pytest

smolagents = pytest.importorskip("smolagents")
PIL_Image = pytest.importorskip("PIL.Image")
pytest.importorskip("openai")
pytest.importorskip("traceai_smolagents")

from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from traceai_smolagents import SmolagentsInstrumentor  # noqa: E402

from _harness import flush, harness  # noqa: E402

provider = harness.provider  # the loopback OpenAI-compatible API (fixture)


def picture(seed=0, size=(8, 6)):
    image = PIL_Image.new("RGB", size, (seed % 256, 10, 20))
    return image


def png_of(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        SmolagentsInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                            config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        SmolagentsInstrumentor().uninstrument()


def model(provider):
    return smolagents.OpenAIServerModel(model_id="m", api_base=provider.url + "/v1", api_key="SYNTHETIC_KEY")


def user_message(*images):
    content = [{"type": "text", "text": "look"}] + [{"type": "image", "image": image} for image in images]
    if not hasattr(smolagents.OpenAIServerModel, "generate"):
        return {"role": "user", "content": content}  # releases before generate() take plain dicts
    from smolagents.models import ChatMessage, MessageRole

    return ChatMessage(role=MessageRole.USER, content=content)


def call(m, messages):
    method = getattr(m, "generate", None) or m.__call__
    return method(messages)


def test_model_input_images_upload_once_on_the_model_span(f01, instrument, provider):
    manager = f01.manager()
    exporter = instrument(manager)
    shot = picture(1)
    m = model(provider)
    call(m, [user_message(shot)])
    call(m, [user_message(shot)])  # memory replay of the same observation image
    flush(manager)
    assert [(u["mime_type"], u["direction"], u["purpose"]) for u in f01.uploads] == [
        ("image/png", "input", "model_input")]
    assert f01.payloads == [png_of(shot)]
    # The model still received the image.
    assert any(b"data:image/png;base64," in raw for _, _, raw in provider.requests)
    llm = [s for s in exporter.get_finished_spans() if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert llm and harness.refs(llm[0])
    manager.shutdown(5)


def test_tool_returned_images_upload_on_the_tool_span(f01, instrument):
    manager = f01.manager()
    exporter = instrument(manager)
    raw, wrapped_image = picture(2), picture(3)

    class Snap(smolagents.Tool):
        name = "snap"
        description = "Take a picture."
        inputs = {"which": {"type": "string", "description": "which"}}
        output_type = "image"

        def forward(self, which):
            if which == "raw":
                return raw
            return smolagents.AgentImage(wrapped_image)

    tool = Snap()
    tool(which="raw", sanitize_inputs_outputs=False)
    tool(which="agent", sanitize_inputs_outputs=False)
    flush(manager)
    assert [(u["mime_type"], u["direction"], u["purpose"]) for u in f01.uploads] == [
        ("image/png", "output", "tool_result")] * 2
    assert sorted(f01.payloads) == sorted([png_of(raw), png_of(wrapped_image)])
    tools = [s for s in exporter.get_finished_spans() if s.attributes.get("gen_ai.span.kind") == "TOOL"]
    assert sum(len(harness.refs(s)) for s in tools) == 2
    manager.shutdown(5)


def test_file_backed_and_audio_values_are_never_read(f01, instrument, tmp_path):
    manager = f01.manager()
    instrument(manager)
    path = tmp_path / "secret.png"
    picture(4).save(path)

    class Files(smolagents.Tool):
        name = "files"
        description = "Return files."
        inputs = {"which": {"type": "string", "description": "which"}}
        output_type = "image"

        def forward(self, which):
            if which == "agent-path":
                return smolagents.AgentImage(str(path))
            return PIL_Image.open(path)

    tool = Files()
    tool(which="agent-path", sanitize_inputs_outputs=False)
    tool(which="opened", sanitize_inputs_outputs=False)
    flush(manager)
    assert f01.uploads == []
    manager.shutdown(5)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_images", "pii_redaction"])
def test_local_restrictions_deny_model_images(f01, instrument, provider, flag):
    manager = f01.manager()
    before = len(f01.after_bootstrap())
    instrument(manager, TraceConfig(**{flag: True}))
    call(model(provider), [user_message(picture(5))])
    flush(manager)
    assert f01.uploads == [] and len(f01.after_bootstrap()) == before
    manager.shutdown(5)


def test_agent_run_with_task_images_uploads_each_image_once(f01, instrument, provider):
    manager = f01.manager()
    instrument(manager)
    agent = smolagents.ToolCallingAgent(tools=[], model=model(provider), max_steps=1)
    try:
        agent.run("describe", images=[picture(6)])
    except Exception:
        pass  # the double never calls a tool; the loop's own errors are not under test
    flush(manager)
    assert [u["purpose"] for u in f01.uploads] == ["model_input"]
    manager.shutdown(5)


def test_disabled_manager_uploads_nothing(f01, instrument, provider):
    instrument(create_attachment_manager(config=AttachmentConfig()))
    call(model(provider), [user_message(picture(7))])
    assert f01.uploads == []


def test_every_model_call_carrying_a_captured_image_is_marked_owner(f01, instrument, provider, monkeypatch):
    """A replayed memory image is already uploaded: the provider adapter below must not upload it again."""
    import openai.resources.chat.completions as completions
    from fi_instrumentation.attachments import tool_media
    from opentelemetry import context as otel_context

    marks = []
    real = completions.Completions.create

    def create(self, *args, **kwargs):
        marks.append(otel_context.get_value(tool_media.OWNER_KEY))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(completions.Completions, "create", create)
    manager = f01.manager()
    instrument(manager)
    shot = picture(11)
    m = model(provider)
    call(m, [user_message(shot)])
    call(m, [user_message(shot)])  # memory replay: the same image object goes to the model again
    call(m, [user_message()])  # no image: no mark
    flush(manager)
    assert marks == [True, True, None]
    assert len(f01.uploads) == 1
    assert sum(raw.count(b"data:image/png;base64,") for _, _, raw in provider.requests) == 2
    manager.shutdown(5)


def test_seen_table_is_fresh_in_a_forked_child_even_if_its_lock_was_held():
    import os
    import signal
    import threading

    from traceai_smolagents import _attachments as sa

    if not hasattr(os, "fork"):
        pytest.skip("no fork")
    held, release = threading.Event(), threading.Event()

    def holder():
        with sa._SEEN._owned_lock():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    held.wait(5)

    class Manager:
        pass

    keep = Manager()
    pid = os.fork()
    if pid == 0:  # child: must not block on the lock the parent's thread held at fork
        signal.alarm(5)
        try:
            image = picture(1)
            sa._SEEN.add(keep, image)
            os._exit(0 if (keep, image) in sa._SEEN else 4)
        except BaseException:
            os._exit(3)
    release.set()
    thread.join()
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
