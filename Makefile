PY ?= python3
VENV := .venv
PYBIN := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

.PHONY: help setup test test-fast test-cov lint clean load-data backtest dashboard restart-dashboard stop-dashboard backup backup-light restore-check smoke-test smoke-test-live refresh-symbol-info run-live run-live-dryrun run-live-once stop-run-live restart-run-live keep-mt5-alive keep-mt5-alive-setup keep-mt5-alive-check keep-mt5-alive-launchagent preflight preflight-strict install-runner-launchagent install-rebaseline-launchagent runner-health runner-log runner-errors restart-runner-launchagent diagnose-runner rebaseline-catalog

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
	@echo "  make backup       Full disaster-recovery zip → ~/Documents (sync to GDrive)"
	@echo "  make backup-light Same, but skip parquets (faster + smaller)"
	@echo "  make restore-check Verify a freshly-restored repo is intact"
	@echo "  make smoke-test   Bridge dry-run (no real order — confirms reachability)"
	@echo "  make smoke-test-live  REAL 0.01-lot order on NASDAQ + close (3s safety countdown)"
	@echo "  make refresh-symbol-info  Pull tick_size/vol_min/contract_size from MT5 → data/symbol_info/"
	@echo ""
	@echo "  ━━ THE DEPLOYMENT RUNNER (without this, nothing trades) ━━━━━━━"
	@echo "  make preflight          End-to-end audit BEFORE go-live (run this first!)"
	@echo "  make preflight-strict   Same but exits 1 on warnings too"
	@echo "  make run-live           Start daemon — polls bars + fires orders"
	@echo "  make run-live-dryrun    Same loop but doesn't send orders"
	@echo "  make run-live-once      Single tick + exit (for cron jobs)"
	@echo ""
	@echo "  ━━ KEEP MT5 RUNNING (macOS only — fixes auto-quit) ━━━━━━━━━━━"
	@echo "  make keep-mt5-alive       Foreground loop: disable App Nap,"
	@echo "                            caffeinate, restart MT5 if it dies"
	@echo "  make keep-mt5-alive-setup One-shot: just disable App Nap, exit"
	@echo "  make keep-mt5-alive-check Print MT5 process state + App Nap config"
	@echo "  make keep-mt5-alive-launchagent  Install LaunchAgent (survives reboot)"
	@echo ""
	@echo "  ━━ VACATION-GRADE RELIABILITY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "  make install-runner-launchagent  Auto-restart runner on crash + at boot"
	@echo "  make runner-health      Heartbeat + bridge + recent activity check"
	@echo "  make runner-log         tail -f /tmp/mt5_runner.log"
	@echo "  make runner-errors      tail -f /tmp/mt5_runner.err"
	@echo ""
	@echo "  make clean        Remove venv + caches"
	@echo ""

# ----- Disaster-recovery backup -----
backup:
	$(PY) scripts/backup_all.py

backup-light:
	$(PY) scripts/backup_all.py --skip-parquets

restore-check:
	$(PY) scripts/sanity_check.py

# ----- Live bridge smoke-test (uses venv Python so deps resolve) -----
smoke-test:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/smoke_test_close.py

smoke-test-live:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	@echo "⚠  This will place a REAL 0.01-lot order on US100.cash and close it."
	@echo "   Press Ctrl+C in the next 3s to abort, or wait..."
	@sleep 3
	$(PYBIN) scripts/smoke_test_close.py --live

# ----- Refresh symbol_info JSON cache from MT5 bridge -----
# Pulls tick_size / volume_min / contract_size for every configured symbol
# into data/symbol_info/<SYM>.json so position-sizing has up-to-date values.
refresh-symbol-info:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/refresh_symbol_info.py

# ----- Deployment runner — the daemon that polls bars + sends orders -----
# Without this running, nothing in deployments.json actually trades.
# Run in a separate terminal alongside `make dashboard`.
run-live:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/run_deployments.py

# Same as run-live but does NOT send orders to the broker — useful for
# debugging the polling loop.
run-live-dryrun:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/run_deployments.py --dry-run

# Single-tick mode (good for cron @ M15 / D1).
run-live-once:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/run_deployments.py --once

# Kill any running deployment-runner process. Looks for both the
# foreground `make run-live` invocation and any LaunchAgent-spawned
# instance. Safe to run when nothing is running (no-op).
stop-run-live:
	@echo "→ stopping deployment runner …"
	-@pkill -f "scripts/run_deployments.py" 2>/dev/null || true
	@sleep 1
	@if pgrep -f "scripts/run_deployments.py" >/dev/null 2>&1; then \
		echo "  ⚠ runner still alive — forcing SIGKILL …"; \
		pkill -9 -f "scripts/run_deployments.py" 2>/dev/null || true; \
	fi
	@echo "  ✓ runner stopped."

