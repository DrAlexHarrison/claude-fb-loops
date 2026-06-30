"""End-to-end: the orchestrator, the demo, the HARD floor gate, and the
capture-swap property."""

from __future__ import annotations

import json

import pytest

from pps_pipeline import _schema_util as su
from pps_pipeline import cli
from pps_pipeline import fixture as F
from pps_pipeline.redact_pass import RedactionResult


def test_build_package_end_to_end(fixture_dir):
    build = cli.build_package(fixture_dir, mode="event_boundary")
    assert len(build.package["timeline"]) == 21
    assert build.chunks == 6
    assert build.package["redaction"]["floor_clean"] is True
    assert su.validation_errors("package.schema.json", build.package) == []


def test_floor_gate_blocks_packaging(monkeypatch, fixture_dir):
    """If a leak survives redaction, build_package must raise PackagingBlocked and
    emit NO package."""
    def fake_redact(events, **kw):
        return RedactionResult(events=list(events), applied=True,
                               floor_clean=False,
                               floor_findings=[{"category": "secret",
                                                "entity": "AWS_ACCESS_KEY"}])
    monkeypatch.setattr(cli, "redact_events", fake_redact)
    with pytest.raises(cli.PackagingBlocked):
        cli.build_package(fixture_dir, mode="event_boundary", strict_gate=True)


def test_demo_runs_clean(capsys):
    rc = cli.main(["demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ASSESSMENT" in out
    assert "evidence_complete: True" in out


def test_package_then_assess_via_cli(tmp_path, fixture_dir):
    pkg_path = tmp_path / "pkg.json"
    assert cli.main(["package", fixture_dir, "-o", str(pkg_path)]) == 0
    pkg = json.loads(pkg_path.read_text())
    assert pkg["session_id"] == F.SESSION_ID
    out_path = tmp_path / "assessment.json"
    assert cli.main(["assess", str(pkg_path), "-o", str(out_path)]) == 0
    a = json.loads(out_path.read_text())
    assert a["evidence_complete"] is True


def test_capture_is_swappable(tmp_path):
    """A second 'capture front-end' (the fixture generator) emitting the SAME
    manifest produces a bundle the packager consumes unchanged — capture is a
    genuine swap point, the contract is the bundle."""
    d1 = tmp_path / "rec_a"
    d2 = tmp_path / "rec_b"
    F.generate(str(d1))
    F.generate(str(d2))
    p1 = cli.build_package(str(d1)).package
    p2 = cli.build_package(str(d2)).package
    assert len(p1["timeline"]) == len(p2["timeline"]) == 21
    assert su.validation_errors("package.schema.json", p2) == []
