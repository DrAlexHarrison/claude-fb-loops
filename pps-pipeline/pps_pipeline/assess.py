"""pps_pipeline.assess — the structured, evidence-cited assessment (the deliverable).

Feeds the (text-only, redaction-gated) ``InterleavedPackage`` to an LLM against
``prompts/assessor.md`` and emits a structured :class:`Assessment`: dimensions
with calibrated 1..5 scores, **timestamped evidence quotes**, strengths, gaps, an
overall, and a self-rated confidence.

Two gates make the intelligence trustworthy:

* **floor gate** — assessment refuses to run on a package whose
  ``redaction.floor_clean`` is ``False`` (no un-redacted secrets reach the LLM).
* **no-fabrication gate** — every dimension score MUST cite >=1 timestamped quote
  that (a) lands on a real timeline timestamp and (b) is grounded verbatim in the
  package text. ``evidence_complete`` is *computed here*, never trusted from the
  model; a dimension with an uncited or fabricated quote fails the gate.

LLM backend is pluggable:

* ``mock`` — record/replay canned responses. Free + deterministic. Used by CI and
  ``make demo``. Never touches the network or downloads a model.
* ``claude`` — headless Claude (``claude -p``, Max auth). The production default;
  never invoked by the tests/demo.
* ``ollama`` — a local LLM via Ollama, the documented free fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Any, Optional

from . import _schema_util as _su
from .interleave import package_text

SCHEMA_VERSION = "1.0"
RUBRIC_VERSION = "pps-default-1.0"

_PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_DEFAULT_MOCK = os.path.join(_PROMPT_DIR, "mock", "assessor_response.json")
_MOCK_DIR = os.path.join(_PROMPT_DIR, "mock")

# Tolerances for the no-fabrication gate.
_T_TOLERANCE = 0.6          # an evidence t must land within this of a real one
_QUOTE_MIN_FRAGMENT = 8     # min grounded fragment length after normalization


class AssessmentError(RuntimeError):
    pass


class AssessmentRejected(AssessmentError):
    """Raised when the no-fabrication / floor gate rejects an assessment."""


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class LLMBackend:
    name = "base"

    def generate(self, prompt: str, package: dict) -> dict:  # pragma: no cover
        raise NotImplementedError


class MockBackend(LLMBackend):
    """Record/replay backend. Deterministic, free, offline.

    Resolution order for a package: an explicit ``canned`` dict (if constructed
    with one) -> a keyed recording ``mock/<key>.json`` -> the default canned
    response. ``record()`` writes a keyed recording so a real assessment can be
    captured once and replayed forever.
    """

    name = "mock"

    def __init__(self, canned: Optional[dict] = None,
                 responses_dir: str = _MOCK_DIR,
                 default_path: str = _DEFAULT_MOCK):
        self.canned = canned
        self.responses_dir = responses_dir
        self.default_path = default_path

    @staticmethod
    def key_for(package: dict) -> str:
        payload = json.dumps([(e["t"], e["kind"], e["text"])
                              for e in package.get("timeline", [])],
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def generate(self, prompt: str, package: dict) -> dict:
        if self.canned is not None:
            return json.loads(json.dumps(self.canned))  # defensive copy
        keyed = os.path.join(self.responses_dir, f"{self.key_for(package)}.json")
        path = keyed if os.path.exists(keyed) else self.default_path
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def record(self, package: dict, response: dict) -> str:
        os.makedirs(self.responses_dir, exist_ok=True)
        path = os.path.join(self.responses_dir, f"{self.key_for(package)}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(response, fh, indent=2, ensure_ascii=False)
        return path


class ClaudeBackend(LLMBackend):
    """Headless Claude (``claude -p``, Max auth). Production default.

    NOT used by tests / ``make demo``. Requires the ``claude`` CLI on PATH.
    """

    name = "claude"

    def __init__(self, model: Optional[str] = None, timeout: int = 180):
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str, package: dict) -> dict:
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=self.timeout)
        if proc.returncode != 0:
            raise AssessmentError(f"claude backend failed: {proc.stderr[:400]}")
        return _extract_json(proc.stdout)


class OllamaBackend(LLMBackend):
    """Local LLM via Ollama — the documented free fallback (no metered spend)."""

    name = "ollama"

    def __init__(self, model: str = "llama3.1", host: str = "http://localhost:11434"):
        self.model = model
        self.host = host

    def generate(self, prompt: str, package: dict) -> dict:  # pragma: no cover
        import urllib.request
        body = json.dumps({"model": self.model, "prompt": prompt,
                           "stream": False, "format": "json"}).encode()
        req = urllib.request.Request(f"{self.host}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
        return _extract_json(data.get("response", ""))


def make_backend(name: str, **kw) -> LLMBackend:
    name = (name or "mock").lower()
    if name == "mock":
        return MockBackend(**kw)
    if name == "claude":
        return ClaudeBackend(**kw)
    if name == "ollama":
        return OllamaBackend(**kw)
    raise ValueError(f"unknown assessor backend: {name!r}")


def _extract_json(text: str) -> dict:
    """Pull the assessment JSON object out of a model's response.

    Unwraps the harness envelopes that carry the model text in a field:
    ``claude -p --output-format json`` -> ``{"result": "<text>"}``; Ollama ->
    ``{"response": "<text>"}``. Then finds the first JSON object in that text.
    """
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "dimensions" not in obj:
            for wrapper in ("result", "response", "content", "text"):
                if isinstance(obj.get(wrapper), str):
                    return _extract_json(obj[wrapper])
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise AssessmentError("no JSON object in model response")
    return json.loads(m.group(0))


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #
def load_rubric() -> str:
    with open(os.path.join(_PROMPT_DIR, "assessor.md"), "r", encoding="utf-8") as fh:
        return fh.read()


def build_prompt(package: dict) -> str:
    return (
        f"{load_rubric()}\n\n"
        "=== WORK-OBSERVATION PACKAGE (text-only, redacted; cite [t] timestamps) ===\n"
        f"{package_text(package)}\n"
        "=== END PACKAGE ===\n\n"
        "Return ONLY the JSON object described above. Every dimension's `evidence` "
        "MUST quote text that appears verbatim above, with its timestamp.\n"
    )


# --------------------------------------------------------------------------- #
# The no-fabrication gate
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _grounded(quote: str, haystack_norm: str) -> bool:
    """A quote is grounded if any of its (…/...-split) segments, normalized to
    >= _QUOTE_MIN_FRAGMENT chars, is a substring of the package text."""
    for seg in re.split(r"\.{2,}|…", quote):
        n = _norm(seg)
        if len(n) >= _QUOTE_MIN_FRAGMENT and n in haystack_norm:
            return True
    # short quotes: accept exact normalized match
    nq = _norm(quote)
    return bool(nq) and nq in haystack_norm


def verify_evidence(assessment: dict, package: dict) -> dict:
    """Compute the no-fabrication verdict.

    Returns ``{"evidence_complete": bool, "problems": [...]}``. A dimension fails
    if it has no evidence, or any evidence timestamp doesn't land on a real
    timeline event, or any quote isn't grounded verbatim in the package text.
    """
    timeline = package.get("timeline", [])
    real_ts = [float(e["t"]) for e in timeline]
    haystack = _norm(package_text(package))
    problems: list[str] = []

    for dim in assessment.get("dimensions", []):
        name = dim.get("name", "<unnamed>")
        ev = dim.get("evidence") or []
        if not ev:
            problems.append(f"{name}: no evidence cited (uncited score)")
            continue
        for j, item in enumerate(ev):
            t = item.get("t")
            quote = item.get("quote", "")
            if t is None or not any(abs(float(t) - rt) <= _T_TOLERANCE for rt in real_ts):
                problems.append(f"{name}: evidence[{j}] t={t} matches no timeline event")
            if not _grounded(quote, haystack):
                problems.append(f"{name}: evidence[{j}] quote not grounded: {quote[:48]!r}")

    return {"evidence_complete": len(problems) == 0, "problems": problems}


# --------------------------------------------------------------------------- #
# assess
# --------------------------------------------------------------------------- #
def assess(package: dict, backend: Any = "mock", strict: bool = True,
           **backend_kw) -> dict:
    """Produce a structured, gated :class:`Assessment` for ``package``.

    ``backend`` is a backend name (``"mock"``/``"claude"``/``"ollama"``) or an
    :class:`LLMBackend` instance. ``strict=True`` raises
    :class:`AssessmentRejected` when the floor gate or the no-fabrication gate
    fails; ``strict=False`` returns the assessment with ``evidence_complete``
    set and the problems attached under ``_gate``.
    """
    # Floor gate: never assess a package that still carries un-redacted secrets.
    if not package.get("redaction", {}).get("floor_clean", False):
        raise AssessmentRejected(
            "redaction floor not clean — refusing to assess (un-redacted "
            "secret/PII may be present)")

    be = backend if isinstance(backend, LLMBackend) else make_backend(backend, **backend_kw)
    raw = be.generate(build_prompt(package), package)

    assessment = {
        "schema_version": SCHEMA_VERSION,
        "session_id": package.get("session_id", ""),
        "rubric_version": raw.get("rubric_version", RUBRIC_VERSION),
        "dimensions": raw.get("dimensions", []),
        "strengths": raw.get("strengths", []),
        "gaps": raw.get("gaps", []),
        "overall": raw.get("overall", ""),
        "confidence": float(raw.get("confidence", 0.0)),
    }

    # No-fabrication gate — computed here, never trusted from the model.
    verdict = verify_evidence(assessment, package)
    assessment["evidence_complete"] = verdict["evidence_complete"]

    # Schema validity is part of the contract.
    schema_errs = _su.validation_errors("assessment.schema.json", assessment)

    if strict:
        if schema_errs:
            raise AssessmentRejected("assessment schema errors: " + "; ".join(schema_errs))
        if not verdict["evidence_complete"]:
            raise AssessmentRejected(
                "no-fabrication gate failed: " + "; ".join(verdict["problems"]))
    else:
        assessment["_gate"] = {"schema_errors": schema_errs, **verdict}

    return assessment
