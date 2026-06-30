"""pps_pipeline.cli — ``pps capture|package|assess|demo``.

Orchestrates the CORE: a ``SessionBundle`` -> chunk -> **redact (HARD floor
gate)** -> interleave (the packager) -> assess. ``pps demo`` runs the whole thing
on the synthetic fixture with no network, no paid software, no recording.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from . import _schema_util as _su
from . import bundle as _bundle
from . import fixture as _fixture
from .assess import assess
from .chunk import make_chunks
from .interleave import interleave, package_text
from .redact_pass import RedactionResult, redact_events


class PackagingBlocked(RuntimeError):
    """Raised when the redaction floor gate blocks packaging (a leak survived)."""


@dataclass
class PackageBuild:
    package: dict
    redaction: RedactionResult
    chunks: int


# --------------------------------------------------------------------------- #
# Core orchestration
# --------------------------------------------------------------------------- #
def build_package(bundle_dir: str, mode: str = "event_boundary",
                  window_s: float = 30.0, ner: bool = False,
                  strict_gate: bool = True) -> PackageBuild:
    """bundle -> chunk -> redact (gate) -> interleave. Returns the package build.

    The redaction floor is a HARD gate: if a secret/PII survives,
    ``strict_gate=True`` raises :class:`PackagingBlocked` and NO package is
    emitted (the leak never reaches the LLM).
    """
    b = _bundle.load_bundle(bundle_dir)
    events = b.raw_events()
    chunks = make_chunks(events, b.duration_s, mode=mode, window_s=window_s)

    red = redact_events(events, ner=ner)
    if not red.floor_clean and strict_gate:
        raise PackagingBlocked(
            "redaction floor gate FAILED — packaging blocked. Residual: "
            + json.dumps(red.floor_findings))

    pkg = interleave(red.events, chunks, b.session_id, mode=mode,
                     floor_clean=red.floor_clean, redaction_applied=red.applied)
    return PackageBuild(package=pkg, redaction=red, chunks=len(chunks))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_assessment(a: dict) -> str:
    lines = [
        "═" * 70,
        f"  ASSESSMENT — {a['session_id']}   (rubric {a['rubric_version']})",
        "═" * 70,
    ]
    for d in a.get("dimensions", []):
        bar = "●" * d["score"] + "○" * (5 - d["score"])
        lines.append(f"  {d['name']:<20} {bar}  {d['score']}/5")
        for ev in d.get("evidence", []):
            lines.append(f"      └─ [{ev['t']:.1f}s] \"{ev['quote']}\"")
        if d.get("rationale"):
            lines.append(f"         {d['rationale']}")
    lines.append("─" * 70)
    if a.get("strengths"):
        lines.append("  Strengths: " + "; ".join(a["strengths"]))
    if a.get("gaps"):
        lines.append("  Gaps:      " + "; ".join(a["gaps"]))
    lines.append(f"  Overall:   {a.get('overall', '')}")
    lines.append(f"  Confidence: {a.get('confidence', 0):.2f}   "
                 f"evidence_complete: {a.get('evidence_complete')}")
    lines.append("═" * 70)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_package(args) -> int:
    build = build_package(args.bundle_dir, mode=args.mode, window_s=args.window,
                          ner=args.ner)
    out = json.dumps(build.package, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"wrote package ({len(build.package['timeline'])} events, "
              f"{build.chunks} chunks, floor_clean="
              f"{build.package['redaction']['floor_clean']}) -> {args.out}",
              file=sys.stderr)
    else:
        print(out)
    return 0


def cmd_assess(args) -> int:
    with open(args.package, "r", encoding="utf-8") as fh:
        pkg = json.load(fh)
    a = assess(pkg, backend=args.backend, strict=not args.no_strict)
    out = json.dumps(a, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"wrote assessment -> {args.out}", file=sys.stderr)
    if args.render or not args.out:
        print(render_assessment(a))
    return 0


def cmd_demo(args) -> int:
    bundle_dir = args.bundle or _fixture.default_fixture_dir()
    # Self-heal: regenerate the fixture if it isn't present.
    import os
    if not os.path.exists(os.path.join(bundle_dir, "manifest.json")):
        _fixture.generate(bundle_dir)
    print(f"[pps demo] bundle: {bundle_dir}", file=sys.stderr)

    build = build_package(bundle_dir, mode="event_boundary")
    pkg = build.package
    print(f"[pps demo] packaged {len(pkg['timeline'])} events into "
          f"{build.chunks} event-boundary chunks; redaction floor_clean="
          f"{pkg['redaction']['floor_clean']}", file=sys.stderr)

    a = assess(pkg, backend="mock", strict=True)
    print(render_assessment(a))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"package": pkg, "assessment": a}, fh, indent=2, ensure_ascii=False)
        print(f"[pps demo] wrote package+assessment -> {args.out}", file=sys.stderr)
    return 0


def cmd_fixture(args) -> int:
    target = args.dir or _fixture.default_fixture_dir()
    print(_fixture.generate(target))
    return 0


def cmd_capture(args) -> int:
    print(
        "pps capture is the SWAPPABLE EDGE (see pps_pipeline/capture/).\n"
        "  • screen+audio : capture/obs_wfrecorder.sh <bundle_dir> [seconds]\n"
        "  • network HAR  : mitmdump -s capture/mitm_har.py --set bundle_dir=<dir>\n"
        "  • candidate CC : python -m pps_pipeline.capture.ccode_jsonl <bundle_dir>\n"
        "Any recorder that drops a valid manifest+streams works — the pipeline's\n"
        "contract is the bundle, not the tool. Then: pps package <bundle_dir>.",
        file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# Argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pps",
        description="PPS work-observation interview pipeline: bundle -> package -> assessment.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("package", help="bundle -> InterleavedPackage (the packager)")
    p.add_argument("bundle_dir")
    p.add_argument("-o", "--out")
    p.add_argument("--mode", choices=["event_boundary", "fixed"], default="event_boundary")
    p.add_argument("--window", type=float, default=30.0, help="fixed-mode window seconds")
    p.add_argument("--ner", action="store_true", help="also apply the Presidio NER ceiling")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("assess", help="InterleavedPackage -> structured Assessment")
    p.add_argument("package")
    p.add_argument("-o", "--out")
    p.add_argument("--backend", default="mock", choices=["mock", "claude", "ollama"])
    p.add_argument("--render", action="store_true")
    p.add_argument("--no-strict", action="store_true", help="don't raise on gate failure")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser("demo", help="synthetic fixture -> package -> assessment (no network)")
    p.add_argument("--bundle", help="bundle dir (default: the committed fixture)")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("fixture", help="(re)generate the synthetic demo bundle")
    p.add_argument("dir", nargs="?")
    p.set_defaults(func=cmd_fixture)

    p = sub.add_parser("capture", help="the swappable capture edge (guidance)")
    p.set_defaults(func=cmd_capture)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PackagingBlocked as exc:
        print(f"\n⛔ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
