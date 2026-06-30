# pps-pipeline — PPS Work-Observation Interview Pipeline (Build 2)

Turn a recorded candidate **work-observation** session into an interleaved,
timestamp-aligned, **text-only** multimodal package, then into a structured,
**evidence-cited** Claude assessment of how the candidate works.

> The **analysis intelligence is the work**: the *packager* (`interleave.py`) and
> the *assessment* (`assess.py` + `prompts/assessor.md`) are the deliverable.
> Capture is an honestly-thin **swappable edge**; consent/legal is out of the core.

## Quickstart

```bash
cd pps-pipeline
make demo      # synthetic bundle -> package -> assessment. No network, no paid software.
make test      # fixture-driven unit suite (free, deterministic)
```

`make demo` runs the whole core on `fixtures/session-demo/` — a synthetic
`SessionBundle` with pre-baked captions, a transcript, a HAR, and a
planted-secret Claude Code `session.jsonl` — so it needs **zero recording**.

## Architecture (bundle-first, capture-agnostic)

```
capture edge (SWAPPABLE)            pps package (the CORE)                 pps assess
  video/audio/HAR/.jsonl  ─►  bundle ─► chunk ─► redact(GATE) ─► interleave ─► Assessment
  (OBS/wf-recorder/mitm)      load+      tool-     fb_assist     THE          evidence-cited,
                              normalize  boundary  redact floor  PACKAGER     no-fabrication gate
```

* **`bundle.py`** — the `SessionBundle` contract: one clock origin `t0`, every
  stream normalized to offsets (whisper/captions emit offsets; HAR + the CC
  `.jsonl` carry absolute timestamps → normalized). The candidate `session.jsonl`
  is parsed with **`fb_assist.transcripts`** (full reuse).
* **`chunk.py` + `interleave.py`** — the packager (the original work). Merges
  captions + speech + tool calls/results + network into **one strictly
  time-ordered, text-only** timeline; every event appears exactly once; **no
  image/video bytes ever enter the package** (enforced structurally).
* **`redact_pass.py`** — runs **`fb_assist.redact`** over *every* text surface; the
  leak-scan floor is a **HARD gate** that blocks packaging on a residual leak.
* **`assess.py` + `prompts/assessor.md`** — structured assessment via a pluggable
  LLM backend; the **no-fabrication gate** requires every dimension score to cite
  ≥1 timestamped quote grounded verbatim in the package.

## Two structurally-enforced investigation constraints

1. **Raw video never reaches the LLM.** `InterleavedPackage` is text only;
   `interleave.assert_text_only` proves no bytes can be present.
2. **The packager is the original work.** `interleave.interleave` guarantees the
   timestamp-merge + strict-ascending order + every-event-exactly-once invariants.

## Backends, all free + deterministic in CI

| Stage | Default (prod) | Free fallback | CI / demo |
|------|----------------|---------------|-----------|
| Captioning | Claude vision (Max auth) | Ollama LLaVA/BLIP | **pre-baked / mock** |
| ASR | faster-whisper (reuse) | — | **bundle ships transcript** |
| Assessor | headless Claude (`claude -p`) | Ollama | **`--mock` record/replay** |

Nothing in the core/demo touches the network or downloads a model.

## Out of the core (v-next, by design)

Consent/legal framing (GDPR/CCPA, retention, encryption-at-rest) — manifest
placeholder only; keystroke **dynamics** (timing/paste, never raw keystrokes);
multi-candidate batch + comparative ranking; a reviewer UI; Argilla human
calibration of the rubric. Real capture front-ends live in `pps_pipeline/capture/`
and are the explicitly-thin edge.

Depends on the sibling **`fb-assist`** package (redaction + transcript parsing +
the faster-whisper voice wrapper). It is resolved on `sys.path` by the package
bootstrap — not pip-installed — to avoid re-pulling its NER stack.
