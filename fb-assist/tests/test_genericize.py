"""Tests for fb_assist.genericize — the SEMANTIC leak guardrail (the IP-leak floor).

Plants a leak and proves the guardrail catches it; proves a genuinely clean
genericization passes; and exercises the meaning-risk heuristic + the two distill
appliers (faithful-summary replacement that still parses as records).

All synthetic values are FAKE (pattern-valid, never real credentials). USE_TF=0 is
forced so importing the sibling redactor never trips the transformers TF path.

Run under pytest:
    USE_TF=0 python -m pytest tests/test_genericize.py -q
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import genericize  # noqa: E402
import fb_assist.transcripts as T  # noqa: E402

# A pattern-valid FAKE Anthropic key (matches SECRET_PATTERNS["ANTHROPIC_KEY"]).
# Never a real credential — same shape the redact recall harness plants.
FAKE_ANTHROPIC_KEY = "sk-ant-api03-" + ("Xy9kLm3nQp7rTv2w" * 6) + "ZZ"

# Skip the (network-bound) GLiNER pass everywhere: tests must stay offline + fast.
NOG = dict(use_gliner=False)


# --------------------------------------------------------------------------- #
# verify_genericization — the egress bar
# --------------------------------------------------------------------------- #
def test_planted_secret_survives_is_caught():
    """A 'genericized' rewrite that still embeds the original key must FAIL, with
    the literal key surfaced in leaked_originals."""
    original = f"The nightly deploy broke because the key {FAKE_ANTHROPIC_KEY} was rotated."
    # Botched genericization: prose reworded, but the real key left sitting in it.
    generic = f"The nightly deploy broke because the API key ({FAKE_ANTHROPIC_KEY}) was still embedded."

    res = genericize.verify_genericization(original, generic, **NOG)

    assert res["ok"] is False, "a surviving secret must not pass"
    leaked_values = [d["text"] for d in res["leaked_originals"]]
    assert FAKE_ANTHROPIC_KEY in leaked_values, "planted key not reported in leaked_originals"
    # The re-identification pass independently flags it as a blocking finding too.
    assert any(d["severity"] in ("high", "critical") for d in res["reid_findings"])


def test_clean_genericization_passes():
    """Identity gone, meaning preserved, no value survives → ok=True, no leaks."""
    original = (
        "Dr. Jane Smith at Northwind Labs reports the billing service deadlocks "
        "when an account syncs; reach her at jane.smith@northwind.example or 415-555-0148."
    )
    generic = (
        "A customer reports that their billing service deadlocks whenever an account "
        "synchronizes during the nightly job."
    )

    res = genericize.verify_genericization(original, generic, **NOG)

    assert res["leaked_originals"] == [], (
        f"clean rewrite wrongly flagged survivors: {res['leaked_originals']}"
    )
    assert res["ok"] is True, f"clean rewrite failed: reid={res['reid_findings']}"


def test_expect_absent_codename_still_present_is_flagged():
    """A caller-named codename left in the generic text trips expect_absent_hits."""
    original = "We hit a crash in the Athena pipeline when the Zephyr cache evicts."
    generic = "We hit a crash in the Athena pipeline when the cache evicts."  # codename survived

    res = genericize.verify_genericization(original, generic, expect_absent=["Athena"], **NOG)

    assert any(h["literal"] == "Athena" for h in res["expect_absent_hits"])
    assert res["ok"] is False, "a surviving codename must not pass"


def test_expect_absent_codename_removed_passes():
    """When the codename is actually removed and nothing else leaks, ok=True."""
    original = "We hit a crash in the Athena pipeline when the cache evicts."
    generic = "We hit a crash in our internal data pipeline when the cache evicts."

    res = genericize.verify_genericization(original, generic, expect_absent=["Athena"], **NOG)

    assert res["expect_absent_hits"] == []
    assert res["ok"] is True


def test_meaning_risk_flags_dropped_load_bearing_token():
    """Dropping a load-bearing marker (ALL-CAPS word + an error code) fires the
    meaning-risk heuristic — but does NOT veto (ok is decided by leaks only)."""
    original = (
        "The UI goes FREEZING for ~8s and the logs show error code E-4521 every "
        "time the sync worker retries against the queue."
    )
    # Reasonable-length paraphrase that quietly drops both load-bearing tokens.
    generic = (
        "The interface becomes unresponsive for several seconds and the logs show a "
        "recurring fault whenever the background worker retries against the queue."
    )

    res = genericize.verify_genericization(original, generic, **NOG)
    kinds = {(f["kind"], f.get("token")) for f in res["meaning_risk_flags"]}
    assert ("dropped_load_bearing_token", "FREEZING") in kinds
    assert ("dropped_load_bearing_token", "E-4521") in kinds
    # No actual leak, so the no-leak verdict is still a pass — meaning is the
    # user's call, never gated here.
    assert res["ok"] is True


def test_meaning_risk_flags_drastically_shorter():
    original = (
        "The diff viewer scrolls to the wrong line after a hot reload; I expected it "
        "to keep my cursor position but instead it jumps to the very top of the file "
        "every single time, which makes iterating on a large file painful."
    )
    generic = "Scroll bug."  # < 25% length

    res = genericize.verify_genericization(original, generic, **NOG)
    assert any(f["kind"] == "drastically_shorter" for f in res["meaning_risk_flags"])


def test_meaning_risk_quiet_when_preserved():
    """A faithful, similar-length rewrite that keeps the markers fires no flags —
    the heuristic doesn't cry wolf."""
    original = "The build hangs with a TIMEOUT after the v2.3.1 upgrade."
    generic = "The build hangs with a TIMEOUT after the v2.3.1 release upgrade."

    res = genericize.verify_genericization(original, generic, **NOG)
    assert res["meaning_risk_flags"] == []


