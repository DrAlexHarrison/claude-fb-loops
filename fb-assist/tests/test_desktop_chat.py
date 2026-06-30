"""Tests for fb_assist.desktop_chat — the claude.ai / Desktop chat (export) edge.

Proves the export-JSON co-pilot on a SYNTHETIC mini-export that carries pattern-
valid FAKE secrets + real-shaped PII, in BOTH message shapes (a ``content[]`` block
array AND a bare top-level ``text`` string):

  * ``message_text`` reads both shapes (and a raw dict, and degrades to "");
  * the export parses as a JSON array AND as a JSONL twin (format sniff);
  * every planted sentinel is BYTE-ABSENT from the redacted output;
  * the HARD two-layer floor (scan_secrets + PII regex) over the OUTPUT is empty;
  * the before/after shows real redactions and the effort signal is attached;
  * the transform is pure — the input conversation is never mutated.

All synthetic values are FAKE (pattern-valid, never real credentials). USE_TF=0 is
forced so importing the sibling redactor never trips the transformers TF path.

Run:  USE_TF=0 python -m pytest tests/test_desktop_chat.py -q
"""

import copy
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import desktop_chat as D  # noqa: E402
from fb_assist.redact import _scan_pii_regex, scan_secrets  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample-export.json"

# Skip the (network-bound) GLiNER pass everywhere — tests stay offline + fast. The
# deterministic floor + Presidio cover every planted sentinel without it.
NOG = dict(use_gliner=False)

# The planted sentinels that MUST NOT survive in the output. Each is caught by a
# DETERMINISTIC layer (scan_secrets regex, or the PII/path regex floor), so the
# byte-absence assertions hold regardless of whether Presidio/GLiNER are available.
DET_SENTINELS = [
    "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLLMMMMNNNNOOOO",  # ANTHROPIC_KEY
    "dana.reed@contoso-labs.com",                                                # EMAIL_ADDRESS
    "AKIAIOSFODNN7EXAMPLE",                                                       # AWS_ACCESS_KEY
    "123-45-6789",                                                                # US_SSN
    "10.0.0.42",                                                                  # IP_ADDRESS
    "/home/dana/code/contoso-internal/billing",                                   # FS_PATH
]
STRIPE_SENTINEL = "sk_test_FAKEfbassist0001"  # conversation 3 (STRIPE_KEY)


# --------------------------------------------------------------------------- #
# message_text — both shapes
# --------------------------------------------------------------------------- #
def test_message_text_content_array_shape():
    convs = D.parse_export(FIXTURE)
    m0 = convs[0].messages[0]  # content[] block array, bare text == ""
    assert isinstance(m0.raw.get("content"), list) and m0.raw["content"]
    assert m0.raw.get("text") == ""
    txt = D.message_text(m0)
    assert "keeps FREEZING" in txt
    assert txt == m0.text  # property delegates to the module fn


def test_message_text_bare_text_shape():
    convs = D.parse_export(FIXTURE)
    m2 = convs[0].messages[2]  # content == [] (empty), bare text populated
    assert m2.raw.get("content") == []
    assert isinstance(m2.raw.get("text"), str) and m2.raw["text"]
    txt = D.message_text(m2)
    assert "Rotated." in txt and "123-45-6789" in txt


def test_message_text_accepts_raw_dict_and_degrades():
    convs = D.parse_export(FIXTURE)
    raw = convs[0].messages[2].raw
    assert D.message_text(raw).startswith("Rotated.")     # raw dict works
    assert D.message_text({}) == ""                        # empty dict -> ""
    assert D.message_text({"content": "nope"}) == ""       # wrong type, no bare text
    assert D.message_text(None) == ""                      # non-dict -> ""
    # content[] present but no usable text block -> falls back to bare text
    assert D.message_text({"content": [{"type": "thinking", "text": "x"}],
                           "text": "fallback"}) == "fallback"


def test_role_maps_human_to_user():
    convs = D.parse_export(FIXTURE)
    assert convs[0].messages[0].role == "user"        # sender "human"
    assert convs[0].messages[1].role == "assistant"


# --------------------------------------------------------------------------- #
# parsing — array AND jsonl twin (format sniff)
# --------------------------------------------------------------------------- #
def test_parse_export_array():
    convs = D.parse_export(FIXTURE)
    assert len(convs) == 3
    assert convs[0].name == "Feedback flow keeps freezing on submit"
    assert convs[1].name == "Untitled"
    assert len(convs[0].messages) == 4


