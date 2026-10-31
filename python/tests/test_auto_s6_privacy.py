"""S6 catalog conformance and S7-pre source-atomic credentials (synthetic only)."""

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from fi_instrumentation import TraceConfig, _secrets
from fi_instrumentation._privacy_catalog import CATALOG, CATALOG_PATH, PRIVACY, credential_name
from fi_instrumentation.auto._config import resolve
from fi_instrumentation.auto.wire import _privacy
from test_auto_round4_privacy_boundary import snapshot
from test_auto_manager import fresh_state  # noqa: F401
from test_auto_round5_registry import echo_server, install_agent, assert_process_exports
from test_auto_safety import CHAT

SPEC = Path(__file__).resolve().parents[2] / "spec" / "auto-instrumentation"
SECRET = "SYNTHETIC_PRIVATE_20261030"


def test_every_spec_privacy_fixture():
    """One SDK test exercises every golden and its second-pass idempotency."""
    fixtures = CATALOG_PATH.with_name("privacy-fixtures.json")
    assert CATALOG_PATH.read_bytes() == (SPEC / "privacy.json").read_bytes()
    assert fixtures.read_bytes() == (SPEC / fixtures.name).read_bytes()
    cases = json.loads(fixtures.read_text())["cases"]
    assert cases
    for case in cases:
        inputs = case["input"]
        kwargs = {"key": inputs["path"], "siblings": inputs.get("siblings"),
                  "credentials": inputs.get("known_secrets", ()),
                  "framed": inputs["site"] != "free_text_urls"}
        actual = _privacy.scrub(inputs["value"], **kwargs)
        assert actual == case["expected"], case["name"]
        assert _privacy.scrub(actual, **kwargs) == actual, "idempotency: " + case["name"]


def test_all_catalog_names_observation_and_export_share_one_predicate(monkeypatch):
    names = [item["example"] for item in CATALOG["credentials"]["patterns"]]
    names += CATALOG["credentials"]["seed_names"]
    names += [name for item in CATALOG["credentials"]["missed_name_sources"] for name in item["names"]]
    names += [name for item in CATALOG["credentials"]["exclusions"] for name in item["examples"]]
    names += [name for item in CATALOG["credentials"]["contextual_exclusions"] for name in item["examples"]]
    for name in names:
        for site in ("header", "query", "body", "environment"):
            monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
            expected = credential_name(name, query=site == "query")
            if site == "header":
                _privacy.observe_request({name: SECRET})
            elif site == "query":
                _privacy.observe_request({}, "https://example.invalid/?" + quote(name, safe="") + "=" + SECRET)
            elif site == "environment":
                monkeypatch.setattr(_secrets.os, "environ", {name: SECRET})
                _secrets.seed_environment()
            else:
                _privacy.observe_body({"nested": [{name: SECRET}]})
            assert (SECRET in _secrets.snapshot()) == expected, (name, site)
        assert (_privacy.scrub(SECRET, key=name) == "<redacted>") == credential_name(name), name


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
@pytest.mark.parametrize("kind", ["CHAIN", "LLM"])
@pytest.mark.parametrize("source", ["header", "query"])
def test_catalog_recognized_dispatch_echoes_on_all_traces(fresh_state, monkeypatch, module, kind, source):
    import importlib
    from fi_instrumentation import FITracer
    from fi_instrumentation.auto import _manager

    client_module = importlib.import_module(module)
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
    def late(request):
        if source == "header":
            request.headers["X-Session-Id"] = SECRET
        else:
            request.url = request.url.copy_add_param("auth_token", SECRET)
    def send(url):
        with client_module.Client(event_hooks={"request": [late]}) as client:
            response = client.post(url + "/v1/chat/completions", json=CHAT)
            return response.status_code, response.content
    with echo_server(credential_header="X-Session-Id") as server:
        baseline = send(server)
        provider, app, fi = install_agent()
        with FITracer(provider.get_tracer("library")).start_as_current_span(
                "outer", attributes={"gen_ai.span.kind": kind}) as outer:
            with provider.get_tracer("native").start_as_current_span("native-sdk") as native:
                actual = send(server)
                native.set_attribute("output.value", actual[1].decode())
            outer.set_attribute("output.value", actual[1].decode())
        assert actual == baseline and SECRET.encode() in actual[1]
    assert_process_exports(provider, (app, fi), [SECRET])
    assert sum(s.attributes.get("traceai.capture.layer") == "wire" for s in fi.get_finished_spans()) == (kind == "CHAIN")
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_catalog_body_field_harvests_unmarked_echoes(fresh_state, monkeypatch, module):
    import importlib
    from fi_instrumentation.auto import _manager
    client_module = importlib.import_module(module)
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
    provider, app, fi = install_agent()
    response = {"choices": [{"message": {"content": SECRET}}]}
    with client_module.Client(transport=client_module.MockTransport(
            lambda request: client_module.Response(200, json=response))) as client:
        actual = client.post("http://127.0.0.1/v1/chat/completions", json={**CHAT, "nested": {"x-vault-token": SECRET}})
    assert actual.json() == response
    assert_process_exports(provider, (app, fi), [SECRET])
    _manager.shutdown()
    provider.shutdown()


