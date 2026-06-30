#!/usr/bin/env python3
"""Transcribe a wav to stdout via faster-whisper (CPU, local, offline, free).

Model via env FBW_MODEL (default base.en ~145MB; small.en ~480MB = more accurate).
First run downloads the model once, then it's cached in ~/.cache/huggingface.
"""
import sys
import os

MODEL = os.environ.get("FBW_MODEL", "base.en")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: transcribe.py <wav>")
    wav = sys.argv[1]
    from faster_whisper import WhisperModel

    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=4)
    segments, _info = model.transcribe(
        wav, vad_filter=True, language="en", beam_size=1
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
