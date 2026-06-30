# fb-os — Feedback OS (Build 1). Free + local: no network, no paid software.
.PHONY: demo test clean help install

PY ?= python3
WORKDIR ?= build/demo

help:
	@echo "fb-os targets:"
	@echo "  make demo   — run the whole closed loop on a seeded inbox (no network/paid)"
	@echo "  make test   — run the test suite (free, deterministic, mock triager)"
	@echo "  make clean  — remove the demo workdir + caches"

# The headline: seeded inbox -> ingest(+leak floor) -> cluster(+suppression) ->
# triage(--mock) -> publish open-questions.json -> answer it -> it flips to answered.
# Publishes to a workdir-local file (never clobbers the user's real CLI stub).
demo:
	@FB_ASSIST_OPEN_QUESTIONS=$(WORKDIR)/open-questions.json $(PY) -m fb_os.cli demo --workdir $(WORKDIR)

test:
	@USE_TF=0 $(PY) -m pytest -q

install:
	@$(PY) -m pip install -e ../fb-assist -e .

clean:
	@rm -rf $(WORKDIR) .pytest_cache fb_os/__pycache__ tests/__pycache__ *.db *.db-wal *.db-shm
	@echo "cleaned."
