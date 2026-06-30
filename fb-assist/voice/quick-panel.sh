#!/usr/bin/env bash
# fb-assist quick-action panel — the physical-button fallback to the in-chat num-key
# quick-bar. At the ship/confirm gate the co-author can pop this; it prints the chosen
# action token to stdout and exits 0:
#
#   ship | tighten | privacy | more | cancel
#
# Usage:
#   quick-panel.sh "Sending: your bug + 47 cleaned lines. Removed 3 secrets, 2 emails."
#
# Uses zenity (the established GUI-dialog fallback on this box) when a display is present;
# falls back to a single-keypress terminal prompt (same 1/2/3/4/0 vocabulary as the
# quick-bar) when headless/over SSH. Local only; it decides nothing — it just relays the
# user's one tap back to the co-author, which still runs the leak-scan floor before any send.
set -uo pipefail

SUMMARY="${1:-Ready to send your feedback to Anthropic.}"

emit() { printf '%s\n' "$1"; exit 0; }

if command -v zenity >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  CHOICE="$(zenity --list --radiolist \
      --title="fb-assist — send to Anthropic?" \
      --text="$SUMMARY" \
      --width=520 --height=300 \
      --column="" --column="Action" \
      TRUE  "Ship to Anthropic" \
      FALSE "Tighten the wording" \
      FALSE "Add more privacy" \
      FALSE "Show me more first" \
      FALSE "Cancel" 2>/dev/null)" || CHOICE="Cancel"
  case "$CHOICE" in
    "Ship to Anthropic")   emit ship ;;
    "Tighten the wording") emit tighten ;;
    "Add more privacy")    emit privacy ;;
    "Show me more first")  emit more ;;
    *)                     emit cancel ;;
  esac
fi

# Headless fallback — same keys as the in-chat quick-bar.
printf '%s\n' "$SUMMARY" >&2
printf '%s' "[1] ship · [2] tighten · [3] more privacy · [4] show more · [0] cancel > " >&2
IFS= read -r -n1 KEY || KEY=0
printf '\n' >&2
case "$KEY" in
  1|"") emit ship ;;
  2)    emit tighten ;;
  3)    emit privacy ;;
  4)    emit more ;;
  *)    emit cancel ;;
esac
