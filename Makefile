PY ?= python3
VENV := .venv
PYBIN := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

.PHONY: help setup test test-fast test-cov lint clean load-data backtest dashboard

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

# Refresh ALL cached parquets from the MT5 bridge in one shot.
# Skips silently for any symbol the bridge doesn't currently serve.
# Usage: make refresh-data
#        make refresh-data BARS_D1=3000          # request more history per cell
refresh-data:
	$(PYBIN) scripts/refresh_all_data.py \
	  --bars-d1 $(or $(BARS_D1),2000) \
	  --bars-h1 $(or $(BARS_H1),8000) \
	  --bars-m15 $(or $(BARS_M15),8000)

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

# Run ALL 7 strategy variants on the given TF and print a comparison table.
# Every result is reconciliation-checked — the gate halts if any strategy lies.
# Usage: make sweep-strategies TF=H1
#        make sweep-strategies TF=M15 LONGONLY=1
sweep-strategies:
	$(PYBIN) scripts/sweep_strategies.py --ticker US100.cash --tf $(or $(TF),H1) \
	  $(if $(LONGONLY),--long-only,)

# Same sweep but with REALISTIC FTMO friction modeled.
# Default commission $3/round-trip, 0.1×ATR per-fill slippage, 60/40 train/test.
# Usage: make sweep-realistic TF=H1 LONGONLY=1
#        make sweep-realistic COMM=5 SLIP=0.15 TRAIN=0.7
sweep-realistic:
	$(PYBIN) scripts/sweep_strategies.py --ticker US100.cash --tf $(or $(TF),H1) \
	  --commission-per-trade $(or $(COMM),3.0) \
	  --slippage-atr-frac $(or $(SLIP),0.1) \
	  --train-pct $(or $(TRAIN),0.6) \
	  $(if $(LONGONLY),--long-only,)

# Multi-ticker × multi-TF × multi-strategy grid sweep.
# Outputs docs/grid_results.md heatmap.
sweep-grid:
	$(PYBIN) scripts/sweep_grid.py \
	  --commission-per-trade $(or $(COMM),3.0) \
	  --slippage-atr-frac $(or $(SLIP),0.1) \
	  --train-pct $(or $(TRAIN),0.6) \
	  $(if $(LONGONLY),--long-only,)

# Vet a strategy in <60s — verifies invariants + 8×3 grid + verdict.
# Generates docs/<name>_screening.md.
# Usage: make screen STRATEGY=ibs
#        make screen STRATEGY=vol_breakout TICKER=US100.cash TF=D1
screen:
	$(PYBIN) scripts/screen_strategy.py --strategy $(STRATEGY) \
	  $(if $(TICKER),--ticker $(TICKER),) \
	  $(if $(TF),--tf $(TF),)

# Run the FTMO pass-rate Monte-Carlo simulator on the current portfolio.
ftmo-sim:
	$(PYBIN) scripts/ftmo_sim.py $(ARGS)

# Streamlit multi-page dashboard. Port 8502 to avoid clashing with v1's :8501.
# Pages are auto-discovered from dashboards/pages/.
dashboard:
	$(VENV)/bin/streamlit run dashboards/app.py --server.port 8502

# Legacy single-page dashboard (control.py) — kept for reference; new work
# goes through `make dashboard`.
dashboard-legacy:
	$(VENV)/bin/streamlit run dashboards/control.py

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
