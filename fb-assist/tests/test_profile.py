"""Tests for fb_assist.profile — the set-once privacy profile / policy store.

The precedence engine is load-bearing: it decides what gets stripped before feedback ships,
so most-specific-wins, the hard-floor guarantee, and the entity rescue/deny merge each get
direct coverage. Everything is hermetic (``tmp_path`` profiles + ``.feedbackpolicy`` files);
the real ``~/.config/fb-assist/`` is never read or written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Importable when run directly (pytest handles this too).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import profile as PF  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def prof(*rules, learned=None) -> dict:
    """A well-formed global profile dict from inline rules."""
    return {"version": 1, "rules": list(rules), "learned": list(learned or [])}


def write_policy(repo_root: Path, obj) -> Path:
    repo_root.mkdir(parents=True, exist_ok=True)
    p = repo_root / PF.POLICY_FILENAME
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# glob matching — ** crosses segments, * does not                              #
# --------------------------------------------------------------------------- #
def test_glob_globstar_vs_single_star():
    assert PF._glob_match("/work/**", "/work/contoso/src")
    assert PF._glob_match("/work/**", "/work")  # trailing /** also matches the dir itself
    assert PF._glob_match("/work/*", "/work/contoso")
    assert not PF._glob_match("/work/*", "/work/contoso/src")  # single star stays in a segment
    assert not PF._glob_match("/work/**", "/other/contoso")


def test_glob_expands_home():
    home_glob = "~/work/**"
    import os

    inside = os.path.expanduser("~/work/proj/file.py")
    assert PF._glob_match(home_glob, inside)


# --------------------------------------------------------------------------- #
# most-specific glob wins (within a tier)                                      #
# --------------------------------------------------------------------------- #
def test_most_specific_glob_wins():
    p = prof(
        {"id": "broad", "match": {"path_glob": "/work/**"}, "action": "mask", "categories": ["file_contents"]},
        {"id": "deep", "match": {"path_glob": "/work/contoso/**"}, "action": "genericize", "categories": ["file_contents"]},
    )
    res = PF.resolve("/work/contoso/src", profile=p)
    d = res["decisions"]["category:file_contents"]
    assert d["action"] == "genericize"  # the deeper glob wins
    assert d["rule_id"] == "deep"
    assert res["source_per_rule"]["category:file_contents"] == "global"

    # A cwd that only the broad rule matches falls back to it.
    res2 = PF.resolve("/work/other/src", profile=p)
    assert res2["decisions"]["category:file_contents"]["rule_id"] == "broad"


def test_action_for_falls_back_to_bare_default():
    p = prof(
        {"id": "default", "match": {}, "action": "genericize"},  # blanket default, no categories
        {"id": "files", "match": {}, "action": "strip", "categories": ["file_contents"]},
    )
    res = PF.resolve("/anywhere", profile=p)
    assert PF.action_for(res, "file_contents") == "strip"      # category-specific
    assert PF.action_for(res, "human_prompts") == "genericize"  # bare default
    assert PF.action_for(res, None) == "genericize"


# --------------------------------------------------------------------------- #
# tier ordering: session > repo > global                                      #
# --------------------------------------------------------------------------- #
def test_tier_ordering_session_beats_repo_beats_global(tmp_path):
    repo_root = tmp_path / "contoso"
    write_policy(repo_root, {"rules": [
        {"id": "repo-rule", "match": {}, "action": "mask", "categories": ["file_contents"]},
    ]})
    p = prof(
        {"id": "global-rule", "match": {}, "action": "genericize", "categories": ["file_contents"]},
    )
    session_rules = [
        {"id": "sess-rule", "match": {}, "action": "allow", "categories": ["file_contents"]},
    ]

    # global only
    r_g = PF.resolve("/x", profile=p)
    assert r_g["decisions"]["category:file_contents"]["rule_id"] == "global-rule"

    # repo overrides global (even though both have empty matches — tier breaks the tie)
    r_r = PF.resolve("/x", repo_root=repo_root, profile=p)
    d_r = r_r["decisions"]["category:file_contents"]
    assert d_r["rule_id"] == "repo-rule" and d_r["source"] == "repo"

    # session overrides repo + global
    r_s = PF.resolve("/x", repo_root=repo_root, session_rules=session_rules, profile=p)
    d_s = r_s["decisions"]["category:file_contents"]
    assert d_s["rule_id"] == "sess-rule" and d_s["source"] == "session"


def test_repo_match_by_basename(tmp_path):
    repo_root = tmp_path / "contoso"
    repo_root.mkdir()
    p = prof({"id": "by-repo", "match": {"repo": "contoso"}, "action": "strip", "categories": ["paths"]})
    res = PF.resolve("/whatever", repo_root=repo_root, profile=p)
    assert res["decisions"]["category:paths"]["rule_id"] == "by-repo"
    # A different repo name doesn't match.
    other = tmp_path / "widget-backend"
    other.mkdir()
    res2 = PF.resolve("/whatever", repo_root=other, profile=p)
    assert "category:paths" not in res2["decisions"]


# --------------------------------------------------------------------------- #
# hard floor: not loosened by a more-specific non-hard rule; unlock overrides  #
# --------------------------------------------------------------------------- #
def test_hard_floor_not_loosened_but_unlock_overrides():
    p = prof(
        # global HARD floor: never send file_contents under /work (a privacy floor).
        {"id": "floor", "match": {"path_glob": "/work/**"}, "action": "never_send",
         "categories": ["file_contents"], "hard": True},
    )
    # A more-specific, higher-tier (session) rule tries to LOOSEN it to allow.
    session_rules = [
        {"id": "loosen", "match": {"session_id": "S1"}, "action": "allow", "categories": ["file_contents"]},
    ]

    # Without unlock: the hard floor holds despite the more-specific allow.
    res = PF.resolve("/work/contoso", session_id="S1", session_rules=session_rules, profile=p)
    d = res["decisions"]["category:file_contents"]
    assert d["action"] == "never_send"
    assert d["floor_enforced"] is True and d["hard"] is True and d["source"] == "global"
    assert any(f["id"] == "floor" for f in res["hard_floors"])

    # With an explicit unlock of the floor's id: the more-specific allow now wins.
    res_u = PF.resolve("/work/contoso", session_id="S1", session_rules=session_rules,
                       profile=p, unlock=["floor"])
    d_u = res_u["decisions"]["category:file_contents"]
    assert d_u["action"] == "allow" and d_u["floor_enforced"] is False and d_u["source"] == "session"
    assert res_u["hard_floors"] == []  # unlocked floor is no longer in force


def test_more_specific_non_hard_rule_DOES_loosen_a_non_hard_rule():
    # Sanity counterpart: a plain (non-hard) broad rule IS loosened by a specific rule.
    p = prof(
        {"id": "broad", "match": {"path_glob": "/work/**"}, "action": "strip", "categories": ["file_contents"]},
        {"id": "deep", "match": {"path_glob": "/work/oss/**"}, "action": "allow", "categories": ["file_contents"]},
    )
    res = PF.resolve("/work/oss/proj", profile=p)
    assert res["decisions"]["category:file_contents"]["action"] == "allow"


def test_specific_hard_rule_can_tighten():
    # A more-specific HARD rule that is *stricter* simply wins (tightening is always allowed).
    p = prof(
        {"id": "broad", "match": {"path_glob": "/work/**"}, "action": "genericize", "categories": ["file_contents"]},
        {"id": "deep", "match": {"path_glob": "/work/secret/**"}, "action": "never_send",
         "categories": ["file_contents"], "hard": True},
    )
    res = PF.resolve("/work/secret/x", profile=p)
    d = res["decisions"]["category:file_contents"]
    assert d["action"] == "never_send" and d["hard"] is True


# --------------------------------------------------------------------------- #
# entity allow rescue + deny strip merge across layers                         #
# --------------------------------------------------------------------------- #
def test_entity_allow_and_deny_merge_across_layers(tmp_path):
    repo_root = tmp_path / "contoso"
    write_policy(repo_root, {"rules": [
        {"id": "repo-ents", "match": {}, "entities": {"allow": ["Contoso"], "deny": ["Zephyr"]}},
    ]})
    p = prof(
        {"id": "global-ents", "match": {}, "entities": {"deny": ["Athena"], "allow": ["Mercury"]}},
    )
    res = PF.resolve("/x", repo_root=repo_root, profile=p)
    ents = res["entities"]
    assert set(ents["deny"]) == {"Athena", "Zephyr"}        # denies union across layers
    assert set(ents["allow"]) == {"Contoso", "Mercury"}    # allows (rescues) union across layers
    # provenance: which layer each entity decision came from
    assert res["source_per_rule"]["entity:Contoso"] == "repo"
    assert res["source_per_rule"]["entity:Athena"] == "global"


def test_hard_deny_not_rescued_by_allow_unless_unlocked():
    p = prof(
        {"id": "floor-deny", "match": {}, "entities": {"deny": ["Athena"]}, "hard": True},
    )
    session_rules = [
        {"id": "rescue", "match": {"session_id": "S1"}, "entities": {"allow": ["Athena"]}},
    ]
    # Hard deny is un-removable: the more-specific allow does NOT rescue it.
    res = PF.resolve("/x", session_id="S1", session_rules=session_rules, profile=p)
    assert "Athena" in res["entities"]["deny"]
    assert "Athena" not in res["entities"]["allow"]
    # Unlocking the hard deny lets the rescue through.
    res_u = PF.resolve("/x", session_id="S1", session_rules=session_rules, profile=p, unlock=["Athena"])
    assert "Athena" in res_u["entities"]["allow"]
    assert "Athena" not in res_u["entities"]["deny"]


# --------------------------------------------------------------------------- #
# learn() round-trips: append -> resolve applies it silently                    #
# --------------------------------------------------------------------------- #
def test_learn_appends_and_resolve_applies(tmp_path):
    profile_path = tmp_path / "profile.json"
    # Start from an empty (missing) profile — load_profile must not crash.
    base = PF.load_profile(profile_path)
    assert base == {"version": 1, "rules": [], "learned": []}

    correction = {
        "entity": "Contoso",
        "action": "allow",                 # user rescued a wrongly-eaten brand
        "repo": "contoso",
        "session_id": "S9",
        "trigger": "user un-redacted brand 'Contoso'",
    }
    rule = PF.learn(correction, profile_path=profile_path, ts=1_700_000_000.0)
    assert rule["match"] == {"repo": "contoso"}            # narrowest sensible scope
    assert rule["entities"]["allow"] == ["Contoso"]
    assert rule["provenance"] == {"ts": 1_700_000_000.0, "session": "S9", "trigger": correction["trigger"]}
    assert rule["id"].startswith("learned-")

    # Persisted into the learned block on disk.
    on_disk = json.loads(profile_path.read_text())
    assert on_disk["learned"][0]["id"] == rule["id"]

    # Next resolve() applies it silently (scoped to the contoso repo).
    repo_root = tmp_path / "contoso"
    repo_root.mkdir()
    res = PF.resolve("/work/contoso", repo_root=repo_root, profile=PF.load_profile(profile_path))
    assert "Contoso" in res["entities"]["allow"]
    # ...but NOT for a different repo (narrow scope held).
    other = tmp_path / "widget-backend"
    other.mkdir()
    res2 = PF.resolve("/work/fuel", repo_root=other, profile=PF.load_profile(profile_path))
    assert "Contoso" not in res2["entities"]["allow"]


def test_learn_codename_deny_and_ts_default(tmp_path):
    profile_path = tmp_path / "p.json"
    rule = PF.learn(
        {"entity": "Zephyr", "action": "strip", "cwd": "/work/contoso", "trigger": "missed codename"},
        profile_path=profile_path,
    )
    assert rule["entities"]["deny"] == ["Zephyr"]
    assert rule["match"] == {"path_glob": "/work/contoso"}  # path scope when no repo given
    assert isinstance(rule["provenance"]["ts"], float)        # ts defaulted to time.time()

    res = PF.resolve("/work/contoso", profile=PF.load_profile(profile_path))
    assert "Zephyr" in res["entities"]["deny"]


def test_learn_dry_run_does_not_persist(tmp_path):
    profile_path = tmp_path / "p.json"
    PF.learn({"entity": "X", "action": "strip"}, profile_path=profile_path, persist=False)
    assert not profile_path.exists()


# --------------------------------------------------------------------------- #
# read_policy / load_profile                                                   #
# --------------------------------------------------------------------------- #
def test_read_policy_json_and_missing(tmp_path):
    repo_root = tmp_path / "repo"
    # Missing file -> {}
    assert PF.read_policy(repo_root) == {}
    # JSON works.
    write_policy(repo_root, {"rules": [{"id": "r", "match": {}, "action": "mask"}]})
    pol = PF.read_policy(repo_root)
    assert pol["rules"][0]["id"] == "r"
    # repo_root=None -> {}
    assert PF.read_policy(None) == {}


def test_read_policy_bare_list_is_wrapped(tmp_path):
    repo_root = tmp_path / "repo"
    write_policy(repo_root, [{"id": "r1", "match": {}, "action": "strip"}])
    pol = PF.read_policy(repo_root)
    assert pol == {"rules": [{"id": "r1", "match": {}, "action": "strip"}]}


def test_load_profile_missing_and_malformed(tmp_path):
    assert PF.load_profile(tmp_path / "nope.json") == {"version": 1, "rules": [], "learned": []}
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json and not yaml: : :", encoding="utf-8")
    loaded = PF.load_profile(bad)
    # Never crashes; degrades to an empty well-formed profile (or a YAML-parsed dict that is
    # still normalized to carry rules/learned).
    assert "rules" in loaded and "learned" in loaded


def test_load_profile_bare_list(tmp_path):
    f = tmp_path / "list.json"
    f.write_text(json.dumps([{"id": "x", "match": {}, "action": "strip"}]), encoding="utf-8")
    loaded = PF.load_profile(f)
    assert loaded["rules"][0]["id"] == "x" and loaded["learned"] == []


@pytest.mark.skipif(not PF._HAVE_YAML, reason="PyYAML not importable")
def test_read_policy_yaml_when_available(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / PF.POLICY_FILENAME).write_text(
        "rules:\n  - id: y1\n    match: {}\n    action: genericize\n    categories: [file_contents]\n",
        encoding="utf-8",
    )
    pol = PF.read_policy(repo_root)
    assert pol["rules"][0]["id"] == "y1"
    res = PF.resolve("/x", repo_root=repo_root, profile=PF.load_profile(tmp_path / "none.json"))
    assert res["decisions"]["category:file_contents"]["action"] == "genericize"


# --------------------------------------------------------------------------- #
# apply_entity_rules — un-redact an allow-listed brand                         #
# --------------------------------------------------------------------------- #
def test_apply_entity_rules_rescues_allow_listed_brand():
    redaction_map = [
        {"category": "DATE_TIME", "original": "Contoso", "replacement": "‹DATE_TIME›"},
        {"category": "EMAIL_ADDRESS", "original": "dana@example.com", "replacement": "‹EMAIL›"},
        {"category": "PERSON", "original": "Athena", "replacement": "‹PERSON›"},
    ]
    resolved = {"entities": {"allow": ["Contoso"], "deny": ["Athena"]}}
    out = PF.apply_entity_rules(redaction_map, resolved)
    originals = [e["original"] for e in out]
    assert "Contoso" not in originals          # brand rescued (un-redacted)
    assert "dana@example.com" in originals       # real PII still redacted
    assert "Athena" in originals                 # deny entity stays redacted here (deny is upstream)
    # Purely functional: input untouched, new list returned.
    assert len(redaction_map) == 3
    assert out is not redaction_map


def test_apply_entity_rules_noop_without_allow_list():
    rmap = [{"category": "EMAIL", "original": "a@b.com", "replacement": "x"}]
    out = PF.apply_entity_rules(rmap, {"entities": {"deny": ["Z"]}})
    assert out == rmap and out is not rmap       # copy, but content-equal


# --------------------------------------------------------------------------- #
# return-shape contract                                                        #
# --------------------------------------------------------------------------- #
def test_resolve_return_shape_keys():
    res = PF.resolve("/x", profile=prof({"match": {}, "action": "mask", "categories": ["paths"]}))
    for key in ("decisions", "effective_rules", "hard_floors", "source_per_rule", "entities"):
        assert key in res
    assert set(res["entities"]) == {"deny", "allow"}
    assert isinstance(res["effective_rules"], list) and res["effective_rules"][0]["action"] == "mask"
