# claude-fb-loops — privacy-preserving feedback co-author for Claude.
# A monorepo of three installable packages: fb-assist (the keystone), fb-os, pps-pipeline.
#
# The headline is `make demo`: watch fb-assist redact a planted-secret session
# end-to-end — download-free, offline, in seconds.

PY ?= python3
FBA := fb-assist
export USE_TF := 0
export USE_FLAX := 0
export TOKENIZERS_PARALLELISM := false

.PHONY: help demo demo-all test test-fb-assist test-fb-os test-pps \
        fixtures setup lint scrub-gate clean

help:
	@echo "claude-fb-loops targets:"
	@echo "  make demo        — fb-assist redacts a planted-secret session, end-to-end (no network, no downloads)"
	@echo "  make demo-all    — demo + the fb-os and pps-pipeline demos"
	@echo "  make test        — run all three test suites (needs 'make setup' for fb-assist's NER recall tests)"
	@echo "  make fixtures     — (re)generate the synthetic fb-assist fixtures"
	@echo "  make setup       — install the packages + NER stack + spaCy model (HEAVY — see banner)"
	@echo "  make scrub-gate  — assert NO real personal data (real home paths etc.) in tracked files"
	@echo "  make lint        — ruff check (if installed)"
	@echo "  make clean       — remove caches + generated fixtures"

# --- The hero: download-free, offline, source-only (no install required) ------
demo:
	@PYTHONPATH=$(FBA) $(PY) $(FBA)/examples/demo.py

demo-all: demo
	@echo "" && echo "=== fb-os demo ===" && $(MAKE) -C fb-os demo
	@echo "" && echo "=== pps-pipeline demo ===" && $(MAKE) -C pps-pipeline demo

# --- Tests --------------------------------------------------------------------
test: test-fb-assist test-fb-os test-pps
	@echo "" && echo "All three suites passed."

test-fb-assist: fixtures
	@echo "== fb-assist ==" && cd $(FBA) && USE_TF=0 $(PY) -m pytest -q

test-fb-os:
	@echo "== fb-os ==" && cd fb-os && USE_TF=0 $(PY) -m pytest -q

test-pps:
	@echo "== pps-pipeline ==" && cd pps-pipeline && USE_TF=0 $(PY) -m pytest -q

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
	$(PY) -m pip install -e ./fb-os -e ./pps-pipeline
	$(PY) -m spacy download en_core_web_sm

# --- Privacy scrub-gate (the publish guard) -----------------------------------
# Asserts NO real personal data survives in the files git would ship. The binding
# check is real-home-paths == 0; a few never-appear identifiers are belt-and-braces.
# (The `.claude-michelle` -> "michelle" account-label in the frozen locate module
#  is an intentional, documented config convention and is NOT a personal leak.)
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
