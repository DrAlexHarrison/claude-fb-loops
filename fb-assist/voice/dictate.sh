#!/usr/bin/env bash
# Toggle push-to-talk dictation. Press the bound hotkey once to start recording,
# press again to stop -> transcribe locally (faster-whisper, CPU) -> type into the
# focused window via xdotool. Fully local/offline; nothing leaves the machine.
#
#   FBW_MIC    pipewire source name (empty = system default input)
#   FBW_MODEL  whisper model (default base.en; small.en for more accuracy)
#   FBW_PYTHON python interpreter that has faster-whisper installed (default: python3)
set -uo pipefail

# Resolve paths relative to this script so it runs from any checkout location.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${FBW_PYTHON:-python3}"
PIDF="/tmp/fbw-dictate.pid"
WAV="/tmp/fbw-dictate.wav"
MIC="${FBW_MIC:-}"

cue() { notify-send -t "${2:-1200}" "$1" >/dev/null 2>&1 || true; }

if [[ -f "$PIDF" ]] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  # --- STOP: end recording, transcribe, type ---
  kill "$(cat "$PIDF")" 2>/dev/null || true
  rm -f "$PIDF"
  cue "🎙️ transcribing…" 1500
  TEXT="$("$PY" "$DIR/transcribe.py" "$WAV" 2>/dev/null)"
  if [[ -n "$TEXT" ]]; then
    xdotool type --clearmodifiers -- "$TEXT"
  else
    cue "🎙️ (nothing heard)" 1200
  fi
else
  # --- START: begin recording ---
  rm -f "$WAV"
  if [[ -n "$MIC" ]]; then
    parecord --device="$MIC" --file-format=wav "$WAV" >/dev/null 2>&1 &
  else
    parecord --file-format=wav "$WAV" >/dev/null 2>&1 &
  fi
  echo $! > "$PIDF"
  cue "🎙️ recording… (toggle hotkey to stop)" 1500
fi
