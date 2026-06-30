"""Privacy-preserving observation (fb_assist reuse): planted secrets/PII are
byte-absent from the package, the leak-scan floor is the HARD gate."""

from __future__ import annotations

from pps_pipeline import redact_pass as RP
from pps_pipeline.fixture import SENTINELS
from pps_pipeline.interleave import package_text


def _assembled(events):
    return "\n".join(e.text for e in events)


def test_planted_sentinels_absent_after_redaction(loaded_bundle):
    res = RP.redact_events(loaded_bundle.raw_events())
    text = _assembled(res.events)
    for s in SENTINELS:
        assert s not in text, f"LEAK: planted sentinel survived: {s}"


def test_sentinels_absent_from_built_package(package):
    txt = package_text(package)
    for s in SENTINELS:
        assert s not in txt, f"LEAK: sentinel in package: {s}"


def test_floor_clean_after_redaction(loaded_bundle):
    res = RP.redact_events(loaded_bundle.raw_events())
    assert res.applied is True
    assert res.floor_clean is True
    assert res.floor_findings == []
    assert not res.blocking


def test_redaction_preserves_non_sensitive_meaning(loaded_bundle):
    res = RP.redact_events(loaded_bundle.raw_events())
    text = _assembled(res.events)
    # the work signal survives; the masks are present
    assert "tests: 3 failed, 18 passed" in text
    assert "Bash: cat .env" in text
    assert "‹ANTHROPIC_KEY›" in text
    assert "‹AWS_ACCESS_KEY›" in text
    assert "‹EMAIL_ADDRESS›" in text


def test_floor_gate_would_block_on_raw_leak():
    """Feeding UN-redacted text to the floor scan returns residual findings —
    i.e. the gate that blocks packaging fires on a hit."""
    raw = ("here is a key sk-ant-api03-RAWLEAK1111222233334444AAAA and "
           "an aws key AKIAIOSFODNN7EXAMPLE and email leak@example.com")
    residual = RP.floor_scan_text(raw)
    assert residual, "floor scan should flag the raw leak (gate would block)"
    cats = {f.category for f in residual}
    assert "secret" in cats and "pii" in cats


def test_floor_scan_clean_on_redacted(loaded_bundle):
    res = RP.redact_events(loaded_bundle.raw_events())
    assert RP.floor_scan_text(_assembled(res.events)) == []


def test_ner_ceiling_optional_does_not_break_floor(loaded_bundle):
    """ner=True (Presidio ceiling) must still yield a clean floor + absent
    sentinels (it only masks MORE)."""
    res = RP.redact_events(loaded_bundle.raw_events(), ner=True)
    text = _assembled(res.events)
    for s in SENTINELS:
        assert s not in text
    assert res.floor_clean is True
