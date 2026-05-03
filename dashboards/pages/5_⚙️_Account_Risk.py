"""
5_⚙️_Account_Risk.py — live MT5 account info, FTMO progress, risk caps editor,
time-guard countdowns, EMERGENCY STOP, FTMO pass-rate gauge (placeholder).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import storage   # noqa: E402
from core.config import (   # noqa: E402
    load_config,
    save_config,
)
from core.risk_engine import LiveRiskTracker   # noqa: E402
from core.time_guards import time_guard_cfg_from_risk_config   # noqa: E402
from dashboards.components.mt5_status import (   # noqa: E402
    render_emergency_stop_indicator,
)
from dashboards.components.time_guard_status import render_countdowns   # noqa: E402


def render_account_section():
    st.markdown("### 🏦  MT5 Account")
    # We don't auto-instantiate a real client (no bridge in test env). Show
    # the user how to wire it.
    st.caption(
        "MT5 account info is fetched by `core.mt5_account.MT5AccountClient`; "
        "wire this in your live deployment script. The client is fully tested "
        "(see `tests/test_mt5_account.py`)."
    )


def render_ftmo_progress(cfg):
    st.markdown("### 📉  FTMO Progress")
    db_path = REPO / "data" / "v2.db"
    tracker = LiveRiskTracker(db_path=db_path,
                                daily_loss_cap_pct=cfg.daily_loss_cap_pct,
                                max_consecutive_losses=cfg.max_consecutive_losses)
    daily_loss = tracker.account_daily_loss_pct()
    cap = cfg.daily_loss_cap_pct
    pct_used = min(daily_loss / cap * 100, 100.0) if cap > 0 else 0
    st.markdown(f"**Daily loss vs cap** — {daily_loss:.2f}% / {cap:.2f}% "
                 f"({pct_used:.0f}% of cap consumed)")
    st.progress(pct_used / 100, text=f"{daily_loss:.2f}% used")
    if daily_loss >= cap:
        st.error("⛔  Daily loss cap reached — live trading BLOCKED.")
    elif daily_loss >= cap * 0.8:
        st.warning("⚠️  >80% of daily cap consumed.")


def render_per_strategy_risk():
    st.markdown("### 🧨  Per-strategy risk state")
    db_path = REPO / "data" / "v2.db"
    try:
        with storage.connect(db_path) as c:
            rows = c.execute(
                "SELECT symbol, strategy, consecutive_losses, "
                "last_loss_at_utc, cooldown_until_utc, daily_loss_pct, "
                "day_start_balance FROM risk_state"
            ).fetchall()
    except Exception:
        rows = []
    if not rows:
        st.caption("(no per-strategy risk state recorded yet)")
        return
    df = pd.DataFrame(rows, columns=[
        "symbol", "strategy", "consecutive_losses",
        "last_loss_at_utc", "cooldown_until_utc",
        "daily_loss_pct", "day_start_balance",
    ])
    st.dataframe(df, use_container_width=True,
                  height=min(360, 36 * (len(df) + 1)))


def render_time_guards(cfg):
    st.markdown("### 🕒  Time guards (INVARIANT-8)")
    tg = time_guard_cfg_from_risk_config(cfg)
    render_countdowns(tg, container=st)
    st.caption(
        "Time-guard rules are NON-OVERRIDABLE in live mode by default. "
        "To disable, edit `data/risk_config.json` AND acknowledge that "
        "this voids FTMO compliance."
    )


def render_emergency_stop_section():
    st.markdown("### ⛔  Emergency Stop")
    sentinel = REPO / "data" / "EMERGENCY_STOP"
    has_stop = render_emergency_stop_indicator(REPO / "data")
    cols = st.columns(2)
    if not has_stop:
        if cols[0].button("⛔  Activate EMERGENCY_STOP",
                              type="primary",
                              use_container_width=True,
                              key="ar_estop_on"):
            confirm_key = "ar_estop_confirm"
            st.session_state[confirm_key] = True
        if st.session_state.get("ar_estop_confirm"):
            st.warning("Confirm: this REFUSES all live orders until removed.")
            cc = st.columns(2)
            if cc[0].button("✅  Yes, activate", key="ar_estop_yes"):
                sentinel.touch()
                st.session_state["ar_estop_confirm"] = False
                st.rerun()
            if cc[1].button("Cancel", key="ar_estop_no"):
                st.session_state["ar_estop_confirm"] = False
                st.rerun()
    else:
        if cols[0].button("✅  Lift EMERGENCY_STOP",
                              type="secondary",
                              use_container_width=True,
                              key="ar_estop_off"):
            sentinel.unlink(missing_ok=True)
            st.toast("EMERGENCY_STOP removed")
            st.rerun()


def render_position_sizing_section():
    """Phase 30: risk-per-trade slider + projected-lots preview per ticker."""
    from core.position_sizer import calc_lots
    from core.symbol_info_loader import load_all as load_all_symbols

    st.markdown("### 📐  Position sizing")
    st.caption(
        "All paper / live opens compute lots dynamically as "
        "`equity × risk_pct% / (stop_distance × $-per-tick)`. "
        "The same formula runs in backtest when `risk_pct` is supplied. "
        "Wire-up location: `core.position_sizer.calc_lots`."
    )

    cols = st.columns(3)
    risk_pct = float(cols[0].select_slider(
        "risk per trade (%)",
        options=[0.10, 0.20, 0.30, 0.50, 0.70, 1.00],
        value=0.30, key="ar_risk_slider",
    ))
    equity = float(cols[1].number_input("account equity ($)",
                                            value=91_400.0, step=1000.0,
                                            key="ar_equity"))
    typical_stop_R = float(cols[2].number_input(
        "stop distance (in 'R' i.e. fraction of price)",
        value=0.0050, format="%.4f", step=0.0010, key="ar_stop_R",
        help="Used only for the projection table — actual trades use the "
              "live signal's stop. 0.005 = 0.5%% from entry.",
    ))

    # Build projected-lots table
    try:
        all_syms = load_all_symbols()
    except Exception as e:
        st.warning(f"Could not load symbol_info.json: {e}")
        return

    # A reasonable typical entry per ticker — used only to compute a stop_distance
    # for the preview. Users see the FORMULA, not a real trade.
    TYPICAL_ENTRY = {
        "US100.cash": 21000.0, "US500.cash": 5500.0,
        "GER40.cash": 18000.0, "EU50.cash": 4900.0,
        "EURUSD": 1.10, "GBPUSD": 1.27, "USDJPY": 150.0,
        "GBPJPY": 191.0, "AUDUSD": 0.66, "NZDUSD": 0.60,
        "XAUUSD": 2000.0, "XAGUSD": 25.0,
    }
    rows = []
    for sym, info in sorted(all_syms.items()):
        entry = TYPICAL_ENTRY.get(sym, 100.0)
        stop_dist = entry * typical_stop_R
        stop = entry - stop_dist
        res = calc_lots(equity=equity, risk_pct=risk_pct,
                          entry_price=entry, stop_price=stop, sym=info)
        rows.append({
            "symbol": sym,
            "typical_entry": entry,
            "stop_distance": round(stop_dist, info.digits),
            "lots": round(res.lots, 4) if res.ok else "—",
            "$ at risk": round(res.money_risk, 2) if res.ok else "—",
            "result": "ok" if res.ok else res.reason,
        })
    st.markdown(f"**Projected lots at {risk_pct:.2f}% on ${equity:,.0f} equity**")
    st.dataframe(pd.DataFrame(rows), use_container_width=True,
                  height=min(420, 36 * (len(rows) + 1)))


def render_risk_caps_editor(cfg):
    st.markdown("### ⚙️  Risk caps (data/risk_config.json)")
    st.caption(
        "Edits save to `data/risk_config.json`. The dashboard re-reads on "
        "every page load."
    )
    cur = cfg.raw
    with st.form("risk_caps_form"):
        cols = st.columns(3)
        new_daily = cols[0].number_input("daily_loss_cap_pct",
                                            value=float(cur["daily_loss_cap_pct"]),
                                            step=0.5, format="%.2f")
        new_consec = int(cols[1].number_input("max_consecutive_losses",
                                                  value=int(cur["max_consecutive_losses"]),
                                                  step=1))
        new_max_open = int(cols[2].number_input("max_open_positions",
                                                    value=int(cur["max_open_positions"]),
                                                    step=1))
        cols2 = st.columns(2)
        new_no_entry = int(cols2[0].number_input("no_entry_minutes_before_close",
                                                    value=int(cur["no_entry_minutes_before_close"]),
                                                    step=5))
        new_buffer = int(cols2[1].number_input("flat_buffer_minutes",
                                                  value=int(cur["flat_buffer_minutes"]),
                                                  step=1))
        if st.form_submit_button("💾  Save"):
            new_cfg = dict(cur)
            new_cfg["daily_loss_cap_pct"] = new_daily
            new_cfg["max_consecutive_losses"] = new_consec
            new_cfg["max_open_positions"] = new_max_open
            new_cfg["no_entry_minutes_before_close"] = new_no_entry
            new_cfg["flat_buffer_minutes"] = new_buffer
            try:
                save_config(new_cfg)
                st.success("Saved. Re-run other pages to pick up the change.")
            except Exception as e:
                st.error(f"Save failed: {e}")


def render_ftmo_pass_rate_widget():
    """Monte-Carlo bootstrap pass-rate driven by the Strategy Library.

    Pick which strategies to include from the recommended portfolio,
    set iterations + days + pass target, click Run. Each pick's
    OOS R-distribution is bootstrapped on the fly from real broker data.
    """
    import numpy as np

    from core import strategy_library
    from core.backtest import run_backtest
    from core.data import load_parquet
    from core.ftmo_simulator import StrategyDist, simulate_pass_rate
    from dashboards.components.state import discover_strategies

    st.markdown("### 🎲  FTMO Pass-Rate Simulator")
    st.caption(
        "Pick which strategies to include in your simulated FTMO portfolio. "
        "Defaults to the ⭐ recommended ones from the Strategy Library.")

    lib = strategy_library.list_library()
    if not lib:
        st.warning(
            "Strategy Library is empty — run `make sweep-grid` to "
            "populate it.")
        return

    label_for: dict[str, strategy_library.LibraryEntry] = {
        f"{e.strategy} · {e.ticker} · {e.tf}"
        + (" ⭐" if e.recommended else "")
        + (" · ✅" if e.has_edge else ""):
        e for e in lib
    }
    default_picks = [k for k, e in label_for.items() if e.recommended]

    picks = st.multiselect(
        "Strategies to include",
        options=list(label_for.keys()),
        default=default_picks,
        key="ftmo_lib_picks",
    )

    cols = st.columns([1, 1, 1, 1, 1])
    n_iter = int(cols[0].number_input("iterations", value=5_000, step=1000,
                                          min_value=500, max_value=50_000,
                                          key="ftmo_n"))
    days = int(cols[1].number_input("days", value=30, step=5,
                                       min_value=5, max_value=90, key="ftmo_d"))
    target = float(cols[2].number_input("pass target %", value=10.0,
                                           step=1.0, min_value=1.0, max_value=20.0,
                                           key="ftmo_t"))
    risk = float(cols[3].number_input("risk %/trade", value=0.5, step=0.05,
                                        min_value=0.05, max_value=2.0,
                                        format="%.2f", key="ftmo_risk"))
    seed = int(cols[4].number_input("seed", value=42, step=1,
                                       key="ftmo_seed"))

    if not st.button("🎲  Run simulation", type="primary",
                       key="ftmo_run"):
        st.info("Pick strategies above and click Run.")
        return

    if not picks:
        st.error("Pick at least one strategy.")
        return

    # Build StrategyDist for each pick by re-running the backtest on the
    # cached parquet and slicing the OOS portion (60/40 split).
    strats_by_name = discover_strategies()
    pool: list[StrategyDist] = []
    debug_rows = []
    small_sample_warns: list[str] = []
    bars_per_day = {"M15": 96, "H1": 24, "H4": 6, "D1": 1}
    with st.spinner(f"Bootstrapping {len(picks)} strategy pools…"):
        for label in picks:
            entry = label_for[label]
            # Strip variant suffix (e.g. ema_cross_9_20 → ema_cross)
            base = entry.strategy
            for needle in ("_9_20", "_12_26", "_20_50", "_30_70",
                            "_20", "_55", "_20_2", "_10"):
                if base.endswith(needle):
                    base = base[: -len(needle)]
                    break
            if base not in strats_by_name:
                debug_rows.append((label, "strategy class not found"))
                continue
            StratCls, ParamsCls = strats_by_name[base]
            ppath = REPO / "data" / f"{entry.ticker}_{entry.tf}.parquet"
            if not ppath.exists():
                debug_rows.append((label, "parquet missing"))
                continue
            try:
                df = load_parquet(ppath)
                if ParamsCls is not None:
                    strat = StratCls(ParamsCls(long_only=entry.long_only))
                else:
                    strat = StratCls()
                r = run_backtest(
                    df, strat.signals(df),
                    starting_balance=100_000,
                    lots=1.0, money_per_unit_price=1.0,
                    commission_per_trade=3.0,
                    slippage_per_fill_atr_frac=0.1,
                    symbol=entry.ticker,
                )
            except Exception as e:
                debug_rows.append((label, f"backtest failed: {e}"))
                continue
            split = int(len(df) * 0.6)
            rs = np.array([t.r_multiple for t in r.trades
                            if t.entry_bar_idx >= split], dtype=float)
            if rs.size == 0:
                debug_rows.append((label, "no OOS trades"))
                continue
            # FIX: trades_per_DAY (not per BAR). Earlier version divided
            # by oos_bars which makes M15 trades_per_day ~96× too small,
            # giving sims 0 trades on most days; D1 strategies were OK
            # but the formula was still semantically wrong.
            bpd = bars_per_day.get(entry.tf, 1)
            n_oos_days = max(1.0, (len(df) - split) / bpd)
            tp_day = max(0.05, len(rs) / n_oos_days)

            if rs.size < 15:
                small_sample_warns.append(
                    f"`{label}` has only {rs.size} OOS trades — "
                    "bootstrap tails are unreliable. "
                    "Consider deselecting or lowering risk %.")

            pool.append(StrategyDist(
                name=label,
                symbol=entry.ticker,
                r_multiples=rs,
                trades_per_day=tp_day,
                risk_per_trade_pct=risk,
            ))

    if small_sample_warns:
        with st.expander(
                f"⚠ {len(small_sample_warns)} small-sample warning(s)",
                expanded=True):
            for w in small_sample_warns:
                st.markdown(f"- {w}")
            st.caption(
                "Bootstrap simulations from <15 OOS trades are dominated "
                "by individual extreme draws. The shown P(pass) and "
                "E[final %] reflect that uncertainty — they are not "
                "predictions, they are stress tests.")

    if debug_rows:
        with st.expander(
            f"⚠ {len(debug_rows)} pick(s) excluded — see why"):
            for label, why in debug_rows:
                st.markdown(f"- `{label}` — {why}")

    if not pool:
        st.error("No usable strategies — see exclusions above.")
        return

    with st.spinner(f"Running {n_iter:,} iterations on {len(pool)} "
                     f"strategies…"):
        res = simulate_pass_rate(
            pool, starting_balance=100_000,
            days=days, daily_loss_cap_pct=5.0, total_loss_cap_pct=10.0,
            pass_target_pct=target, n_iterations=n_iter, seed=seed,
        )

    cols = st.columns(4)
    cols[0].metric("P(pass)", f"{res.p_pass*100:.1f}%")
    cols[1].metric("P(daily breach)", f"{res.p_daily_breach*100:.1f}%")
    cols[2].metric("P(total breach)", f"{res.p_total_breach*100:.1f}%")
    cols[3].metric("E[final %]", f"{res.expected_final_pct:+.2f}%")

    st.markdown("**Per-strategy contribution** (mean across all iterations)")
    contrib_rows = []
    for name, ret in res.contrib_R_per_strategy.items():
        contrib_rows.append({
            "strategy": name,
            "contrib_return_%": round(ret, 2),
            "contrib_dd_%": round(res.contrib_dd_per_strategy.get(name, 0), 2),
        })
    contrib_df = pd.DataFrame(contrib_rows).sort_values(
        "contrib_return_%", ascending=False)
    st.dataframe(contrib_df, use_container_width=True)


def main():
    st.set_page_config(page_title="Account & Risk", page_icon="⚙️", layout="wide")
    st.title("⚙️  Account & Risk")
    cfg = load_config()
    render_account_section()
    render_ftmo_progress(cfg)
    render_position_sizing_section()
    render_per_strategy_risk()
    render_time_guards(cfg)
    render_emergency_stop_section()
    render_risk_caps_editor(cfg)
    render_ftmo_pass_rate_widget()


main()
