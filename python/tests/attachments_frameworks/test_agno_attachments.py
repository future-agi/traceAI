"""Real Agno agent + real OpenAI SDK (loopback) -> traceai_agno -> loopback F01.

Privacy is the intersection of the FI policy and the OpenInference TraceConfig.
"""

import pytest

pytest.importorskip("agno")
pytest.importorskip("openinference.instrumentation.agno")
pytest.importorskip("traceai_agno")

from conftest import PDF, b64, flush, png, refs, wav  # noqa: E402
from agno.agent import Agent  # noqa: E402
from agno.media import Audio, File, Image  # noqa: E402
from agno.models.openai import OpenAIChat  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from openinference.instrumentation import TraceConfig as OITraceConfig  # noqa: E402
from traceai_agno import AgnoInstrumentorWrapper, configure_agno_tracing  # noqa: E402

IMAGE = png(seed=51)


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    for key in ("OPENINFERENCE_HIDE_INPUTS", "OPENINFERENCE_HIDE_INPUT_IMAGES", "OPENINFERENCE_HIDE_OUTPUTS"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans

    def install(manager, config=None):
        configure_agno_tracing(tracer_provider=tracer_provider, attachments=manager, config=config)
        return exporter

    yield install
    AgnoInstrumentorWrapper().uninstrument()


def agent(provider):
    model = OpenAIChat(id="gpt-4o-mini", api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0)
    try:
        return Agent(model=model, telemetry=False)
    except TypeError:  # older Agno has no telemetry switch; AGNO_TELEMETRY=false covers it
        return Agent(model=model)


def model_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.endswith((".invoke", ".ainvoke"))]


def test_image_bytes_reach_f01_on_the_model_span(f01, provider, instrument):
    manager = f01.manager()
    exporter = instrument(manager)
    agent(provider).run("what is this", images=[Image(content=IMAGE, format="png")])
    flush(manager)
    assert f01.payloads == [IMAGE]
    (body,) = f01.uploads
    assert body["mime_type"] == "image/png" and body["direction"] == "input" and body["purpose"] == "model_input"
    (span,) = model_spans(exporter)
    assert [r["occurrence_id"] for r in refs(span)] == [body["occurrence_id"]]


def test_async_run_and_audio(f01, provider, instrument):
    import asyncio

    manager = f01.manager()
    instrument(manager)
    asyncio.run(agent(provider).arun("listen", audio=[Audio(content=wav(), format="wav")]))
    flush(manager)
    assert f01.payloads == [wav()]


def test_file_bytes_pdf(f01, provider, instrument):
    manager = f01.manager()
    instrument(manager)
    try:
        agent(provider).run("read", files=[File(content=PDF, mime_type="application/pdf", filename="a.pdf")])
    except Exception:
        pass  # the OpenAI chat formatter may reject files on some versions; capture happens before the call
    flush(manager)
    assert f01.payloads == [PDF]


@pytest.mark.parametrize("oi", [OITraceConfig(hide_input_images=True), OITraceConfig(hide_inputs=True),
                                OITraceConfig(hide_input_messages=True)])
def test_openinference_restriction_alone_denies(f01, provider, instrument, oi):
    manager = f01.manager()
    instrument(manager, config=oi)
    agent(provider).run("what is this", images=[Image(content=IMAGE, format="png")])
    flush(manager)
    assert f01.after_bootstrap() == []


@pytest.mark.parametrize("env", ["FI_HIDE_INPUT_IMAGES", "FI_HIDE_INPUTS", "FI_PII_REDACTION"])
def test_fi_restriction_alone_denies_even_with_permissive_oi_config(f01, provider, instrument, monkeypatch, env):
    monkeypatch.setenv(env, "true")
    manager = f01.manager()
    instrument(manager, config=OITraceConfig(hide_input_images=False, hide_inputs=False))
    agent(provider).run("what is this", images=[Image(content=IMAGE, format="png")])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_openinference_env_restriction_denies(f01, provider, instrument, monkeypatch):
    monkeypatch.setenv("OPENINFERENCE_HIDE_INPUT_IMAGES", "true")
    manager = f01.manager()
    instrument(manager)
    agent(provider).run("what is this", images=[Image(content=IMAGE, format="png")])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_url_and_filepath_are_never_read(f01, provider, instrument, tmp_path):
    manager = f01.manager()
    instrument(manager)
    path = tmp_path / "secret.png"
    path.write_bytes(IMAGE)
    run = agent(provider)
    for image in (Image(url="http://127.0.0.1:9/never.png"), Image(filepath=str(path))):
        try:
            run.run("what is this", images=[image])
        except Exception:
            pass  # Agno itself may try to load the source for the provider; the instrumentation never does
    flush(manager)
    assert f01.payloads == []


def test_mismatched_bytes_and_disabled_manager(f01, provider, instrument):
    instrument(create_attachment_manager(config=AttachmentConfig(enabled=False)))
    agent(provider).run("x", images=[Image(content=IMAGE, format="png")])
    AgnoInstrumentorWrapper().uninstrument()
    manager = f01.manager()
    instrument(manager)
    agent(provider).run("x", images=[Image(content=b"GIF89a not a png at all", format="png")])
    flush(manager)
    assert f01.payloads == []
