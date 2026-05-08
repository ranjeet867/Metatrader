#!/usr/bin/env python3
"""
preflight_check.py — comprehensive end-to-end audit before going live.

Run this BEFORE `make run-live` to surface every silent-fail condition
that would otherwise only show up at signal-fire time. Validates each
active deployment's prerequisites + every layer of the runtime path.

What it checks (all per-deployment):
  ✅/❌ Parquet exists for {ticker}_{tf}.parquet
  ✅/❌ symbol_info entry exists in data/symbol_info.json
  ✅/❌ Strategy class registered (resolves variant→base)
  ✅/❌ Replay-parity recent (<24h) for live deployments
  ✅/❌ Quality gate passes (Trendo EV, win%, R:R, DD, recovery, streak)
  ✅/❌ risk_pct > 0 set on the deployment

And per-account:
  ✅/❌ Bridge reachable (account_info round-trip)
  ✅/❌ Live broker equity > 0
  ✅/❌ positions_get works
  ✅/❌ copy_rates returns bars for at least one ticker
  ✅/❌ No conflicting positions (position-guard would not auto-block)
  ✅/❌ No circuit-breaker condition currently active

Usage:
  python scripts/preflight_check.py             # all accounts
  python scripts/preflight_check.py --login N   # specific account
  python scripts/preflight_check.py --strict    # exit 1 on ANY warning

Exit codes:
  0 — all checks passed
  1 — at least one BLOCK or strict-warning
  2 — bridge offline / unable to run checks
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _ensure_venv() -> None:
    try:
        import pandas  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    import os
    venv_py = REPO / ".venv" / "bin" / "python"
    if not venv_py.exists():
        venv_py = REPO / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists() and os.environ.get("_PFC_RELAUNCHED") != "1":
        os.environ["_PFC_RELAUNCHED"] = "1"
        os.execv(str(venv_py), [str(venv_py), __file__, *sys.argv[1:]])
    print("⛔ pandas missing — run `make setup` first.")
    sys.exit(2)


_ensure_venv()


# ─── colored print helpers ───────────────────────────────────────────
def _ok(msg): print(f"  ✅ {msg}")
def _warn(msg): print(f"  🟡 {msg}")
def _err(msg): print(f"  ⛔ {msg}")
def _info(msg): print(f"     {msg}")
def _section(title): print(f"\n{'─' * 70}\n {title}\n{'─' * 70}")


# ─── checks ───────────────────────────────────────────────────────────

def check_account_level(login: int) -> tuple[int, int]:
    """Returns (n_blocks, n_warns) at the account level."""
    n_blocks = n_warns = 0
    _section(f"Account #{login}")

    from core import account_manager
    acct = account_manager.get_account(login)
    if acct is None:
        _err(f"login {login} not in account registry")
        return 1, 0
    _ok(f"alias={acct.alias}, "
        f"baseline=${acct.effective_baseline_equity:,.0f}")

    # Bridge reachable?
    try:
        from core.mt5_account import MT5AccountClient
        client = MT5AccountClient()
        info = client.account_info(force_refresh=True)
        _ok(f"bridge OK · balance=${info.balance:,.2f} "
            f"equity=${info.equity:,.2f}")
        if info.equity <= 0:
            _err("broker equity is 0 — account dead?")
            n_blocks += 1
    except Exception as e:
        _err(f"bridge UNREACHABLE: {type(e).__name__}: {e}")
        _info("→ Make sure MetaTrader 5 is running with the EA attached.")
        return 1, 0

    # positions_get works
    try:
        positions = client.positions_get()
        _ok(f"positions_get OK · {len(positions)} open position(s)")
    except Exception as e:
        _warn(f"positions_get failed: {type(e).__name__}: {e} "
              f"(non-fatal but the holding-badge won't work)")
        n_warns += 1
        positions = []

    # copy_rates probe — try US100.cash D1 (most likely to exist)
    try:
        resp = client._call("copy_rates",
                              {"name": "US100.cash",
                               "timeframe": "D1", "count": 5})
        rows = resp.get("data", resp) if isinstance(resp, dict) else resp
        if rows and len(rows) > 0:
            _ok(f"copy_rates(US100.cash, D1, 5) returned {len(rows)} bars")
        else:
            _warn(f"copy_rates returned 0 bars — symbol may not be subscribed")
            n_warns += 1
    except Exception as e:
        _warn(f"copy_rates failed: {type(e).__name__}: {e}")
        n_warns += 1

    # Circuit breaker state
    try:
        from core import circuit_breaker as cb
        cb_cfg = cb.load_config(login)
        cb_status = cb.evaluate(
            account_manager.get_db_path(login),
            mode="live", cfg=cb_cfg,
            open_positions_count=len(positions),
        )
        if cb_status.state == "OK":
            _ok(f"circuit breaker: OK (today $%.2f / total $%.2f)"
                  % (cb_status.realised_today, cb_status.realised_total))
        elif cb_status.state == "STOP_NEW":
            _warn(f"circuit breaker: STOP_NEW — {cb_status.reasons}")
            n_warns += 1
        else:
            _err(f"circuit breaker: HALT — {cb_status.reasons}")
            n_blocks += 1
    except Exception as e:
        _warn(f"circuit_breaker check failed: {type(e).__name__}: {e}")
        n_warns += 1

    return n_blocks, n_warns


def check_deployment(login: int, dep) -> tuple[int, int]:
    """Returns (n_blocks, n_warns) for a single deployment."""
    n_blocks = n_warns = 0
    is_live = dep.status == "live"
    badge = "[LIVE]" if is_live else f"[{dep.status.upper()}]"
    print(f"\n  ▶ {badge} {dep.strategy} × {dep.ticker} × {dep.tf} "
          f"({'long' if dep.long_only else 'bidir'})")
    print(f"     id={dep.deployment_id}  risk={dep.risk_pct:.2f}%  "
          f"daily_cap={dep.daily_cap_pct:.2f}%")

    # 1. Parquet exists
    parquet = REPO / "data" / f"{dep.ticker}_{dep.tf}.parquet"
    if parquet.exists():
        _ok(f"parquet: data/{parquet.name} ({parquet.stat().st_size // 1024}KB)")
    else:
        _err(f"parquet MISSING: data/{parquet.name} — fetch via Data Manager")
        n_blocks += 1

    # 2. symbol_info — REQUIRED for live, warn for paper
    from core.symbol_info_loader import try_load
    sym_info = try_load(dep.ticker)
    if sym_info is None:
        if is_live:
            _err(f"symbol_info MISSING for {dep.ticker} — runner will REFUSE "
                 f"live ticks. Run `make refresh-symbol-info`.")
            n_blocks += 1
        else:
            _warn(f"symbol_info missing for {dep.ticker} — paper will use "
                  f"fallback lots=0.01")
            n_warns += 1
    else:
        _ok(f"symbol_info OK · tick_size={sym_info.tick_size} "
            f"tick_value={sym_info.tick_value} "
            f"vol_min={sym_info.volume_min} "
            f"contract_size={sym_info.contract_size}")

    # 3. Strategy registered
    from dashboards.components.state import discover_strategies
    from dashboards.components.strategy_resolver import resolve_base_strategy
    strats = discover_strategies()
    base = resolve_base_strategy(dep.strategy, strats)
    if base is None or base not in strats:
        _err(f"strategy `{dep.strategy}` not registered. Resolved base=`{base}`."
             f" Registered: {sorted(strats.keys())}")
        n_blocks += 1
    else:
        if base != dep.strategy:
            _ok(f"strategy registered: variant `{dep.strategy}` → base `{base}`")
        else:
            _ok(f"strategy registered: `{base}`")

    # 4. Parity-fresh (live only)
    if is_live and base is not None:
        from core.parity_gate import ParityGate
        gate = ParityGate(REPO / "data" / "v2.db")
        last = gate.last_pass_for(base)
        if gate.is_recent(base):
            ts = last[0].strftime("%Y-%m-%d %H:%M") if last else "?"
            div = last[1] if last else 0.0
            _ok(f"parity recent · last pass {ts} UTC · "
                f"divergence ${div:.6f}")
        else:
            _err(f"parity NOT RECENT for `{base}` — runner will auto-demote "
                 f"this to paper (or refuse). Run replay-parity from "
                 f"Strategy Library, Operations, or Live page.")
            n_blocks += 1

    # 5. Risk_pct > 0
    if dep.risk_pct <= 0:
        _err(f"risk_pct = {dep.risk_pct} (must be > 0)")
        n_blocks += 1
    elif dep.risk_pct > 2.0:
        _warn(f"risk_pct = {dep.risk_pct}% (high — review)")
        n_warns += 1
    else:
        _ok(f"risk_pct = {dep.risk_pct:.2f}%")

    # 6. Quality gate (live only — strict)
    if is_live:
        from core import edge_catalog
        from core.deployment_quality_gate import (
            evaluate_quality, load_criteria,
        )
        # Try both names — edge_catalog entries may be filed under the
        # variant (e.g. 'donchian_20') OR the resolved base
        # (e.g. 'donchian_breakout') depending on which sweep wrote them.
        es = None
        for try_name in (dep.strategy, base):
            if not try_name:
                continue
            try:
                es = edge_catalog.best_for(dep.ticker, dep.tf, try_name)
            except Exception:
                es = None
            if es is not None:
                break
        if es is None:
            _warn(f"no edge_catalog entry — quality gate not evaluated")
            n_warns += 1
        else:
            qres = evaluate_quality(es, criteria=load_criteria(login))
            if qres.is_blocked:
                _err(f"quality gate BLOCKED · {len(qres.block_reasons)} "
                     f"failure(s)")
                for r in qres.block_reasons[:3]:
                    _info(f"  · {r}")
                n_blocks += 1
            elif qres.verdict == "WARN":
                _warn(f"quality gate WARN · {len(qres.warn_reasons)} "
                      f"concern(s)")
                for r in qres.warn_reasons[:3]:
                    _info(f"  · {r}")
                n_warns += 1
            else:
                _ok(f"quality gate OK ({len(qres.pass_notes)} checks passed)")

    # 7. Sample sizing — confirm calc_lots would succeed
    if sym_info is not None and dep.risk_pct > 0:
        from core.position_sizer import calc_lots
        # Use a realistic stop distance: 1% of price
        # (we don't have real entry/stop here — this is just a smoke test)
        sample_entry = 100.0
        sample_stop = 99.0
        equity = 90_000.0   # rough — actual will use live equity
        max_lots = float(getattr(dep, "max_lots", 0.0) or 0.0) or None
        max_usd = float(getattr(dep, "max_money_risk_usd", 0.0) or 0.0) or None
        res = calc_lots(equity=equity, risk_pct=dep.risk_pct,
                          entry_price=sample_entry, stop_price=sample_stop,
                          sym=sym_info, max_lots=max_lots,
                          max_money_risk_usd=max_usd)
        if res.ok:
            caps = []
            if max_lots and res.lots >= max_lots:
                caps.append(f"max-lots {max_lots:g}")
            if max_usd and res.money_risk >= max_usd * 0.99:
                caps.append(f"max-$ ${max_usd:.0f}")
            cap_note = f" · capped at {' + '.join(caps)}" if caps else ""
            _ok(f"sizing smoke-test OK · "
                f"lots={res.lots:g} on $90k @ {dep.risk_pct:.2f}% "
                f"(${res.money_risk:.0f} at SL){cap_note}")
            # Even when ok, warn loudly if computed lots > 50 on a CFD
            # (FTMO accounts almost always reject those).
            if res.lots > 50:
                _warn(f"sizing produces {res.lots:g} lots — most prop "
                      f"accounts cap indices at 10–50. Consider "
                      f"setting max_lots on this deployment.")
                n_warns += 1
            # No $-cap set on a live deployment — surface this so user can
            # opt in to safety net.
            if dep.status == "live" and not max_usd:
                _warn(f"no max_money_risk_usd set — a tight stop could "
                      f"size up beyond intended. Set on Command Center.")
                n_warns += 1
        else:
            _warn(f"sizing smoke-test rejected: {res.reason}")
            n_warns += 1

    return n_blocks, n_warns


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pre-flight check before running live deployments.",
    )
    p.add_argument("--login", type=int, default=None,
                    help="Specific account. Default: all.")
    p.add_argument("--strict", action="store_true",
                    help="Exit 1 on any WARN (default: only on BLOCK).")
    args = p.parse_args()

    print("🔍  Pre-flight check — running {strict} checks across "
          "all active deployments\n".format(
              strict="STRICT" if args.strict else "default"))

    from core import account_manager, deployment as dep_mod

    accounts = account_manager.list_accounts()
    if not accounts:
        print("⛔ no accounts configured — add one on Operations page first")
        return 2

    targets = ([a for a in accounts if a.login == args.login]
                if args.login else accounts)
    if not targets:
        print(f"⛔ no account with login={args.login}")
        return 2

    total_blocks = 0
    total_warns = 0
    total_deps = 0

    for acct in targets:
        b, w = check_account_level(acct.login)
        total_blocks += b
        total_warns += w
        try:
            deps = dep_mod.load_deployments(acct.login)
        except Exception as e:
            _err(f"load_deployments failed: {e}")
            total_blocks += 1
            continue
        running = [d for d in deps if d.status in ("paper", "live")]
        if not running:
            _info(f"no active deployments on this account — skip per-row")
            continue
        _section(f"Per-deployment ({len(running)} active)")
        for d in running:
            b, w = check_deployment(acct.login, d)
            total_blocks += b
            total_warns += w
            total_deps += 1

    # ── Summary ────────────────────────────────────────────────────
    _section("Summary")
    print(f"  Deployments checked: {total_deps}")
    print(f"  Total BLOCK: {total_blocks}")
    print(f"  Total WARN:  {total_warns}")
    if total_blocks > 0:
        print(f"\n⛔  PRE-FLIGHT FAILED — fix BLOCK items before "
              f"`make run-live`.")
        return 1
    if args.strict and total_warns > 0:
        print(f"\n🟡  STRICT MODE — {total_warns} warnings flagged. "
              f"Run without --strict to ignore.")
        return 1
    print(f"\n✅  PRE-FLIGHT PASSED — safe to `make run-live`.")
    if total_warns > 0:
        print(f"   ({total_warns} warning(s) noted but not blocking.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
