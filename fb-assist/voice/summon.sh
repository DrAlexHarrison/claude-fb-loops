#!/usr/bin/env bash
# Summon the fb-assist feedback co-author into the focused terminal.
#
# Types "/fb" (and Enter) into whatever window has focus, via xdotool — bind this to
# a WM hotkey (e.g. Super+F in Cinnamon/GNOME) so flagging a bug is ONE physical key
# from anywhere, no "remember to type /fb." Pass words to pre-fill the report:
#   summon.sh                 -> types "/fb" + Enter   (infer-from-session)
#   summon.sh express         -> types "/fb express"   (fast hard-send path)
#   summon.sh the diff view froze   -> types "/fb the diff view froze"
#
# Set FB_SUMMON_NOENTER=1 to type without submitting (review before you send it).
# Fully local; types into your own session, nothing leaves the machine.
set -uo pipefail

cue() { command -v notify-send >/dev/null && notify-send -t 1200 "$1" >/dev/null 2>&1 || true; }

command -v xdotool >/dev/null || { cue "fb-assist: xdotool not installed"; exit 1; }

ARGS="$*"
CMD="/fb"; [ -n "$ARGS" ] && CMD="/fb $ARGS"

xdotool type --clearmodifiers -- "$CMD"
[ -n "${FB_SUMMON_NOENTER:-}" ] || xdotool key --clearmodifiers Return
