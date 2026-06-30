"""fb_assist.profile — the set-once privacy intelligence / policy store (Build 3, spec §10).

Why it exists (the biggest power-user win — "stop re-asking"):
A redaction co-author that re-asks "strip this? genericize that?" every session is
exhausting. The fix is a *persistent privacy profile* the user trains once: "always
genericize under ``~/work/**``", "codenames Athena/Zephyr → always strip", "repos
flagged ``.nofeedback`` → never send file contents." Pre-applied, never re-asked — so
power users "train it once, then watch it go quiet." Plus the redactor **learns from
corrections** (:func:`learn`): every override the user makes is remembered, scoped to the
narrowest sensible match, and auto-applied next time — Anthropic's "Claude proposes,
humans correct, the system learns" loop turned inward on the redactor itself.

Three layers, **most-specific-wins**, with **hard floors** (spec §10):

* **Global profile** — ``~/.config/fb-assist/profile.json`` (per-machine user data). Holds
  hand-authored ``rules`` + a ``learned`` block grown by :func:`learn`.
* **Per-repo policy** — ``<repo_root>/.feedbackpolicy`` (committed, team-shareable, like a
  ``.gitignore`` for feedback). JSON, or YAML when PyYAML is importable.
* **Session overrides** — passed in at :func:`resolve` time (highest specificity).

Precedence (the load-bearing logic — :func:`resolve`):
  1. A rule applies when its ``match`` (``path_glob`` / ``repo`` / ``session_id``; multiple
     keys = AND; empty match = always) fits the call site.
  2. Specificity tiers **session > repo > global**; within a tier a more-specific glob
     (more path segments, fewer wildcards) wins.
  3. Most-specific wins **per decision key** (a category, an entity, or the bare action) —
     **except** a rule marked ``hard`` is a privacy *floor*: a less-strict higher-specificity
     rule cannot *loosen* it without an explicit ``unlock``. Entity allow/deny lists merge
     across layers (union); ``hard`` denies are un-removable; the ``allow`` list is the
     brand/codename **rescue** list (e.g. "Saturday" — a real brand Presidio wrongly eats as
     a DATE_TIME — must be un-redacted), and the ``deny`` list is codenames detectors miss
     that must always be stripped.

Pure-stdlib core. JSON is canonical; ``.feedbackpolicy`` may be YAML *iff* PyYAML is present
(graceful JSON fallback — never hard-fail on the optional dep). No network. Every entry point
takes explicit paths/dicts so callers (and tests) stay hermetic — the real
``~/.config/fb-assist/`` is touched only when a path is not supplied.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

PathLike = Union[str, os.PathLike]

# Optional dependency: PyYAML for .feedbackpolicy. Importable -> used; absent -> JSON only.
# The module MUST work fully without it (spec / build rule), so this is never required.
try:  # pragma: no cover - trivial import guard
    import yaml  # type: ignore

    _HAVE_YAML = True
except Exception:  # pragma: no cover - environment dependent
    yaml = None  # type: ignore
    _HAVE_YAML = False

PROFILE_VERSION = 1
POLICY_FILENAME = ".feedbackpolicy"

# User data, per-machine (syncs across Alex's boxes out of band). A *stable* known location,
# deliberately overridable by env for the multi-machine setup and by param for tests.
DEFAULT_PROFILE_PATH = Path(
    os.environ.get("FB_ASSIST_PROFILE", str(Path.home() / ".config" / "fb-assist" / "profile.json"))
)

# Specificity tiers (higher == more specific). Most-specific-wins reads these first.
TIER_GLOBAL = 1
TIER_REPO = 2
TIER_SESSION = 3
_TIER_NAME = {TIER_GLOBAL: "global", TIER_REPO: "repo", TIER_SESSION: "session"}

# The transform vocabulary, ranked by how strict (privacy-protective) each action is. A
# ``hard`` floor at action X may not be *loosened* to a lower-ranked action by a more-specific
# non-hard rule. (mask/tokenize both hide the value from the outbound bundle; tokenize is
# locally reversible so it ranks just under mask. genericize keeps meaning; allow ships as-is.)
ACTIONS = ("never_send", "strip", "mask", "tokenize", "genericize", "allow")
STRICTNESS = {"never_send": 5, "strip": 4, "mask": 3, "tokenize": 2, "genericize": 1, "allow": 0}


# --------------------------------------------------------------------------- #
# Glob matching — ``**`` (globstar) aware, so a single ``*`` stays within one  #
# path segment and ``**`` crosses them (that's what makes a deeper glob strictly#
# more specific than a shallower one).                                          #
# --------------------------------------------------------------------------- #
def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """Translate a gitignore-ish path glob to an anchored regex.

    * ``**`` matches any number of path segments (including zero) — crosses ``/``.
    * ``*`` matches within a single segment (never ``/``); ``?`` one non-``/`` char.
    * ``~`` is expanded. A trailing ``/**`` also matches the base directory itself.
    """
    pattern = os.path.expanduser(pattern)
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:[^/]+/)*")  # zero or more whole segments
                elif out and out[-1] == "/":
                    out[-1] = "(?:/.*)?"  # trailing /** also matches the dir itself
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + r"/?$")


def _glob_match(pattern: str, path: str) -> bool:
    return _glob_to_regex(pattern).match(path) is not None


def _norm_path(p: Optional[PathLike]) -> Optional[str]:
    if p is None:
        return None
    return os.path.normpath(os.path.expanduser(os.fspath(p)))


# --------------------------------------------------------------------------- #
# Rule matching + specificity                                                  #
# --------------------------------------------------------------------------- #
def _match_applies(
    match: Optional[Mapping[str, Any]],
    cwd: Optional[str],
    session_id: Optional[str],
    repo_name: Optional[str],
) -> bool:
    """A rule applies iff every present ``match`` key fits the call site (keys AND together).

    An empty/missing ``match`` applies always (the global default). A key whose value is
    ``None``/``""`` is treated as "unset" (does not constrain).
    """
    if not match:
        return True
    pg = match.get("path_glob")
    if pg not in (None, ""):
        if cwd is None or not _glob_match(pg, cwd):
            return False
    repo = match.get("repo")
    if repo not in (None, ""):
        if repo_name is None or repo != repo_name:
            return False
    sid = match.get("session_id")
    if sid not in (None, ""):
        if session_id is None or sid != session_id:
            return False
    return True


def _specificity(rule: Mapping[str, Any], tier: int) -> tuple:
    """A sortable specificity key. Tier dominates; then number of AND-ed match keys; then a
    more-specific glob (more segments, fewer wildcards, longer). Empty match -> least specific.
    """
    m = rule.get("match") or {}
    n_keys = sum(1 for k in ("path_glob", "repo", "session_id") if m.get(k) not in (None, ""))
    seg = wild = glen = 0
    glob = m.get("path_glob")
    if glob:
        expanded = os.path.expanduser(glob)
        seg = expanded.count("/")
        wild = expanded.count("*") + expanded.count("?")
        glen = len(expanded)
    return (tier, n_keys, seg, -wild, glen)


def _aslist(x: Any) -> list:
    if x is None:
        return []
    if isinstance(x, (list, tuple, set)):
        return list(x)
    return [x]


# --------------------------------------------------------------------------- #
# Load / save — global profile + per-repo .feedbackpolicy                      #
# --------------------------------------------------------------------------- #
def _empty_profile() -> dict:
    return {"version": PROFILE_VERSION, "rules": [], "learned": []}


def _parse_text(text: str) -> Any:
    """JSON first (canonical); fall back to YAML only if PyYAML is importable. Returns the
    parsed object, or ``None`` if neither parser succeeds (caller decides the empty form)."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if _HAVE_YAML:
        try:
            return yaml.safe_load(text)
        except Exception:
            return None
    return None


def load_profile(path: Optional[PathLike] = None) -> dict:
    """Load the global profile; a missing/empty/unparseable file -> a well-formed empty
    profile (never crashes). Default path is ``~/.config/fb-assist/profile.json``.

    A bare top-level JSON list is accepted as ``rules``; a dict is normalized to always carry
    ``version`` / ``rules`` / ``learned`` so downstream code never key-errors.
    """
    path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return _empty_profile()
    data = _parse_text(text)
    if isinstance(data, list):
        return {"version": PROFILE_VERSION, "rules": list(data), "learned": []}
    if not isinstance(data, dict):
        return _empty_profile()
    data.setdefault("version", PROFILE_VERSION)
    data.setdefault("rules", [])
    data.setdefault("learned", [])
    return data


def read_policy(repo_root: Optional[PathLike]) -> dict:
    """Read ``<repo_root>/.feedbackpolicy`` (JSON, or YAML if PyYAML present). Missing/empty
    /unparseable -> ``{}``. A bare list is wrapped as ``{"rules": [...]}``."""
    if repo_root is None:
        return {}
    p = Path(repo_root) / POLICY_FILENAME
    try:
        text = p.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return {}
    data = _parse_text(text)
    if isinstance(data, list):
        return {"rules": list(data)}
    if isinstance(data, dict):
        return data
    return {}


def _atomic_write_text(path: PathLike, text: str) -> None:
    """Write text atomically (tmp in same dir -> fsync -> os.replace), creating parents.

    Atomic so a concurrent reader of the profile never sees a half-written file, and a crash
    mid-write leaves the prior profile intact — the learned block is durable, not lossy.
    """
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fbprofile-tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def save_profile(profile: Mapping[str, Any], path: Optional[PathLike] = None) -> str:
    """Persist a profile dict to ``path`` (default real profile path) as pretty JSON. Returns
    the path written. JSON is always the on-disk canonical form for the global profile."""
    path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    _atomic_write_text(path, json.dumps(profile, indent=2, ensure_ascii=False) + "\n")
    return str(path)


def _coerce_profile(profile: Any) -> dict:
    """Accept a profile dict, a path to load, or None (default path)."""
    if profile is None:
        return load_profile()
    if isinstance(profile, (str, os.PathLike)):
        return load_profile(profile)
    if isinstance(profile, Mapping):
        prof = dict(profile)
        prof.setdefault("rules", [])
        prof.setdefault("learned", [])
        return prof
    return _empty_profile()


def _rules_of(obj: Any) -> list[dict]:
    """Flatten a profile/policy dict's ``rules`` + ``learned`` into a rule list (dicts only)."""
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if not isinstance(obj, Mapping):
        return []
    rules: list = []
    rules.extend(obj.get("rules") or [])
    rules.extend(obj.get("learned") or [])
    return [r for r in rules if isinstance(r, dict)]


# --------------------------------------------------------------------------- #
# resolve — the precedence engine (the load-bearing logic)                     #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class _Candidate:
    rule: dict
    layer: str
    spec: tuple
    rid: str


def _rule_view(rule: Mapping[str, Any], layer: str, rid: str) -> dict:
    """A compact, transparency-friendly projection of a contributing rule."""
    return {
        "id": rid,
        "source": layer,
        "action": rule.get("action"),
        "match": dict(rule.get("match") or {}),
        "categories": list(rule.get("categories")) if rule.get("categories") else None,
        "entities": rule.get("entities"),
        "hard": bool(rule.get("hard")),
    }


def resolve(
    cwd: Optional[PathLike],
    session_id: Optional[str] = None,
    repo_root: Optional[PathLike] = None,
    session_rules: Optional[Sequence[Mapping[str, Any]]] = None,
    profile: Any = None,
    *,
    repo: Optional[str] = None,
    unlock: Optional[Iterable[str]] = None,
) -> dict:
    """Resolve the effective privacy policy for a call site across all three layers.

    Args:
      cwd: the working directory the feedback is about (matched against ``path_glob``).
      session_id: the session being reported (matched against ``match.session_id``).
      repo_root: repo root; its ``.feedbackpolicy`` is read as the repo layer, and its
        basename is the default repo name for ``match.repo``.
      session_rules: highest-specificity per-session override rules.
      profile: a profile dict, a path to one, or None (load the default global profile).
      repo: explicit repo name for ``match.repo`` (overrides ``repo_root``'s basename).
      unlock: rule ids (or entity names) whose ``hard`` floor the caller explicitly lifts.

    Returns a dict with the effective decisions and full provenance::

        {
          "decisions": { "<key>": {action, hard, source, rule_id, floor_enforced}, ... },
          "effective_rules": [ <rule view>, ... ],   # rules that won a key or moved an entity
          "hard_floors": [ <rule view>, ... ],        # hard rules in force (not unlocked)
          "source_per_rule": { "<key>": "<layer>", ... },  # where each decision came from
          "entities": { "deny": [...], "allow": [...] },
          "unlocked": [ ... ],
        }

    A *decision key* is ``"category:<cat>"``, a bare ``"action"`` (a default applying to all
    content), or ``"entity:<name>"``. Most-specific wins per key, with ``hard`` floors that a
    less-strict, higher-specificity rule cannot loosen unless its id is in ``unlock``.
    """
    unlock_set = set(unlock or [])
    prof = _coerce_profile(profile)
    cwd_norm = _norm_path(cwd)
    if repo is not None:
        repo_name: Optional[str] = repo
    elif repo_root is not None:
        repo_name = os.path.basename(os.path.normpath(os.fspath(repo_root))) or None
    else:
        repo_name = None

    # Assemble the layered rule sets (global = hand-authored + learned).
    layered: list[tuple[str, int, list[dict]]] = [("global", TIER_GLOBAL, _rules_of(prof))]
    if repo_root is not None:
        layered.append(("repo", TIER_REPO, _rules_of(read_policy(repo_root))))
    if session_rules:
        layered.append(("session", TIER_SESSION, [r for r in session_rules if isinstance(r, dict)]))

    # Keep only the rules that match this call site, tagged with specificity + a stable id.
    matching: list[_Candidate] = []
    for layer, tier, rules in layered:
        for idx, rule in enumerate(rules):
            if _match_applies(rule.get("match"), cwd_norm, session_id, repo_name):
                rid = str(rule.get("id") or f"{layer}:{idx}")
                matching.append(_Candidate(rule, layer, _specificity(rule, tier), rid))

    contributors: "dict[str, dict]" = {}  # rid -> rule view (deduped)

    def _note(c: _Candidate) -> None:
        contributors.setdefault(c.rid, _rule_view(c.rule, c.layer, c.rid))

    # ---- Action decisions, per key (category:<c> for categoried rules, else bare "action").
    per_key: "dict[str, list[_Candidate]]" = {}
    for c in matching:
        action = c.rule.get("action")
        if action is None:
            continue  # entity-only rule: contributes via the entity merge below, not an action
        cats = c.rule.get("categories")
        if cats:
            keys = [f"category:{cat}" for cat in cats]
        elif c.rule.get("entities"):
            continue  # entity-scoped action -> handled by the entity merge, not a blanket default
        else:
            keys = ["action"]
        for k in keys:
            per_key.setdefault(k, []).append(c)

    decisions: "dict[str, dict]" = {}
    for key, cands in per_key.items():
        winner = max(cands, key=lambda c: c.spec)
        w_strict = STRICTNESS.get(winner.rule.get("action"), 0)
        # The in-force hard floor = the strictest hard (non-unlocked) rule for this key.
        floor: Optional[_Candidate] = None
        floor_strict = -1
        for c in cands:
            if c.rule.get("hard") and c.rid not in unlock_set:
                s = STRICTNESS.get(c.rule.get("action"), 0)
                if s > floor_strict:
                    floor, floor_strict = c, s
        if floor is not None and floor_strict > w_strict:
            chosen, enforced = floor, True  # a less-strict winner cannot loosen the floor
        else:
            chosen, enforced = winner, False
        _note(chosen)
        decisions[key] = {
            "action": chosen.rule.get("action"),
            "hard": bool(chosen.rule.get("hard")),
            "source": chosen.layer,
            "rule_id": chosen.rid,
            "floor_enforced": enforced,
        }

    # ---- Entity allow/deny merge (union across layers; most-specific disposition per entity;
    # hard denies are un-removable; allow = brand/codename rescue list).
    deny_best: "dict[str, tuple[tuple, str, str]]" = {}   # entity -> (spec, layer, rid)
    allow_best: "dict[str, tuple[tuple, str, str]]" = {}
    hard_deny: "dict[str, str]" = {}                        # entity -> rid of a hard deny
    for c in matching:
        ents = c.rule.get("entities") or {}
        if not isinstance(ents, Mapping):
            continue
        for d in _aslist(ents.get("deny")):
            if d not in deny_best or c.spec > deny_best[d][0]:
                deny_best[d] = (c.spec, c.layer, c.rid)
            if c.rule.get("hard"):
                hard_deny.setdefault(d, c.rid)
            _note(c)
        for a in _aslist(ents.get("allow")):
            if a not in allow_best or c.spec > allow_best[a][0]:
                allow_best[a] = (c.spec, c.layer, c.rid)
            _note(c)

    entity_source: "dict[str, str]" = {}
    final_deny: list[str] = []
    final_allow: list[str] = []
    for e in sorted(set(deny_best) | set(allow_best)):
        hard_locked = e in hard_deny and hard_deny[e] not in unlock_set and e not in unlock_set
        d = deny_best.get(e)
        a = allow_best.get(e)
        if hard_locked:
            final_deny.append(e)
            entity_source[f"entity:{e}"] = hard_deny[e]  # the floor's owning layer is in deny_best
            if d:
                entity_source[f"entity:{e}"] = d[1]
        elif a is not None and (d is None or a[0] >= d[0]):
            final_allow.append(e)  # explicit rescue wins ties (and any non-hard deny it outranks)
            entity_source[f"entity:{e}"] = a[1]
        else:
            final_deny.append(e)
            entity_source[f"entity:{e}"] = d[1] if d else "global"

    # ---- Transparency views.
    hard_floors = [
        _rule_view(c.rule, c.layer, c.rid)
        for c in matching
        if c.rule.get("hard") and c.rid not in unlock_set
    ]
    source_per_rule: "dict[str, str]" = {k: v["source"] for k, v in decisions.items()}
    source_per_rule.update(entity_source)

    return {
        "decisions": decisions,
        "effective_rules": list(contributors.values()),
        "hard_floors": hard_floors,
        "source_per_rule": source_per_rule,
        "entities": {"deny": final_deny, "allow": final_allow},
        "unlocked": sorted(unlock_set),
    }


def action_for(resolved: Mapping[str, Any], category: Optional[str] = None) -> Optional[str]:
    """The effective action for ``category`` (falling back to the bare ``action`` default),
    or ``None`` if no rule speaks to it. A convenience over :func:`resolve`'s ``decisions``."""
    decisions = resolved.get("decisions", {})
    if category is not None:
        d = decisions.get(f"category:{category}")
        if d:
            return d["action"]
    d = decisions.get("action")
    return d["action"] if d else None


# --------------------------------------------------------------------------- #
# learn — remember a user's correction, scoped narrowly, applied silently next #
# --------------------------------------------------------------------------- #
def _new_rule_id(kind: str = "learned", ts: Optional[float] = None) -> str:
    ts = time.time() if ts is None else ts
    return f"{kind}-{int(ts * 1000)}-{os.urandom(2).hex()}"


def learn(
    correction: Mapping[str, Any],
    profile_path: Optional[PathLike] = None,
    *,
    ts: Optional[float] = None,
    profile: Optional[Mapping[str, Any]] = None,
    persist: bool = True,
) -> dict:
    """Turn one user override into a durable learned rule, scoped to the narrowest sensible
    match (this repo / path, this entity), and append it to the profile's ``learned`` block.

    The redactor self-improves per-user: next :func:`resolve` applies the learned rule
    silently — power users train it once, then it goes quiet. The ``correction`` describes the
    override::

        {
          "entity": "Saturday",            # the value the user re-judged (optional)
          "action": "allow",               # allow => rescue (entities.allow); else strip/...
          "category": "file_contents",     # optional; scopes the action to one category
          "repo": "saturday",              # narrowing: prefer repo, then path_glob, then session
          "cwd" / "path_glob": "...",      # alt narrowing by location
          "session_id": "...",             # provenance + last-resort narrowing
          "hard": false,                   # promote the correction to a hard floor
          "trigger": "user un-redacted brand 'Saturday'",  # provenance
        }

    ``ts`` defaults to ``None`` -> :func:`time.time` (tests pin it for determinism). With
    ``persist=False`` the rule is built and returned but not written (dry run). Returns the new
    rule dict (already appended to the persisted ``learned`` block when ``persist``).
    """
    ts = time.time() if ts is None else ts
    action = correction.get("action", "strip")
    entity = correction.get("entity")

    # Narrowest sensible match: repo and/or path, else fall back to the session.
    match: dict = {}
    if correction.get("repo"):
        match["repo"] = correction["repo"]
    pg = correction.get("path_glob") or correction.get("cwd")
    if pg:
        match["path_glob"] = pg
    if not match and correction.get("session_id"):
        match["session_id"] = correction["session_id"]

    entities: dict = {"deny": [], "allow": []}
    if entity is not None:
        # allow => the user rescued a wrongly-eaten brand; anything else => a codename the
        # detectors missed that must always be stripped.
        (entities["allow"] if action == "allow" else entities["deny"]).append(entity)

    rule: dict = {
        "id": _new_rule_id("learned", ts),
        "match": match,
        "action": action,
        "entities": entities,
        "hard": bool(correction.get("hard", False)),
        "learned": True,
        "provenance": {
            "ts": ts,
            "session": correction.get("session_id"),
            "trigger": correction.get("trigger"),
        },
    }
    if correction.get("category"):
        rule["categories"] = [correction["category"]]

    if persist:
        prof = dict(profile) if profile is not None else load_profile(profile_path)
        prof.setdefault("version", PROFILE_VERSION)
        learned = list(prof.get("learned") or [])
        learned.append(rule)
        prof["learned"] = learned
        prof.setdefault("rules", prof.get("rules") or [])
        save_profile(prof, profile_path)
    return rule


# --------------------------------------------------------------------------- #
# apply_entity_rules — the pure brand-rescue helper                            #
# --------------------------------------------------------------------------- #
def apply_entity_rules(
    redaction_map: Sequence[Mapping[str, Any]],
    resolved: Mapping[str, Any],
) -> list[dict]:
    """Drop redactions of allow-listed (rescued) brands from a redaction map — purely
    functional (returns a NEW list; input untouched).

    ``redaction_map`` is a sequence of ``{"category","original","replacement", ...}`` entries
    (the shape ``transcripts.py`` / ``package.diff_preview`` use). Any entry whose ``original``
    is on the resolved ``entities.allow`` rescue list is removed, i.e. the wrongly-eaten brand
    (e.g. "Saturday", which Presidio mis-tags as a DATE_TIME) is *un-redacted* and survives into
    the outbound bundle. The ``deny`` list is enforced upstream (detectors strip those); this
    helper is only the rescue side, kept small and total so it is trivially testable.
    """
    allow = set(_aslist(resolved.get("entities", {}).get("allow")))
    if not allow:
        return [dict(e) for e in redaction_map]
    return [dict(e) for e in redaction_map if e.get("original") not in allow]


# --------------------------------------------------------------------------- #
# CLI — parity with the sibling modules' library-CLI convention                #
# --------------------------------------------------------------------------- #
def _cli_resolve(args) -> int:
    session_rules = json.loads(Path(args.session_rules).read_text()) if args.session_rules else None
    profile = args.profile if args.profile else None
    res = resolve(
        args.cwd,
        session_id=args.session_id,
        repo_root=args.repo_root,
        session_rules=session_rules,
        profile=profile,
        repo=args.repo,
        unlock=args.unlock or None,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def _cli_learn(args) -> int:
    correction = json.loads(args.correction) if args.correction else json.load(sys.stdin)
    rule = learn(correction, profile_path=args.profile, persist=not args.dry_run)
    print(json.dumps(rule, indent=2, ensure_ascii=False))
    return 0


def _cli_show(args) -> int:
    print(json.dumps(load_profile(args.profile), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fb_assist.profile", description="fb-assist privacy profile / policy store")
    sub = p.add_subparsers(dest="cmd", required=True)

    rs = sub.add_parser("resolve", help="resolve the effective policy for a call site")
    rs.add_argument("cwd")
    rs.add_argument("--session-id", default=None)
    rs.add_argument("--repo-root", default=None)
    rs.add_argument("--repo", default=None)
    rs.add_argument("--session-rules", default=None, help="path to a JSON list of override rules")
    rs.add_argument("--profile", default=None, help="path to a profile.json (default: ~/.config/...)")
    rs.add_argument("--unlock", nargs="*", default=[], help="rule ids / entity names whose hard floor to lift")
    rs.set_defaults(func=_cli_resolve)

    ln = sub.add_parser("learn", help="append a learned rule from a correction (JSON arg or stdin)")
    ln.add_argument("correction", nargs="?", default=None, help="correction JSON; omit to read stdin")
    ln.add_argument("--profile", default=None)
    ln.add_argument("--dry-run", action="store_true", help="build + print the rule without persisting")
    ln.set_defaults(func=_cli_learn)

    sh = sub.add_parser("show", help="print the loaded global profile")
    sh.add_argument("--profile", default=None)
    sh.set_defaults(func=_cli_show)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
