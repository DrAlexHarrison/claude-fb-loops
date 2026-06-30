"""Tests for fb_assist.watcher — the frustration/delight signal-capture hook.

Hermetic: every test points the state dir and the disable-file at ``tmp_path`` via
env overrides, so nothing touches the real ``/tmp`` state or ``~/.config``. The
pure detectors are tested directly; the hook glue is driven end-to-end by feeding
a JSON payload on a monkeypatched ``sys.stdin`` and asserting the stdout nudge.

The contract under test is precision-first behavior: detect the right moments,
offer ONCE, never nag, fully disable-able, pre-draft for one-tap /fb — and the hook
must ALWAYS return 0 and NEVER raise.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

# Make the package importable when run directly (pytest also handles this).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import watcher as W  # noqa: E402


# --------------------------------------------------------------------------- #
# Hermetic environment: state dir + disable file both under tmp_path           #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """Redirect state + disable file into tmp; the off-file is ABSENT by default."""
    state_dir = tmp_path / "watcher-state"
    state_dir.mkdir()
    off_file = tmp_path / "watcher.off"  # not created => enabled
    monkeypatch.setenv("FB_ASSIST_WATCHER_DIR", str(state_dir))
    monkeypatch.setenv("FB_ASSIST_WATCHER_OFF", str(off_file))
    return {"state_dir": state_dir, "off_file": off_file}


def run_main(monkeypatch, event, payload, *, argv=None):
    """Drive main() with ``payload`` JSON on stdin; return (rc)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return W.main([event] if argv is None else argv)


# --------------------------------------------------------------------------- #
# Sentiment detection (pure)                                                   #
# --------------------------------------------------------------------------- #
def test_detect_sentiment_frustration_wtf():
    assert W.detect_sentiment("wtf why won't this build") == "frustration"


def test_detect_sentiment_delight_perfect():
    assert W.detect_sentiment("perfect, that's exactly it") == "delight"


def test_detect_sentiment_neutral_none():
    assert W.detect_sentiment("ok, please refactor the parser") is None
    assert W.detect_sentiment("") is None
    assert W.detect_sentiment(None) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ugh this keeps failing", "frustration"),
        ("it's still broken", "frustration"),
        ("come on, seriously?", "frustration"),
        ("this is broken again", "frustration"),
        ("nice, finally", "delight"),
        ("I love it", "delight"),
        ("so good, wow", "delight"),
        ("perfectionism slows me down", None),   # 'perfect' as a substring must NOT fire
        ("the unicorn is magnificent", None),
    ],
)
def test_detect_sentiment_table(text, expected):
    assert W.detect_sentiment(text) == expected


def test_frustration_takes_precedence_over_delight():
    # Mixed signal: catching the bug matters more than catching the cheer.
    assert W.detect_sentiment("wtf, perfect timing for a crash") == "frustration"


# --------------------------------------------------------------------------- #
# Retry storm (pure)                                                           #
# --------------------------------------------------------------------------- #
def test_retry_storm_fires_on_third_not_before():
    st = W.WatcherState(session_id="s")
    assert W.register_tool_event(st, "Bash", True, now=100.0) is False  # 1
    assert W.register_tool_event(st, "Bash", True, now=101.0) is False  # 2
    assert W.register_tool_event(st, "Bash", True, now=102.0) is True   # 3 -> storm


def test_retry_storm_success_breaks_streak():
    st = W.WatcherState(session_id="s")
    W.register_tool_event(st, "Bash", True, now=100.0)
    W.register_tool_event(st, "Bash", True, now=101.0)
    W.register_tool_event(st, "Bash", False, now=101.5)  # a win clears the streak
    assert W.register_tool_event(st, "Bash", True, now=102.0) is False
    assert W.register_tool_event(st, "Bash", True, now=103.0) is False
    assert W.register_tool_event(st, "Bash", True, now=104.0) is True