# --------------------------------------------------------------------------- #
# distill appliers
# --------------------------------------------------------------------------- #
VERBOSE = "Here is a very detailed walkthrough. " + ("UNIQUE_MARKER_XYZ " * 60)


def _exchange_records() -> list[dict]:
    """A tiny 3-record exchange: human prompt, a VERBOSE assistant reply, a thanks."""
    return [
        {"type": "user", "uuid": "u1", "sessionId": "s", "timestamp": "2026-06-29T00:00:00Z",
         "isSidechain": False, "parentUuid": None,
         "message": {"role": "user", "content": "Please refactor the FooBarBaz widget."}},
        {"type": "assistant", "uuid": "a1", "sessionId": "s", "timestamp": "2026-06-29T00:00:01Z",
         "parentUuid": "u1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": VERBOSE}]}},
        {"type": "user", "uuid": "u2", "sessionId": "s", "timestamp": "2026-06-29T00:00:02Z",
         "parentUuid": "a1", "message": {"role": "user", "content": "thanks!"}},
    ]


def _records_as_T(records: list[dict]):
    return [T.Record(line=i + 1, raw=r, type=r.get("type", "")) for i, r in enumerate(records)]


def test_distill_apply_replaces_one_span_faithfully():
    records = _exchange_records()
    # Locate the verbose assistant-text span via the real extractor.
    span = next(T.assistant_text(_records_as_T(records)))
    assert "UNIQUE_MARKER_XYZ" in span.text

    summary = "[distilled] Assistant proposed a refactor of the widget."
    out = genericize.distill_apply(records, span, summary)

    # Input untouched (pure).
    assert "UNIQUE_MARKER_XYZ" in json.dumps(records), "distill_apply mutated its input"
    blob = json.dumps(out)
    assert "UNIQUE_MARKER_XYZ" not in blob, "verbose body survived the distill"
    assert summary in blob, "summary missing from distilled records"
    # Still parses as records, same count.
    assert len(out) == len(records)
    for r in out:
        json.dumps(r)  # serializable
    assert T.get_at(out[1], ("message", "content", 0, "text")) == summary


def test_distill_apply_stale_span_refuses():
    """A span that doesn't resolve to its own text in the given records is refused
    (fail loud, never silently corrupt)."""
    records = _exchange_records()
    span = next(T.assistant_text(_records_as_T(records)))
    # Mutate the record so the span no longer matches.
    records[1]["message"]["content"][0]["text"] = "something else entirely"
    try:
        genericize.distill_apply(records, span, "summary")
    except ValueError:
        return
    assert False, "expected ValueError for a stale span"


def test_distill_turn_range_collapses_exchange():
    records = _exchange_records()
    summary = "[distilled] User asked for a widget refactor; assistant explained the plan; user thanked."

    out = genericize.distill_turn_range(records, 0, 2, summary)

    # Input untouched (pure).
    assert len(records) == 3 and "UNIQUE_MARKER_XYZ" in json.dumps(records)
    # The whole verbose exchange collapsed to a single summary record.
    assert len(out) == 1
    blob = json.dumps(out)
    assert "UNIQUE_MARKER_XYZ" not in blob, "verbose exchange survived the distill"
    assert "Please refactor the FooBarBaz widget." not in blob, "original prompt survived"
    assert summary in blob
    # Still a coherent, parseable record with the inherited envelope + honesty marker.
    rec = out[0]
    json.dumps(rec)
    assert rec["fbAssistDistilled"] is True
    assert rec["fbAssistDistilledCount"] == 3
    assert rec["sessionId"] == "s"
    assert rec["message"]["content"] == summary


def test_distill_turn_range_partial_range_keeps_surrounding():
    records = _exchange_records()
    out = genericize.distill_turn_range(records, 1, 1, "[distilled] long assistant reply")
    # Replaced only the middle record; the human prompt + thanks survive verbatim.
    assert len(out) == 3
    assert out[0]["uuid"] == "u1" and out[2]["uuid"] == "u2"
    blob = json.dumps(out)
    assert "UNIQUE_MARKER_XYZ" not in blob
    assert "Please refactor the FooBarBaz widget." in blob  # untouched neighbour


def test_distill_turn_range_bad_range_raises():
    records = _exchange_records()
    for bad in [(-1, 0), (0, 3), (2, 1)]:
        try:
            genericize.distill_turn_range(records, bad[0], bad[1], "x")
        except IndexError:
            continue
        assert False, f"expected IndexError for range {bad}"


if __name__ == "__main__":
    # Lightweight standalone runner (mirrors the sibling test modules).
    fns = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"FAIL  {name}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
