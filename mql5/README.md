# MT5 file-bridge EA — `V2Bridge.mq5`

The Python side of `mt5_quant_trader_v2` talks to MetaTrader 5 over a
**file-based JSON RPC**. Python writes a request file, the EA reads it,
runs the call, and writes the response file. No TCP, no DLL, no Python
package on the MT5 side.

This file (`V2Bridge.mq5`) is the EA. Drop it on a chart and the bridge
is running.

## What it provides

| RPC method | Used by |
|---|---|
| `account_info` | KPI strip, FTMO progress, `account_detect.detect_account_via_bridge` |
| `symbol_info` | Position sizer for tick_value / volume_step |
| `copy_rates` | Data Manager refresh, every backtest's parquet update |
| `positions_get` | Position Manager panel, account statement |
| `position_close` | Close All / Close One in Position Manager |
| `history_deals_get` | Account statement realized P&L curve |

## Install

1. Open **MetaEditor** in MT5 (F4 from the terminal).
2. Copy `mql5/V2Bridge.mq5` into your MQL5 `Experts/` directory.
3. Compile (F7). It should report 0 errors / 0 warnings.
4. In MT5 terminal, drag `V2Bridge` from the Navigator → Expert Advisors
   onto **any chart** (it doesn't matter which — the EA polls files,
   not chart data).
5. In the EA dialog:
   - **Common** tab: tick **Allow algorithmic trading**.
   - **Inputs** tab: leave defaults unless you know what you're doing.
6. Click OK. You should see `V2Bridge EA started` in the Experts log.

## Configure file paths

By default the EA reads `MQL5/Files/Common/v2_bridge_request.json` and
writes `MQL5/Files/Common/v2_bridge_response.json`. The Python side
expects the same. If you need to change, edit:

- EA `RequestFile` and `ResponseFile` inputs
- Python `core/data.py::fetch_from_bridge` — file paths

## Smoke-test the bridge

From the repo root, after the EA is running:

```bash
python -c "from core.mt5_account import MT5AccountClient; \
           print(MT5AccountClient().account_info())"
```

Should print your real account info (login, server, balance, …).
If it times out, check that:
- Auto-trading is enabled (smiley button on the toolbar is green)
- The EA's Experts log shows `V2Bridge EA started` and is polling
- The path you're checking is correct (Files vs Common Files)

## Security note

This EA only reacts to Python requests written to the file. It does
NOT open new positions on its own — `position_close` is the only
trade method exposed. There is **no order-placement RPC** in the
bridge by design; live order placement is handled by
`core/live_executor.py` on the Python side, which talks to a separate
order-routing path (or to a higher-trust EA you build on top of this
one).

## License

MIT — same as the rest of the repo.