def test_retry_storm_different_tools_independent():
    st = W.WatcherState(session_id="s")
    assert W.register_tool_event(st, "Bash", True, now=100.0) is False
    assert W.register_tool_event(st, "Edit", True, now=100.1) is False
    assert W.register_tool_event(st, "Bash", True, now=100.2) is False
    # Bash has only 2 errors; no storm despite 3 total errors across tools.
    assert "retry_storm" not in st.notified


def test_retry_storm_respects_rolling_window():
    st = W.WatcherState(session_id="s")
    W.register_tool_event(st, "Bash", True, now=0.0)
    W.register_tool_event(st, "Bash", True, now=1.0)
    # Third error is far outside the window -> the first two have aged out.
    assert W.register_tool_event(st, "Bash", True, now=10_000.0) is False


# --------------------------------------------------------------------------- #
# Rage (pure)                                                                  #
# --------------------------------------------------------------------------- #
def test_rage_repeated_prompt():
    st = W.WatcherState(session_id="s", recent_prompts=["fix the build"])
    assert W.detect_rage(st, "Fix the build") == "rage"  # case/space-insensitive repeat
    assert W.detect_rage(st, "do something else") is None


def test_rage_clear_after_failure():
    st = W.WatcherState(session_id="s", last_tool_failed=True)
    assert W.detect_rage(st, "/clear") == "rage"
    st2 = W.WatcherState(session_id="s", last_tool_failed=False)
    assert W.detect_rage(st2, "/clear") is None


# --------------------------------------------------------------------------- #
# Offer-once gate + disable (pure)                                             #
# --------------------------------------------------------------------------- #
def test_should_notify_offer_once():
    st = W.WatcherState(session_id="s")
    assert W.should_notify(st, "frustration") is True
    st.notified.add("frustration")
    assert W.should_notify(st, "frustration") is False
    # A different class is still offerable.
    assert W.should_notify(st, "delight") is True


def test_should_notify_disabled_blocks_everything():
    st = W.WatcherState(session_id="s")
    assert W.should_notify(st, "frustration", disabled=True) is False


def test_is_disabled_reflects_off_file(hermetic):
    assert W.is_disabled() is False
    hermetic["off_file"].write_text("")  # create the kill switch
    assert W.is_disabled() is True


# --------------------------------------------------------------------------- #
# Pre-draft + state round-trip                                                 #
# --------------------------------------------------------------------------- #
def test_record_predraft_written_to_state():
    st = W.WatcherState(session_id="s", turn=4)
    entry = W.record_predraft(st, "frustration", "wtf this is broken", now=42.0)
    assert st.predraft["frustration"] is entry
    assert entry["needle"] == "wtf this is broken"
    assert entry["turn"] == 4
    assert entry["turn_range"] == [3, 4]
    assert entry["timestamp"] == 42.0


def test_state_save_load_roundtrip(hermetic):
    st = W.WatcherState(session_id="sess-A", turn=2)
    st.notified.add("frustration")
    W.record_predraft(st, "frustration", "needle here", now=1.0)
    st.tool_errors["Bash"] = [1.0, 2.0]
    W.save_state(st)

    loaded = W.load_state("sess-A")
    assert loaded.session_id == "sess-A"
    assert loaded.turn == 2
    assert loaded.notified == {"frustration"}          # set survives JSON round-trip
    assert loaded.predraft["frustration"]["needle"] == "needle here"
    assert loaded.tool_errors["Bash"] == [1.0, 2.0]


def test_load_state_missing_is_fresh():
    st = W.load_state("never-seen")
    assert st.session_id == "never-seen"
    assert st.notified == set()
    assert st.predraft == {}


# --------------------------------------------------------------------------- #
# make_nudge                                                                   #
# --------------------------------------------------------------------------- #
def test_make_nudge_variants_mention_fb():
    for sc in ("retry_storm", "rage", "frustration"):
        assert "/fb" in W.make_nudge(sc)
        assert "Rough patch" in W.make_nudge(sc)
    assert "/fb" in W.make_nudge("delight")
    assert "win" in W.make_nudge("delight")  # delight gets a positive flavor


