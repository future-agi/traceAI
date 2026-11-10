"""URL preflight and project-name regressions shared with the sibling recipes."""

from test_perplexity_recipe import app, _assert_main_refusal, clean_environment, validation_servers
import pytest


@pytest.mark.parametrize("path", [
    "/v1//", "/v1///", "//", "/v1/other", "/V1", "/v2",
    "/%76%31", "/v1%2F", "/v1%3F", "/v1/../v1",
])
def test_perplexity_only_accepts_exact_base_path(path, monkeypatch, capsys, validation_servers):
    _assert_main_refusal("https://api.perplexity.ai" + path, "base paths", monkeypatch, capsys, validation_servers)


@pytest.mark.parametrize("url", [
    "http://api。perplexity.ai/v1", "http://api.perplexity．ai/v1",
    "https://api.perplexity.ai｡/v1", "https://☃.example/v1",
    "https://api.perplexity.ai../v1", "https://api..perplexity.ai/v1",
    "https://api%2eperplexity.ai/v1", "https://api\\perplexity.ai/v1",
    "https://api.perplexity.ai:bad/v1", "https://api.perplexity.ai:70000/v1",
    " https://api.perplexity.ai/v1", "https://api.perplexity.ai/v1 ",
    "https://api.perplexity.ai/\nv1", "https://api.perplexity.ai/\tv1",
    "/v1", "api.perplexity.ai/v1",
    "file:///v1", "https:///v1", "https://xn--/v1",
])
def test_malformed_urls_exit_before_tracing(url, monkeypatch, capsys, validation_servers):
    _assert_main_refusal(url, app.BASE_URL_ENV, monkeypatch, capsys, validation_servers)


def test_null_byte_rejected_by_direct_client_preflight():
    # The OS cannot store NUL in an environment value; direct callers still can supply it.
    with pytest.raises(ValueError, match="control characters"):
        app.check_base_url("https://api.perplexity.ai/v1\x00")


def test_setup_tracing_uses_project_env_and_argument(monkeypatch):
    recorded = []
    monkeypatch.setattr(app, "register", lambda **kw: recorded.append(kw) or object())
    monkeypatch.setattr(app.OpenAIInstrumentor, "instrument", lambda *a, **kw: None)
    monkeypatch.delenv("FI_PROJECT_NAME", raising=False)
    app.setup_tracing()
    monkeypatch.setenv("FI_PROJECT_NAME", "perplexity-env-project")
    app.setup_tracing()
    app.setup_tracing("perplexity-explicit-project")
    assert [row["project_name"] for row in recorded] == [
        "perplexity-agent-api", "perplexity-env-project", "perplexity-explicit-project",
    ]
