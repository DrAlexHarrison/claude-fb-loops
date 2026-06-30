"""The assessment (the deliverable intelligence): structured + schema-valid, the
no-fabrication evidence gate, and the floor gate — all free + deterministic via
the --mock record/replay backend."""

from __future__ import annotations

import copy

import pytest

from pps_pipeline import _schema_util as su
from pps_pipeline import assess as A


def test_assessment_is_structured_and_schema_valid(package):
    a = A.assess(package, backend="mock", strict=True)
    assert su.validation_errors("assessment.schema.json", a) == []
    assert a["session_id"] == package["session_id"]
    assert a["dimensions"] and len(a["dimensions"]) >= 3


def test_every_dimension_cites_a_real_timestamped_quote(package):
    a = A.assess(package, backend="mock", strict=True)
    assert a["evidence_complete"] is True
    real_ts = {round(e["t"], 1) for e in package["timeline"]}
    for dim in a["dimensions"]:
        assert dim["evidence"], f"{dim['name']} has no evidence"
        for ev in dim["evidence"]:
            assert any(abs(ev["t"] - rt) <= 0.6 for rt in real_ts)


def test_no_fabrication_gate_rejects_uncited_score(package):
    """An assessment with a dimension whose score cites nothing must be rejected."""
    bad = {
        "rubric_version": "pps-default-1.0",
        "dimensions": [{"name": "debugging_approach", "score": 5,
                        "evidence": [], "rationale": "trust me"}],
        "overall": "x", "confidence": 0.9,
    }
    be = A.MockBackend(canned=bad)
    with pytest.raises(A.AssessmentRejected):
        A.assess(package, backend=be, strict=True)


def test_no_fabrication_gate_rejects_fabricated_quote(package):
    """A quote that isn't grounded in the package text (or cites a phantom
    timestamp) is rejected."""
    bad = {
        "rubric_version": "pps-default-1.0",
        "dimensions": [{"name": "tool_fluency", "score": 5,
                        "evidence": [{"t": 999.0,
                                      "quote": "solved a quantum computing proof"}],
                        "rationale": "fabricated"}],
        "overall": "x", "confidence": 0.9,
    }
    be = A.MockBackend(canned=bad)
    with pytest.raises(A.AssessmentRejected):
        A.assess(package, backend=be, strict=True)
    # non-strict surfaces the problem instead of raising
    soft = A.assess(package, backend=be, strict=False)
    assert soft["evidence_complete"] is False
    assert soft["_gate"]["problems"]


def test_verify_evidence_directly(package):
    a = A.assess(package, backend="mock", strict=True)
    v = A.verify_evidence(a, package)
    assert v["evidence_complete"] is True and v["problems"] == []


def test_floor_gate_refuses_unclean_package(package):
    """assess must refuse a package whose redaction floor is not clean."""
    dirty = copy.deepcopy(package)
    dirty["redaction"]["floor_clean"] = False
    with pytest.raises(A.AssessmentRejected):
        A.assess(dirty, backend="mock", strict=True)


def test_mock_record_replay_roundtrip(package, tmp_path):
    """Record one assessment keyed by the package, then replay it deterministically."""
    canned = A.assess(package, backend="mock", strict=True)
    be = A.MockBackend(responses_dir=str(tmp_path))
    path = be.record(package, {
        "rubric_version": "pps-default-1.0",
        "dimensions": canned["dimensions"],
        "strengths": canned["strengths"], "gaps": canned["gaps"],
        "overall": canned["overall"], "confidence": canned["confidence"],
    })
    assert path.endswith(".json")
    replayed = A.assess(package, backend=be, strict=True)
    assert replayed["dimensions"] == canned["dimensions"]
    # keying is content-addressed: same package -> same key
    assert be.key_for(package) == A.MockBackend.key_for(package)


def test_confidence_in_range(package):
    a = A.assess(package, backend="mock", strict=True)
    assert 0.0 <= a["confidence"] <= 1.0
