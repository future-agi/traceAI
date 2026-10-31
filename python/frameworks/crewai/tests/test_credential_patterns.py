"""Generated structural credential regressions; synthetic values only."""

import copy
import itertools
import json

import pytest
from fi_instrumentation import TraceConfig

from traceai_crewai._privacy import (
    REDACTED,
    _CrewAITraceConfig,
    _credential_key,
    _safe_data,
)

SECRET = "FAKE_PATTERN_CREDENTIAL_251f03"
# Independent expectations, not generated from the production allowlist.
SAFE_NAMES = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "input_tokens_details",
    "output_tokens_details",
    "prompt_tokens_details",
    "completion_tokens_details",
    "num_tokens",
    "author_id",
    "author_name",
    "authorship",
    "api_secret_id",
    "reasoning_tokens",
    "token_count",
    "max_tokens",
    "max_completion_tokens",
    "tokens_used",
    "token_usage",
    "tokenizer",
    "author",
    "authors",
    "authority",
    "session_count",
    "session_duration_ms",
    "token_id",
    "security_token_id",
    "vault_token_count",
    "consul_token_ttl",
    "subject_token_type",
    "service_token_count",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "audio_tokens",
    "tokenizer_name",
    "tokenizer_version",
    "id",
    "project_id",
    "shipping",
    "opportunity",
    "purchase_order",
    "query",
    "answer",
    "business_data",
)


@pytest.mark.parametrize("prefix", ["", "X-", "x_", "vendor-", "X-Service-"])
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
def test_generated_credential_matrix(prefix, case):
    concepts = (
        ("token",),
        ("security", "token"),
        ("private", "token"),
        ("gitlab", "token"),
        ("circle", "token"),
        ("api", "secret"),
        ("app", "secret"),
        ("password",),
        ("passwd",),
        ("passphrase",),
        ("api", "key"),
        ("access", "key"),
        ("private", "key"),
        ("credential",),
        ("auth",),
        ("authorization",),
        ("authentication",),
        ("cookie",),
        ("session", "id"),
        ("session", "token"),
        ("signature",),
        ("bearer",),
        ("jwt",),
        ("otp", "code"),
        ("totp",),
    )
    for words, separator in itertools.product(
        concepts, ("", "-", "_", " ", "/", ":", ".")
    ):
        name = case(prefix + separator.join(words))
        assert _credential_key(name), name
        payload = {
            "headers": {name: SECRET, "Accept": "application/json"},
            f"client.config.{name}[0].value": SECRET,
            "raw": json.dumps({name: SECRET, "answer": "useful answer"}),
            "total_tokens": 42,
            "id": "business-id",
        }
        before = copy.deepcopy(payload)
        projected = _safe_data(payload)
        assert SECRET not in json.dumps(projected), name
        assert projected["headers"]["Accept"] == "application/json"
        assert projected["total_tokens"] == 42 and projected["id"] == "business-id"
        assert json.loads(projected["raw"])["answer"] == "useful answer"
        assert payload == before
        assert (
            _CrewAITraceConfig(TraceConfig()).mask(
                f"client.config[{name}].value", SECRET
            )
            == REDACTED
        )


@pytest.mark.parametrize("name", ["pin", "PIN", "x-pin", "X_OTP", "otp"])
def test_short_credentials_match_whole_normalized_name(name):
    assert _safe_data({name: SECRET}) == {name: REDACTED}


@pytest.mark.parametrize("name", SAFE_NAMES)
def test_explicit_metadata_exceptions_preserve_values_and_not_children(name):
    for spelling in (name, name.upper(), name.replace("_", "-"), "X-" + name):
        assert not _credential_key(spelling), spelling
        payload = {spelling: 42, f"usage.{spelling}": 42, "id": "safe-id"}
        assert _safe_data(payload) == payload
        # An allowlisted parent cannot bypass inspection of its children.
        assert SECRET not in json.dumps(_safe_data({spelling: {"api_key": SECRET}}))
        assert _credential_key(f"{spelling}.api_key")


def test_unknown_vendor_token_metadata_is_not_a_generic_bypass():
    for name in ("private_token_count", "vendor_secret_id", "vendor_auth_type"):
        assert _safe_data({name: SECRET})[name] == REDACTED


@pytest.mark.parametrize("shape", ["pairs", "tuples", "name", "key", "header"])
def test_header_lists_and_serialized_lists(shape, service_credentials):
    fields = list(service_credentials.items()) + [("Accept", "application/json")]
    if shape == "pairs":
        headers = [list(item) for item in fields]
    elif shape == "tuples":
        headers = fields
    else:
        headers = [
            {shape: key, "value": value, "description": "business data"}
            for key, value in fields
        ]
    original = copy.deepcopy(headers)
    projected = _safe_data({"headers": headers, "json": json.dumps(headers)})
    rendered = json.dumps(projected)
    assert all(value not in rendered for value in service_credentials.values())
    assert "application/json" in rendered
    assert "Accept" in rendered
    assert headers == original
    if shape not in ("pairs", "tuples"):
        assert all(
            item["description"] == "business data" for item in projected["headers"]
        )
    for name in service_credentials:
        assert name in rendered


@pytest.mark.parametrize("selector", ["name", "key", "header", "0"])
@pytest.mark.parametrize("brackets", [False, True])
def test_flattened_header_records(selector, brackets):
    value_field = "1" if selector == "0" else "value"

    def key(index, field):
        return (
            f"headers[{index}]['{field}']" if brackets else f"headers.{index}.{field}"
        )

    original = {
        key(0, selector): "Authorization",
        key(0, value_field): SECRET,
        key(1, selector): "Accept",
        key(1, value_field): "application/json",
        "headers['X-Gitlab-Token'][0]": SECRET,
        "usage.total_tokens": 42,
        "request_id": "business-id",
    }
    before = copy.deepcopy(original)
    safe = _safe_data(original)
    assert SECRET not in json.dumps(safe)
    assert safe[key(0, selector)] == "Authorization"
    assert safe[key(1, value_field)] == "application/json"
    assert safe["usage.total_tokens"] == 42
    assert original == before
