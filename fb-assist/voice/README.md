# Fast-response affordances

Three small, local-only helpers that make giving feedback a keystroke. They are
*offered*, never forced — the in-chat **quick-bar** (the `1/2/3/4/0` vocabulary the
co-author renders, defined in `prompts/co-author.md`) is the primary path and needs no
setup. These add physical hotkeys + buttons on top.

| Helper | What it does | Bind to |
|---|---|---|
| `summon.sh` | Types `/fb` (and Enter) into the focused window — summon the co-author from anywhere, no "remember to type it." `summon.sh express` pre-fills the fast path. | a global hotkey, e.g. **Super+F** |
| `dictate.sh` | Push-to-talk: record → local faster-whisper transcribe → type the text in. People say ~5× more than they type — this is the *deep* feedback lane. Toggle to start/stop. | a global hotkey, e.g. **Super+V** |
| `quick-panel.sh` | Pops a zenity button panel (Ship / Tighten / More privacy / Show more / Cancel) at the confirm gate; prints the choice. Falls back to a one-keypress terminal prompt when headless. | invoked by the co-author |

Everything runs on-box; nothing leaves the machine. `summon.sh` and `quick-panel.sh`
resolve their own paths, so they work from any clone location.

## Binding the hotkeys

**Cinnamon** (this workstation):
```bash
DIR=~/.claude/skills/fb/../../  # or the repo's fb-assist/voice path
# Summon  → Super+F
gsettings set org.cinnamon.desktop.keybindings.custom-keybinding:/org/cinnamon/desktop/keybindings/custom-keybindings/fb-summon/ name "fb summon"
gsettings set org.cinnamon.desktop.keybindings.custom-keybinding:/org/cinnamon/desktop/keybindings/custom-keybindings/fb-summon/ command "/abs/path/to/fb-assist/voice/summon.sh"
gsettings set org.cinnamon.desktop.keybindings.custom-keybinding:/org/cinnamon/desktop/keybindings/custom-keybindings/fb-summon/ binding "['<Super>f']"
```
(then add `custom-keybindings/fb-summon/` to the `custom-list` key). **GNOME** uses the
`org.gnome.settings-daemon.plugins.media-keys` custom-keybindings schema with the same
three fields. Pick combos that don't already collide on your desktop — check first:
```bash
gsettings list-recursively | grep -i "<Super>f"   # empty = free to bind
```
Or bind through the Settings GUI (Keyboard → Shortcuts → Custom) pointing at the script.
