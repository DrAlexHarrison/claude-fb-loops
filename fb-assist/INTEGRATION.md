# fb-assist toolbox — integration playbook & gotchas

Proven by `tests/test_integration.py` (7/7 green; full suite 89/89). The three modules — `transcripts.py` (extract), `redact.py` (detect/redact/gate), `package.py` (assemble/swap/preview) — **compose with ZERO module surgery**. All glue is the bridge + call-ordering below. This is the co-author's playbook and the contract the MCP server must honor.

## The validated call-sequence

```python
import fb_assist.transcripts as T, fb_assist.redact as R, fb_assist.package as P

# 1) PARSE — keep BOTH views (see gotcha #1)
records       = list(T.parse(path))            # Record objects
original_raws = [r.raw for r in records]       # raw dicts

# 2) DETECT — WHERE (locators) + WHAT (findings, run per-span in step 3)
loc = T.redaction_map(records)                 # pass Record OBJECTS

# 3) REDACT — two layers: bulk structural strip + char-precise narrative mask
sanitized = R.strip_categories(original_raws, [
    "file_contents","bash_output","tool_calls","websearch","thinking_blocks",
    "hook_output","injected_memory","env_metadata","paths"], mode="replace")
rmap = []
for i, raw in enumerate(sanitized):            # THE BRIDGE, per kept-narrative span
    rec = T.Record(line=i+1, raw=raw, type=raw.get("type",""))
    for sp in list(T.human_prompts([rec])) + list(T.assistant_text([rec])):
        findings = R.scan_secrets(sp.text) + R.scan_pii(sp.text)   # span-local offsets
        chosen   = R.merge_redaction_spans(findings)
        if not chosen: continue
        masked, _ = R.apply_redactions(sp.text, findings, style="mask")
        T.replace_span(raw, sp, masked)        # locator -> in-place mutation
        rmap += [{"uuid": sp.uuid, "category": f.entity, "original": f.text,
                  "replacement": f"‹{R._token_label(f.entity)}›", "count": 1} for f in chosen]

# 4) ASSEMBLE — {real_path: sanitized records} under the 1 MB budget
payload = P.assemble_payload(description, {path: sanitized}, limit=1_000_000,
            effort_signal={"redaction":"surgical","quality":4,"alignment_confidence":5})

# 5) PREVIEW — concise included/stripped gate text
print(P.diff_preview(original_raws, sanitized, redaction_map=rmap).render())

# 6) SWAP-RESTORE — non-destructive, around the REAL /feedback submit
with P.swap_restore(payload.targets, backup_root=...) as h:
    run_feedback()                              # /feedback reads the sanitized file here
# originals byte-exact back on disk (sha256-verified)

# 7) EGRESS GATE — TWO layers (see gotcha #2)
#   HARD (machine-decidable): deterministic floor over the ACTUAL upload bytes
assert R.scan_secrets(upload_bytes_text) == []        # + PII regex floor — proven empty
#   SOFT: NER leak_scan over RENDERED CONTENT -> candidates the co-author adjudicates/self-repairs
candidates = R.leak_scan(rendered_content_text)
```

## The locator ↔ redaction_map bridge (the one real seam)
`transcripts` emits a **locator** per region `{category,line,uuid,field,path,start,end,text}` — "human_prompts lives at message.content of record U." `package.diff_preview` wants `{uuid,category,original,replacement,count}` — char-level redactions. They don't line up. Bridge = run detectors on the located region's TEXT → char-offset Findings → mask in place (`R.apply_redactions` + `T.replace_span`) → emit one diff_preview entry per chosen Finding. `replacement` mirrors `apply_redactions` via `R._token_label` so previews show the true post-mask string. (Optional: lift `_mask_narrative` into `fb_assist` as a reusable helper.)

## Six gotchas the co-author / MCP layer MUST know
1. **TYPE SEAM (#1 footgun):** `transcripts.*` (parse, redaction_map, extractors, replace_span) take **Record objects**; `redact.strip_categories` and `package.*` take **raw dicts** (`record.raw`). Mixing → `'dict' has no attribute 'type'`. Parse once, keep both views.
2. **Two-layer gate — don't run NER over raw JSONL.** NER hallucinates PII from structure (UUID→"credit card", `sessionId`→"US_BANK_NUMBER", `claude-opus-4-8`→"person") and from the placeholder labels themselves. The **hard, machine-decidable gate = the deterministic floor (`scan_secrets` + PII regex) over the ACTUAL upload bytes** (zero false positives, proven empty post-redaction). NER `leak_scan` runs over the **rendered content surface** and yields **candidates for self-repair**, never a boolean veto (matches spec §9 "re-prompt Claude to fix + re-scan").
3. **Recipe = strip bulk + mask narrative.** Keep `human_prompts`/`assistant_text` OUT of the strip set and mask them char-precise; strip everything else. Buried secrets (file_contents/bash_output) are removed wholesale; pasted-into-prose secrets are masked while MEANING survives (validated: "the /feedback flow keeps FREEZING" survived; sk-ant/AKIA/email/SSN/name → ‹MARKERS›).
4. **`"tool_results"` is NOT a strip category** (raises). Use **`"tool_calls"`** — it scrubs BOTH stored copies (model-visible `tool_result` block + structured `toolUseResult` mirror).
5. **NER over-redacts brands/codenames** (Presidio masked "Saturday" as ‹DATE_TIME›). → the persistent profile / `.feedbackpolicy` allowlist (spec §10) is load-bearing: the co-author must let the user **rescue a wrongly-eaten brand/codename and learn it** for next time.
6. **Live files:** `swap_restore` refuses an actively-written transcript unless `allow_live=True`; the safe target is a **past/closed** session (spec §15). For the current session: checkpoint, then treat as past.

## Status
- Detection recall on the planted set: **100%** (secret_layer 3/3, pii_layer 3/3).
- Asserted hard: every planted sentinel ABSENT from sanitized + bundle; deterministic floor over upload = 0; no planted value resurfaces via NER; diff_preview shows per-category redactions; swap byte-exact sha256 restore.
- Modules unchanged; bridge lives in the test (lift to a helper if desired).
