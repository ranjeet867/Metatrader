# Recompile the bridge EA — 30 seconds

Your `MT5BridgeFile.mq5` source on disk is **already patched** with the
Phase 2.5 handlers (`positions_get`, `history_deals_get`). Two things
left for you:

1. Recompile in MetaEditor.
2. Detach + re-attach the EA in MT5 so it picks up the new `.ex5`.

That's it. After this the dashboard's **Position Manager** panel goes
from read-only to fully wired — Close One, Close All, manual-close
detection, all working.

## Source path

The patched file is here:

```
~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Experts/MT5BridgeFile.mq5
```

Header now reads `version "1.1"` and the docblock mentions Phase 2.5.

## Step-by-step

### 1. Switch to MetaEditor

If you already had MetaEditor open (you did — that's how we made the
edits in the first place), Wine sometimes hides it instead of focusing.

- **Cmd+Tab** through the running apps until you see MetaEditor's icon.
- Or click `Tools → MetaQuotes Language Editor` in MT5's top menu.
- Or right-click `MT5BridgeFile` in the Navigator → *Modify*.
- Or `Mission Control` (three-finger swipe up) and click the
  MetaEditor window.

### 2. Make sure the right file is the active tab

Top of the editor window should say `MT5BridgeFile.mq5`. Scroll to
line 21 and confirm `#property version "1.1"`. If it says `1.0`,
you're looking at a stale copy — close it and re-open from
`MQL5/Experts/MT5BridgeFile.mq5`.

### 3. Compile

Press **F7**. Or click the green checkmark / **Compile** button in
the toolbar.

The Output panel at the bottom should print:

```
'MT5BridgeFile.mq5'  MT5BridgeFile.ex5    0 errors, 0 warnings
```

If you get errors, copy the first one and paste it back to me.

### 4. Reload the EA in MT5

The compiled `.ex5` is what MT5 actually runs, and it caches the old
one until you tell it otherwise.

Easiest way:

- In MT5, find the chart that has the EA running.
- Right-click the chart → **Expert Advisors → Remove**.
- Drag `MT5BridgeFile` from `Navigator → Expert Advisors` back onto
  the chart.
- Tick **Allow algorithmic trading** on the *Common* tab → **OK**.

Or if you prefer not to detach: right-click the chart → **Expert
Advisors → Properties** → **OK**. That re-loads the EA.

### 5. Check the Experts log

Bottom of MT5 → **Toolbox → Experts** tab. You should see:

```
MT5BridgeFile: ready. Watching MQL5/Files/mt5qt/req/
```

The first line after restart. No errors below it.

### 6. Refresh the dashboard

In the running Streamlit dashboard (Operations page → Position
Manager section), hit **R** or click the page refresh. The yellow
upgrade warning disappears and the open-positions table populates.

## What changed in the EA

Two new dispatch branches were added after the existing
`position_close` handler:

```mql5
} else if (method == "positions_get") {
   reply = ok(id, json_positions());
} else if (method == "history_deals_get") {
   reply = ok(id, json_history_deals(
      jstr(body, "params.since_utc"),
      jstr(body, "params.until_utc")));
```

And two handler functions were added before `tf_from_string`:

- `json_positions()` — returns every open position with ticket,
  symbol, side, lots, entry/current price, SL/TP, profit, swap,
  commission, magic, comment.
- `json_history_deals(since, until)` — returns closed deals in the
  window, used by the reconciliation loop to detect manual closes.

Both use the existing `kv_str / kv_int / kv_dbl / esc` helpers your
file already had, so there's no new include or DLL dependency.

## Troubleshooting

**Compile fails with `'PositionGetTicket' - identifier not declared`**
— your MT5 build is older than 2380 (October 2020). Update MT5.

**Compile succeeds but the warning still shows in the dashboard**
— the EA didn't reload. Detach and re-attach (step 4); a chart
refresh or *Properties → OK* sometimes isn't enough.

**Position table populates but Close button silently fails** — check
the *Experts* tab for a `retcode` line. Common ones: `10018`
(market closed), `10006` (account disabled), `10027` (algo trading
not allowed → tick the box on EA properties).
