.PHONY: test test-bench lint type-check audit clean

PYTHON ?= .venv/bin/python

test:             ## Run all tests (unit + benchmarks)
	$(PYTHON) -m pytest

test-bench:       ## Run benchmarks only
	$(PYTHON) -m pytest tests/test_benchmarks.py --benchmark-only --benchmark-min-rounds=10 --benchmark-sort=mean

test-bench-save:  ## Run benchmarks and save results
	$(PYTHON) -m pytest tests/test_benchmarks.py --benchmark-only --benchmark-autosave --benchmark-sort=mean

test-bench-compare:  ## Compare current benchmarks against saved baseline
	$(PYTHON) -m pytest tests/test_benchmarks.py --benchmark-only --benchmark-compare=0

lint:             ## Run ruff linter
	$(PYTHON) -m ruff check src tests

type-check:       ## Run type checker
	$(PYTHON) -m ty check src

audit:            ## Audit dependencies for vulnerabilities
	$(PYTHON) -m pip-audit

clean:            ## Remove build artifacts and caches
	rm -rf .pytest_cache .benchmarks .coverage htmlcov build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