# Kill the runner if running, then start it fresh in foreground.
# Use this when you want to pick up code changes without leaving
# orphan processes behind.
restart-run-live: stop-run-live run-live

# ----- Keep MT5 alive on macOS (App Nap + caffeinate) -----
# macOS aggressively suspends idle GUI apps. This script disables
# App Nap for MT5 + runs `caffeinate -dimsu` to prevent system sleep
# while it's alive. Run it in a separate terminal alongside the runner.
keep-mt5-alive:
	@bash scripts/keep_mt5_alive.sh

keep-mt5-alive-setup:
	@bash scripts/keep_mt5_alive.sh --setup-only

keep-mt5-alive-check:
	@bash scripts/keep_mt5_alive.sh --check

keep-mt5-alive-launchagent:
	@bash scripts/keep_mt5_alive.sh --install-launchagent

# ----- Pre-flight check (run BEFORE go-live) -----
preflight:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/preflight_check.py

preflight-strict:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/preflight_check.py --strict

# ----- Vacation-grade reliability -----
# Install the runner as a macOS LaunchAgent — auto-starts at login,
# restarts on crash, survives reboots.
install-runner-launchagent:
	@bash scripts/install_runner_launchagent.sh

# Schedule weekly catalog rebaseline so the cost-priced PF/R/score
# numbers in the catalog never drift more than 7d behind real bars.
# Pairs with the edge-decay autolearner (#269) — autolearner needs
# fresh catalog_R values to compute meaningful z-scores.
install-rebaseline-launchagent:
	@bash scripts/install_rebaseline_launchagent.sh

# News calendar daily refresh — keeps red-folder blackout cache fresh
# so the runner's news-blackout gate works during multi-day vacations.
install-news-refresh-launchagent:
	@bash scripts/install_news_refresh_launchagent.sh

# Vacation-grade: install all unattended-operation LaunchAgents in one
# command. After this + setting up MT5 with autostart, the system runs
# itself for weeks.
vacation-setup: install-runner-launchagent install-rebaseline-launchagent install-news-refresh-launchagent keep-mt5-alive-launchagent
	@chmod +x scripts/keep_mt5_alive.sh 2>/dev/null || true
	@echo ""
	@echo "🌴  VACATION SETUP COMPLETE"
	@echo "   • Runner LaunchAgent: auto-starts at login, restarts on crash"
	@echo "   • MT5 keep-alive: prevents App Nap from suspending MT5"
	@echo "   • Weekly rebaseline: refreshes catalog every Sunday 22:00"
	@echo "   • News refresh: pulls FF calendar every day 06:00"
	@echo ""
	@echo "Verify with:"
	@echo "  make runner-health"
	@echo "  make diagnose-runner"

# v2.db hot/cold split — move trades closed >90 days ago into a
# separate archive DB so the hot DB stays small and dashboard queries
# stay fast. Default is dry-run; pass APPLY=1 to actually move.
# Refresh ForexFactory news calendar cache. Run daily via cron or
# LaunchAgent. The runner reads the cached file on every signal to
# block opens around red-folder events.
refresh-news-calendar:
	@$(PYBIN) scripts/refresh_news_calendar.py

archive-old-trades:
	@if [ "$(APPLY)" = "1" ]; then \
		$(PYBIN) scripts/archive_old_trades.py --apply; \
	else \
		$(PYBIN) scripts/archive_old_trades.py; \
		echo ""; \
		echo "🛈 Add APPLY=1 to actually move data:"; \
		echo "   make archive-old-trades APPLY=1"; \
	fi

# Health check: confirm the runner heartbeat is fresh, bridge is up,
# trades have been firing recently. Run anytime to verify the
# unattended setup is alive.
runner-health:
	@if [ ! -x "$(PYBIN)" ]; then \
		echo "⛔ venv missing — run 'make setup' first."; exit 1; \
	fi
	$(PYBIN) scripts/runner_health.py

# Tail the LaunchAgent log
runner-log:
	@tail -f /tmp/mt5_runner.log

runner-errors:
	@tail -f /tmp/mt5_runner.err

