"""fb_assist.reputation — pseudonymous reputation token for trusted feedback.

A user who submits well-redacted feedback earns reputation, carried as a signed,
pseudonymous token — its only identifier is a one-way hash of a locally-generated
public key, no PII — so Anthropic's triage can weight a trusted contributor higher
without learning who they are. Signing prefers Ed25519 (asymmetric: the verifier needs
only the public key) and falls back to HMAC-SHA256 (symmetric, needs a shared secret)
when ``cryptography`` is absent.

This module proves a token's authenticity and the user's *local claim* of their score;
the real trust floor is Anthropic's own server-side ledger of accepted submissions per
pseudonymous id, not the self-claimed number (see :func:`verify_token`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from . import profile as _profile

PathLike = Union[str, os.PathLike]

# Optional dependency: cryptography for Ed25519 (real asymmetric signing). Present -> used;
# absent -> stdlib HMAC-SHA256 symmetric fallback. The module is fully functional either way.
try:  # pragma: no cover - import guard, environment dependent
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.exceptions import InvalidSignature

    _HAVE_ED25519 = True
except Exception:  # pragma: no cover - exercised only on stdlib-only boxes
    Ed25519PrivateKey = None  # type: ignore
    Ed25519PublicKey = None  # type: ignore
    serialization = None  # type: ignore
    InvalidSignature = Exception  # type: ignore
    _HAVE_ED25519 = False

# Token / reputation schema version (bumped if the signed payload shape ever changes).
SCHEMA_VERSION = 1
REPUTATION_BLOCK_KEY = "reputation"

BACKEND_ED25519 = "ed25519"
BACKEND_HMAC = "hmac"

# The active backend for *new* identities. A token always records the backend it was
# minted under, so verification stays correct even across a backend change.
BACKEND = BACKEND_ED25519 if _HAVE_ED25519 else BACKEND_HMAC

# Reputation accrual model (a saturating hyperbolic curve — honest diminishing returns).
#   score = REPUTATION_CAP * credits / (credits + REPUTATION_HALF_CREDITS)
# Monotonic, asymptotes to the cap, never exceeds it. ``credits`` accumulate one
# quality-weighted unit per accepted submission, so the first accepted contributions move
# the needle most and later ones move it less — exactly the "careful filterer earns trust,
# but trust is bounded and un-farmable" shape.
REPUTATION_CAP = 100.0
REPUTATION_HALF_CREDITS = 10.0

# Default freshness window for verification: a token older than this (or dated in the
# future beyond the skew) is stale. 7 days mirrors a typical server-side request-log retention window.
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600
DEFAULT_FUTURE_SKEW_SECONDS = 300  # tolerate small clock skew on the issuer side

# Length (hex chars) of the pseudonymous id and key-id fingerprints. 32 hex == 128 bits,
# collision-resistant for this population while staying compact in the footer.
_PID_HEXLEN = 32
_KEYID_HEXLEN = 16


# --------------------------------------------------------------------------- #
# Backend abstraction — same API for Ed25519 (asymmetric) and HMAC (symmetric) #
# --------------------------------------------------------------------------- #
def _b2b(data: bytes, hexlen: int = _PID_HEXLEN) -> str:
    """A one-way blake2b digest, hex, truncated. Used for ids/fingerprints only."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()[:hexlen]


def _generate_keypair(backend: str, _rng: Optional[Any] = None) -> tuple[str, str]:
    """Return ``(secret_key_hex, verify_key_hex)`` for a fresh identity.

    * ed25519: secret = raw 32-byte private seed; verify = raw 32-byte public key. The
      verify key is genuinely public and safe to publish/embed.
    * hmac: secret = 32 random bytes; verify == secret (symmetric). The "verify key" is
      therefore secret and must NOT be embedded — see :func:`_verify_key_is_public`.
    """
    if backend == BACKEND_ED25519:
        if not _HAVE_ED25519:  # pragma: no cover - guarded by caller
            raise RuntimeError("ed25519 backend unavailable")
        priv = Ed25519PrivateKey.generate()
        sk = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        vk = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return sk.hex(), vk.hex()
    # HMAC symmetric: one random secret serves as both signing and verifying key.
    secret = secrets.token_bytes(32) if _rng is None else _rng(32)
    return secret.hex(), secret.hex()


