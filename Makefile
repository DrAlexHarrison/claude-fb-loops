# claude-fb-loops — privacy-preserving feedback co-author for Claude.
# Two installable packages: fb-assist (the keystone) and fb-os (the org-wide loop).
#
# The headline is `make demo`: watch fb-assist redact a planted-secret session
# end-to-end — download-free, offline, in seconds.

PY ?= python3
FBA := fb-assist
export USE_TF := 0
export USE_FLAX := 0
export TOKENIZERS_PARALLELISM := false

.PHONY: help demo demo-all demo-api \
        install uninstall test test-fb-assist test-fb-os \
        fixtures setup lint scrub-gate clean

help:
	@echo "claude-fb-loops targets:"
	@echo "  make demo        — fb-assist redacts a planted-secret session, end-to-end (no network, no downloads)"
	@echo "  make demo-all    — every surface's demo (CLI + the API edge + fb-os)"
	@echo "  make install     — activate /fb: install the skill + register the MCP server (idempotent)"
	@echo "  make test        — run both test suites (needs 'make setup' for fb-assist's NER recall tests)"
	@echo "  make fixtures     — (re)generate the synthetic fb-assist fixtures"
	@echo "  make setup       — install the packages + NER stack + spaCy model (HEAVY — see banner)"
	@echo "  make scrub-gate  — assert NO real personal data (real home paths etc.) in tracked files"
	@echo "  make lint        — ruff check (if installed)"
	@echo "  make clean       — remove caches + generated fixtures"

# --- The hero: download-free, offline, source-only (no install required) ------
demo:
	@PYTHONPATH=$(FBA) $(PY) $(FBA)/examples/demo.py

# Per-surface demo — runs off a built-in synthetic fixture, offline, no install.
demo-api:
	@PYTHONPATH=$(FBA) $(PY) -m fb_assist.claude_repro demo

demo-all: demo
	@echo "" && echo "=== API / Console (claude-repro) ===" && $(MAKE) demo-api
	@echo "" && echo "=== fb-os demo ===" && $(MAKE) -C fb-os demo

# --- Activate /fb in your own Claude Code (the keystone) ----------------------
install:
	@$(PY) $(FBA)/scripts/install.py
uninstall:
	@$(PY) $(FBA)/scripts/install.py --uninstall

# --- Tests --------------------------------------------------------------------
test: test-fb-assist test-fb-os
	@echo "" && echo "Both suites passed."

test-fb-assist: fixtures
	@echo "== fb-assist ==" && cd $(FBA) && USE_TF=0 $(PY) -m pytest -q

test-fb-os:
	@echo "== fb-os ==" && cd fb-os && USE_TF=0 $(PY) -m pytest -q

# --- Synthetic fixtures (deterministic; never committed) ----------------------
fixtures:
	@$(PY) $(FBA)/tests/fixtures/generate_fixtures.py all >/dev/null && \
		echo "synthetic fixtures ready (sample-mid + sample-large)."

# --- Full install (heavy) -----------------------------------------------------
setup:
	@echo "================================================================"
	@echo " HEAVY INSTALL — first run downloads ~100 MB of models:"
	@echo "   GLiNER PII (~86 MB) + spaCy en_core_web_sm (~12 MB)."
	@echo "   (voice extra adds faster-whisper ~145 MB; not installed here.)"
	@echo "   'make demo' needs NONE of this — it runs offline from source."
	@echo "================================================================"
	$(PY) -m pip install -e ./$(FBA)
	$(PY) -m pip install -e ./fb-os
	$(PY) -m spacy download en_core_web_sm

# --- Privacy scrub-gate (the publish guard) -----------------------------------
# Asserts NO real personal data survives in the files git would ship. The binding
# check is real-home-paths == 0; a few never-appear identifiers are belt-and-braces.
scrub-gate:
	@$(PY) scripts/scrub_gate.py

# --- Lint ---------------------------------------------------------------------
lint:
	@ruff check . || echo "(ruff not installed — skipping)"

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf */.pytest_cache .ruff_cache fb-os/build
	@rm -f $(FBA)/tests/fixtures/sample-mid.jsonl $(FBA)/tests/fixtures/sample-large.jsonl
	@echo "cleaned."
