"""Execute the README verbatim with real clients and a loopback OTLP sink."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

from traceai_tavily import TavilyInstrumentor

from _tavily_fake import TAVILY_KEY
from test_extended_operations import PRIVATE_OUTPUT, PRIVATE_URL, Transport
from test_extended_wire import receiver


def test_readme_snippets_run_end_to_end(monkeypatch):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    snippets = re.findall(r"```python\n(.*?)```", readme, re.S)
    assert len(snippets) == 3
    transport = Transport()
    original = requests.Session.send

    def send(session, request, **kwargs):
        if urlsplit(request.url).hostname == "api.tavily.com":
            return transport.sync_send(request, **kwargs)
        return original(session, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setenv("TAVILY_API_KEY", TAVILY_KEY)
    monkeypatch.setenv("FI_API_KEY", "synthetic-fi-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-fi-secret-key")
    namespace = {}
    with receiver() as (endpoint, records):
        monkeypatch.setenv("FI_BASE_URL", endpoint.rsplit("/v1/traces", 1)[0])
        try:
            for snippet in snippets:
                exec(compile(snippet, "README.md", "exec"), namespace)
        finally:
            TavilyInstrumentor().uninstrument()
            if "tracer_provider" in namespace:
                namespace["tracer_provider"].shutdown()
    spans = [
        span for _, payload in records for resource in payload["resourceSpans"]
        for scope in resource["scopeSpans"] for span in scope["spans"]
    ]
    assert [span["name"] for span in spans] == [
        "tavily.search", "tavily.research", "tavily.get_research", "tavily.research",
    ]
    assert [method for method, _ in transport.calls] == ["search", "research", "research", "research"]
    assert PRIVATE_OUTPUT in namespace["result"]["content"]
    for private in (TAVILY_KEY, PRIVATE_URL, PRIVATE_OUTPUT):
        assert private not in json.dumps(spans)