def test_credential_ancestry_preserved_during_harvesting(monkeypatch):
    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())
    value = {"metadata.session_id": {"values": [SECRET]}, "usage": {"input_tokens": "ordinary-metric-value"}}
    _privacy.observe_body(value)
    assert _secrets.snapshot() == (SECRET,)
    assert _privacy.scrub(value)["metadata.session_id"] == "<redacted>"


def test_catalog_media_and_mime_siblings_on_every_export_surface():
    png = "iVBORw0KGgo="
    attrs = {"parts[0].mimeType[0]": "audio/pcm", "parts.0.content.chunk": "AQIDBA==",
             "parts.1.content.chunk": "ordinary", "output.value": png,
             "output.audio": [0.125, -0.25], "metadata.x-vault-token": SECRET,
             "http.response.url": "https://example.invalid/?auth_token=" + SECRET,
             "gen_ai.usage.input_tokens": 17, "gen_ai.usage.output_tokens": 5}
    ctx = trace.SpanContext(123, 456, False, trace.TraceFlags(1), trace.TraceState([("vendor", png)]))
    original = ReadableSpan(name=png, context=ctx, parent=ctx, resource=Resource(attrs),
        instrumentation_scope=InstrumentationScope(png, png, attributes=attrs), attributes=attrs,
        status=trace.Status(trace.StatusCode.ERROR, png), events=[Event(png, attrs, 15)],
        links=[trace.Link(ctx, attrs)], start_time=10, end_time=20)
    clean = _privacy.export_span(original, TraceConfig())
    content = snapshot(clean)
    assert all(raw not in content for raw in (SECRET, png, "AQIDBA=="))
    assert clean.name == "<omitted: 12 base64 chars>"
    for surface in (clean.attributes, clean.resource.attributes, clean.instrumentation_scope.attributes,
                    clean.events[0].attributes, clean.links[0].attributes):
        assert surface["parts.0.content.chunk"] == "<omitted: 8 base64 chars>"
        assert surface["parts.1.content.chunk"] == "ordinary"
        assert surface["output.audio"] == "<omitted: media>"
        assert surface["gen_ai.usage.input_tokens"] == 17
        assert surface["gen_ai.usage.output_tokens"] == 5
    assert original.attributes["output.value"] == png


def test_media_magic_decode_is_prefix_bounded(monkeypatch):
    import base64
    import fi_instrumentation._privacy_catalog as module
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 4096).decode()
    decode, sizes = base64.b64decode, []
    def observed(*args, **kwargs):
        result = decode(*args, **kwargs)
        sizes.append(len(result))
        return result
    monkeypatch.setattr(module.base64, "b64decode", observed)
    assert PRIVACY.media_value(encoded) == f"<omitted: {len(encoded)} base64 chars>"
    assert sizes and max(sizes) <= CATALOG["media"]["base64_prefix_decoded_bytes"]


@pytest.mark.parametrize("missing", ["api_key", "secret_key"])
@pytest.mark.parametrize("source", ["argument", "environment"])
def test_partial_higher_source_falls_back_to_complete_file_pair(tmp_path, caplog, missing, source):
    path = tmp_path / "futureagi.toml"
    path.write_text('api_key = "FILE_API_KEY_123456"\nsecret_key = "FILE_SECRET_KEY_123456"\n')
    partial = {"secret_key" if missing == "api_key" else "api_key": SECRET}
    overrides = partial if source == "argument" else {}
    env = {"FI_" + key.upper(): value for key, value in partial.items()} if source == "environment" else {}
    config = resolve(overrides, env, str(path))
    assert (config.api_key, config.secret_key) == ("FILE_API_KEY_123456", "FILE_SECRET_KEY_123456")
    assert config.sources["api_key"] == config.sources["secret_key"] == f"file {path}"
    assert len(caplog.records) == 1 and "FI_" + missing.upper() in caplog.text
    assert SECRET not in caplog.text