# --------------------------------------------------------------------------- #
# End-to-end main() over stdin                                                 #
# --------------------------------------------------------------------------- #
def test_main_frustrated_prompt_emits_nudge(monkeypatch, capsys):
    payload = {
        "session_id": "sess-e2e",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "ugh wtf this is still broken",
    }
    rc = run_main(monkeypatch, "UserPromptSubmit", payload)
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "/fb" in obj["hookSpecificOutput"]["additionalContext"]

    # Pre-draft was written so /fb can pick it up one-tap.
    st = W.load_state("sess-e2e")
    assert "frustration" in st.notified
    assert "frustration" in st.predraft
    assert st.predraft["frustration"]["needle"]


def test_main_neutral_prompt_emits_nothing(monkeypatch, capsys):
    payload = {
        "session_id": "sess-neutral",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "please add a test for the parser",
    }
    rc = run_main(monkeypatch, "UserPromptSubmit", payload)
    assert rc == 0
    assert capsys.readouterr().out == ""
    st = W.load_state("sess-neutral")
    assert st.notified == set()
    assert st.predraft == {}


def test_main_offers_once_no_renotify(monkeypatch, capsys):
    sid = "sess-once"
    # First frustrated prompt -> nudge.
    rc1 = run_main(
        monkeypatch, "UserPromptSubmit",
        {"session_id": sid, "prompt": "ugh this is broken"},
    )
    assert rc1 == 0
    assert capsys.readouterr().out != ""

    # A DIFFERENT frustrated prompt (same signal-class) -> NO second nudge.
    rc2 = run_main(
        monkeypatch, "UserPromptSubmit",
        {"session_id": sid, "prompt": "come on, seriously"},
    )
    assert rc2 == 0
    assert capsys.readouterr().out == ""


def test_main_disable_file_suppresses_everything(monkeypatch, capsys, hermetic):
    hermetic["off_file"].write_text("")  # flip the kill switch
    rc = run_main(
        monkeypatch, "UserPromptSubmit",
        {"session_id": "sess-off", "prompt": "ugh wtf this is broken"},
    )
    assert rc == 0
    assert capsys.readouterr().out == ""
    # Disabled = total no-op: state file is never even created.
    assert not W.state_path("sess-off").exists()


def test_main_post_tool_storm_flushes_on_next_prompt(monkeypatch, capsys):
    sid = "sess-storm"
    err = {"session_id": sid, "tool_name": "Bash", "tool_response": {"is_error": True}}
    for _ in range(W.RETRY_STORM_THRESHOLD):
        rc = run_main(monkeypatch, "PostToolUse", err)
        assert rc == 0
        assert capsys.readouterr().out == ""  # PostToolUse never emits

    # Storm staged; a (neutral) prompt surfaces it.
    rc = run_main(
        monkeypatch, "UserPromptSubmit",
        {"session_id": sid, "prompt": "what now"},
    )
    assert rc == 0
    obj = json.loads(capsys.readouterr().out)
    assert "/fb" in obj["hookSpecificOutput"]["additionalContext"]

    st = W.load_state(sid)
    assert "retry_storm" in st.notified
    assert "retry_storm" in st.predraft
    assert st.pending == []  # flushed


def test_main_malformed_stdin_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("this is not json {{{"))
    rc = W.main(["UserPromptSubmit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_empty_stdin_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert W.main(["Stop"]) == 0
    assert capsys.readouterr().out == ""


def test_main_unknown_event_is_noop(monkeypatch, capsys):
    rc = run_main(
        monkeypatch, "SessionEnd",
        {"session_id": "sess-end", "hook_event_name": "SessionEnd"},
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_never_raises_on_garbage_payload(monkeypatch, capsys):
    # A list payload (not a dict) and a weird event must not raise.
    monkeypatch.setattr(sys, "stdin", io.StringIO("[1, 2, 3]"))
    assert W.main(["UserPromptSubmit"]) == 0
    assert capsys.readouterr().out == ""
