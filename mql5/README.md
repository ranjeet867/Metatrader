# MT5 file-bridge EA

The Python side of `mt5_quant_trader_v2` talks to MetaTrader 5 over a
**file-based JSON RPC**. Python writes a request file, the EA reads it,
runs the call, and writes the response file. No TCP, no DLL, no Python
package on the MT5 side.

This folder ships **two equivalent EAs**. Pick one.

| File | When to use |
|---|---|
| **`MT5BridgeFile.mq5`** (v1.1) | Recommended. The bridge most v1 codebases grew up with — already running for most existing users. |
| `V2Bridge.mq5` | Clean fresh install written for v2. Same wire format; slightly simpler folder layout. |

If you already have one of these attached to a chart in MT5, **don't
install the other**. They're mutually exclusive.

## Install / upgrade

➜ **See [`INSTALL.md`](./INSTALL.md)** for step-by-step instructions
covering both *fresh install* and *upgrading an existing install* paths,
plus a troubleshooting matrix and the Python-side smoke test.

## What it provides

| RPC method | Used by |
|---|---|
| `account_info` | KPI strip, FTMO progress, `account_detect.detect_account_via_bridge` |
| `symbol_info` | Position sizer for `tick_value` / `volume_step` |
| `copy_rates` | Data Manager refresh, every backtest's parquet update |
| `positions_get` | Position Manager panel, account statement |
| `position_close` | Close All / Close One in Position Manager |
| `history_deals_get` | Account statement realized P&L curve, manual-close detection |
| `order_send` | Live executor (Phase 7) |
| `ping` | Bridge health check |

## Security note

The EA reacts only to requests written by Python. It does not open new
positions on its own. The Phase 7 `order_send` method is intentionally
gated behind `core/live_executor.py` — there is **no UI button that
sends an order directly to the EA**, only the Python live runner does
that, and only after replay-parity and FTMO guard checks pass.

If you want a stricter setup, comment out the `order_send` dispatch
branch in `OnTimer()` and recompile — Python will get an
`unknown method` error, and live execution is effectively disabled at
the bridge layer.

## License

MIT — same as the rest of the repo.