def test_parse_export_jsonl_twin(tmp_path):
    """The same conversations, emitted as a line-delimited JSONL twin, ingest
    identically (some third-party export tools call the artifact .jsonl)."""
    data = json.loads(FIXTURE.read_text())
    twin = tmp_path / "conversations.jsonl"
    twin.write_text("\n".join(json.dumps(c) for c in data) + "\n")
    convs = D.parse_export(twin)
    assert len(convs) == 3
    assert [c.uuid for c in convs] == [c["uuid"] for c in data]
    # and redaction works the same on the JSONL-sourced conversation
    fb = D.redact_conversation(convs[0], **NOG)
    for s in DET_SENTINELS:
        assert s not in fb.rendered_after


def test_parse_export_accepts_loaded_list():
    data = json.loads(FIXTURE.read_text())
    convs = D.parse_export(data)
    assert len(convs) == 3 and convs[0].uuid == data[0]["uuid"]


# --------------------------------------------------------------------------- #
# select_conversation — the "this one issue" picker
# --------------------------------------------------------------------------- #
def test_select_conversation_by_index_uuid_needle():
    by_index = D.select_conversation(FIXTURE, index=2)
    assert by_index is not None and by_index.name == "Stripe webhook retry storm"

    by_uuid = D.select_conversation(FIXTURE, uuid="22222222-2222-4222-8222-222222222222")
    assert by_uuid is not None and by_uuid.name == "Untitled"

    by_needle = D.select_conversation(FIXTURE, needle="freezing")
    assert by_needle is not None and by_needle.uuid.startswith("11111111")

    # needle that matches only message text (not name/summary)
    by_body = D.select_conversation(FIXTURE, needle="ICU pluralization")
    assert by_body is not None and by_body.name == "Untitled"

    assert D.select_conversation(FIXTURE, index=99) is None
    assert D.select_conversation(FIXTURE, uuid="does-not-exist") is None


# --------------------------------------------------------------------------- #
# the redaction hero — byte-absence + the HARD two-layer floor
# --------------------------------------------------------------------------- #
def test_every_sentinel_byte_absent_from_output():
    conv = D.select_conversation(FIXTURE, index=0)
    fb = D.redact_conversation(conv, **NOG)
    for s in DET_SENTINELS:
        assert s not in fb.rendered_after, f"sentinel survived in output: {s!r}"
    # and absent from the redaction_map's rendered replacements / serialized artifact
    blob = json.dumps([{k: v for k, v in e.items() if k != "original"}
                       for e in fb.redaction_map], ensure_ascii=False)
    for s in DET_SENTINELS:
        assert s not in blob


def test_hard_floor_clean_over_actual_output_bytes():
    """The HARD, machine-decidable gate: the deterministic floor (scan_secrets +
    PII regex) over the ACTUAL output bytes is empty. Re-asserted independently of
    the driver's own ``floor_clean`` bookkeeping."""
    conv = D.select_conversation(FIXTURE, index=0)
    fb = D.redact_conversation(conv, **NOG)
    residual = scan_secrets(fb.rendered_after) + _scan_pii_regex(fb.rendered_after)
    assert residual == [], [r.entity for r in residual]
    assert fb.floor_clean is True
    assert fb.floor_residual == []


def test_stripe_key_redacted_in_third_conversation():
    conv = D.select_conversation(FIXTURE, index=2)
    fb = D.redact_conversation(conv, **NOG)
    assert STRIPE_SENTINEL not in fb.rendered_after
    assert fb.floor_clean is True
    cats = {e["category"] for e in fb.redaction_map}
    assert "STRIPE_KEY" in cats


# --------------------------------------------------------------------------- #
# before/after + effort signal
# --------------------------------------------------------------------------- #
def test_before_after_shows_real_redactions():
    conv = D.select_conversation(FIXTURE, index=0)
    fb = D.redact_conversation(conv, **NOG)

    changed = [t for t in fb.turns if t.changed]
    assert changed, "expected at least one redacted turn"
    assert fb.redaction_map, "expected a non-empty redaction_map"

    # the deterministic categories MUST be present (Presidio extras are bonus)
    cats = {e["category"] for e in fb.redaction_map}
    for must in ("ANTHROPIC_KEY", "EMAIL_ADDRESS", "AWS_ACCESS_KEY",
                 "US_SSN", "IP_ADDRESS", "FS_PATH"):
        assert must in cats, f"missing deterministic redaction: {must}"

    # before holds the secret; after holds the marker — a genuine transform
    t0 = fb.turns[0]
    assert "sk-ant-api03-" in t0.before
    assert "‹ANTHROPIC_KEY›" in t0.after
    assert "sk-ant-api03-AAAA" not in t0.after

    rendered = D.render_before_after(fb)
    assert "BEFORE" in rendered and "AFTER" in rendered
    assert "‹ANTHROPIC_KEY›" in rendered


