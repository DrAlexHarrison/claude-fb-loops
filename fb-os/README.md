# fb-os — Feedback OS

The **org-wide side** of the feedback loop. `fb-assist` is the edge: it
co-authors redacted feedback and ships it through Claude Code's real `/feedback`.
`fb-os` is the org: it **ingests** those distilled artifacts, **clusters** them
locally (a lightweight Clio reproduction), runs an internal **triager** that
**auto-generates the living `open-questions.json`**, and publishes that file in the
**exact path + shape the CLI's `/fb` already reads** — closing the loop.

```
stage_review bundle ─▶ ingest (+leak floor) ─▶ embed ─▶ cluster (+min-size suppression)
   ─▶ triager (Claude, or free --mock) ─▶ open-questions.json ──▶ /fb asks ONE probe
        ▲                                                                    │
        └──────────────── user's answer re-enters, retires the question ◀────┘
```

## Run it (no network, no paid software)

```bash
make demo        # seeded inbox → clustered → triaged → published → answered (loop closed)
make test        # free, deterministic (mock triager, no live Claude call)
```

`make demo` prints each stage and publishes a schema-valid `open-questions.json` to a
workdir-local path (it never clobbers the real `~/.config/fb-assist/open-questions.json`).

## The seam (what the `/fb` co-author consumes)

- **File:** `~/.config/fb-assist/open-questions.json` (override `$FB_ASSIST_OPEN_QUESTIONS`).
- **Schema:** [`fb_os/schema/open-questions.schema.json`](fb_os/schema/open-questions.schema.json).
- **Selector:** `fb_os.questions.rank_for(report_context)` — the same selection rule both
  ends use so the producer and consumer can't drift. Returns the single most-relevant
  **open**, unexpired, surface-applicable question, or `None`. *One. Never a survey.*

## The keystone

[`fb_os/questions.py`](fb_os/questions.py) owns the file: the `OpenQuestion` /
`OpenQuestionSet` models, `load` / `merge` (stable ids, retire answered/expired) /
`publish` (atomic, via `fb_assist.package._atomic_write`), and the shared `rank_for`
selector. Everything else (`store`, `ingest`, `embed`, `cluster`, `triager`, `metrics`)
is scaffolding around that one closed loop.

## Zero-download core, documented production upgrades

| Concern | Core (ships, runs anywhere) | Production upgrade (optional extra) |
|---|---|---|
| Store | stdlib `sqlite3` + JSON embeddings + pure-Python cosine | Postgres + pgvector (`FB_OS_STORE_BACKEND=postgres`, `[postgres]`) |
| Embeddings | deterministic feature-hashing vectorizer | `sentence-transformers` BGE-M3/MiniLM (`--backend sbert`, `[embed]`) |
| Clustering | threshold agglomeration + c-TF-IDF labels + min-size suppression | BERTopic (UMAP+HDBSCAN) — the literal Clio repro (`[cluster]`) |
| Triager | free `--mock` record/replay (`fixtures/triager-replay.json`) | headless Claude `claude -p` under Max auth (`--backend claude`) |
| Dashboard | committed static HTML (`fb-os metrics --html`) | Metabase (`dashboards/metabase/`, AGPL, internal-only) |

Reuses `fb_assist` for transcript parsing, the redaction **leak-scan floor** (re-run
on ingest; a blocking hit **quarantines** the bundle), the atomic writer, and the
effort-signal schema — **reused, never reimplemented**.

## CLI

```
fb-os ingest --inbox DIR --db feedback-os.db        # +leak floor +embed
fb-os cluster --db feedback-os.db                   # +min-cluster-size suppression
fb-os triage  --db feedback-os.db --questions OUT --mock fixtures/triager-replay.json
fb-os publish-questions --db feedback-os.db --out ~/.config/fb-assist/open-questions.json
fb-os metrics --db feedback-os.db --html dashboard.html
fb-os demo                                           # the whole loop, end to end
```