def _verify_key_is_public(backend: str) -> bool:
    """True iff the verify key is safe to embed in the token (asymmetric backends)."""
    return backend == BACKEND_ED25519


def _sign(backend: str, secret_key_hex: str, message: bytes) -> str:
    """Sign ``message`` with the identity's secret key; return a hex signature."""
    sk = bytes.fromhex(secret_key_hex)
    if backend == BACKEND_ED25519:
        if not _HAVE_ED25519:  # pragma: no cover
            raise RuntimeError("ed25519 backend unavailable")
        priv = Ed25519PrivateKey.from_private_bytes(sk)
        return priv.sign(message).hex()
    return hmac.new(sk, message, hashlib.sha256).hexdigest()


def _verify_sig(backend: str, verify_key_hex: str, message: bytes, signature_hex: str) -> bool:
    """Check ``signature_hex`` over ``message`` using the verify key. Total (never raises)."""
    try:
        vk = bytes.fromhex(verify_key_hex)
        sig = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        return False
    if backend == BACKEND_ED25519:
        if not _HAVE_ED25519:  # pragma: no cover - a token minted elsewhere; can't verify here
            return False
        try:
            Ed25519PublicKey.from_public_bytes(vk).verify(sig, message)
            return True
        except InvalidSignature:
            return False
        except Exception:
            return False
    expected = hmac.new(vk, message, hashlib.sha256).digest()
    return hmac.compare_digest(expected, sig)


