# Startup checklist — after a laptop reboot

A complete, in-order recipe for getting the system back to "trading
unattended" state after rebooting your Mac. If you have the LaunchAgents
installed (recommended), most of this is automatic.

---

## TL;DR — fully-automated path (LaunchAgents installed)

If you've previously run `make install-runner-launchagent` and `make
keep-mt5-alive-launchagent`, **everything starts at login automatically**:

1. macOS logs you in
2. `keep_mt5_alive` LaunchAgent → launches MetaTrader 5 + disables App Nap + caffeinates
3. `mt5_runner` LaunchAgent → starts `scripts/run_deployments.py` polling every 60s
4. **You don't need to do anything.** Just open the dashboard:
   ```bash
   make dashboard
   ```
   and verify on the **⚡ Command Center** page that everything is green.

If any of those LaunchAgents are missing, install them:

```bash
make install-runner-launchagent     # auto-restart deployment runner
make keep-mt5-alive-launchagent     # auto-launch + keep MT5 running
```

Verify:
```bash
make keep-mt5-alive-check       # prints MT5 process state + App Nap config
make runner-health              # prints heartbeat + bridge + recent activity
```

---

## Manual path — what to run if you don't have LaunchAgents

Open **three terminals** in `~/Documents/mt5_quant_trader_v2`:

### Terminal 1 — keep MT5 alive
```bash
make keep-mt5-alive
```
This loop launches MT5 if it's not running, disables App Nap, and runs
`caffeinate -dimsu` to prevent display/system sleep. Leave it open.

### Terminal 2 — the deployment runner (THE thing that fires trades)
```bash
make run-live
```
Polls the bridge every 60s, fetches bars, calls `runner.tick()`, sends
orders via `LiveExecutor.send_order`. Without this, NO trades fire.

### Terminal 3 — the dashboard
```bash
make dashboard
```
Streamlit UI at http://localhost:8502.

---

## Full cheatsheet — every make command, by purpose

### Daily-use
| Command | What it does |
|---|---|
| `make dashboard` | Streamlit UI on :8502 |
| `make run-live` | Start deployment runner (polls + fires orders) |
| `make run-live-once` | Single tick + exit (good for cron) |
| `make run-live-dryrun` | Same loop, NO orders sent (debug mode) |
| `make runner-health` | Heartbeat + bridge + recent activity check |
| `make runner-log` | `tail -f /tmp/mt5_runner.log` |
| `make runner-errors` | `tail -f /tmp/mt5_runner.err` |

### Pre-flight (run before going live with new deployments)
| Command | What it does |
|---|---|
| `make refresh-symbol-info` | Pull tick_size / vol_min / contract_size from MT5 |
| `make preflight` | End-to-end audit (parquet, parity, sizing, etc) |
| `make preflight-strict` | Same, but exits 1 on warnings too |
| `make smoke-test` | Bridge dry-run on US100.cash (no real order) |
| `make smoke-test-live` | Real 0.01-lot order + close (3s safety countdown) |

### Background services / vacation mode
| Command | What it does |
|---|---|
| `make keep-mt5-alive-launchagent` | Install: auto-launch MT5 at login |
| `make install-runner-launchagent` | Install: auto-restart runner on crash + at boot |
| `make keep-mt5-alive` | Foreground keep-alive loop |
| `make keep-mt5-alive-check` | Print MT5 process state + App Nap config |
| `make keep-mt5-alive-setup` | One-shot: just disable App Nap, exit |

### Backup / restore
| Command | What it does |
|---|---|
| `make backup` | Full zip → `~/Documents/mt5_v2_backup_*.zip` (sync to GDrive) |
| `make backup-light` | Same, skip parquets (faster + smaller) |
| `make restore-check` | Verify a freshly-restored repo is intact |

### Tests + linting
| Command | What it does |
|---|---|
| `make test` | Run all 749 tests |
| `make test-fast` | Skip slow / integration tests |
| `make test-cov` | Tests with coverage report |
| `make lint` | ruff linter |

### Setup / cleanup
| Command | What it does |
|---|---|
| `make setup` | Create venv + install deps |
| `make clean` | Remove venv + caches |

---

## After-reboot in-order checklist

After cold boot, **wait 30 seconds for macOS to finish login**, then:

```bash
# 1. Verify MT5 is alive
make keep-mt5-alive-check
# Expect: ✅ MT5 is running (pid=...) · App Nap disabled: 1

# 2. Verify the runner is alive
make runner-health
# Expect: ✅ bridge OK · ✅ heartbeat fresh · ✅ LaunchAgent running

# 3. (optional) Run preflight to catch silent-fail conditions
make preflight
# Look for any ⛔ items and fix before relying on the system

# 4. Open the dashboard to monitor
make dashboard
# Visit http://localhost:8502 → Command Center page
```

Everything green on the Command Center page = you can walk away.

---

## If MT5 didn't auto-start at login

Check the LaunchAgent:

```bash
launchctl list | grep keep_mt5
# Expect a row like: <pid>  0  com.user.keep_mt5_alive
# If pid is "-", the agent crashed last time. Look at /tmp/mt5_alive.err
```

Reinstall:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.keep_mt5_alive.plist 2>/dev/null
make keep-mt5-alive-launchagent
```

Or run the foreground keep-alive (keeps MT5 running while terminal is open):
```bash
make keep-mt5-alive
```

---

## If the runner didn't auto-start

Check:
```bash
launchctl list | grep mt5_runner
tail -50 /tmp/mt5_runner.log
tail -20 /tmp/mt5_runner.err
```

Reinstall:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.mt5_runner.plist 2>/dev/null
make install-runner-launchagent
```

Or run in foreground:
```bash
make run-live
```

---

## Going on vacation — the full ritual

Before leaving:

```bash
# 1. Make sure both LaunchAgents are installed
make install-runner-launchagent
make keep-mt5-alive-launchagent

# 2. Pre-flight audit
make preflight
# Fix any ⛔ items

# 3. Take a fresh backup
make backup
# Sync the resulting .zip in ~/Documents to Google Drive

# 4. Confirm system is healthy
make runner-health

# 5. Set system preferences:
#    - Energy Saver: "Prevent computer from sleeping" ON when on AC
#    - Lid behavior: "Allow accessories to wake computer" ON
#    - Set up auto-login (System Settings → Users → Login Options)
#      so reboots come back without user interaction
```

When you come back:

```bash
make runner-health           # confirm runner has been polling
make backup                   # take a fresh backup
make dashboard                # check the Command Center for any ⛔ tiles
```

---

## Emergency stop (anytime, anywhere)

From the dashboard: **⚡ Command Center page → 🛑 Emergency stop**.

Two checkboxes available:
- "Close every open position on the broker" — flatten everything
- "Kill the deployment-runner process" — stop new ticks (LaunchAgent
  will auto-restart after 10s if installed)

Click **⛔ ACTIVATE NOW**. The flag persists until you click **✅
Deactivate** on the same panel.