def test_effort_signal_attached():
    conv = D.select_conversation(FIXTURE, index=0)
    fb = D.redact_conversation(conv, level="genericize", quality=4,
                               alignment_confidence=5, **NOG)
    sig = fb.effort_signal
    assert sig["redaction"] == "genericize"
    assert sig["quality"] == 4
    assert sig["alignment_confidence"] == 5
    assert sig["summary"]["floor_clean"] is True
    assert sig["summary"]["redactions"] == fb.redaction_count

    # the reused package footer + the gate proof line render
    footer = D.render_effort_signal(fb)
    assert "[fb-assist effort signal]" in footer
    assert "redaction=genericize" in footer
    assert "CLEAN" in footer
    assert "PASS" in footer  # genericize verification bar


def test_included_stripped_built_from_redaction_map():
    conv = D.select_conversation(FIXTURE, index=0)
    fb = D.redact_conversation(conv, **NOG)
    summary = D.render_included_stripped(fb)
    assert "INCLUDED" in summary and "STRIPPED" in summary
    assert f"{fb.redaction_count} values" in summary
    # the summary must not leak a raw sentinel value
    for s in DET_SENTINELS:
        assert s not in summary


def test_no_narrative_redactions_path():
    """A conversation with no narrative text (attachment-only / empty turns) yields
    zero redactions and the 'no narrative redactions' render — deterministic, since
    there is no text for any detector (regex OR NER) to fire on."""
    inert = {
        "uuid": "inert-0000",
        "name": "Attachment only",
        "chat_messages": [
            {"uuid": "e1", "sender": "human", "content": [], "text": "   ",
             "attachments": [{"file_name": "x.png", "file_type": "image/png"}], "files": []},
            {"uuid": "e2", "sender": "assistant", "content": [], "text": "",
             "attachments": [], "files": []},
        ],
    }
    conv = D.parse_export([inert])[0]
    fb = D.redact_conversation(conv, **NOG)
    assert fb.redaction_count == 0
    assert fb.counts["turns_rendered"] == 0
    assert fb.floor_clean is True
    assert "no narrative redactions" in D.render_before_after(fb)


# --------------------------------------------------------------------------- #
# purity + round-trip
# --------------------------------------------------------------------------- #
def test_input_conversation_never_mutated():
    convs = D.parse_export(FIXTURE)
    conv = convs[0]
    before_raw = copy.deepcopy(conv.raw)
    before_text = D.message_text(conv.messages[0])
    _ = D.redact_conversation(conv, **NOG)
    assert conv.raw == before_raw, "redact_conversation mutated the input!"
    assert D.message_text(conv.messages[0]) == before_text


def test_genericized_conversation_roundtrip():
    conv = D.select_conversation(FIXTURE, index=0)
    raw_before = copy.deepcopy(conv.raw)
    fb = D.redact_conversation(conv, **NOG)
    gen = D.genericized_conversation(conv, fb)

    # input still untouched
    assert conv.raw == raw_before

    # the genericized dict is valid + re-ingestable, and carries no sentinel
    blob = json.dumps(gen, ensure_ascii=False)
    for s in DET_SENTINELS:
        assert s not in blob
    reparsed = D.parse_export([gen])
    assert len(reparsed) == 1
    assert len(reparsed[0].messages) == len(conv.messages)
    # the round-trip text equals the redacted text for a known turn
    assert "‹ANTHROPIC_KEY›" in D.message_text(reparsed[0].messages[0])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_defaults_to_synthetic_fixture(capsys):
    rc = D.main([])  # no args => default fixture, conversation 0
    out = capsys.readouterr().out
    assert rc == 0  # floor clean => exit 0
    assert "BEFORE" in out and "AFTER" in out
    assert "[fb-assist effort signal]" in out
    assert "CLEAN" in out
    for s in DET_SENTINELS:
        assert s not in out


def test_cli_list_and_json_modes(capsys):
    assert D.main(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "3 conversation(s)" in listing

    assert D.main(["--conversation", "0", "--json"]) == 0
    js = capsys.readouterr().out
    payload = json.loads(js)
    assert payload["floor_clean"] is True
    assert payload["effort_signal"]["redaction"] == "genericize"
    for s in DET_SENTINELS:
        assert s not in js


def test_cli_select_by_needle(capsys):
    rc = D.main(["--conversation", "Stripe", "--max-turns", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert STRIPE_SENTINEL not in out