# --------------------------------------------------------------------------- #
# Canonicalization — deterministic signing bytes (signature excluded)          #
# --------------------------------------------------------------------------- #
def _canonical_bytes(obj: Mapping[str, Any]) -> bytes:
    """Deterministic JSON encoding for signing/digesting (sorted keys, tight separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _signing_payload(token: Mapping[str, Any]) -> dict:
    """The token minus its ``signature`` — exactly the bytes that get signed/verified."""
    return {k: v for k, v in token.items() if k != "signature"}


def effort_signal_digest(effort_signal: Optional[Mapping[str, Any]]) -> str:
    """A stable one-way digest binding a token to *this* submission's effort signal.

    The ``reputation_token`` field is excluded before digesting (it is the token itself —
    including it would be self-referential, and it is ``None`` at mint time anyway). Binding
    the rest means a token minted for signal A fails verification if lifted onto signal B —
    a stolen token can't be re-stapled to a different (e.g. low-effort) submission.
    """
    if not effort_signal:
        return _b2b(b"\x00fb-assist:empty-effort-signal", _PID_HEXLEN)
    scrubbed = {k: v for k, v in effort_signal.items() if k != "reputation_token"}
    return _b2b(_canonical_bytes(scrubbed), _PID_HEXLEN)


# --------------------------------------------------------------------------- #
# Identity — locally generated keypair, stored in the profile reputation block #
# --------------------------------------------------------------------------- #
def _empty_reputation(backend: str, ts: float) -> dict:
    sk, vk = _generate_keypair(backend)
    pid = _b2b(bytes.fromhex(vk), _PID_HEXLEN)
    return {
        "schema_version": SCHEMA_VERSION,
        "backend": backend,
        "secret_key": sk,          # SECRET signing material — never leaves the machine
        "verify_key": vk,          # public (ed25519) / == secret (hmac); embed only if public
        "pseudonymous_id": pid,
        "key_id": _b2b(bytes.fromhex(vk), _KEYID_HEXLEN),
        "score": 0.0,
        "credits": 0.0,
        "acceptances": 0,
        "created_at": ts,
        "rotated_from": None,
        "migration": None,         # signed carry-forward assertion (set by rotate_keys)
    }


def _public_view(rep: Mapping[str, Any]) -> dict:
    """The shareable projection of a reputation block — NO secret material.

    Note for the hmac backend ``verify_key`` IS the secret, so it is *omitted* here too;
    only ed25519 (asymmetric) exposes a real public key.
    """
    backend = rep.get("backend", BACKEND)
    view = {
        "schema_version": rep.get("schema_version", SCHEMA_VERSION),
        "backend": backend,
        "pseudonymous_id": rep.get("pseudonymous_id"),
        "key_id": rep.get("key_id"),
        "reputation_score": round(float(rep.get("score", 0.0)), 6),
        "acceptances": int(rep.get("acceptances", 0)),
        "created_at": rep.get("created_at"),
        "rotated_from": rep.get("rotated_from"),
    }
    if _verify_key_is_public(backend):
        view["public_key"] = rep.get("verify_key")
    return view


def _coerce_profile_with_path(
    profile: Any, profile_path: Optional[PathLike]
) -> tuple[dict, Optional[PathLike]]:
    """Load a profile dict + remember where to persist it. Mirrors profile._coerce_profile,
    but keeps the path so reputation writes go back to the right file."""
    if profile is None:
        return _profile.load_profile(profile_path), profile_path
    if isinstance(profile, (str, os.PathLike)):
        return _profile.load_profile(profile), profile
    if isinstance(profile, Mapping):
        prof = dict(profile)
        return prof, profile_path
    return _profile.load_profile(profile_path), profile_path


def issue_identity(
    *,
    profile_path: Optional[PathLike] = None,
    profile: Any = None,
    backend: Optional[str] = None,
    force: bool = False,
    persist: bool = True,
    ts: Optional[float] = None,
) -> dict:
    """Get-or-create the local pseudonymous identity, returning its **public view**.

    A keypair is generated once and stored in the profile's ``reputation`` block; on every
    later call the same identity is returned (stable ``pseudonymous_id`` across the user's
    machines, because the profile syncs). Pass ``force=True`` to mint a brand-new identity
    (discarding the old one — usually you want :func:`rotate_keys` instead, which carries
    reputation forward). ``backend`` defaults to the best available (``ed25519`` if present).
    """
    ts = time.time() if ts is None else ts
    prof, path = _coerce_profile_with_path(profile, profile_path)
    existing = prof.get(REPUTATION_BLOCK_KEY)
    if existing and not force:
        return _public_view(existing)
    rep = _empty_reputation(backend or BACKEND, ts)
    prof[REPUTATION_BLOCK_KEY] = rep
    if persist:
        _save_profile_preserving(prof, path)
    return _public_view(rep)


def load_identity(
    profile_path: Optional[PathLike] = None, profile: Any = None
) -> Optional[dict]:
    """The full stored reputation block (INCLUDING secret material), or ``None`` if none yet."""
    prof, _ = _coerce_profile_with_path(profile, profile_path)
    rep = prof.get(REPUTATION_BLOCK_KEY)
    return dict(rep) if isinstance(rep, Mapping) else None


def _require_identity(
    profile: Any, profile_path: Optional[PathLike], identity: Optional[Mapping[str, Any]]
) -> tuple[dict, dict, Optional[PathLike]]:
    """Resolve (profile_dict, reputation_block, path), creating the identity if absent."""
    if identity is not None:
        prof, path = _coerce_profile_with_path(profile, profile_path)
        return prof, dict(identity), path
    prof, path = _coerce_profile_with_path(profile, profile_path)
    rep = prof.get(REPUTATION_BLOCK_KEY)
    if not isinstance(rep, Mapping):
        rep = _empty_reputation(BACKEND, time.time())
        prof[REPUTATION_BLOCK_KEY] = rep
        _save_profile_preserving(prof, path)
    return prof, dict(rep), path


def pseudonymous_id(
    profile_path: Optional[PathLike] = None, profile: Any = None
) -> str:
    """The stable pseudonymous id for this machine's identity (creating one if needed)."""
    return issue_identity(profile_path=profile_path, profile=profile)["pseudonymous_id"]


def _save_profile_preserving(prof: Mapping[str, Any], path: Optional[PathLike]) -> None:
    """Persist via profile.save_profile, ensuring the privacy blocks stay well-formed.

    We never touch ``rules`` / ``learned`` — only guarantee they exist so a reputation-only
    profile still loads cleanly through the privacy engine.
    """
    out = dict(prof)
    out.setdefault("version", _profile.PROFILE_VERSION)
    out.setdefault("rules", out.get("rules") or [])
    out.setdefault("learned", out.get("learned") or [])
    _profile.save_profile(out, path)


