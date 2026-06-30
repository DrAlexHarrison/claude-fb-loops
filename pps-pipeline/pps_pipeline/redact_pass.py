"""pps_pipeline.redact_pass — privacy-preserving observation (fb_assist reuse).

A candidate work-observation is sensitive: the screen, the terminal, the network,
and the candidate's own prompts can all carry secrets / PII / company IP. This
module runs ``fb_assist.redact`` — the *proven* detector floor — over **every
text surface** (speech transcript, frame captions, HAR bodies/headers, and the
``session.jsonl`` narrative) *before* anything is assembled into the package.

The leak-scan floor is a **HARD gate**: if the assembled, redacted text still
trips the secret/PII egress scan, ``floor_clean`` is ``False`` and packaging is
blocked (the orchestrator refuses to emit a package; the assessor refuses to
read one). This mirrors Build 3's planted-sentinel discipline: prove byte-absence,
don't trust the redactor.

No re-implementation: detection + masking + the egress gate are all
``fb_assist.redact`` functions. We only orchestrate which surfaces get scrubbed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from fb_assist import redact as _r

from .bundle import RawEvent


@dataclass
class RedactionResult:
    events: list[RawEvent]
    applied: bool
    floor_clean: bool
    floor_findings: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        """True when the floor is not clean — packaging must be blocked."""
        return not self.floor_clean


def _redact_text(text: str, ner: bool = False, use_gliner: bool = False,
                 use_gitleaks: bool = False) -> str:
    """Mask secrets + PII in one short text surface (reuse fb_assist detectors).

    Default = the **deterministic detector floor only** (regex secrets +
    detect-secrets + regex email/IP/SSN). This is portable (works without the
    NER stack installed) and — crucially — never mangles a non-sensitive quote
    the assessor will cite, because regex matches are exact and context-free.

    ``ner=True`` additionally APPLIES Presidio (and GLiNER if ``use_gliner``)
    findings for maximum recall on names/orgs/locations — the semantic ceiling.
    GLiNER stays off by default (must not re-pull its ONNX over a metered link).
    """
    if not text:
        return text
    findings = _r.scan_secrets(text, use_gitleaks=use_gitleaks,
                               use_detect_secrets=True)
    pii = _r.scan_pii(text, use_gliner=use_gliner)
    if not ner:
        # Apply only the deterministic regex PII (email/IP/SSN); collect-but-drop
        # the context-dependent NER spans so masking is reproducible.
        pii = [f for f in pii if f.detector == "regex"]
    findings += pii
    masked, _ = _r.apply_redactions(text, findings, style="mask")
    return masked


def redact_events(events: Sequence[RawEvent], ner: bool = False,
                  use_gliner: bool = False, use_gitleaks: bool = False) -> RedactionResult:
    """Redact every event's text surface, then run the leak-scan floor gate.

    Returns a :class:`RedactionResult` with the redacted events and the gate
    verdict. ``floor_clean`` is the HARD gate: ``False`` means a secret/PII
    survived and packaging MUST NOT proceed. ``ner=True`` opts into the
    Presidio/GLiNER semantic ceiling for extra masking (see :func:`_redact_text`).
    """
    redacted: list[RawEvent] = []
    for e in events:
        redacted.append(RawEvent(e.t, e.kind,
                                 _redact_text(e.text, ner, use_gliner, use_gitleaks),
                                 e.source, dict(e.meta)))

    # The HARD floor gate runs over exactly what would reach the LLM (the
    # assembled timeline text). It is computed from the DETERMINISTIC detectors
    # only (regex secrets + detect-secrets/gitleaks + regex email/IP/SSN), built
    # DIRECTLY rather than by filtering leak_scan's output — leak_scan dedups
    # spans and a context-dependent Presidio hit can shadow the deterministic
    # one at the same span, so filtering the deduped list is unreliable. NER is
    # the semantic *ceiling*, never the pass/fail gate.
    assembled = "\n".join(e.text for e in redacted)
    floor = _floor_findings(assembled, use_gliner=use_gliner,
                            use_gitleaks=use_gitleaks)
    floor_clean = len(floor) == 0

    # Full adversarial report (incl. NER + paths + IP-markers) for visibility.
    report = _r.leak_scan(assembled, use_gliner=use_gliner)

    return RedactionResult(
        events=redacted,
        applied=True,
        floor_clean=floor_clean,
        floor_findings=[f.to_dict() for f in floor],  # masked by default
        summary=_r.summarize_findings(report),
    )


def _floor_findings(text: str, use_gliner: bool = False,
                    use_gitleaks: bool = False) -> list:
    """The deterministic secret+PII floor for ``text`` (no NER, no dedup shadow)."""
    sec = _r.scan_secrets(text, use_gitleaks=use_gitleaks, use_detect_secrets=True)
    pii = [f for f in _r.scan_pii(text, use_gliner=use_gliner)
           if f.detector == "regex"]
    return sec + pii


def floor_scan_text(text: str, use_gliner: bool = False) -> list:
    """Run the leak-scan floor over arbitrary assembled text (secrets + PII).

    Helper for the gate test: feeding *unredacted* text here returns a non-empty
    list, i.e. the gate would block. Returns the residual secret/PII Findings
    from the deterministic detectors (the hard floor).
    """
    return _floor_findings(text, use_gliner=use_gliner)
