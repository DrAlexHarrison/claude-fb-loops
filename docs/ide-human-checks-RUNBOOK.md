# IDE human checks — granular runbook

Five checks I can't run headlessly. Each is self-contained. Do them in any order; report back "1: yes/no, 2: …". Total ~10 min. **Setup once, then the checks.**

---

## SETUP (once, ~30 sec)
1. Open a terminal.
2. Open this repo in your IDE — VS Code: `code <path-to>/claude-fb-loops` (or "File → Open Folder"); JetBrains/Cursor: open the folder the same way. This is where the `fb-assist` MCP server is registered and the `/fb` skill lives (run `make install` first if you haven't).
3. Wait for VS Code to finish loading (bottom status bar stops spinning).

---

## CHECK 1 — Does `/fb` show in the panel's slash menu?  (30 sec)
1. In VS Code, click the **Claude icon** in the left activity bar (the sidebar) to open the Claude panel.
2. Click into the panel's text input box at the bottom.
3. Type a single forward slash:  `/`
4. **Look at the popup list that appears.** Scroll it.
5. **Report:**  Is **`fb`** (or `/fb`) in that list?  → **CHECK 1: yes / no**

## CHECK 2 — Can a panel chat call the fb-assist tools?  (1 min)
1. Still in the Claude **panel** (not a terminal), type this and press Enter:
   `Run the fb-assist tool list_sessions and tell me how many sessions it found.`
2. Watch what happens. It may ask permission to use a tool — if so, **click Allow**.
3. **Report:**  Did it actually run a tool named like **`mcp__fb-assist__list_sessions`** and return a number (not "I don't have that tool")?  → **CHECK 2: yes / no**  (if it shows a tool-permission prompt at all, that's already a "yes" — note that too)

## CHECK 3 — Does the Extension GUI "Give feedback" attach your transcript?  (3 min — the HAR-analog)
*This is the one that decides whether our swap-restore covers the GUI feedback button.*
1. Open a terminal (leave VS Code open).
2. Type:  `sudo mitmproxy --version`  — if it says "command not found", type:  `sudo apt install -y mitmproxy`  and Enter (small download).  *(If you'd rather not, skip to step 8 — I'll do a guided capture another way.)*
3. In the terminal, type:  `mitmweb -p 8080 --set confdir=~/.mitmproxy`  and Enter. A browser tab opens showing captured traffic. Leave it running.
4. Back in VS Code's Claude **panel**, find any Claude reply and look for a **thumbs-up/down or a "…" menu → "Give feedback"** affordance on the message.
5. Click **Give feedback**, type the word `test-fb-capture` in the box, and **Submit**.
6. Switch to the **mitmweb browser tab**. In its filter box type:  `feedback`
7. **Report:**  Click the request that appears. Look at its **size** and **body**.  → **CHECK 3:**  is the body **small (~under 1 KB, just a rating/description)** or **large (tens of KB+, clearly containing your conversation)?**  Tell me the size and whether you see conversation text in it.
8. *(skip-path)* If you skipped mitmproxy: just tell me "skip 3" and I'll stage a Playwright capture you click through instead.

## CHECK 4 — JetBrains zero-code claim  (5 min — only if you have a JetBrains IDE; else skip)
1. Do you have PyCharm / IntelliJ / GoLand / Android Studio installed?  If **no**, reply **"CHECK 4: skip"**.
2. If yes: open it on the folder `~/claude-fb-loops`.
3. Open its **integrated terminal** (View → Tool Windows → Terminal).
4. Type:  `claude`  and Enter.  Wait for it to start.
5. Type:  `/fb`  and Enter.
6. **Report:**  Did `/fb` load (it should say something like "you are now the feedback co-author")?  → **CHECK 4: yes / no / skip**

## CHECK 5 — Is `getDiagnostics` still missing from the panel?  (1 min)
1. In the VS Code Claude **panel**, type and Enter:
   `Call the tool mcp__ide__getDiagnostics and show me the result.`
2. **Report:**  Did it run (returns diagnostics or "no problems"), or did it say it has **no such tool**?  → **CHECK 5: ran / no-such-tool**

---

### When done, reply like:
`1: yes, 2: yes, 3: small ~300 bytes no convo text, 4: skip, 5: no-such-tool`

That single line closes E4/E5 and the IDE panel parity questions. Don't overthink any of them — a "no" or "skip" is just as useful as a "yes."
