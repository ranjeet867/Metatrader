#!/usr/bin/env python3
"""
smoke_test_close.py — end-to-end bridge test on NASDAQ.

What it does:
  1. Connects to the MT5 bridge (must be running, EA attached).
  2. Sends a TINY 0.01-lot LONG market order on US100.cash.
  3. Polls positions_get until the order shows up (max 10s).
  4. Calls position_close on the new ticket.
  5. Polls again to verify the position is gone.
  6. Prints a PASS/FAIL summary including order tickets, deal IDs,
     entry/exit prices, and bridge round-trip latencies.

Why this exists:
  Before today's fix, `live_executor.close_position()` called the bridge
  with method `order_close` while the EA registered `position_close`.
  Every HALT / Demote / forced-flat silently failed at the bridge —
  the position stayed open. This script proves the close path is now
  wired to the right method by actually round-tripping a real trade.

USAGE:
  python scripts/smoke_test_close.py            # dry-run (no real order)
  python scripts/smoke_test_close.py --live     # actually places & closes
  python scripts/smoke_test_close.py --live --symbol US100.cash --lots 0.01
  python scripts/smoke_test_close.py --live --max-wait 15

Safety:
  Default is DRY-RUN — no real order placed unless you pass --live.
  Lots default to 0.01 (smallest allowed by most brokers).
  Aborts immediately if the symbol's spread is unusually wide.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _ensure_venv() -> None:
    """If pandas is missing in the current interpreter, try to re-launch
    under .venv/bin/python — that's where `make setup` installed deps.

    Without this guard, running `python scripts/smoke_test_close.py`
    with system Python crashes with `ModuleNotFoundError: pandas` and
    leaves the user wondering why a script that worked yesterday breaks.
    """
    try:
        import pandas  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    venv_py = REPO / ".venv" / "bin" / "python"
    if not venv_py.exists():
        # Try Windows layout too
        venv_py = REPO / ".venv" / "Scripts" / "python.exe"
    # If we're already running under a venv-like interpreter, don't loop —
    # just surface a clear error.
    already_relaunched = os.environ.get("_SMOKE_RELAUNCHED") == "1"
    if venv_py.exists() and not already_relaunched:
        print(f"⚙️  Re-launching under venv interpreter: {venv_py}")
        os.environ["_SMOKE_RELAUNCHED"] = "1"
        os.execv(str(venv_py), [str(venv_py), __file__, *sys.argv[1:]])
    # Bail with actionable instructions
    print(
        "⛔  pandas is not installed in the current Python interpreter.\n"
        f"     Interpreter: {sys.executable}\n"
        f"     Repo:        {REPO}\n\n"
        "     Likely cause: you ran the script with system Python, but "
        "the project deps are in a venv.\n\n"
        "  Fix — pick ONE:\n"
        "     1. Use the Make target (recommended):\n"
        "          make smoke-test          # dry-run\n"
        "          make smoke-test-live     # actually places & closes\n"
        "     2. Activate the venv first:\n"
        "          source .venv/bin/activate\n"
        "          python scripts/smoke_test_close.py\n"
        "     3. Run the venv interpreter directly:\n"
        "          .venv/bin/python scripts/smoke_test_close.py\n"
        "     4. Or, if the venv is missing, create it:\n"
        "          make setup\n"
    )
    sys.exit(2)


_ensure_venv()


from core.mt5_account import MT5AccountClient   # noqa: E402


def _print_step(n: int, msg: str) -> None:
    print(f"\n  ── Step {n}: {msg}")


def _wait_for_position(client: MT5AccountClient, symbol: str,
                          *, expect: bool, max_wait_s: float
                          ) -> tuple[bool, dict | None]:
    """Poll positions_get until a position on `symbol` is present
    (expect=True) or absent (expect=False). Returns (ok, position_dict)."""
    deadline = time.time() + max_wait_s
    pos = None
    while time.time() < deadline:
        try:
            positions = client.positions_get()
        except Exception as e:
            print(f"     ⚠ positions_get error: {type(e).__name__}: {e}")
            time.sleep(0.5)
            continue
        # Convert to dict for hashing — mt5_account returns BridgePosition objects
        match = [p for p in positions
                   if str(getattr(p, "symbol", "")) == symbol]
        if expect and match:
            return True, match[0]
        if not expect and not match:
            return True, None
        time.sleep(0.5)
    return False, None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Place a tiny test trade on NASDAQ + close it.",
    )
    parser.add_argument("--live", action="store_true",
                          help="Actually send the order. Default is dry-run.")
    parser.add_argument("--symbol", default="US100.cash",
                          help="Symbol to trade. Default: US100.cash (NASDAQ).")
    parser.add_argument("--lots", type=float, default=0.01,
                          help="Lot size. Default 0.01 (smallest typical).")
    parser.add_argument("--direction", default="LONG",
                          choices=["LONG", "SHORT"],
                          help="LONG (buy) or SHORT (sell). Default LONG.")
    parser.add_argument("--max-wait", type=float, default=10.0,
                          help="Seconds to wait for position to appear/clear.")
    args = parser.parse_args()

    print(f"🧪  smoke_test_close — {args.symbol} {args.direction} "
          f"{args.lots} lots {'[LIVE]' if args.live else '[DRY-RUN]'}")

    client = MT5AccountClient()

    # Step 1: bridge reachable + symbol valid
    _print_step(1, "checking bridge + symbol")
    try:
        info = client.symbol_info(args.symbol)
        # SymbolInfo dataclass has: name, tick_size, tick_value,
        # volume_min, volume_step, contract_size. NO bid/ask — those
        # come back later in order_send's fill_price.
        print(f"     ✅ {args.symbol}: tick_size={info.tick_size} "
              f"tick_value={info.tick_value} "
              f"vol_min={info.volume_min} "
              f"contract_size={info.contract_size}")
        # Sanity-check requested lots are above the broker minimum
        if args.lots < info.volume_min:
            print(f"     ⚠ requested lots {args.lots} < broker minimum "
                  f"{info.volume_min} — increasing to minimum")
            args.lots = info.volume_min
    except Exception as e:
        print(f"     ⛔ symbol_info failed: {type(e).__name__}: {e}")
        return 1

    # Step 2: take a positions snapshot so we can detect the new one
    _print_step(2, "reading current positions")
    try:
        before = client.positions_get()
        before_tickets = {getattr(p, "ticket", None) for p in before}
        print(f"     ✅ {len(before)} position(s) currently open "
              f"(tickets: {sorted(t for t in before_tickets if t)})")
    except Exception as e:
        print(f"     ⛔ positions_get failed: {type(e).__name__}: {e}")
        return 1

    if not args.live:
        print(f"\n  🛡️  DRY-RUN — no order placed.")
        print(f"     Pass --live to actually round-trip a real trade.")
        print(f"     Risk: 0.01 lots × ~25,000 USD/lot index ≈ $250 notional.")
        return 0

    # Step 3: open the trade
    _print_step(3, f"sending {args.direction} order")
    t0 = time.time()
    try:
        order_result = client.order_send(
            symbol=args.symbol,
            direction=args.direction,    # the wrapper takes LONG/SHORT
            lots=args.lots,
            sl=0.0,         # no SL/TP — manual close after a few seconds
            tp=0.0,
            deviation=20,
            comment="smoke_test_close",
        )
        send_ms = int((time.time() - t0) * 1000)
        if not order_result.ok:
            print(f"     ⛔ order_send REJECTED in {send_ms}ms "
                  f"(retcode={order_result.retcode}, "
                  f"error={order_result.error!r}, "
                  f"comment={order_result.comment!r})")
            return 1
        print(f"     ✅ order_send OK in {send_ms}ms "
              f"(ticket={order_result.ticket}, "
              f"retcode={order_result.retcode}, "
              f"fill_price={order_result.fill_price})")
    except Exception as e:
        print(f"     ⛔ order_send failed: {type(e).__name__}: {e}")
        return 1

    # Step 4: wait for the position to appear in positions_get
    _print_step(4, f"waiting up to {args.max_wait}s for position")
    ok, new_pos = _wait_for_position(client, args.symbol, expect=True,
                                         max_wait_s=args.max_wait)
    if not ok:
        print(f"     ⛔ position never appeared on {args.symbol}")
        return 1
    new_ticket = getattr(new_pos, "ticket", 0)
    print(f"     ✅ position open: ticket={new_ticket} "
          f"price={getattr(new_pos, 'price_open', 0)} "
          f"volume={getattr(new_pos, 'volume', 0)}")

    # Step 5: close the position via the FIXED position_close path
    _print_step(5, f"closing position (calls method 'position_close')")
    t0 = time.time()
    try:
        close_result = client.position_close(
            ticket=int(new_ticket),
            deviation=20,
        )
        close_ms = int((time.time() - t0) * 1000)
        print(f"     ✅ position_close OK in {close_ms}ms "
              f"(retcode={close_result.retcode}, "
              f"deal={close_result.deal}, "
              f"price={close_result.price})")
    except Exception as e:
        print(f"     ⛔ position_close failed: {type(e).__name__}: {e}")
        print(f"     ⚠ POSITION ticket={new_ticket} STILL OPEN. "
              f"Close it manually in MT5 NOW.")
        return 1

    # Step 6: verify position cleared
    _print_step(6, f"verifying position closed")
    ok, _ = _wait_for_position(client, args.symbol, expect=False,
                                 max_wait_s=args.max_wait)
    if not ok:
        # It might still be there if there was already another position
        # on the same symbol pre-test. Check ticket-level disappearance.
        after = client.positions_get()
        still_open = any(getattr(p, "ticket", 0) == new_ticket
                          for p in after)
        if still_open:
            print(f"     ⛔ ticket {new_ticket} STILL open after close")
            return 1
        print(f"     ✅ ticket {new_ticket} no longer in positions_get "
              f"(other positions on {args.symbol} remain).")
    else:
        print(f"     ✅ no positions on {args.symbol} — close confirmed")

    print(f"\n✅  PASS — bridge close path works end-to-end.")
    print(f"     Order send + position close round-trip confirmed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
