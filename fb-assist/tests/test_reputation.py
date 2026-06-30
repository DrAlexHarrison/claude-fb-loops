"""Tests for fb_assist.reputation — the pseudonymous careful-filterer trust token.

The token is the privacy-bearing artifact Anthropic would weight server-side, so the
contract gets direct, adversarial coverage:

  * issuance + a STABLE pseudonymous id (same across "machines" = same synced profile);
  * mint -> verify round-trip is valid;
  * a tampered token (any signed field) -> invalid (bad_signature);
  * effort-signal binding — a token minted for signal A fails against signal B;
  * revocation — a revoked pseudonymous id -> invalid;
  * key rotation carries reputation forward via a signature-chained migration;
  * accumulation grows with accepted contributions, with diminishing returns + an honest cap;
  * pseudonymity — NO PII (email / name / path) ever appears in the token bytes, and the
    pseudonymous id is a one-way hash, not reversible to identity;
  * BOTH crypto backends: the Ed25519 asymmetric path (when ``cryptography`` is present) and
    the stdlib HMAC-SHA256 symmetric fallback (forced here regardless of availability);
  * the effort-signal footer integration shape (serialize -> rep=... -> verify).

Everything is hermetic: tmp_path profiles only; the real ``~/.config/fb-assist/`` is never
touched. All planted PII is SYNTHETIC. Forces ``USE_TF=0`` (no ML backends needed here).

Run:  USE_TF=0 python -m pytest tests/test_reputation.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pytest

# Importable when run directly (pytest handles this too).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import reputation as R  # noqa: E402
from fb_assist import profile as PF  # noqa: E402
from fb_assist.package import _render_effort_footer  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def ppath(tmp_path: Path, name: str = "profile.json") -> Path:
    return tmp_path / name


SIGNAL_A = {"redaction": "surgical", "quality": 4, "alignment_confidence": 0.9,
            "summary": {"redactions": 3, "floor_clean": True}}
SIGNAL_B = {"redaction": "blanket", "quality": 1, "summary": {"floor_clean": False}}

# Every backend the build supports; the hmac path is always exercised, ed25519 only when
# the optional dependency is importable (so the suite is green on a stdlib-only box too).
BACKENDS = [R.BACKEND_HMAC] + ([R.BACKEND_ED25519] if R._HAVE_ED25519 else [])


def ident(backend: str, ts: float = 1000.0) -> dict:
    """A hermetic identity for ``backend`` (no profile / disk involved)."""
    return R._empty_reputation(backend, ts)


def verify_key_for(token_or_rep, rep=None):
    """The verify key a verifier would use: embedded for ed25519, the shared secret for hmac."""
    rep = rep if rep is not None else token_or_rep
    return None if R._verify_key_is_public(rep["backend"]) else rep["verify_key"]


# --------------------------------------------------------------------------- #
# issuance + stable pseudonymous id                                            #
# --------------------------------------------------------------------------- #
def test_issue_identity_is_idempotent_and_stable(tmp_path):
    p = ppath(tmp_path)
    a = R.issue_identity(profile_path=p)
    b = R.issue_identity(profile_path=p)
    assert a["pseudonymous_id"] == b["pseudonymous_id"]
    assert a["pseudonymous_id"]  # non-empty
    # public view never carries secret material
    assert "secret_key" not in a and "verify_key" not in a


def test_pseudonymous_id_is_hash_of_public_key_hexlen(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    rep = R.load_identity(p)
    pid = rep["pseudonymous_id"]
    assert pid == R._b2b(bytes.fromhex(rep["verify_key"]), R._PID_HEXLEN)
    assert len(pid) == R._PID_HEXLEN
    int(pid, 16)  # valid hex


def test_same_synced_profile_yields_same_identity_across_machines(tmp_path):
    # "Cross-machine sync": machine B loads the SAME profile dict machine A wrote.
    p = ppath(tmp_path)
    a = R.issue_identity(profile_path=p)
    synced = json.loads(Path(p).read_text())          # the file that rides config sync
    b = R.issue_identity(profile=synced, persist=False)
    assert a["pseudonymous_id"] == b["pseudonymous_id"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_force_backend_sets_backend(backend):
    rep = ident(backend)
    assert rep["backend"] == backend
    # hmac verify key == secret (symmetric); ed25519 verify key != secret (asymmetric)
    if backend == R.BACKEND_HMAC:
        assert rep["verify_key"] == rep["secret_key"]
    else:
        assert rep["verify_key"] != rep["secret_key"]


# --------------------------------------------------------------------------- #
# mint -> verify round-trip                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", BACKENDS)
def test_mint_verify_roundtrip_valid(backend):
    rep = ident(backend)
    tok = R.mint_token(SIGNAL_A, nonce="n1", ts=1000.0, identity=rep)
    res = R.verify_token(tok, public_key=verify_key_for(rep), effort_signal=SIGNAL_A, now=1000.0)
    assert res["valid"] is True
    assert res["reason"] == "ok"
    assert res["pseudonymous_id"] == rep["pseudonymous_id"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_token_binds_pid_score_digest_nonce(backend):
    rep = ident(backend)
    rep["score"] = 42.5
    tok = R.mint_token(SIGNAL_A, nonce="abc", ts=1234.0, identity=rep)
    assert tok["pseudonymous_id"] == rep["pseudonymous_id"]
    assert tok["reputation_score"] == 42.5
    assert tok["nonce"] == "abc"
    assert tok["issued_at"] == 1234.0
    assert tok["effort_signal_digest"] == R.effort_signal_digest(SIGNAL_A)
    assert tok["schema_version"] == R.SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# tampering -> invalid                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("field", ["reputation_score", "nonce", "issued_at", "effort_signal_digest"])
def test_tampered_signed_field_is_rejected(backend, field):
    rep = ident(backend)
    tok = R.mint_token(SIGNAL_A, nonce="n1", ts=1000.0, identity=rep)
    tok = dict(tok)
    tok[field] = 999.0 if field in ("reputation_score", "issued_at") else "tampered"
    res = R.verify_token(tok, public_key=verify_key_for(rep), now=1000.0)
    assert res["valid"] is False
    assert res["reason"] == "bad_signature"


@pytest.mark.parametrize("backend", BACKENDS)
def test_tampered_signature_is_rejected(backend):
    rep = ident(backend)
    tok = dict(R.mint_token(SIGNAL_A, nonce="n1", ts=1000.0, identity=rep))
    # flip a hex nibble in the signature
    sig = list(tok["signature"])
    sig[0] = "0" if sig[0] != "0" else "1"
    tok["signature"] = "".join(sig)
    res = R.verify_token(tok, public_key=verify_key_for(rep), now=1000.0)
    assert res["valid"] is False and res["reason"] == "bad_signature"


def test_cannot_claim_another_pid_by_swapping_key():
    # An attacker re-signs a changed token with THEIR OWN key but keeps a victim's pid.
    # The pid<->key binding check catches it (the token's pid no longer hashes to the key).
    victim = ident(BACKENDS[0])
    attacker = ident(BACKENDS[0])
    tok = dict(R.mint_token(SIGNAL_A, nonce="n", ts=1000.0, identity=attacker))
    tok["pseudonymous_id"] = victim["pseudonymous_id"]  # lie about identity
    # re-sign with attacker's key so the signature itself is valid
    tok.pop("signature")
    tok["signature"] = R._sign(attacker["backend"], attacker["secret_key"], R._canonical_bytes(tok))
    res = R.verify_token(tok, public_key=verify_key_for(attacker), now=1000.0)
    assert res["valid"] is False and res["reason"] == "pseudonymous_id_mismatch"


# --------------------------------------------------------------------------- #
# effort-signal binding — a token for A fails against B                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", BACKENDS)
def test_effort_signal_binding(backend):
    rep = ident(backend)
    tok = R.mint_token(SIGNAL_A, nonce="n1", ts=1000.0, identity=rep)
    vk = verify_key_for(rep)
    assert R.verify_token(tok, public_key=vk, effort_signal=SIGNAL_A, now=1000.0)["valid"] is True
    bad = R.verify_token(tok, public_key=vk, effort_signal=SIGNAL_B, now=1000.0)
    assert bad["valid"] is False and bad["reason"] == "effort_signal_mismatch"


def test_effort_digest_ignores_reputation_token_field():
    # The reputation_token field is excluded from the digest (it's the token itself).
    base = {"quality": 4, "redaction": "surgical"}
    with_tok = {**base, "reputation_token": "fbrep1.whatever"}
    assert R.effort_signal_digest(base) == R.effort_signal_digest(with_tok)


# --------------------------------------------------------------------------- #
# revocation                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", BACKENDS)
def test_revoked_pid_is_rejected(backend):
    rep = ident(backend)
    tok = R.mint_token(SIGNAL_A, nonce="n1", ts=1000.0, identity=rep)
    res = R.verify_token(tok, public_key=verify_key_for(rep),
                         revocation_list=[rep["pseudonymous_id"]], now=1000.0)
    assert res["valid"] is False and res["reason"] == "revoked"


def test_non_revoked_pid_passes_revocation_check():
    rep = ident(BACKENDS[0])
    tok = R.mint_token(SIGNAL_A, nonce="n1", ts=1000.0, identity=rep)
    res = R.verify_token(tok, public_key=verify_key_for(rep),
                         revocation_list=["some-other-pid"], now=1000.0)
    assert res["valid"] is True


# --------------------------------------------------------------------------- #
# freshness window                                                             #
# --------------------------------------------------------------------------- #
def test_stale_token_rejected():
    rep = ident(BACKENDS[0])
    tok = R.mint_token(SIGNAL_A, nonce="n1", ts=1000.0, identity=rep)
    res = R.verify_token(tok, public_key=verify_key_for(rep),
                         now=1000.0 + R.DEFAULT_MAX_AGE_SECONDS + 10)
    assert res["valid"] is False and res["reason"] == "stale"


def test_future_dated_token_rejected():
    rep = ident(BACKENDS[0])
    tok = R.mint_token(SIGNAL_A, nonce="n1", ts=5000.0, identity=rep)
    res = R.verify_token(tok, public_key=verify_key_for(rep), now=1000.0)
    assert res["valid"] is False and res["reason"] == "issued_in_future"


# --------------------------------------------------------------------------- #
# accumulation — grows with diminishing returns + honest cap                   #
# --------------------------------------------------------------------------- #
def test_accumulation_grows_and_has_diminishing_returns(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    prev = 0.0
    first_delta = None
    last_delta = None
    for i in range(20):
        view = R.record_acceptance(quality=5, profile_path=p)
        delta = view["reputation_score"] - prev
        if i == 0:
            first_delta = delta
        last_delta = delta
        assert view["reputation_score"] > prev  # strictly increasing
        prev = view["reputation_score"]
    assert first_delta > last_delta  # diminishing returns


def test_accumulation_never_exceeds_cap(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    for _ in range(500):
        R.record_acceptance(quality=5, profile_path=p)
    score = R.reputation_score(profile_path=p)
    assert score < R.REPUTATION_CAP
    assert score > 0.9 * R.REPUTATION_CAP  # but clearly approaching it


def test_high_quality_outearns_low_quality(tmp_path):
    hi, lo = ppath(tmp_path, "hi.json"), ppath(tmp_path, "lo.json")
    R.issue_identity(profile_path=hi)
    R.issue_identity(profile_path=lo)
    for _ in range(5):
        R.record_acceptance(quality=5, profile_path=hi)
        R.record_acceptance(quality=1, profile_path=lo)
    assert R.reputation_score(profile_path=hi) > R.reputation_score(profile_path=lo)


def test_quality_derived_from_effort_signal(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    view = R.record_acceptance(effort_signal=SIGNAL_A, profile_path=p)
    assert view["reputation_score"] > 0
    assert view["acceptances"] == 1


def test_minted_token_reflects_accrued_score(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    for _ in range(4):
        R.record_acceptance(quality=5, profile_path=p)
    score = R.reputation_score(profile_path=p)
    tok = R.mint_token(SIGNAL_A, nonce="n", ts=1000.0, profile_path=p)
    assert tok["reputation_score"] == pytest.approx(score)


# --------------------------------------------------------------------------- #
# key rotation carries reputation                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", BACKENDS)
def test_rotation_carries_reputation_and_signs_migration(tmp_path, backend):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p, backend=backend, force=True)
    for _ in range(6):
        R.record_acceptance(quality=5, profile_path=p)
    before = R.reputation_score(profile_path=p)
    old_pid = R.load_identity(p)["pseudonymous_id"]
    old_vk = R.load_identity(p)["verify_key"]

    rot = R.rotate_keys(profile_path=p)
    new_id = rot["identity"]
    assert new_id["pseudonymous_id"] != old_pid                # fresh identity
    assert new_id["reputation_score"] == pytest.approx(before)  # reputation carried
    assert new_id["rotated_from"] == old_pid
    assert rot["previous_pseudonymous_id"] == old_pid

    # the migration assertion is signed by the OLD key and verifies under it
    mig = rot["migration"]
    pk = None if R._verify_key_is_public(backend) else old_vk
    vres = R.verify_migration(mig, public_key=pk)
    assert vres["valid"] is True
    assert vres["old_pseudonymous_id"] == old_pid
    assert vres["new_pseudonymous_id"] == new_id["pseudonymous_id"]


def test_rotation_fresh_drops_reputation(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    for _ in range(5):
        R.record_acceptance(quality=5, profile_path=p)
    rot = R.rotate_keys(profile_path=p, carry_reputation=False)
    assert rot["identity"]["reputation_score"] == 0.0
    assert rot["migration"] is None


def test_tampered_migration_rejected(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    R.record_acceptance(quality=5, profile_path=p)
    old_vk = R.load_identity(p)["verify_key"]  # capture before rotating it away
    rot = R.rotate_keys(profile_path=p)
    mig = dict(rot["migration"])
    mig["reputation_score"] = 999.0  # inflate without re-signing
    # for the symmetric fallback the verifier supplies the old shared secret out-of-band
    pk = None if R._verify_key_is_public(mig["backend"]) else old_vk
    res = R.verify_migration(mig, public_key=pk)
    assert res["valid"] is False and res["reason"] == "bad_signature"


# --------------------------------------------------------------------------- #
# pseudonymity / privacy invariant — NO PII in the token bytes                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", BACKENDS)
def test_no_pii_in_token_bytes(backend):
    # An effort signal STUFFED with synthetic PII; the token must leak none of it, because
    # the signal is bound only via a one-way digest.
    pii_signal = {
        "quality": 5,
        "user_email": "dana.lee@northwind-labs.example",
        "author": "Dana Lee",
        "path": "/home/dana/code/secret-project/auth.py",
        "ssn": "987-65-4321",
    }
    rep = ident(backend)
    tok = R.mint_token(pii_signal, nonce="n1", ts=1000.0, identity=rep)
    blob = json.dumps(tok) + "|" + R.serialize_token(tok)
    for sentinel in ("dana.lee@northwind-labs.example", "Dana Lee",
                     "/home/dana/code/secret-project/auth.py", "987-65-4321"):
        assert sentinel not in blob


def test_token_fields_are_only_opaque_values():
    # Whitelist the keys allowed in a token; none of them is identity-bearing.
    rep = ident(BACKENDS[0])
    tok = R.mint_token(SIGNAL_A, nonce="n", ts=1000.0, identity=rep)
    allowed = {"schema_version", "backend", "pseudonymous_id", "reputation_score",
               "effort_signal_digest", "nonce", "issued_at", "key_id", "public_key", "signature"}
    assert set(tok) <= allowed


def test_pseudonymous_id_not_reversible_to_inputs(tmp_path):
    # The pid is a hash of a RANDOM public key — not derived from any user identifier — so
    # two identities created with identical "context" still differ, and the pid reveals nothing.
    a = R.issue_identity(profile_path=ppath(tmp_path, "a.json"))
    b = R.issue_identity(profile_path=ppath(tmp_path, "b.json"))
    assert a["pseudonymous_id"] != b["pseudonymous_id"]  # randomness, not identity, drives it


# --------------------------------------------------------------------------- #
# crypto backend selection                                                     #
# --------------------------------------------------------------------------- #
def test_hmac_fallback_requires_shared_secret():
    rep = ident(R.BACKEND_HMAC)
    tok = R.mint_token(SIGNAL_A, nonce="n", ts=1000.0, identity=rep)
    assert tok["public_key"] is None  # symmetric: never embed the (secret) verify key
    # without the shared secret it is unverifiable BY DESIGN
    res = R.verify_token(tok, now=1000.0)
    assert res["valid"] is False and res["reason"] == "missing_verification_key"
    # with the registered shared secret it verifies
    ok = R.verify_token(tok, public_key=rep["verify_key"], now=1000.0)
    assert ok["valid"] is True
    # a WRONG shared secret -> bad signature
    bad = R.verify_token(tok, public_key="00" * 32, now=1000.0)
    assert bad["valid"] is False and bad["reason"] == "bad_signature"


@pytest.mark.skipif(not R._HAVE_ED25519, reason="cryptography/Ed25519 not installed")
def test_ed25519_asymmetric_embeds_public_key_and_self_verifies():
    rep = ident(R.BACKEND_ED25519)
    tok = R.mint_token(SIGNAL_A, nonce="n", ts=1000.0, identity=rep)
    assert tok["backend"] == R.BACKEND_ED25519
    assert tok["public_key"] == rep["verify_key"]      # public key is safe to embed
    assert tok["public_key"] != rep["secret_key"]       # and it is NOT the private key
    # verifiable with NOTHING secret supplied (the whole point of asymmetric)
    assert R.verify_token(tok, now=1000.0)["valid"] is True


def test_default_backend_prefers_ed25519_when_available():
    expected = R.BACKEND_ED25519 if R._HAVE_ED25519 else R.BACKEND_HMAC
    assert R.BACKEND == expected


# --------------------------------------------------------------------------- #
# serialization + effort-signal footer integration                            #
# --------------------------------------------------------------------------- #
def test_serialize_deserialize_roundtrip():
    rep = ident(BACKENDS[0])
    tok = R.mint_token(SIGNAL_A, nonce="n", ts=1000.0, identity=rep)
    s = R.serialize_token(tok)
    assert s.startswith("fbrep1.")
    assert R.deserialize_token(s) == tok


def test_verify_accepts_serialized_string():
    rep = ident(BACKENDS[0])
    tok = R.mint_token(SIGNAL_A, nonce="n", ts=1000.0, identity=rep)
    s = R.serialize_token(tok)
    res = R.verify_token(s, public_key=verify_key_for(rep), effort_signal=SIGNAL_A, now=1000.0)
    assert res["valid"] is True


def test_attach_reputation_token_fills_field_and_renders_in_footer(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    enriched = R.attach_reputation_token(SIGNAL_A, nonce="n", ts=1000.0, profile_path=p)
    # original signal untouched (pure function); copy carries the token
    assert "reputation_token" not in SIGNAL_A
    assert enriched["reputation_token"].startswith("fbrep1.")
    # the existing footer renderer accepts it verbatim -> rep=...
    footer = _render_effort_footer(enriched)
    assert "rep=fbrep1." in footer
    # and the token in the footer verifies, bound to this very signal
    res = R.verify_token(enriched["reputation_token"], effort_signal=SIGNAL_A, now=1000.0)
    assert res["valid"] is True


def test_attached_token_is_bound_to_its_own_signal(tmp_path):
    # The token attached to a signal must NOT verify against a different signal.
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    enriched = R.attach_reputation_token(SIGNAL_A, nonce="n", ts=1000.0, profile_path=p)
    res = R.verify_token(enriched["reputation_token"], effort_signal=SIGNAL_B, now=1000.0)
    assert res["valid"] is False and res["reason"] == "effort_signal_mismatch"


# --------------------------------------------------------------------------- #
# coexistence with the privacy profile (does not disturb rules / learned)      #
# --------------------------------------------------------------------------- #
def test_reputation_block_coexists_with_profile_rules(tmp_path):
    p = ppath(tmp_path)
    # seed a real privacy profile (rules + a learned correction)
    PF.save_profile({"version": 1, "rules": [{"action": "strip", "match": {}}], "learned": []}, p)
    PF.learn({"entity": "Tuesday", "action": "allow", "repo": "tuesday"}, profile_path=p)
    # now add the reputation identity to the SAME profile file
    R.issue_identity(profile_path=p)
    R.record_acceptance(quality=5, profile_path=p)
    # privacy blocks survive intact...
    prof = PF.load_profile(p)
    assert prof["rules"] == [{"action": "strip", "match": {}}]
    assert len(prof["learned"]) == 1 and prof["learned"][0]["entities"]["allow"] == ["Tuesday"]
    # ...and the resolve engine still works over the same file
    resolved = PF.resolve(str(tmp_path), profile=p)
    assert resolved["decisions"]["action"]["action"] == "strip"
    # ...and reputation is present + non-zero
    assert R.reputation_score(profile_path=p) > 0


def test_profile_learn_preserves_reputation_block(tmp_path):
    p = ppath(tmp_path)
    R.issue_identity(profile_path=p)
    R.record_acceptance(quality=5, profile_path=p)
    pid = R.load_identity(p)["pseudonymous_id"]
    # a later privacy-side learn() must not clobber the reputation block
    PF.learn({"entity": "Zephyr", "action": "strip"}, profile_path=p)
    rep = R.load_identity(p)
    assert rep is not None and rep["pseudonymous_id"] == pid
    assert R.reputation_score(profile_path=p) > 0


def test_quality_weight_tolerates_non_numeric():
    # A non-numeric quality (e.g. a dict/list) must fall back to the neutral default,
    # never raise (regression: `weight` was left unbound), and stay in the valid band.
    assert 0.05 <= R._quality_weight({"oops": "not a number"}, None) <= 1.0
    assert 0.05 <= R._quality_weight([1, 2, 3], None) <= 1.0
    assert R._quality_weight(5, None) == 1.0      # numeric path unaffected
    assert R._quality_weight(None, None) == 0.7   # unrated -> neutral default


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
