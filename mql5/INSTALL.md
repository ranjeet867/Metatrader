# MT5 bridge EA — install / upgrade

This folder ships **two equivalent file-bridge EAs**. Pick one. They
both let the Python side talk to MT5 over a JSON-on-disk RPC; they
just use slightly different filenames and folder layouts.

| File | When to use | File path | Methods |
|---|---|---|---|
| `MT5BridgeFile.mq5` | You're already running this — the bridge most of the v1 codebase grew up with. **Recommended for existing users.** | `MQL5/Files/mt5qt/req/<id>.json` (folder of per-request files) | All Phase 1 + 2.5 + 7 methods, version 1.1 |
| `V2Bridge.mq5` | You want a clean fresh install written for v2. Self-contained and slightly simpler | `MQL5/Files/v2_bridge_request.json` (single request file) | Same method set, identical wire format |

If `MT5BridgeFile.mq5` is already attached to a chart in MT5, you
**don't need to install V2Bridge.mq5**. Just keep what you have and
follow the **Upgrade an existing install** section below.

## Quick check — which one am I running?

In MT5 → bottom *Toolbox* panel → **Experts** tab. Look at the most
recent log line.

- If you see `MT5BridgeFile: ready. Watching MQL5/Files/mt5qt/req/`
  → you're running `MT5BridgeFile.mq5`. Use that one.
- If you see `V2Bridge EA started. Polling v2_bridge_request.json`
  → you're running `V2Bridge.mq5`. Use that one.
- If you don't see either, the EA isn't attached to any chart. Go
  to **Fresh install** below.

You should run **only one** EA at a time. If you accidentally have
both, the Python side will see overlapping responses and break.

---

## Upgrade an existing install (you're already running an EA)

This is the path for the current state of your repo: the patched
`MT5BridgeFile.mq5` (version 1.1) is already on disk in your Wine
MT5 Experts folder, and a backup copy is checked into the repo at
`mql5/MT5BridgeFile.mq5`. You just need to recompile and reload.

### 1. Open MetaEditor

If you've been editing the EA in this repo, MetaEditor is likely
already open. Find it via:

- **Cmd+Tab** through running apps until MetaEditor's icon appears.
- In MT5: top menu *Tools → MetaQuotes Language Editor*.
- In MT5 *Navigator*: right-click `MT5BridgeFile` → **Modify**.
- Three-finger swipe up (Mission Control) and click MetaEditor.

### 2. Confirm you have the patched version

The file path in MetaEditor's title bar should be:

```
MT5BridgeFile.mq5 - [.../MQL5/Experts/MT5BridgeFile.mq5]
```

Scroll to line 21. It should read:

```mql5
#property version   "1.1"
```

If it still says `1.0`, you have a stale copy open. Close the tab
and re-open from `MQL5/Experts/MT5BridgeFile.mq5`, or copy
`mql5/MT5BridgeFile.mq5` from this repo over the file in your
MT5 Experts folder.

### 3. Compile

Press **F7**, or click the green compile button in the toolbar.
Bottom *Output* panel should print:

```
'MT5BridgeFile.mq5'  MT5BridgeFile.ex5    0 errors, 0 warnings
```

If you get errors, copy the first one and paste it back to the
assistant.

### 4. Reload the EA in MT5

The compiled `.ex5` is what MT5 actually runs. It caches the old
one until you reload.

- Switch to **MetaTrader 5**.
- Find the chart with the EA running (top-right corner of the
  chart shows the EA's smiley face).
- Right-click that chart → **Expert Advisors → Remove**.
- Drag `MT5BridgeFile` from *Navigator → Expert Advisors* back onto
  the chart.
- *Common* tab → tick **Allow algorithmic trading** → **OK**.

(Shortcut: right-click the chart → **Expert Advisors → Properties
→ OK**. Sometimes that re-loads the EA without detaching. If
unsure, the detach + re-attach version always works.)

### 5. Verify

In MT5 → *Toolbox → Experts*. Most recent line should be:

```
MT5BridgeFile: ready. Watching MQL5/Files/mt5qt/req/
```

That's the EA confirming it's running v1.1 with the new methods.

### 6. Confirm the dashboard sees it

Open the running Streamlit dashboard → **Operations** page. The
yellow "unknown method: positions_get" warning should be gone, and
the **Position Manager** table populates with any open positions.
If you have nothing open right now, the table just says "no open
positions" — that's expected and means the connection works.

---

## Fresh install (no EA running yet)

### Option A — install `MT5BridgeFile.mq5` (recommended)

1. In MT5: *File → Open Data Folder*. A Finder window opens to
   your Wine MT5 data dir.
2. Navigate into `MQL5/Experts/`.
3. Copy `mql5/MT5BridgeFile.mq5` from this repo into that folder.
4. Open **MetaEditor** (MT5 *Tools → MetaQuotes Language Editor*).
5. *File → Open* → pick the file you just copied.
6. **F7** to compile. Should be `0 errors, 0 warnings`.
7. Switch back to MT5. Drag `MT5BridgeFile` from
   *Navigator → Expert Advisors* onto **any open chart** (it doesn't
   matter which symbol — the EA polls files, not chart data).
8. *Common* tab → tick **Allow algorithmic trading** → **OK**.
9. Verify in *Toolbox → Experts*: `MT5BridgeFile: ready.`.

### Option B — install `V2Bridge.mq5`

Same steps as Option A, but copy `mql5/V2Bridge.mq5` instead. The
expected log line is `V2Bridge EA started. Polling
v2_bridge_request.json every 100 ms`.

---

## Smoke-test from Python

After the EA is running, from the repo root:

```bash
make refresh-data
```

If parquets refresh without timing out, the bridge is good. For a
narrower test:

```bash
.venv/bin/python -c "from core.mt5_account import MT5AccountClient; \
                     print(MT5AccountClient().account_info())"
```

You should see your real account info (login, balance, equity, server).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `unknown method: positions_get` in dashboard | Old EA `.ex5` still loaded | Recompile (F7) and detach + reattach EA |
| `Compile fails: 'PositionGetTicket' - identifier not declared` | MT5 build < 2380 | Update MT5 to a recent build |
| Compile clean, dashboard still shows warning | EA didn't reload | Detach + reattach (step 4 above) |
| Position table populates but Close button silently fails | Algo trading disabled | EA *Properties → Common* → tick "Allow algorithmic trading" |
| `retcode=10018` on close | Market closed | Wait for the session to open |
| `retcode=10027` on close | Algo trading not allowed for the symbol | Broker-specific — check Market Watch → right-click symbol → Properties |
| Both `MT5BridgeFile` and `V2Bridge` are running | Overlapping responses | Detach one of them |

For deeper troubleshooting, see `docs/RUNBOOK.md` § 14.

## What changed in v1.1

`MT5BridgeFile.mq5` v1.0 → v1.1 added two RPC methods used by the
new Position Manager UI:

- `positions_get` — every open position with ticket, symbol, lots,
  entry, current price, SL/TP, profit, swap, commission (always 0
  on positions, MT5 attributes it to deals), magic, comment.
- `history_deals_get(since_utc, until_utc)` — closed deals in the
  window, used by the reconciliation loop to detect manual closes
  done in MT5.

Two new dispatch branches in `OnTimer()` (after `position_close`)
and two new handler functions (`json_positions`, `json_history_deals`)
before `tf_from_string`. No new includes, no DLL — uses the same
`kv_str / kv_int / kv_dbl / esc` helpers the file already had.

## License

MIT — same as the rest of the repo.
