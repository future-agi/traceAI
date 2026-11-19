"""Environment fixtures for the package's offline regression tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def _collector_at_a_closed_local_port(monkeypatch):
    """Point the collector URLs at a closed local port for every test.

    A test that calls register() exports spans at shutdown; with the URLs unset that goes to
    api.futureagi.com. A test that needs them absent (a default-endpoint test) removes them
    itself with monkeypatch.delenv.
    """
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "http://127.0.0.1:9")


@pytest.fixture
def clean_env():
    """Fixture to clean environment variables before each test."""
    # Store original env vars
    original_env = dict(os.environ)

    # Clear relevant environment variables
    env_vars_to_clear = [
        "FI_PROJECT_NAME",
        "FI_PROJECT_VERSION_NAME",
        "FI_API_KEY",
        "FI_SECRET_KEY",
        "FI_HIDE_LLM_INVOCATION_PARAMETERS",
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_INPUT_MESSAGES",
        "FI_HIDE_OUTPUT_MESSAGES",
        "FI_HIDE_INPUT_IMAGES",
        "FI_HIDE_INPUT_TEXT",
        "FI_HIDE_OUTPUT_TEXT",
        "FI_HIDE_EMBEDDING_VECTORS",
        "FI_BASE64_IMAGE_MAX_LENGTH",
        "FI_HIDE_ERROR_DETAILS",
        "FI_PII_REDACTION",
    ]

    for var in env_vars_to_clear:
        os.environ.pop(var, None)

    # FI_BASE_URL and FI_GRPC_URL are left alone: the autouse fixture
    # _collector_at_a_closed_local_port sets them for every test.

    yield

    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)