# Restart the runner LaunchAgent — use when runner-health shows the
# heartbeat is STALE (alive but hung). launchctl `KeepAlive` only
# triggers on actual crash; a hung-but-alive process never restarts.
restart-runner-launchagent:
	@PLIST="$$HOME/Library/LaunchAgents/com.user.mt5_runner.plist"; \
	if [ ! -f "$$PLIST" ]; then \
		echo "⛔ LaunchAgent not installed. Run 'make install-runner-launchagent'."; \
		exit 1; \
	fi; \
	echo "🛑 Stopping runner..."; \
	launchctl unload "$$PLIST" 2>/dev/null || true; \
	sleep 1; \
	echo "▶️  Starting runner..."; \
	launchctl load "$$PLIST"; \
	sleep 2; \
	echo "✅ Restarted. Verify with:"; \
	echo "   launchctl list | grep mt5_runner"; \
	echo "   tail -f /tmp/mt5_runner.log"

# Diagnose a hung runner: show last log lines + stack sample of the
# running pid so you can see where it's stuck before restarting.
diagnose-runner:
	@echo "─── launchctl status ───"
	@launchctl list | grep mt5_runner || echo "  (not loaded)"
	@echo
	@echo "─── tail -30 /tmp/mt5_runner.log ───"
	@tail -30 /tmp/mt5_runner.log 2>/dev/null || echo "  (no log)"
	@echo
	@echo "─── tail -20 /tmp/mt5_runner.err ───"
	@tail -20 /tmp/mt5_runner.err 2>/dev/null || echo "  (no err)"
	@echo
	@PID=$$(launchctl list | grep mt5_runner | awk '{print $$1}'); \
	if [ -n "$$PID" ] && [ "$$PID" != "-" ]; then \
		echo "─── Stack sample of pid $$PID (5s) ───"; \
		sample $$PID 5 -mayDie 2>/dev/null | tail -50 || echo "  (sample unavailable)"; \
	fi

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

# Run the autonomous portfolio optimizer.
# Usage:  make optimize
#         make optimize ARGS="--tickers XAUUSD,USOIL,JP225.cash --tfs D1,H1"
#         make optimize ARGS="--top 50 --risk 0.5 --require-sustained"
optimize:
	$(PYBIN) scripts/optimize_portfolio.py $(ARGS)

# Custom backtest sweep with explicit train/test split.
# Usage:  make sweep ARGS="--train-pct 0.7"
#         make sweep ARGS="--ticker USDJPY --tf D1 --train-pct 0.6"
sweep:
	$(PYBIN) scripts/sweep_grid.py $(ARGS)

# Convenience: same as `make optimize` with the metals + energies + Asian
# bundles pre-filled. Run `make refresh-data` first to make sure the
# parquets exist.
optimize-commodities:
	$(PYBIN) scripts/optimize_portfolio.py \
	  --tickers XAUUSD,XAGUSD,XPTUSD,USOIL,UKOIL,JP225.cash,HK50.cash \
	  --tfs D1,H1 --top 30 --risk 0.5

# Streamlit multi-page dashboard. Port 8502 to avoid clashing with v1's :8501.
# Pages are auto-discovered from dashboards/pages/.
dashboard:
	$(VENV)/bin/streamlit run dashboards/app.py --server.port 8502

# Kill the dashboard (anything bound to :8502 + any matching streamlit
# process). Safe to run when nothing is running (no-op).
stop-dashboard:
	@echo "→ stopping dashboard on :8502 …"
	-@lsof -ti tcp:8502 | xargs -r kill -9 2>/dev/null || true
	-@pkill -f "streamlit run dashboards/app.py" 2>/dev/null || true
	@sleep 1
	@echo "  ✓ dashboard stopped."

# Kill any process bound to the dashboard port and re-launch. Useful after
# config / component changes that Streamlit's hot-reload can't pick up
# (e.g. new component files added to dashboards/components/).
# Usage:  make restart-dashboard
restart-dashboard:
	@echo "→ killing any process listening on :8502 …"
	-@lsof -ti tcp:8502 | xargs -r kill -9 2>/dev/null || true
	-@pkill -f "streamlit run dashboards/app.py" 2>/dev/null || true
	@sleep 1
	@echo "→ relaunching dashboard …"
	@$(VENV)/bin/streamlit run dashboards/app.py --server.port 8502 \
	  > /tmp/v2_dashboard.log 2>&1 &
	@sleep 2
	@echo "→ dashboard restarted at http://localhost:8502 (logs: /tmp/v2_dashboard.log)"

# Legacy single-page dashboard (control.py) — kept for reference; new work
# goes through `make dashboard`.
dashboard-legacy:
	$(VENV)/bin/streamlit run dashboards/control.py

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
