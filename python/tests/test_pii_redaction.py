import os
from unittest import mock

import pytest

from fi_instrumentation.instrumentation.pii_redaction import (
    redact_pii_in_string,
    redact_pii_in_value,
)


class TestRedactPiiInString:
    def test_email(self):
        assert redact_pii_in_string("Contact john@example.com for info") == (
            "Contact <EMAIL_ADDRESS> for info"
        )

    def test_multiple_emails(self):
        text = "From alice@test.org to bob@test.org"
        assert redact_pii_in_string(text) == "From <EMAIL_ADDRESS> to <EMAIL_ADDRESS>"

    def test_ssn(self):
        assert redact_pii_in_string("SSN: 123-45-6789") == "SSN: <SSN>"

    def test_ssn_dot_separator(self):
        assert redact_pii_in_string("SSN: 123.45.6789") == "SSN: <SSN>"

    def test_ssn_space_separator(self):
        assert redact_pii_in_string("SSN: 123 45 6789") == "SSN: <SSN>"

    def test_credit_card_spaces(self):
        result = redact_pii_in_string("Card: 4111 1111 1111 1111")
        assert "<CREDIT_CARD>" in result

    def test_credit_card_dashes(self):
        result = redact_pii_in_string("Card: 4111-1111-1111-1111")
        assert "<CREDIT_CARD>" in result

    def test_phone_us(self):
        result = redact_pii_in_string("Call 555-123-4567 now")
        assert "<PHONE_NUMBER>" in result

    def test_phone_with_country_code(self):
        result = redact_pii_in_string("Call +1 555-123-4567")
        assert "<PHONE_NUMBER>" in result

    def test_phone_with_parens(self):
        result = redact_pii_in_string("Call (555) 123-4567")
        assert "<PHONE_NUMBER>" in result

    def test_ip_address(self):
        assert redact_pii_in_string("Server at 192.168.1.100 is down") == (
            "Server at <IP_ADDRESS> is down"
        )

    def test_ip_address_boundary(self):
        assert redact_pii_in_string("IP: 10.0.0.1") == "IP: <IP_ADDRESS>"

    def test_api_key_sk_live(self):
        key = "sk-live-ABCDEFGHIJKLMNOPQRSTuvwx"
        assert redact_pii_in_string(f"Key: {key}") == "Key: <API_KEY>"

    def test_api_key_pk_test(self):
        key = "pk_test_ABCDEFGHIJKLMNOPQRSTuvwx"
        assert redact_pii_in_string(f"Key: {key}") == "Key: <API_KEY>"

    def test_no_pii_passthrough(self):
        text = "Hello, this is a normal sentence."
        assert redact_pii_in_string(text) == text

    def test_empty_string(self):
        assert redact_pii_in_string("") == ""

    def test_mixed_pii(self):
        text = "Email john@acme.com, SSN 123-45-6789, IP 10.0.0.1"
        result = redact_pii_in_string(text)
        assert "<EMAIL_ADDRESS>" in result
        assert "<SSN>" in result
        assert "<IP_ADDRESS>" in result
        assert "john@acme.com" not in result
        assert "123-45-6789" not in result
        assert "10.0.0.1" not in result


class TestRedactPiiInValue:
    def test_string(self):
        result = redact_pii_in_value("Email: test@example.com")
        assert result == "Email: <EMAIL_ADDRESS>"

    def test_list_of_strings(self):
        result = redact_pii_in_value(["test@a.com", "no pii here", "192.168.1.1"])
        assert result == ["<EMAIL_ADDRESS>", "no pii here", "<IP_ADDRESS>"]

    def test_non_string_passthrough(self):
        assert redact_pii_in_value(42) == 42
        assert redact_pii_in_value(3.14) == 3.14
        assert redact_pii_in_value(True) is True
        assert redact_pii_in_value(None) is None

    def test_list_with_mixed_types(self):
        result = redact_pii_in_value(["test@a.com", 42, True])
        assert result == ["<EMAIL_ADDRESS>", 42, True]


class TestTraceConfigMaskPiiIntegration:
    """Test TraceConfig.mask behavior with hide_inputs/hide_outputs settings.

    Note: TraceConfig does not have a pii_redaction parameter. PII redaction
    is handled separately via redact_pii_in_string/redact_pii_in_value.
    These tests verify the mask method's key-based redaction behavior.
    """

    def test_mask_default_passthrough(self):
        from fi_instrumentation.instrumentation.config import TraceConfig

        config = TraceConfig()
        result = config.mask("some.attribute", "Contact john@example.com")
        assert result == "Contact john@example.com"

    def test_mask_hide_inputs_redacts_input_value(self):
        from fi_instrumentation.instrumentation.config import (
            REDACTED_VALUE,
            TraceConfig,
        )

        config = TraceConfig(hide_inputs=True)
        result = config.mask("input.value", "john@example.com")
        assert result == REDACTED_VALUE

    def test_mask_hide_inputs_hides_input_mime_type(self):
        from fi_instrumentation.instrumentation.config import TraceConfig

        config = TraceConfig(hide_inputs=True)
        result = config.mask("input.mime_type", "application/json")
        assert result is None

    def test_mask_callable_value(self):
        from fi_instrumentation.instrumentation.config import TraceConfig

        config = TraceConfig()
        result = config.mask("some.attribute", lambda: "Contact john@example.com")
        assert result == "Contact john@example.com"

    def test_mask_hide_outputs_redacts_output_value(self):
        from fi_instrumentation.instrumentation.config import (
            REDACTED_VALUE,
            TraceConfig,
        )

        config = TraceConfig(hide_outputs=True)
        result = config.mask("output.value", "some output data")
        assert result == REDACTED_VALUE