# --------------------------------------------------------------------------- #
# Accumulation — local reputation score with diminishing returns + honest cap  #
# --------------------------------------------------------------------------- #
def _score_from_credits(credits: float) -> float:
    """The saturating curve: cap * credits / (credits + half). Bounded in [0, cap)."""
    credits = max(0.0, float(credits))
    return REPUTATION_CAP * credits / (credits + REPUTATION_HALF_CREDITS)


def _quality_weight(quality: Any, effort_signal: Optional[Mapping[str, Any]]) -> float:
    """Map an acceptance's quality into a credit weight in (0, 1].

    Accepts an explicit ``quality``, else derives one from the effort signal's ``quality`` /
    clean-floor summary. Scale convention (matching the codebase, where ``quality`` is an
    integer self-rating — ``desktop_chat`` defaults ``quality: int = 4``):

      * ``q >= 1``      -> an **N-of-5 self-rating**: ``weight = min(1, q/5)`` (so 5->1.0,
        4->0.8, 1->0.2);
      * ``0 < q < 1``   -> already a **0..1 fraction**, used as-is.

    A high self-rating earns close to a full credit; a sloppy one earns less, so reputation
    tracks *careful* filtering, not raw volume.
    """
    q = quality
    if q is None and effort_signal:
        q = effort_signal.get("quality")
    weight = 0.7  # neutral default for an unrated or non-numeric quality
    if q is not None:
        try:
            qf = float(q)
        except (TypeError, ValueError):
            pass  # non-numeric quality -> keep the neutral default
        else:
            # q >= 1 is an N-of-5 self-rating (5->1.0, 4->0.8, 1->0.2); 0<q<1 is a fraction.
            weight = min(1.0, qf / 5.0) if qf >= 1.0 else max(0.0, qf)
    # A demonstrably clean privacy floor is the core "careful filterer" signal — small bonus.
    if effort_signal:
        summary = effort_signal.get("summary") or {}
        if summary.get("floor_clean"):
            weight = min(1.0, weight + 0.1)
    return max(0.05, weight)  # an accepted contribution always earns *something*


def record_acceptance(
    *,
    quality: Any = None,
    effort_signal: Optional[Mapping[str, Any]] = None,
    profile_path: Optional[PathLike] = None,
    profile: Any = None,
    persist: bool = True,
    ts: Optional[float] = None,
) -> dict:
    """Record that one of the user's contributions was accepted; grow the local score.

    Adds a quality-weighted credit and recomputes the saturating ``score``. Returns the
    updated **public view** (so callers never see secret material). With ``persist=False``
    the computation is returned without writing (useful for tests / previews).

    LOCAL CLAIM ONLY: this is the user-side model of their reputation. Anthropic keeps the
    *authoritative* count server-side; this score is the value the token *claims*, which the
    server cross-checks against its own ledger for the pid (see module docstring).
    """
    ts = time.time() if ts is None else ts
    prof, rep, path = _require_identity(profile, profile_path, None)
    weight = _quality_weight(quality, effort_signal)
    rep["credits"] = float(rep.get("credits", 0.0)) + weight
    rep["acceptances"] = int(rep.get("acceptances", 0)) + 1
    rep["score"] = _score_from_credits(rep["credits"])
    rep["updated_at"] = ts
    prof[REPUTATION_BLOCK_KEY] = rep
    if persist:
        _save_profile_preserving(prof, path)
    return _public_view(rep)


def reputation_score(
    profile_path: Optional[PathLike] = None, profile: Any = None
) -> float:
    """The current local reputation score (0 if no identity / no acceptances yet)."""
    rep = load_identity(profile_path, profile)
    return round(float(rep.get("score", 0.0)), 6) if rep else 0.0


