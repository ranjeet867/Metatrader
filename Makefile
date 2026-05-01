PY ?= python3
VENV := .venv
PYBIN := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

.PHONY: help setup test test-fast test-cov lint clean load-data backtest

help:
	@echo ""
	@echo "  mt5_quant_trader_v2 — production-grade rebuild"
	@echo "  ─────────────────────────────────────────────"
	@echo "  make setup        Create venv + install deps"
	@echo "  make test         Run all tests (the gate that must always be green)"
	@echo "  make test-fast    Skip slow/integration tests"
	@echo "  make test-cov     Run tests with coverage report"
	@echo "  make lint         Run ruff linter"
	@echo "  make load-data    Fetch real US100.cash H1 from MT5 bridge → parquet"
	@echo "  make backtest     Run the EMA cross backtest on locked data"
	@echo "  make clean        Remove venv + caches"
	@echo ""

setup:
	@if [ ! -d $(VENV) ]; then $(PY) -m venv $(VENV); fi
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@echo ""
	@echo "  ✓ venv ready — try: make test"

test:
	$(PYTEST)

test-fast:
	$(PYTEST) -m "not slow and not integration"

test-cov:
	$(PYTEST) --cov=core --cov=strategies --cov-report=term-missing

lint:
	$(VENV)/bin/ruff check core/ strategies/ tests/ scripts/

load-data:
	$(PYBIN) scripts/load_real_data.py --ticker US100.cash --tf H1 --bars 8000

# Shortcut: import a parquet from v1's daily cache. Fast path if you already
# fetched data in v1 and just want to start backtesting in v2.
import-from-v1:
	$(PYBIN) scripts/import_from_v1.py --ticker US100.cash --tf H1

# Query MT5 bridge for a symbol's tick metadata so we can set
# money_per_unit_price correctly in the backtester.
inspect-symbol:
	$(PYBIN) scripts/inspect_symbol.py --ticker US100.cash

backtest:
	$(PYBIN) scripts/run_backtest.py

# Import from v1 cache for any timeframe (defaults to H1).
# Usage: make import-tf TF=M15
import-tf:
	$(PYBIN) scripts/import_from_v1.py --ticker US100.cash --tf $(or $(TF),H1)

# Backtest at any timeframe with the strongest config so far (12/26 long-only).
# Usage: make backtest-tf TF=M15
backtest-tf:
	$(PYBIN) scripts/run_backtest.py --ticker US100.cash --tf $(or $(TF),H1) \
	  --fast 12 --slow 26 --long-only

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
