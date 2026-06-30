#!/usr/bin/env bash
# pps capture edge — screen + audio recorder (OBS / wf-recorder).
#
# THE THIN EDGE. Records video.mkv into a bundle dir and stamps t0 so every other
# stream can be normalized to it. Swap this for OBS, ffmpeg x11grab, etc. — the
# pipeline only cares that a valid manifest + streams land in $BUNDLE_DIR.
#
# Usage: obs_wfrecorder.sh <bundle_dir> [duration_s]
set -euo pipefail

BUNDLE_DIR="${1:?usage: obs_wfrecorder.sh <bundle_dir> [duration_s]}"
DURATION="${2:-0}"
mkdir -p "$BUNDLE_DIR"

T0="$(date +%s.%N)"
VIDEO="$BUNDLE_DIR/video.mkv"

echo "[pps capture] t0=$T0  -> $VIDEO" >&2

if command -v wf-recorder >/dev/null 2>&1; then
  # Wayland
  if [ "$DURATION" -gt 0 ] 2>/dev/null; then
    timeout "$DURATION" wf-recorder -a -f "$VIDEO" || true
  else
    wf-recorder -a -f "$VIDEO"
  fi
elif command -v ffmpeg >/dev/null 2>&1; then
  # X11 fallback (region/display via $DISPLAY)
  EXTRA=()
  [ "$DURATION" -gt 0 ] 2>/dev/null && EXTRA=(-t "$DURATION")
  ffmpeg -y -f x11grab -i "${DISPLAY:-:0}" -f pulse -i default "${EXTRA[@]}" "$VIDEO" || true
else
  echo "[pps capture] no wf-recorder/ffmpeg; install one (edge dep)" >&2
  exit 1
fi

# The recorder is responsible only for video + t0. transcript/captions/network/
# ccode_session are added by the other edge tools and assembled into manifest.json
# (see pps_pipeline.fixture.write_manifest for the schema).
echo "$T0" > "$BUNDLE_DIR/.t0_epoch"
echo "[pps capture] done. Build the manifest, then: pps package $BUNDLE_DIR" >&2
