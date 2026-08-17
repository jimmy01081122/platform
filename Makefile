PYTHON ?= .venv/bin/python
EXP_ID ?= example_exploration
STAGE ?= S2
PLATFORM ?= discrete_edge_workstation
RUN_DIR ?=
BUILD ?= build

# Repo-root imports (explorations.*) plus the edgeflow package under src/.
export PYTHONPATH := $(CURDIR):$(CURDIR)/src
# Exact-file-set governance assertions fail if Python writes __pycache__ into
# a declared package directory. Never remove this.
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: doctor new-exp init-run check-run summary validate-configs \
        venv test test-py test-cpp build-cpp clean-pyc seal-evidence verify-evidence

# --- environment -----------------------------------------------------------

venv:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r explorations/moe_cycle_simulator/requirements-phase0.lock
	.venv/bin/pip install "PyYAML>=6" "pytest>=8" zstandard

# --- tests -----------------------------------------------------------------

test: test-py test-cpp

test-py: clean-pyc
	$(PYTHON) -m pytest tests/ -q -p no:cacheprovider
	$(PYTHON) -m pytest explorations/moe_cycle_simulator/tests -q -p no:cacheprovider
	$(PYTHON) -m pytest explorations/moe_cycle_simulator/phase1/tests -q -p no:cacheprovider
	$(PYTHON) -m pytest explorations/moe_cycle_simulator/phase2/tests -q -p no:cacheprovider
	$(PYTHON) -m pytest explorations/moe_cycle_simulator/phase7/tests -q -p no:cacheprovider

build-cpp:
	@for p in phase3 phase4 phase5 phase6; do \
	  cmake -S explorations/moe_cycle_simulator/$$p -B $(BUILD)/$$p -DCMAKE_BUILD_TYPE=Release >/dev/null && \
	  cmake --build $(BUILD)/$$p -j4 >/dev/null && echo "built $$p" || exit 1; \
	done

test-cpp: build-cpp
	@for p in phase3 phase4 phase5 phase6; do \
	  echo "--- ctest $$p ---"; (cd $(BUILD)/$$p && ctest --output-on-failure) || exit 1; \
	done

clean-pyc:
	@find . -name '__pycache__' -type d -not -path './.venv/*' -not -path './evidence/*' -exec rm -rf {} + 2>/dev/null || true

# --- evidence immutability -------------------------------------------------

# evidence/ holds irreplaceable measurement data. Git does not preserve mode
# bits beyond the exec flag, so re-apply read-only after any clone or checkout.
seal-evidence:
	chmod -R a-w evidence
	@echo "evidence/ sealed read-only"

verify-evidence:
	@cd $(CURDIR) && sha256sum -c --quiet governance/lineage/EVIDENCE_SHA256SUMS && \
	  echo "evidence integrity: OK ($$(wc -l < governance/lineage/EVIDENCE_SHA256SUMS) files)"

# --- project bookkeeping ---------------------------------------------------

doctor:
	$(PYTHON) scripts/projectctl.py doctor

new-exp:
	$(PYTHON) scripts/projectctl.py new-exp $(EXP_ID)

init-run:
	$(PYTHON) scripts/projectctl.py init-run $(EXP_ID) --stage $(STAGE) --platform $(PLATFORM)

check-run:
	@test -n "$(RUN_DIR)" || (echo "RUN_DIR is required" && exit 2)
	$(PYTHON) scripts/projectctl.py check-run $(RUN_DIR)

summary:
	$(PYTHON) scripts/projectctl.py summary

validate-configs:
	$(PYTHON) scripts/projectctl.py validate-configs
