from orchestrator.guardrails import pii


def test_scan_detects_email_phone_ssn():
    text = "Contact: a.b@example.com or +1 (415) 555-2671. SSN 123-45-6789."
    kinds = {m.kind for m in pii.scan(text)}
    assert {"email", "phone", "ssn"} <= kinds


def test_scan_detects_valid_credit_card_only():
    valid = "4111 1111 1111 1111"  # Luhn-valid Visa test number
    invalid = "4111 1111 1111 1112"
    valid_kinds = {m.kind for m in pii.scan(valid)}
    invalid_kinds = {m.kind for m in pii.scan(invalid)}
    assert "credit_card" in valid_kinds
    assert "credit_card" not in invalid_kinds


def test_scan_detects_ipv4():
    assert any(m.kind == "ipv4" for m in pii.scan("server at 10.0.0.1 ok"))


def test_scan_clean_text_empty():
    assert pii.scan("nothing personal here") == []


def test_redact_round_trip_mapping():
    text = "Email a@b.co, phone 415-555-2671, again a@b.co"
    redacted, mapping = pii.redact(text)
    assert "a@b.co" not in redacted
    assert "415-555-2671" not in redacted
    # all placeholders should be in the mapping with original values
    for token, original in mapping.items():
        assert token.startswith("[PII:")
        assert original  # non-empty


def test_redact_noop_returns_empty_mapping():
    out, mapping = pii.redact("hello world")
    assert out == "hello world"
    assert mapping == {}


def test_luhn_valid_rejects_short():
    assert pii.luhn_valid("12") is False
    assert pii.luhn_valid("4111111111111111") is True
