"""Real firecrawl-py v2 clients against a loopback Firecrawl API -> loopback F01."""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("firecrawl")
pytest.importorskip("traceai_firecrawl")

from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from firecrawl import AsyncFirecrawl, Firecrawl  # noqa: E402
from traceai_firecrawl import FirecrawlInstrumentor  # noqa: E402

from _harness import data_uri, flush, png, refs, span_named  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "frameworks" / "firecrawl" / "tests"))
from _firecrawl_fake import FakeFirecrawl  # noqa: E402

SHOT = png(seed=61)
ACTION_SHOT = png(seed=62)
KEY = "fc-SYNTHETIC-PLACEHOLDER"


@pytest.fixture
def fake():
    server = FakeFirecrawl()
    yield server
    server.close()


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        FirecrawlInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                           config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        FirecrawlInstrumentor().uninstrument()


def page(screenshot, actions=None):
    data = {"markdown": "body", "screenshot": screenshot, "metadata": {"sourceURL": "https://example.com"}}
    if actions is not None:
        data["actions"] = {"screenshots": actions}
    return {"success": True, "data": data}


def test_inline_screenshots_upload_and_hosted_urls_are_never_fetched(f01, instrument, fake):
    manager = f01.manager()
    exporter = instrument(manager)
    hosted = fake.origin + "/hosted/screenshot.png"
    fake.routes[("POST", "/v2/scrape")] = (200, page(data_uri("image/png", SHOT), [data_uri("image/jpeg", ACTION_SHOT), hosted]))
    document = Firecrawl(api_key=KEY, api_url=fake.origin, max_retries=1).scrape("https://example.com", formats=["screenshot"])
    flush(manager)
    assert document.screenshot == data_uri("image/png", SHOT)  # the SDK result is unchanged
    assert sorted((u["mime_type"], u["direction"], u["purpose"]) for u in f01.uploads) == [
        ("image/jpeg", "output", "tool_result"), ("image/png", "output", "tool_result")]
    assert sorted(f01.payloads) == sorted([SHOT, ACTION_SHOT])
    assert fake.calls_to("GET", "/hosted/screenshot.png") == 0
    assert len(refs(span_named(exporter, "firecrawl.scrape"))) == 2
    manager.shutdown(5)


def test_hosted_only_screenshot_uploads_nothing(f01, instrument, fake):
    manager = f01.manager()
    instrument(manager)
    fake.routes[("POST", "/v2/scrape")] = (200, page(fake.origin + "/hosted/x.png"))
    Firecrawl(api_key=KEY, api_url=fake.origin, max_retries=1).scrape("https://example.com", formats=["screenshot"])
    flush(manager)
    assert f01.uploads == [] and fake.calls_to("GET", "/hosted/x.png") == 0
    manager.shutdown(5)


def test_async_client_and_crawl_documents(f01, instrument, fake):
    manager = f01.manager()
    instrument(manager)
    fake.routes[("POST", "/v2/scrape")] = (200, page(data_uri("image/png", SHOT)))

    async def go():
        return await AsyncFirecrawl(api_key=KEY, api_url=fake.origin, max_retries=1).scrape(
            "https://example.com", formats=["screenshot"])

    asyncio.run(go())
    flush(manager)
    assert f01.payloads == [SHOT]
    manager.shutdown(5)


@pytest.mark.parametrize("flag", ["hide_outputs", "hide_inputs", "pii_redaction"])
def test_local_restrictions_deny(f01, instrument, fake, flag):
    manager = f01.manager()
    before = len(f01.after_bootstrap())
    instrument(manager, TraceConfig(**{flag: True}))
    fake.routes[("POST", "/v2/scrape")] = (200, page(data_uri("image/png", SHOT)))
    Firecrawl(api_key=KEY, api_url=fake.origin, max_retries=1).scrape("https://example.com", formats=["screenshot"])
    flush(manager)
    assert f01.uploads == [] and len(f01.after_bootstrap()) == before
    manager.shutdown(5)


def test_disabled_manager_uploads_nothing(f01, instrument, fake):
    instrument(create_attachment_manager(config=AttachmentConfig()))
    fake.routes[("POST", "/v2/scrape")] = (200, page(data_uri("image/png", SHOT)))
    Firecrawl(api_key=KEY, api_url=fake.origin, max_retries=1).scrape("https://example.com", formats=["screenshot"])
    assert f01.uploads == []
