from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string


def test_valid_credit_card_is_redacted():
    # Valid Visa card number passing Luhn algorithm
    card_text = "Please charge my card 4012-8888-8888-1881 for the subscription."
    redacted = redact_pii_in_string(card_text)
    assert "<CREDIT_CARD>" in redacted
    assert "4012" not in redacted


def test_uuid_with_numeric_segments_is_not_corrupted():
    # UUID from issue #195 whose digits would previously match bare 13-19 digit regex
    uuid_str = "73630065-0794-4450-a1f9-8cc987a02b09"
    redacted = redact_pii_in_string(uuid_str)
    assert redacted == uuid_str
    assert "<CREDIT_CARD>" not in redacted


def test_numeric_timestamps_are_preserved():
    ts_text = "Event occurred at timestamp 1725700000000 in cluster."
    redacted = redact_pii_in_string(ts_text)
    assert redacted == ts_text
    assert "1725700000000" in redacted


def test_trace_config_preserves_session_and_user_ids():
    cfg = TraceConfig(pii_redaction=True)

    # session.id must remain intact
    session_id = "73630065-0794-4450-a1f9-8cc987a02b09"
    masked_session = cfg.mask(SpanAttributes.SESSION_ID, session_id)
    assert masked_session == session_id

    # user.id must remain intact
    user_id = "1234567890123"
    masked_user = cfg.mask(SpanAttributes.USER_ID, user_id)
    assert masked_user == user_id


def test_other_pii_types_continue_to_redact():
    text = (
        "User test.user@example.com with SSN 123-45-6789 and IP 192.168.1.10 "
        "called from +1 (555) 123-4567 with key sk-live-123456789012345678901234."
    )
    redacted = redact_pii_in_string(text)
    assert "<EMAIL_ADDRESS>" in redacted
    assert "<SSN>" in redacted
    assert "<IP_ADDRESS>" in redacted
    assert "<PHONE_NUMBER>" in redacted
    assert "<API_KEY>" in redacted
