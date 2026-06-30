# pps-pipeline — Build 2 (PPS work-observation interview pipeline)
#
# The CORE runs on a SYNTHETIC fixture: no recording, no paid software, no
# network, no model downloads. `make demo` proves the hard part end-to-end.

PY ?= python
export USE_TF := 0

.PHONY: demo test fixture package clean help

help:
	@echo "make demo     - synthetic bundle -> package -> evidence-cited assessment (no network)"
	@echo "make test     - run the unit suite (fixture-driven, free, deterministic)"
	@echo "make fixture  - (re)generate the synthetic demo bundle"
	@echo "make package  - build + print the InterleavedPackage for the fixture"

demo:
	$(PY) -m pps_pipeline.cli demo

test:
	$(PY) -m pytest -q

fixture:
	$(PY) -m pps_pipeline.fixture

package:
	$(PY) -m pps_pipeline.cli package fixtures/session-demo

clean:
	rm -rf .pytest_cache **/__pycache__ */**/__pycache__
