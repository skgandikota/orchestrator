from coracle.guardrails import secrets

_AKIA = "AK" + "IA"
_GHP = "ghp" + "_"
_BEGIN = "-----" + "BEGIN" + " "
_PK_HEADER = "RSA " + "PRIVATE" + " " + "KEY"


def test_scan_detects_aws_access_key():
    text = "creds: " + _AKIA + "ABCDEFGHIJKLMNOP and value"
    findings = secrets.scan(text)
    assert any(f.rule == "aws_access_key" for f in findings)


def test_scan_detects_github_and_openai_keys():
    text = "tokens: " + _GHP + ("A" * 36) + " and sk-" + ("x" * 30)
    findings = secrets.scan(text)
    rules = {f.rule for f in findings}
    assert "github_token" in rules
    assert "openai_key" in rules


def test_scan_detects_private_key_block_and_jwt():
    text = (
        _BEGIN + _PK_HEADER + "-----\nbody\n"
        "jwt: " + "ey" + "JhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepartXXXXXX"
    )
    rules = {f.rule for f in secrets.scan(text)}
    assert "private_key_block" in rules
    assert "jwt" in rules


def test_scan_returns_empty_for_clean_text():
    assert secrets.scan("hello world, no secrets here") == []


def test_redact_replaces_all_findings():
    text = "k1=" + _AKIA + "ABCDEFGHIJKLMNOP k2=" + _GHP + ("B" * 36)
    redacted = secrets.redact(text)
    assert _AKIA not in redacted
    assert _GHP not in redacted
    assert "[REDACTED:aws_access_key]" in redacted
    assert "[REDACTED:github_token]" in redacted


def test_redact_noop_when_no_findings():
    assert secrets.redact("nothing here") == "nothing here"


def test_redact_accepts_explicit_findings_list():
    text = _AKIA + "ABCDEFGHIJKLMNOP"
    findings = secrets.scan(text)
    assert secrets.redact(text, findings).startswith("[REDACTED:")


def test_mask_handles_short_strings():
    # exercise the short-snippet branch of _mask
    findings = secrets.scan(_AKIA + "ABCDEFGHIJKLMNOP")
    assert findings
    assert "…" in findings[0].snippet
