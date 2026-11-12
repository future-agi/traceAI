"""Auto with the provider-media (#697) and framework (#691) adapters: real SDKs, loopback F01.

- report() rows equal each INSTALLED integration's own descriptor, and every integration
  whose installed version declares none is unqualified;
- LiteLLM over the OpenAI instrumentor, both enabled through traceai.init, uploads the
  call's image ONCE, on the LiteLLM span (LiteLLM's owner mark governs the nested call);
- with attachments OFF, the same call never reaches F01.

Rows that need a core with ``provider_media`` skip on an older core unless
TRACEAI_EXPECT_PROVIDER_ADAPTERS=1, which turns the skip into a failure.
"""

import importlib
import importlib.util
import os

import pytest

traceai = pytest.importorskip("traceai")

from conftest import b64, flush, png, refs  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
EXPECT = os.environ.get("TRACEAI_EXPECT_PROVIDER_ADAPTERS") == "1"

# report row name -> (instrumentor package, descriptor module, descriptor name). Independent of
# traceai._MEDIA_ADAPTERS on purpose: a wrong module or name there fails these rows.
PROVIDERS = {
    "google_genai": ("traceai_google_genai", "traceai_google_genai", "google_genai_attachment_capabilities"),
    "vertexai": ("traceai_vertexai", "traceai_vertexai", "vertexai_attachment_capabilities"),
    "bedrock": ("traceai_bedrock", "traceai_bedrock", "bedrock_attachment_capabilities"),
    "litellm": ("traceai_litellm", "traceai_litellm", "litellm_attachment_capabilities"),
    "groq": ("traceai_groq", "traceai_groq._audio", "groq_attachment_capabilities"),
    "fireworks": ("traceai_fireworks", "traceai_fireworks", "fireworks_attachment_capabilities"),
    "xai": ("traceai_xai", "traceai_xai", "xai_attachment_capabilities"),
    "mistralai": ("traceai_mistralai", "traceai_mistralai", "mistralai_attachment_capabilities"),
    "openrouter": ("traceai_openrouter", "traceai_openrouter", "openrouter_attachment_capabilities"),
    "portkey": ("traceai_portkey", "traceai_portkey", "portkey_attachment_capabilities"),
    "ollama": ("traceai_ollama", "traceai_ollama", "ollama_attachment_capabilities"),
    "vllm": ("traceai_vllm", "traceai_vllm", "vllm_attachment_capabilities"),
    "minimax": ("traceai_minimax", "traceai_minimax", "minimax_attachment_capabilities"),
}
FRAMEWORKS = {
    "agno": ("traceai_agno", "traceai_agno._attachments", "agno_attachment_capabilities"),
    "pydantic_ai": ("traceai_pydantic_ai", "traceai_pydantic_ai._attachments", "pydantic_ai_attachment_capabilities"),
    "langchain": ("traceai_langchain", "traceai_langchain._attachments", "langchain_attachment_capabilities"),
    "llamaindex": ("traceai_llamaindex", "traceai_llamaindex._attachments", "llamaindex_attachment_capabilities"),
}
UNCLAIMED = {"together": "traceai_together", "deepseek": "traceai_deepseek", "cerebras": "traceai_cerebras"}


def _installed(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _declared(path, name):
    """The installed version's own descriptor operations, or None when it declares none."""
    try:
        return list(getattr(importlib.import_module(path), name)["operations"])
    except Exception:
        return None


@pytest.fixture
def provider_media():
    try:
        return importlib.import_module("fi_instrumentation.attachments.provider_media")
    except ImportError:
        if EXPECT:
            raise
        pytest.skip("this core has no provider_media")


@pytest.fixture
def auto_state(monkeypatch):
    for name, value in (("_tracer_provider", None), ("_init_options", None), ("_instrumented", set()),
                        ("_adapter_status", {}), ("_attachment_manager_borrowed", False)):
        monkeypatch.setattr(traceai, name, value)
    yield
    for module, name in (("traceai_openai", "OpenAIInstrumentor"), ("traceai_litellm", "LiteLLMInstrumentor")):
        try:
            instrumentor = getattr(importlib.import_module(module), name)()
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
        except ImportError:
            pass


def _rows():
    return {row["name"]: row for row in traceai.report()["integrations"]}


def test_report_rows_equal_each_installed_descriptor(provider_media, auto_state):
    rows, checked = _rows(), []
    for name, (package, path, attr) in {**PROVIDERS, **FRAMEWORKS}.items():
        row = rows[name]
        assert row["package"] == package
        if not _installed(package):
            assert (row["status"], row["media"]) == ("not_installed", "unqualified"), name
            continue
        declared = _declared(path, attr)
        if not declared:
            assert row["media"] == "unqualified" and "media_operations" not in row, name
            continue
        assert (row["media"], row["media_operations"]) == ("provider_adapter", declared), name
        if name in PROVIDERS and name != "groq":
            # The package's lazy descriptor IS the core's matrix row for it.
            assert declared == list(provider_media.capabilities(package)["operations"]), name
        checked.append(name)
    for name, package in UNCLAIMED.items():
        assert rows[name]["media"] == "unqualified", name
        assert list(provider_media.capabilities(package)["operations"]) == [], name
    # A VM run names the rows it installed, so a silently missing package cannot pass.
    expected = [n for n in os.environ.get("TRACEAI_EXPECT_MEDIA_ROWS", "").split(",") if n]
    assert sorted(set(expected) - set(checked)) == [], checked


def test_rows_without_a_declared_descriptor_are_unqualified(auto_state):
    """Runs on every core: an installed version that declares no descriptor is unqualified."""
    rows = _rows()
    for name, (package, path, attr) in {**PROVIDERS, **FRAMEWORKS}.items():
        if _installed(package) and not _declared(path, attr):
            assert rows[name]["media"] == "unqualified", name
    for name in UNCLAIMED:
        assert rows[name]["media"] == "unqualified", name


def _init(f01, monkeypatch, libraries, enabled=True):
    monkeypatch.setenv("SSL_CERT_FILE", str(f01.cert))
    monkeypatch.setenv("FI_API_KEY", "SYNTHETIC_KEY")
    monkeypatch.setenv("FI_SECRET_KEY", "SYNTHETIC_SECRET")
    options = {"attachment_config": AttachmentConfig(enabled=True, endpoint=f01.endpoint)} if enabled else {}
    tracer_provider = traceai.init(project_name="synthetic", project_type="observe", auto_instrument=libraries,
                                   verbose=False, set_global_tracer_provider=False, **options)
    exporter = InMemorySpanExporter()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracer_provider, exporter


def _litellm_image(litellm, provider, image):
    litellm.completion(model="openai/gpt-synthetic", api_base=provider.url + "/v1", api_key="sk-synthetic",
                       messages=[{"role": "user", "content": [
                           {"type": "text", "text": "what is this"},
                           {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(image)}}]}])


