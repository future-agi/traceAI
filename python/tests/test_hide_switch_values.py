"""A privacy switch someone set must never silently do nothing.

true, 1, yes and on turn a switch on; false, 0, no and off turn it off (any case,
surrounding spaces ignored); an empty value is unset. Any other value hides, with one
warning that names the variable and the accepted values and never repeats the value.
"""

import logging

import pytest

from fi_instrumentation.instrumentation import config as config_module
from fi_instrumentation.instrumentation.config import TraceConfig

LOGGER = "fi_instrumentation.instrumentation.logging"


@pytest.fixture
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("FI_HIDE_") or name == "FI_PII_REDACTION":
            monkeypatch.delenv(name, raising=False)
    config_module._warned_switches.clear()
    yield
    config_module._warned_switches.clear()


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", " TRUE ", "On"])
def test_true_values_hide(clean_env, monkeypatch, raw):
    monkeypatch.setenv("FI_HIDE_INPUTS", raw)
    assert TraceConfig().hide_inputs is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", " OFF "])
def test_false_values_show(clean_env, monkeypatch, raw):
    monkeypatch.setenv("FI_HIDE_OUTPUTS", raw)
    assert TraceConfig().hide_outputs is False


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_value_is_unset(clean_env, monkeypatch, raw):
    monkeypatch.setenv("FI_HIDE_INPUT_MESSAGES", raw)
    config = TraceConfig()
    assert config.hide_input_messages is False
    assert config.hide_error_details is False


def test_unrecognised_value_hides_with_one_warning_that_names_the_variable_and_not_the_value(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("FI_HIDE_OUTPUT_TEXT", "maybe-later")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert TraceConfig().hide_output_text is True
        assert TraceConfig().hide_output_text is True
    messages = [record.getMessage() for record in caplog.records]
    named = [message for message in messages if "FI_HIDE_OUTPUT_TEXT" in message]
    assert len(named) == 1
    assert "true, 1, yes, on" in named[0]
    assert all("maybe-later" not in message for message in messages)


def test_unrecognised_error_details_value_hides_error_details(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "enabled-please")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert TraceConfig().hide_error_details is True
    assert all("enabled-please" not in record.getMessage() for record in caplog.records)


def test_recognised_error_details_value_still_wins_over_the_content_switches(clean_env, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "on")
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "off")
    assert TraceConfig().hide_error_details is False


def test_unrecognised_pii_redaction_turns_redaction_on(clean_env, monkeypatch):
    monkeypatch.setenv("FI_PII_REDACTION", "sure")
    assert TraceConfig().pii_redaction is True


def test_an_explicit_argument_still_wins_over_the_environment(clean_env, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "maybe")
    assert TraceConfig(hide_inputs=False).hide_inputs is False


def test_a_number_that_cannot_be_read_warns_without_its_value(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", "not-a-number-xyz")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        TraceConfig()
    assert all("not-a-number-xyz" not in record.getMessage() for record in caplog.records)
