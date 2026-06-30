"""fb_os.fixtures — generate synthetic ``stage_review`` bundles for the demo + tests.

**Never Alex's real data** — every bundle here is hand-authored, already-redacted
synthetic feedback, written in the exact on-disk shape ``fb_assist.package.Payload.stage``
produces (``description.txt`` + a per-session ``.jsonl`` + ``effort-signal.json``,
and occasionally an additive ``artifact.json``). Used by ``make demo`` and the tests.

The set is designed to exercise every path of the core:
  * several multi-artifact **themes** that should cluster,
  * one **singleton** with unique vocabulary -> suppressed by the min-cluster-size floor,
  * one **planted-secret** bundle -> quarantined by the leak-scan floor,
  * a mix of surfaces (cli/ide), effort-signal sidecars vs footer-only, and a manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

# Each theme: a list of (artifact_id, surface, description, effort_signal, mode).
# mode: "sidecar" (write effort-signal.json), "footer" (only the description footer),
#       "manifest" (also write an artifact.json), "report_only" (no .jsonl).
_THEMES = {
    "attach_scope": [
        ("a_attach_01", "cli",
         "I wanted to send feedback about one earlier session but /feedback only lets me pick a time window, "
         "not a single past session. Attaching just that one conversation by scope would be huge for privacy.",
         {"redaction": "surgical(1 override)", "quality": 5, "alignment_confidence": 5, "reputation_token": "rep_kf3a"},
         "sidecar"),
        ("a_attach_02", "ide",
         "Feedback attach scope is too coarse for privacy. I want to attach one specific past session by scope, "
         "not the whole time window. A per-session attach scope would let me send feedback on sensitive code safely.",
         {"redaction": "surgical(2 overrides)", "quality": 4, "alignment_confidence": 5, "reputation_token": None},
         "sidecar"),
        ("a_attach_03", "cli",
         "Privacy concern: when I attach a session to feedback it pulls the current one too. I just want to "
         "attach one past session by scope. Please let /feedback narrow the attach window to a single session.",
         {"redaction": "recipe(default)", "quality": 4, "alignment_confidence": 4, "reputation_token": None},
         "footer"),
        ("a_attach_04", "cli",
         "Would love a way to scope the feedback attachment to exactly one previous session for privacy. "
         "The time-window attach is risky on shared repos.",
         {"redaction": "surgical(1 override)", "quality": 5, "alignment_confidence": 5, "reputation_token": "rep_kf3a"},
         "manifest"),
    ],
    "redaction_aggressive": [
        ("a_redact_01", "cli",
         "The redaction is too aggressive — it stripped my whole bash output even though there was no secret. "
         "I'd like redaction to be less aggressive and keep harmless command output.",
         {"redaction": "recipe(strict)", "quality": 4, "alignment_confidence": 4, "reputation_token": None},
         "sidecar"),
        ("a_redact_02", "ide",
         "Redaction false positive: it masked a public URL as if it were a secret. Aggressive redaction is "
         "removing useful context from my feedback. Can the redaction floor be tuned per category?",
         {"redaction": "recipe(strict)", "quality": 3, "alignment_confidence": 4, "reputation_token": None},
         "footer"),
        ("a_redact_03", "cli",
         "Redaction stripped too much — my error message got masked and now the feedback is useless. "
         "Less aggressive redaction of harmless output would help a lot.",
         {"redaction": "recipe(strict)", "quality": 4, "alignment_confidence": 3, "reputation_token": None},
         "sidecar"),
    ],
    "watcher_noise": [
        ("a_watch_01", "cli",
         "The proactive watcher prompts me too often to capture feedback. The nagging is noisy and interrupts "
         "my flow. I'd like the watcher to offer once and stay quiet.",
         {"redaction": "recipe(default)", "quality": 3, "alignment_confidence": 4, "reputation_token": None},
         "sidecar"),
        ("a_watch_02", "ide",
         "The watcher nags too often — it keeps prompting me to capture feedback on every rough patch. Too many "
         "prompts in one session. Please make the watcher offer once and stay quiet when I dismissed it.",
         {"redaction": "recipe(default)", "quality": 3, "alignment_confidence": 3, "reputation_token": None},
         "report_only"),
        ("a_watch_03", "cli",
         "The rough-patch watcher nags repeatedly in one session. The repeated prompts are distracting. "
         "One quiet offer per session would be ideal.",
         {"redaction": "recipe(default)", "quality": 4, "alignment_confidence": 4, "reputation_token": None},
         "sidecar"),
    ],
    "voice_latency": [
        ("a_voice_01", "cli",
         "The voice push-to-talk confirm is slow — there's a noticeable lag between pressing Super+V and the "
         "transcription appearing. The latency makes the voice confirm feel sluggish.",
         {"redaction": "recipe(default)", "quality": 4, "alignment_confidence": 5, "reputation_token": "rep_v9"},
         "sidecar"),
        ("a_voice_02", "cli",
         "Voice confirm latency is high. After I speak, the whisper transcription takes a few seconds. "
         "Faster local voice transcription would make the confirm flow much smoother.",
         {"redaction": "recipe(default)", "quality": 5, "alignment_confidence": 5, "reputation_token": "rep_v9"},
         "footer"),
        ("a_voice_03", "ide",
         "There is lag in the voice confirm — pressing the hotkey then waiting for transcription is slow. "
         "Please speed up the voice path; the latency breaks my concentration.",
         {"redaction": "recipe(default)", "quality": 4, "alignment_confidence": 4, "reputation_token": None},
         "sidecar"),
    ],
}

# A singleton with unique vocabulary — should be SUPPRESSED by min-cluster-size.
_SINGLETON = (
    "a_singleton_kerning", "cli",
    "The kerning of the monospace glyph ligatures in the changelog renderer looks slightly off on my niche "
    "terminal emulator with a custom Nerd Font patch.",
    {"redaction": "recipe(default)", "quality": 2, "alignment_confidence": 2, "reputation_token": None},
    "sidecar",
)

# A planted-secret bundle — must be QUARANTINED by the leak-scan floor, never clustered.
# (Synthetic, regex-shaped Anthropic key — exercises defense-in-depth, not a real key.)
_PLANTED_SECRET = (
    "a_planted_secret", "cli",
    "Quick note: the export worked but it printed my key sk-ant-api03-PLANTEDsecretVALUE1234567890abcdEF in "
    "the logs which seems wrong. Sharing the session so you can see the leak.",
    {"redaction": "none", "quality": 3, "alignment_confidence": 3, "reputation_token": None},
    "sidecar",
)

_FOOTER_KEYMAP = (("redaction", "redaction"), ("quality", "quality"),
                  ("alignment_confidence", "alignment_confidence"), ("reputation_token", "rep"))


def _render_footer(sig: dict) -> str:
    bits = []
    if sig.get("redaction"):
        bits.append(f"redaction={sig['redaction']}")
    if sig.get("quality") is not None:
        bits.append(f"quality={sig['quality']}")
    if sig.get("alignment_confidence") is not None:
        bits.append(f"alignment_confidence={sig['alignment_confidence']}")
    if sig.get("reputation_token"):
        bits.append(f"rep={sig['reputation_token']}")
    return "[fb-assist effort signal] " + "; ".join(bits) if bits else ""


def _jsonl_records(artifact_id: str, surface: str, secret: str | None = None) -> str:
    sid = f"sess-{artifact_id}"
    user_text = "‹human_prompts stripped: 96 chars›" if secret is None else \
        f"please review this output containing {secret}"
    recs = [
        {"type": "user", "uuid": f"{artifact_id}-u1", "sessionId": sid, "gitBranch": "‹gitBranch›",
         "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": f"{artifact_id}-a1", "sessionId": sid,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "‹assistant_text stripped: 140 chars›"}]}},
    ]
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n"


def _write_bundle(inbox: Path, artifact_id: str, surface: str, description: str,
                  sig: dict, mode: str, *, secret: str | None = None,
                  answers_question_id: str | None = None) -> Path:
    d = inbox / artifact_id
    d.mkdir(parents=True, exist_ok=True)

    footer = _render_footer(sig)
    full_desc = description.rstrip()
    if footer:
        full_desc = f"{full_desc}\n\n---\n{footer}"
    (d / "description.txt").write_text(full_desc, encoding="utf-8")

    if mode != "report_only":
        (d / f"sess-{artifact_id}.jsonl").write_text(
            _jsonl_records(artifact_id, surface, secret=secret), encoding="utf-8")

    if mode in ("sidecar", "manifest"):
        (d / "effort-signal.json").write_text(json.dumps(sig, indent=2), encoding="utf-8")

    # Write an additive artifact.json manifest when the bundle needs to DECLARE
    # something the three base files can't carry: a non-default surface, an explicit
    # manifest demo, or an answers_question_id (the loop-closing artifact). Pure-cli
    # sidecar/footer/report_only bundles stay on the derive path (no manifest), so
    # ingest's present-or-derive logic is exercised both ways.
    has_jsonl = mode != "report_only"
    write_manifest = (mode == "manifest") or (surface != "cli") or (answers_question_id is not None)
    if write_manifest:
        manifest = {
            "schema_version": "1.0", "artifact_id": artifact_id, "surface": surface,
            "session_ids": [f"sess-{artifact_id}"] if has_jsonl else [],
            "description_ref": "description.txt",
            "transcript_refs": [f"sess-{artifact_id}.jsonl"] if has_jsonl else [],
            "report_only": not has_jsonl, "answers_question_id": answers_question_id,
            "effort_signal": sig,
        }
        (d / "artifact.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return d


def generate_inbox(inbox_dir, *, include_secret: bool = True, include_singleton: bool = True) -> list[str]:
    """Write the full synthetic bundle set under ``inbox_dir``. Returns the artifact ids."""
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for theme in _THEMES.values():
        for (aid, surface, desc, sig, mode) in theme:
            _write_bundle(inbox, aid, surface, desc, sig, mode)
            ids.append(aid)
    if include_singleton:
        aid, surface, desc, sig, mode = _SINGLETON
        _write_bundle(inbox, aid, surface, desc, sig, mode)
        ids.append(aid)
    if include_secret:
        aid, surface, desc, sig, mode = _PLANTED_SECRET
        _write_bundle(inbox, aid, surface, desc, sig, mode,
                      secret="sk-ant-api03-PLANTEDsecretVALUE1234567890abcdEF")
        ids.append(aid)
    return ids


def write_answer_bundle(inbox_dir, question_id: str, *, artifact_id: str = "a_answer_loop") -> str:
    """Write a single bundle that *answers* an open question (closes the loop): its
    manifest sets ``answers_question_id`` to ``question_id``. Used by ``make demo``'s
    second pass and the loop test."""
    sig = {"redaction": "recipe(default)", "quality": 5, "alignment_confidence": 5, "reputation_token": "rep_kf3a"}
    desc = ("Answering your question: yes — per-session attach scope is exactly what I want for /feedback. "
            "A single past session by scope, not the whole window. That would resolve my privacy worry.")
    _write_bundle(Path(inbox_dir), artifact_id, "cli", desc, sig, "manifest",
                  answers_question_id=question_id)
    return artifact_id