def test_complementary_partial_sources_never_form_a_pair(tmp_path, caplog):
    path = tmp_path / "futureagi.toml"
    path.write_text('secret_key = "FILE_SECRET_123456"\n')
    config = resolve({"api_key": "ARG_API_123456"}, {"FI_SECRET_KEY": "ENV_SECRET_123456"}, str(path))
    assert (config.api_key, config.secret_key) == (None, None)
    assert len(caplog.records) == 3
    assert not config.has_credentials


def test_complete_argument_pair_shadows_environment_and_malformed_file(tmp_path):
    path = tmp_path / "futureagi.toml"
    path.write_text('api_key = invalid\nsecret_key = invalid\n')
    config = resolve({"api_key": "arg-api", "secret_key": "arg-secret"},
                     {"FI_API_KEY": "env-api", "FI_SECRET_KEY": "env-secret"}, str(path))
    assert (config.api_key, config.secret_key) == ("arg-api", "arg-secret")
    assert config.sources["api_key"] == config.sources["secret_key"] == "argument"


@pytest.mark.parametrize("missing", ["x-api-key", "x-secret-key"])
def test_exporter_partial_headers_never_mix_with_environment(monkeypatch, missing, caplog):
    from fi_instrumentation.otel import HTTPSpanExporter
    monkeypatch.setenv("FI_API_KEY", "ENV_API_123456")
    monkeypatch.setenv("FI_SECRET_KEY", "ENV_SECRET_123456")
    partial = {"x-secret-key" if missing == "x-api-key" else "x-api-key": SECRET}
    exporter = HTTPSpanExporter(endpoint="http://127.0.0.1:1/v1/traces", headers={**partial, "x-extra": "keep"})
    try:
        headers = dict(getattr(exporter, "_headers", None) or exporter._client._headers)
        assert headers["x-api-key"] == "ENV_API_123456"
        assert headers["x-secret-key"] == "ENV_SECRET_123456"
        assert headers["x-extra"] == "keep"
        assert len(caplog.records) == 1 and SECRET not in caplog.text
    finally:
        exporter.shutdown()


def test_file_pair_reaches_collector_without_readding_partial_environment(fresh_state, tmp_path, monkeypatch, caplog):
    from auto_mock_server import MockServer
    from fi_instrumentation.auto import _manager
    path = tmp_path / "futureagi.toml"
    path.write_text('api_key = "FILE_API_123456"\nsecret_key = "FILE_SECRET_123456"\n')
    monkeypatch.setenv("FI_API_KEY", SECRET)
    monkeypatch.delenv("FI_SECRET_KEY", raising=False)
    with MockServer() as collector:
        provider = _manager.init(config_file=str(path), base_url=collector.url, batch=False, instrumentations=[])
        with provider.get_tracer("application").start_as_current_span("atomic-pair"):
            pass
        assert _manager.flush(3000)
        assert len(collector.requests) == 1
        headers = collector.requests[0]["headers"]
        assert (headers["x-api-key"], headers["x-secret-key"]) == ("FILE_API_123456", "FILE_SECRET_123456")
    warnings = [record for record in caplog.records if "incomplete FI credential pair" in record.message]
    assert len(warnings) == 1 and "FI_SECRET_KEY" in warnings[0].message
    assert SECRET not in caplog.text
    _manager.shutdown()
    provider.shutdown()


def test_partial_argument_uses_complete_environment_not_file(tmp_path, caplog):
    path = tmp_path / "futureagi.toml"
    path.write_text('api_key = invalid\nsecret_key = invalid\n')
    config = resolve({"api_key": SECRET}, {"FI_API_KEY": "env-api", "FI_SECRET_KEY": "env-secret"}, str(path))
    assert (config.api_key, config.secret_key) == ("env-api", "env-secret")
    assert config.sources["api_key"] == "env FI_API_KEY"
    assert config.sources["secret_key"] == "env FI_SECRET_KEY"
    assert len(caplog.records) == 1 and SECRET not in caplog.text