# --------------------------------------------------------------------------- #
# Mint — a signed assertion binding pid + score + effort-signal digest          #
# --------------------------------------------------------------------------- #
def mint_token(
    effort_signal: Optional[Mapping[str, Any]] = None,
    *,
    nonce: Optional[str] = None,
    ts: Optional[float] = None,
    profile_path: Optional[PathLike] = None,
    profile: Any = None,
    identity: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Mint a signed reputation token bound to ``effort_signal``.

    The signed assertion binds ``{pseudonymous_id, reputation_score, effort_signal_digest,
    nonce, issued_at, schema_version}`` (plus the verify key / key id) and appends a
    ``signature``. Binding the effort-signal digest is what stops a token being lifted from
    one submission onto another; the ``nonce`` is for the server's replay dedup.

    ``nonce`` / ``ts`` are keyword-only (pinned by tests for determinism); when omitted a
    random nonce and the current time are used. ``identity`` may be supplied to mint
    hermetically without touching any profile.
    """
    ts = time.time() if ts is None else ts
    nonce = secrets.token_hex(16) if nonce is None else nonce
    _, rep, _ = _require_identity(profile, profile_path, identity)

    backend = rep.get("backend", BACKEND)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "backend": backend,
        "pseudonymous_id": rep["pseudonymous_id"],
        "reputation_score": round(float(rep.get("score", 0.0)), 6),
        "effort_signal_digest": effort_signal_digest(effort_signal),
        "nonce": nonce,
        "issued_at": ts,
        "key_id": rep.get("key_id"),
        # Embed the verify key ONLY when it is genuinely public (ed25519). For hmac the
        # verify key is the secret and is withheld — the verifier supplies it out-of-band.
        "public_key": rep["verify_key"] if _verify_key_is_public(backend) else None,
    }
    signature = _sign(backend, rep["secret_key"], _canonical_bytes(payload))
    return {**payload, "signature": signature}


# --------------------------------------------------------------------------- #
# Verify — the function Anthropic runs server-side                              #
# --------------------------------------------------------------------------- #
def verify_token(
    token: Union[Mapping[str, Any], str],
    *,
    revocation_list: Optional[Iterable[str]] = None,
    public_key: Optional[str] = None,
    effort_signal: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    max_age_seconds: Optional[float] = DEFAULT_MAX_AGE_SECONDS,
    future_skew_seconds: float = DEFAULT_FUTURE_SKEW_SECONDS,
) -> dict:
    """Verify a reputation token. **This is the server-side function Anthropic runs.**

    Accepts the token as a dict OR the compact :func:`serialize_token` string (so the
    effort-signal footer value verifies directly). Checks, in order:

      1. **schema** — well-formed dict, supported ``schema_version``;
      2. **signature** — authentic + untampered, against the verify key (the embedded
         ``public_key`` for ed25519, or the ``public_key`` argument — which for the hmac
         fallback is the shared secret registered at enrollment);
      3. **key binding** — ``pseudonymous_id`` equals ``blake2b(verify_key)``, so a token
         can't claim someone else's pid;
      4. **freshness** — ``issued_at`` within ``[now - max_age, now + skew]`` (a ``nonce``
         is required; replay *dedup* across nonces is the server's job);
      5. **effort-signal binding** — when ``effort_signal`` is supplied, its digest must
         match the token's ``effort_signal_digest`` (a token minted for A fails against B);
      6. **revocation** — ``pseudonymous_id`` not on ``revocation_list``.

    Returns ``{valid, reason, pseudonymous_id, reputation_score}``. ``reason`` is ``"ok"``
    when valid, else the first failing check. The returned ``reputation_score`` is the
    token's **self-claimed** score — authentic but local; Anthropic weights it against its
    own authoritative ledger for the pid (see the module docstring boundary).
    """
    now = time.time() if now is None else now
    revoked = set(revocation_list or ())

    if isinstance(token, str):
        try:
            token = deserialize_token(token)
        except Exception:
            return _verdict(False, "malformed", None, None)
    if not isinstance(token, Mapping):
        return _verdict(False, "malformed", None, None)

    pid = token.get("pseudonymous_id")
    claimed_score = token.get("reputation_score")

    # (1) schema
    if token.get("schema_version") != SCHEMA_VERSION:
        return _verdict(False, "unsupported_schema", pid, claimed_score)
    backend = token.get("backend")
    if backend not in (BACKEND_ED25519, BACKEND_HMAC):
        return _verdict(False, "unsupported_backend", pid, claimed_score)
    for field in ("pseudonymous_id", "nonce", "issued_at", "effort_signal_digest", "signature"):
        if token.get(field) in (None, ""):
            return _verdict(False, f"missing_{field}", pid, claimed_score)

    # Resolve the verify key: explicit arg wins; else the embedded public key (ed25519 only).
    verify_key = public_key or token.get("public_key")
    if not verify_key:
        # hmac with no shared secret provided -> unverifiable by design (symmetric fallback).
        return _verdict(False, "missing_verification_key", pid, claimed_score)

    # (2) signature
    if not _verify_sig(backend, verify_key, _canonical_bytes(_signing_payload(token)),
                       token["signature"]):
        return _verdict(False, "bad_signature", pid, claimed_score)

    # (3) key binding — pid must be the hash of the verify key that just validated the sig.
    try:
        if _b2b(bytes.fromhex(verify_key), _PID_HEXLEN) != pid:
            return _verdict(False, "pseudonymous_id_mismatch", pid, claimed_score)
    except (ValueError, TypeError):
        return _verdict(False, "pseudonymous_id_mismatch", pid, claimed_score)

    # (4) freshness
    try:
        issued = float(token["issued_at"])
    except (TypeError, ValueError):
        return _verdict(False, "missing_issued_at", pid, claimed_score)
    if issued > now + future_skew_seconds:
        return _verdict(False, "issued_in_future", pid, claimed_score)
    if max_age_seconds is not None and issued < now - float(max_age_seconds):
        return _verdict(False, "stale", pid, claimed_score)

    # (5) effort-signal binding (only when the verifier supplies the signal to bind against)
    if effort_signal is not None:
        if token["effort_signal_digest"] != effort_signal_digest(effort_signal):
            return _verdict(False, "effort_signal_mismatch", pid, claimed_score)

    # (6) revocation
    if pid in revoked:
        return _verdict(False, "revoked", pid, claimed_score)

    return _verdict(True, "ok", pid, claimed_score)


def _verdict(valid: bool, reason: str, pid: Optional[str], score: Any) -> dict:
    try:
        score_f = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_f = None
    return {
        "valid": valid,
        "reason": reason,
        "pseudonymous_id": pid,
        "reputation_score": score_f,
    }


# --------------------------------------------------------------------------- #
# Revocation + key rotation (with a signature-chained reputation carry-forward) #
# --------------------------------------------------------------------------- #
def rotate_keys(
    *,
    profile_path: Optional[PathLike] = None,
    profile: Any = None,
    carry_reputation: bool = True,
    ts: Optional[float] = None,
    persist: bool = True,
) -> dict:
    """Rotate to a fresh keypair, optionally carrying reputation forward via a signed
    **migration assertion**.

    Use after suspected key compromise, or to deliberately break linkability to past
    submissions while keeping earned trust. A new identity (new ``pseudonymous_id``) is
    generated; when ``carry_reputation`` the prior score/credits/acceptances are copied to
    the new identity AND a migration assertion::

        {old_pseudonymous_id, new_pseudonymous_id, reputation_score, issued_at,
         old_public_key?, signature}

    is signed by the **old** key. A verifier checks that signature with the old verify key
    (:func:`verify_migration`) to confirm the new identity legitimately inherits the old
    one's reputation — without either pid revealing a human. The old pid is the natural
    thing to add to a revocation list once rotation is done.

    Returns ``{"identity": <new public view>, "migration": <assertion or None>,
    "previous_pseudonymous_id": <old pid or None>}``.
    """
    ts = time.time() if ts is None else ts
    prof, path = _coerce_profile_with_path(profile, profile_path)
    old = prof.get(REPUTATION_BLOCK_KEY)
    old_is_real = isinstance(old, Mapping) and old.get("secret_key")

    new = _empty_reputation(BACKEND, ts)
    migration: Optional[dict] = None
    previous_pid: Optional[str] = None

    if old_is_real:
        previous_pid = old.get("pseudonymous_id")
        new["rotated_from"] = previous_pid
        if carry_reputation:
            new["credits"] = float(old.get("credits", 0.0))
            new["acceptances"] = int(old.get("acceptances", 0))
            new["score"] = _score_from_credits(new["credits"])
            old_backend = old.get("backend", BACKEND)
            assertion = {
                "schema_version": SCHEMA_VERSION,
                "type": "reputation_migration",
                "backend": old_backend,
                "old_pseudonymous_id": previous_pid,
                "new_pseudonymous_id": new["pseudonymous_id"],
                "reputation_score": round(float(new["score"]), 6),
                "issued_at": ts,
                # the OLD verify key, so the assertion is checkable (ed25519 only; withheld
                # for the symmetric fallback exactly as in mint_token).
                "old_public_key": old.get("verify_key") if _verify_key_is_public(old_backend) else None,
            }
            assertion["signature"] = _sign(old_backend, old["secret_key"], _canonical_bytes(assertion))
            migration = assertion
            new["migration"] = migration

    prof[REPUTATION_BLOCK_KEY] = new
    if persist:
        _save_profile_preserving(prof, path)
    return {
        "identity": _public_view(new),
        "migration": migration,
        "previous_pseudonymous_id": previous_pid,
    }


def verify_migration(
    migration: Mapping[str, Any],
    *,
    public_key: Optional[str] = None,
    revocation_list: Optional[Iterable[str]] = None,
) -> dict:
    """Verify a rotation migration assertion (the old key signed the new pid carry-forward).

    Confirms the assertion is authentic under the **old** verify key (embedded for ed25519,
    or supplied via ``public_key`` for the hmac fallback) and that the old pid binds to that
    key. Optionally rejects when the *new* pid is already revoked. Returns
    ``{valid, reason, old_pseudonymous_id, new_pseudonymous_id, reputation_score}``.
    """
    revoked = set(revocation_list or ())
    if not isinstance(migration, Mapping):
        return {"valid": False, "reason": "malformed", "old_pseudonymous_id": None,
                "new_pseudonymous_id": None, "reputation_score": None}
    old_pid = migration.get("old_pseudonymous_id")
    new_pid = migration.get("new_pseudonymous_id")
    score = migration.get("reputation_score")
    base = {"old_pseudonymous_id": old_pid, "new_pseudonymous_id": new_pid,
            "reputation_score": float(score) if score is not None else None}

    if migration.get("type") != "reputation_migration" or migration.get("schema_version") != SCHEMA_VERSION:
        return {"valid": False, "reason": "unsupported_schema", **base}
    backend = migration.get("backend")
    if backend not in (BACKEND_ED25519, BACKEND_HMAC):
        return {"valid": False, "reason": "unsupported_backend", **base}
    if not migration.get("signature") or not old_pid or not new_pid:
        return {"valid": False, "reason": "missing_field", **base}

    verify_key = public_key or migration.get("old_public_key")
    if not verify_key:
        return {"valid": False, "reason": "missing_verification_key", **base}
    if not _verify_sig(backend, verify_key, _canonical_bytes(_signing_payload(migration)),
                       migration["signature"]):
        return {"valid": False, "reason": "bad_signature", **base}
    try:
        if _b2b(bytes.fromhex(verify_key), _PID_HEXLEN) != old_pid:
            return {"valid": False, "reason": "pseudonymous_id_mismatch", **base}
    except (ValueError, TypeError):
        return {"valid": False, "reason": "pseudonymous_id_mismatch", **base}
    if new_pid in revoked:
        return {"valid": False, "reason": "revoked", **base}
    return {"valid": True, "reason": "ok", **base}


# --------------------------------------------------------------------------- #
# Serialization + effort-signal integration seam                               #
# --------------------------------------------------------------------------- #
def serialize_token(token: Mapping[str, Any]) -> str:
    """Encode a token as a compact, URL-safe ``fbrep1.<base64url(json)>`` string.

    This is the value that goes into an effort signal's ``reputation_token`` field, so it
    survives the footer render (``rep=<string>``) and can be handed straight back to
    :func:`verify_token`. Round-trips losslessly with :func:`deserialize_token`.
    """
    raw = _canonical_bytes(token)
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"fbrep1.{b64}"


def deserialize_token(s: str) -> dict:
    """Inverse of :func:`serialize_token`. Accepts the ``fbrep1.`` form or bare base64url;
    also passes through an already-decoded JSON object string for convenience."""
    if not isinstance(s, str):
        raise TypeError("token must be a str")
    body = s.split(".", 1)[1] if s.startswith("fbrep1.") else s
    body = body.strip()
    if body.startswith("{"):  # tolerate a raw JSON string
        return json.loads(body)
    pad = "=" * (-len(body) % 4)
    raw = base64.urlsafe_b64decode(body + pad)
    return json.loads(raw.decode("utf-8"))


def attach_reputation_token(
    effort_signal: Mapping[str, Any],
    *,
    nonce: Optional[str] = None,
    ts: Optional[float] = None,
    profile_path: Optional[PathLike] = None,
    profile: Any = None,
    identity: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Return a COPY of ``effort_signal`` with ``reputation_token`` set to a freshly minted,
    serialized token bound to that signal.

    This is the integration seam: the shape matches what ``package._render_effort_footer`` /
    ``desktop_chat`` / ``server_side`` already expect, so no existing module changes —
    ``assemble_payload(..., effort_signal=attach_reputation_token(sig))`` just works, and the
    footer renders ``rep=fbrep1...``. The token binds the digest of *this* signal (minus the
    ``reputation_token`` field itself), so it can't be re-used on a different submission.
    """
    token = mint_token(
        effort_signal,
        nonce=nonce,
        ts=ts,
        profile_path=profile_path,
        profile=profile,
        identity=identity,
    )
    out = dict(effort_signal)
    out["reputation_token"] = serialize_token(token)
    return out


# --------------------------------------------------------------------------- #
# CLI — parity with the sibling modules' library-CLI convention                #
# --------------------------------------------------------------------------- #
def _cli_identity(args) -> int:
    view = issue_identity(profile_path=args.profile, force=args.force)
    print(json.dumps(view, indent=2, ensure_ascii=False))
    return 0


def _cli_mint(args) -> int:
    effort = json.loads(args.effort_signal) if args.effort_signal else None
    token = mint_token(effort, nonce=args.nonce, profile_path=args.profile)
    out = serialize_token(token) if args.serialize else json.dumps(token, indent=2, ensure_ascii=False)
    print(out)
    return 0


def _cli_verify(args) -> int:
    token: Union[str, dict]
    raw = args.token if args.token else sys.stdin.read().strip()
    token = raw if raw.startswith("fbrep1.") else json.loads(raw)
    effort = json.loads(args.effort_signal) if args.effort_signal else None
    res = verify_token(
        token,
        revocation_list=args.revoke or None,
        public_key=args.public_key,
        effort_signal=effort,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res["valid"] else 1


def _cli_accept(args) -> int:
    view = record_acceptance(quality=args.quality, profile_path=args.profile)
    print(json.dumps(view, indent=2, ensure_ascii=False))
    return 0


def _cli_rotate(args) -> int:
    res = rotate_keys(profile_path=args.profile, carry_reputation=not args.fresh)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fb_assist.reputation",
        description="fb-assist pseudonymous reputation token",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("identity", help="show (or create) the local pseudonymous identity")
    pi.add_argument("--profile", default=None)
    pi.add_argument("--force", action="store_true", help="mint a brand-new identity (drops the old)")
    pi.set_defaults(func=_cli_identity)

    pm = sub.add_parser("mint", help="mint a token bound to an effort signal (JSON)")
    pm.add_argument("--effort-signal", default=None, help="effort-signal JSON to bind")
    pm.add_argument("--nonce", default=None)
    pm.add_argument("--profile", default=None)
    pm.add_argument("--serialize", action="store_true", help="emit the compact fbrep1 string")
    pm.set_defaults(func=_cli_mint)

    pv = sub.add_parser("verify", help="verify a token (server-side check; arg or stdin)")
    pv.add_argument("token", nargs="?", default=None, help="token JSON or fbrep1 string; omit for stdin")
    pv.add_argument("--public-key", default=None, help="verify key (hmac fallback: shared secret)")
    pv.add_argument("--effort-signal", default=None, help="bind-check against this effort signal")
    pv.add_argument("--revoke", nargs="*", default=[], help="revoked pseudonymous ids")
    pv.set_defaults(func=_cli_verify)

    pa = sub.add_parser("accept", help="record an accepted contribution (grow the score)")
    pa.add_argument("--quality", default=None, help="0..1 or a 0..5 self-rating")
    pa.add_argument("--profile", default=None)
    pa.set_defaults(func=_cli_accept)

    pr = sub.add_parser("rotate", help="rotate keys, carrying reputation forward by default")
    pr.add_argument("--profile", default=None)
    pr.add_argument("--fresh", action="store_true", help="do NOT carry reputation forward")
    pr.set_defaults(func=_cli_rotate)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