def _litellm(provider_media):
    litellm = pytest.importorskip("litellm")
    pytest.importorskip("traceai_openai")
    traceai_litellm = pytest.importorskip("traceai_litellm")
    if not _declared("traceai_litellm", "litellm_attachment_capabilities"):
        if EXPECT:
            pytest.fail("traceai_litellm declares no media descriptor")
        pytest.skip("this traceai_litellm has no media adapter")
    return litellm, traceai_litellm


def _sent_image(provider, image):
    return [path for path, body, raw in provider.requests if b64(image).encode() in raw]


@pytest.mark.parametrize("libraries", [["openai", "litellm"], ["litellm", "openai"]])
def test_auto_litellm_over_openai_uploads_once(f01, provider, provider_media, auto_state, monkeypatch, libraries):
    litellm, _ = _litellm(provider_media)
    tracer_provider, exporter = _init(f01, monkeypatch, libraries)
    manager = traceai.get_attachment_manager()
    try:
        assert manager.ready(5), manager.status()
        rows = _rows()
        assert (rows["litellm"]["status"], rows["litellm"]["media"]) == ("registered", "provider_adapter")
        assert (rows["openai"]["status"], rows["openai"]["media"]) == ("registered", "provider_adapter")
        image = png(seed=71)
        _litellm_image(litellm, provider, image)
        flush(manager)
        assert len(_sent_image(provider, image)) == 1  # the nested OpenAI SDK call sent the image
        assert f01.payloads == [image]
        owners = [span.name for span in exporter.get_finished_spans() for _ in refs(span)]
        assert owners == ["completion"], owners
        # The OpenAI instrumentor traced the nested call, and captured nothing for it.
        nested = [span for span in exporter.get_finished_spans() if span.name != "completion"]
        assert nested and all(not refs(span) for span in nested), [s.name for s in exporter.get_finished_spans()]
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


def test_control_openai_alone_captures_the_litellm_call(f01, provider, provider_media, auto_state, monkeypatch):
    """Without the LiteLLM instrumentor, the OpenAI one owns the same bytes: the claim above matters."""
    litellm, _ = _litellm(provider_media)
    tracer_provider, exporter = _init(f01, monkeypatch, ["openai"])
    manager = traceai.get_attachment_manager()
    try:
        assert manager.ready(5), manager.status()
        image = png(seed=72)
        _litellm_image(litellm, provider, image)
        flush(manager)
        assert f01.payloads == [image]
        owners = [span.name for span in exporter.get_finished_spans() for _ in refs(span)]
        assert len(owners) == 1 and owners != ["completion"], owners
    finally:
        manager.shutdown(5)
        tracer_provider.shutdown()


@pytest.mark.parametrize("off", ["default", "kill_switch"])
def test_auto_litellm_over_openai_off_never_reaches_f01(f01, provider, provider_media, auto_state, monkeypatch,
                                                        off):
    litellm, _ = _litellm(provider_media)
    if off == "kill_switch":
        monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    tracer_provider, exporter = _init(f01, monkeypatch, ["openai", "litellm"], enabled=(off == "kill_switch"))
    try:
        assert traceai.report()["attachments"]["state"] == "disabled"
        image = png(seed=73)
        _litellm_image(litellm, provider, image)
        assert len(_sent_image(provider, image)) == 1
        assert f01.state["requests"] == []  # not even a session bootstrap
        assert all(not refs(span) for span in exporter.get_finished_spans())
        assert {span.name for span in exporter.get_finished_spans()} >= {"completion"}
    finally:
        tracer_provider.shutdown()
