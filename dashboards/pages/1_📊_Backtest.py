"""
1_📊_Backtest.py — single-strategy backtest with reconciliation enforced
+ in-process sweep + train/test partition + Promote buttons.

Migrated from dashboards/control.py with the following NEW behaviour:
  - Time-guard toggles (weekend_flat / daily_close_flat)
  - Reconciliation banner shows the EXACT $ divergence
  - "Promote to Paper Portfolio" — adds the config to session_state for Page 3
  - Sweep mode is in-process (no subprocess), with st.progress
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.backtest_stats import compute_full_stats   # noqa: E402
from core import cost_defaults   # noqa: E402
from core.config import load_config   # noqa: E402
from core.data import load_parquet   # noqa: E402
from core.symbol_info_loader import try_load as try_load_symbol_info   # noqa: E402
from dashboards.components import reconciliation_badge   # noqa: E402
from dashboards.components.charts import (   # noqa: E402
    cumulative_pnl_figure,
    drawdown_figure,
    equity_figure,
    heatmap_test_R,
    streak_figure,
    trade_reasons_figure,
    trades_to_dataframe,
    win_loss_donut,
)
from dashboards.components.forms import render_params_form   # noqa: E402
from dashboards.components.state import (   # noqa: E402
    DEFAULT_LOTS,
    DEFAULT_MONEY_PER_UNIT,
    KEY_LAST_BACKTEST,
    KEY_LAST_BACKTEST_META,
    KEY_PROMOTE_TO_PAPER,
    discover_data,
    resolve_money_per_unit,
    discover_strategies,
)


def _run_one(df, strat, *, balance, lots, mpu, comm, slip, train_pct,
              symbol, enforce_weekend_flat, enforce_daily_flat,
              no_entry_min, risk_pct=None, symbol_info=None):
    sigs = strat.signals(df)
    result = run_backtest(
        df, sigs,
        starting_balance=balance, lots=lots, money_per_unit_price=mpu,
        commission_per_trade=comm,
        slippage_per_fill_atr_frac=slip,
        symbol=symbol,
        enforce_weekend_flat=enforce_weekend_flat,
        enforce_daily_flat=enforce_daily_flat,
        no_entry_minutes_before_close=no_entry_min,
        risk_pct=risk_pct, symbol_info=symbol_info,
    )
    train, test = partition_train_test(result, train_pct, n_bars=len(df))
    return {"result": result, "signals": sigs,
            "train": train, "test": test,
            "split_idx": int(len(df) * train_pct),
            "candles": df}


def render_backtest_section(strategies, data_index, cfg):
    if not data_index:
        st.warning("No parquets cached. Use the Data Manager page to fetch some.")
        return
    if not strategies:
        st.warning("No strategies found under strategies/.")
        return

    # ── Query-param prefill (deep-link from Composer / Strategy Library) ─
    # When the Composer's per-row "📊 Backtest" link is clicked, the URL
    # arrives as e.g. /Backtest?ticker=XAUUSD&tf=D1&strategy=donchian_55
    # Without this block, the form would ignore the URL and show
    # whatever was last in session_state — wrong cell, misleading
    # results.
    #
    # Two extra concerns the user surfaced:
    #   (1) "donchian_55" must set period=55 on the form, not just pick
    #       the base donchian_breakout (which defaults to period=20).
    #   (2) After deep-link, the page should AUTO-RUN the backtest — not
    #       require an extra "Run Backtest" click.
    try:
        qp = dict(st.query_params)
    except Exception:
        qp = {}
    qp_marker = "_bt_qp_applied"

    def _variant_to_base_and_params(variant: str) -> tuple[str, dict]:
        """Map e.g. 'donchian_55' → ('donchian_breakout', {'period': 55}).
        Mirrors core.edge_catalog._variant_name_from_config in reverse."""
        if variant.startswith("donchian_"):
            try:
                return "donchian_breakout", {"period": int(variant.split("_")[1])}
            except (ValueError, IndexError):
                return "donchian_breakout", {}
        if variant.startswith("ema_cross_"):
            parts = variant.split("_")
            if len(parts) >= 4:
                try:
                    return "ema_cross", {
                        "fast_period": int(parts[2]),
                        "slow_period": int(parts[3]),
                    }
                except ValueError:
                    pass
            return "ema_cross", {}
        if variant.startswith("ema_pullback_"):
            parts = variant.split("_")
            if len(parts) >= 4:
                try:
                    return "ema_pullback", {
                        "fast_period": int(parts[2]),
                        "slow_period": int(parts[3]),
                    }
                except ValueError:
                    pass
            return "ema_pullback", {}
        if variant.startswith("rsi_"):
            parts = variant.split("_")
            if len(parts) >= 3:
                try:
                    return "rsi_meanrev", {
                        "oversold": int(parts[1]),
                        "overbought": int(parts[2]),
                    }
                except ValueError:
                    pass
            return "rsi_meanrev", {}
        if variant.startswith("bbands_"):
            parts = variant.split("_")
            if len(parts) >= 3:
                try:
                    return "bbands_meanrev", {
                        "bb_period": int(parts[1]),
                        "bb_k": float(parts[2]),
                    }
                except ValueError:
                    pass
            return "bbands_meanrev", {}
        return variant, {}

    if qp and not st.session_state.get(qp_marker):
        if "ticker" in qp and qp["ticker"] in data_index:
            st.session_state["bt_ticker"] = qp["ticker"]
        ticker_key = st.session_state.get("bt_ticker") or sorted(data_index)[0]
        if "tf" in qp and qp["tf"] in data_index.get(ticker_key, {}):
            st.session_state["bt_tf"] = qp["tf"]
        if "strategy" in qp:
            variant = qp["strategy"]
            base, variant_params = _variant_to_base_and_params(variant)
            # Fallback to resolver if our parser couldn't decompose
            if base not in strategies:
                from dashboards.components.strategy_resolver import (
                    resolve_base_strategy,
                )
                resolved = resolve_base_strategy(variant, strategies)
                if resolved and resolved in strategies:
                    base = resolved
                elif variant in strategies:
                    base = variant
            # If the URL carries a base64-encoded full config (Composer
            # Backtest deep-link does this since 2026-05), use it AS THE
            # SOURCE OF TRUTH — covers stop_atr_mult / target_atr_mult /
            # rsi_period / etc. that variant-name decoding can't capture.
            # This makes a Composer→Backtest round-trip reproduce the
            # exact catalog metrics.
            if "config" in qp:
                import base64 as _b64
                import json as _json
                raw = qp["config"]
                # Restore base64 padding
                pad = "=" * (-len(raw) % 4)
                try:
                    cfg_dict = _json.loads(
                        _b64.urlsafe_b64decode((raw + pad).encode("ascii"))
                            .decode("utf-8")
                    )
                    if isinstance(cfg_dict, dict):
                        # Full config wins — overwrites partial variant
                        # decoding and adds atr_mults / etc.
                        variant_params = {**variant_params, **cfg_dict}
                except (ValueError, TypeError):
                    pass
            if base in strategies:
                st.session_state["bt_strat"] = base
                # Pre-set the form's per-param widget keys so the form
                # renders with the variant's specific values (e.g.
                # period=55 for donchian_55, not the default 20).
                # Widget keys are formatted as `bt_p_{base}__{field}`
                # by render_params_form().
                for fld, val in variant_params.items():
                    st.session_state[f"bt_p_{base}__{fld}"] = val
            # Auto-run flag — fire the backtest on first render so the
            # user lands directly on the chart instead of the empty
            # "Set parameters in the sidebar" placeholder.
            st.session_state["_bt_auto_run"] = True
            st.session_state["_bt_auto_run_meta"] = {
                "ticker": qp.get("ticker", ""),
                "tf": qp.get("tf", ""),
                "strategy": base,
                "variant": variant,
                "from_url": True,
            }
        st.session_state[qp_marker] = True

    # ----- Top-of-page config strip (moved out of sidebar) ──────────
    # Pre-fix the entire config form lived in Streamlit's sidebar,
    # crowding it with 8+ widgets and making the nav cramped. Now: the
    # sidebar holds only navigation + global account selector, and
    # backtest config sits inline at the top of the main content area
    # where there's room. Catalog cost defaults are used silently;
    # power-user knobs are tucked behind an expander.
    st.markdown("### ⚙️  Backtest config")
    cfg_cols = st.columns([2, 1, 2, 1])
    with cfg_cols[0]:
        ticker = st.selectbox("ticker", sorted(data_index.keys()),
                                key="bt_ticker")
        tfs_avail = sorted(data_index[ticker].keys(),
                            key=lambda x: {"M15": 0, "H1": 1,
                                            "H4": 2, "D1": 3}.get(x, 9))
    with cfg_cols[1]:
        tf = st.selectbox("timeframe", tfs_avail, key="bt_tf")
    with cfg_cols[2]:
        strat_name = st.selectbox("strategy", sorted(strategies.keys()),
                                    key="bt_strat")
        StratCls, ParamsCls = strategies[strat_name]
    with cfg_cols[3]:
        st.write("")   # vertical alignment spacer
        run_btn = st.button("▶ Run Backtest", type="primary",
                              width="stretch", key="bt_run")

    # Strategy params row — only show if the strategy has params
    if ParamsCls is None:
        params_obj = None
    else:
        with st.expander("🔧  Strategy parameters",
                          expanded=False):
            params_obj = render_params_form(
                ParamsCls, key_prefix=f"bt_p_{strat_name}",
            )

    # Cost + sizing override (collapsed by default — catalog defaults
    # used silently on first run for parity with sweep/rebaseline)
    st.caption(
        f"⚖️ Using catalog cost defaults: "
        f"{cost_defaults.cost_config_badge()}. Open the override "
        f"expander below to test what-if scenarios."
    )
    with st.expander("⚙️ Override defaults (cost + sizing + time guards)",
                      expanded=False):
        ov_cols = st.columns(3)
        with ov_cols[0]:
            balance = float(st.number_input(
                "starting balance ($)",
                value=cost_defaults.DEFAULT_STARTING_BALANCE_USD,
                step=1000.0, key="bt_bal",
            ))
            sizing_mode = st.radio(
                "sizing mode",
                ["risk %", "fixed lots"],
                index=0, horizontal=True, key="bt_sizing_mode",
                help="risk %: lots computed per-trade from equity × "
                      "risk%. fixed lots: legacy.",
            )
            if sizing_mode == "risk %":
                risk_pct_val = float(st.number_input(
                    "risk per trade (%)",
                    value=0.3, step=0.1, format="%.2f",
                    min_value=0.05, max_value=5.0,
                    key="bt_risk_pct",
                ))
                lots = 0.0
                sym_info = try_load_symbol_info(ticker)
                if sym_info is None:
                    st.warning(
                        f"No symbol_info for `{ticker}` — falling "
                        f"back to fixed lots. Run "
                        f"`make refresh-symbol-info`.",
                    )
                    sizing_mode = "fixed lots"
                    risk_pct_val = None
                    sym_info = None
            else:
                risk_pct_val = None
                sym_info = None
                lots = float(st.number_input(
                    "lots",
                    value=DEFAULT_LOTS.get(ticker, 0.1),
                    step=0.1, format="%.2f", key="bt_lots",
                ))
            mpu = float(st.number_input(
                "money per 1.0 unit per lot ($)",
                value=resolve_money_per_unit(ticker),
                step=1.0, format="%.2f", key="bt_mpu",
            ))
        with ov_cols[1]:
            comm = float(st.number_input(
                "commission $/trade",
                value=cost_defaults.DEFAULT_COMMISSION_USD,
                step=0.5, format="%.2f", key="bt_comm",
            ))
            slip = float(st.number_input(
                "slippage (× ATR)",
                value=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
                step=0.05, format="%.3f", key="bt_slip",
            ))
            train_pct = float(st.slider(
                "train fraction (rest is OOS)",
                0.1, 0.95, cost_defaults.DEFAULT_TRAIN_PCT, 0.05,
                key="bt_train",
            ))
        with ov_cols[2]:
            st.markdown("**Time guards (INVARIANT-8)**")
            enforce_weekend = st.checkbox(
                "Enforce weekend flat",
                value=cfg.weekend_flat_all,
                key="bt_weekend_flat",
                help="Force-close ALL positions Friday 19:55 UTC.",
            )
            enforce_daily = st.checkbox(
                "Enforce daily flat (stocks/indices)",
                value=True, key="bt_daily_flat",
                help="Force-close stocks/indices weekday 19:55 UTC.",
            )
            no_entry = int(st.number_input(
                "no-entry window (min before close)",
                value=0, step=5, min_value=0, max_value=120,
                key="bt_no_entry",
            ))

    st.markdown("---")

    # Auto-run flag set by other pages (e.g. Portfolio Composer's
    # "Open in Backtest" button). Consumed once and cleared so a
    # natural refresh doesn't re-fire.
    auto_run = st.session_state.pop("_bt_auto_run", False)
    auto_meta = st.session_state.pop("_bt_auto_run_meta", None)
    if auto_run and auto_meta:
        st.success(
            f"📥 Arrived from Composer — auto-running `{auto_meta['strategy']}` "
            f"× `{auto_meta['ticker']}` × `{auto_meta['tf']}` "
            f"({auto_meta.get('side', 'long')}, "
            f"R:R {auto_meta.get('rr_label', '—')})…"
        )
    effective_run = run_btn or auto_run

    # ----- Main area -----
    st.subheader(f"Backtest — {ticker} {tf} • {strat_name}")
    if not effective_run and KEY_LAST_BACKTEST not in st.session_state:
        st.info("Set parameters in the sidebar and click **Run Backtest**.")
        return

    if effective_run:
        try:
            df = load_parquet(data_index[ticker][tf])
        except Exception as e:
            st.error(f"Could not load parquet: {e}")
            return
        try:
            strat = StratCls() if params_obj is None else StratCls(params_obj)
        except Exception as e:
            st.error(f"Could not instantiate strategy: {e}")
            return
        try:
            run = _run_one(
                df, strat,
                balance=balance, lots=lots, mpu=mpu,
                comm=comm, slip=slip, train_pct=train_pct,
                symbol=ticker,
                enforce_weekend_flat=enforce_weekend,
                enforce_daily_flat=enforce_daily,
                no_entry_min=no_entry,
                risk_pct=risk_pct_val, symbol_info=sym_info,
            )
        except Exception as e:
            st.error(f"Backtest failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc())
            return
        st.session_state[KEY_LAST_BACKTEST] = run
        st.session_state[KEY_LAST_BACKTEST_META] = {
            "ticker": ticker, "tf": tf, "strat": strat_name,
            "balance": balance, "lots": lots, "mpu": mpu,
            "comm": comm, "slip": slip, "train_pct": train_pct,
            "enforce_weekend": enforce_weekend, "enforce_daily": enforce_daily,
            "no_entry_minutes": no_entry,
            "params_obj": params_obj,
            "tolerance": run["result"].reconcile_tolerance,
            "sizing_mode": sizing_mode, "risk_pct": risk_pct_val,
        }

    run = st.session_state[KEY_LAST_BACKTEST]
    meta = st.session_state[KEY_LAST_BACKTEST_META]
    r = run["result"]

    # Reconciliation banner — INVARIANT-1
    reconciliation_badge.render(r.reconciles, r.sum_realized_pnl,
                                 r.equity_curve_pnl, meta["tolerance"])

    # Headline metrics
    n = r.n_trades
    if n > 0:
        wins = sum(1 for t in r.trades if t.realized_pnl > 0)
        win_rate = wins / n * 100.0
        gw = sum(t.realized_pnl for t in r.trades if t.realized_pnl > 0)
        gl = -sum(t.realized_pnl for t in r.trades if t.realized_pnl <= 0)
        pf = (gw / gl) if gl > 0 else float("inf") if gw > 0 else 0.0
        avg_R = sum(t.r_multiple for t in r.trades) / n
    else:
        win_rate = pf = avg_R = 0.0
    ret_pct = r.equity_curve_pnl / meta["balance"] * 100.0 if meta["balance"] else 0.0

    # FULL-history headline (train + test combined) — useful context
    # but DO NOT use for go/no-go decision; that's the OOS row below.
    st.markdown(
        "**Full-history stats** _(train + test combined — context only,"
        " NOT used for deploy gate)_"
    )
    cols = st.columns(6)
    cols[0].metric("trades", f"{n}")
    cols[1].metric("win rate", f"{win_rate:.1f}%")
    pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
    cols[2].metric("profit factor", pf_text)
    cols[3].metric("avg R", f"{avg_R:+.3f}")
    cols[4].metric("return", f"{ret_pct:+.2f}%",
                    delta=f"${r.equity_curve_pnl:+,.0f}")
    cols[5].metric("skipped", f"{r.skipped_signals}",
                    help="signals dropped because in_no_entry_window")

    # OOS-only headline — THIS is what the gate banner below evaluates.
    # Same methodology as Composer / Strategy Library / catalog Score.
    if n > 0 and run.get("test") is not None:
        _t = run["test"]
        _oos_n = _t.n_trades
        _oos_pf = _t.profit_factor
        _oos_R = _t.avg_R
        # PartitionMetrics.win_rate is already a percentage (0-100),
        # NOT a fraction. Don't multiply by 100 again.
        _oos_wr = _t.win_rate
        _oos_pnl = _t.sum_pnl
        _oos_ret_pct = _oos_pnl / meta["balance"] * 100.0 if meta["balance"] else 0.0
        st.markdown(
            "**OOS stats (test 40% slice)** _— this is what the deploy "
            "gate + catalog Score evaluate. **Trust these for go/no-go.**_"
        )
        oos_cols = st.columns(6)
        oos_cols[0].metric("OOS trades", f"{_oos_n}")
        oos_cols[1].metric("OOS win rate", f"{_oos_wr:.1f}%")
        oos_pf_text = ("inf" if _oos_pf == float("inf")
                          else f"{_oos_pf:.2f}")
        oos_cols[2].metric("OOS PF", oos_pf_text)
        oos_cols[3].metric("OOS avg R", f"{_oos_R:+.3f}")
        oos_cols[4].metric("OOS return", f"{_oos_ret_pct:+.2f}%",
                              delta=f"${_oos_pnl:+,.0f}")
        # Compare with full-history PF — surface divergence as a hint
        delta_pf = (_oos_pf - pf) if (pf != float("inf")
                                          and _oos_pf != float("inf")) else 0
        oos_cols[5].metric(
            "vs full-history",
            f"{delta_pf:+.2f} PF",
            help=("OOS PF minus full-history PF. Positive means recent "
                  "data is BETTER than older — strategy is doing well "
                  "in current regime. Negative means recent data is "
                  "WORSE — strategy may be decaying."),
        )

    # ── Hard-gate banner ─────────────────────────────────────────────
    # Evaluate the SAME quality gates the Composer uses, against the
    # JUST-RUN cost-priced result. If the cell would fail any gate, show
    # a red error banner BEFORE Deploy so the user can't accidentally
    # promote a losing cell to live. Single source of truth lives in
    # core.edge_catalog.HARD_GATE_THRESHOLDS — tweak there, banner +
    # Composer pick it up automatically.
    if n > 0:
        from core.edge_catalog import (
            _evaluate_hard_gates as _gate_check,
            HARD_GATE_THRESHOLDS as _GATE_T,
        )
        # CRITICAL: gate evaluation MUST match the Composer/Library
        # methodology — both use OOS (60/40 test partition) metrics, NOT
        # full-history. Same cell, same answer everywhere.
        #
        # Pre-fix this used compute_full_stats(r, ...) which is the
        # FULL backtest's PF + recovery. That mixes train + test → on a
        # cell where train was a different regime (common post-2020),
        # the full-history view shows PF < 1 even when OOS PF is > 1.
        # Result: Composer says "deploy_safe ✓ score 21" while Backtest
        # says "🚫 FAIL — recover never". Two stories.
        #
        # Fix: compute the gate stats on the TEST partition only.
        # Build a synthetic BacktestResult containing only the OOS
        # trades, run compute_full_stats on it, and use its recovery
        # number. The OOS overall PF == OOS test PF (same trades).
        from core.backtest import BacktestResult as _BR
        _split_idx = run.get("split_idx", 0)
        _test_trades = [t for t in r.trades
                          if t.entry_bar_idx >= _split_idx]
        try:
            _oos_eq = compute_full_stats(_BR(
                trades=_test_trades,
                equity_curve=r.equity_curve,
                starting_balance=meta["balance"],
                ending_balance=meta["balance"]
                                + sum(t.realized_pnl for t in _test_trades),
                sum_realized_pnl=sum(t.realized_pnl for t in _test_trades),
                equity_curve_pnl=sum(t.realized_pnl for t in _test_trades),
                reconciles=True,
            ), starting_balance=meta["balance"])
            _recovery_for_gate = _oos_eq.recovery_duration_days
        except Exception:
            # Fallback to full-history recovery if synthetic build fails
            _recovery_for_gate = compute_full_stats(
                r, starting_balance=meta["balance"]
            ).recovery_duration_days

        _test_pf = run["test"].profit_factor
        _test_r = run["test"].avg_R
        _test_n = run["test"].n_trades
        # overall_pf gate uses OOS PF (same as catalog).
        _gates = _gate_check(
            overall_pf=_test_pf if _test_pf != float("inf") else 99.0,
            test_pf=_test_pf if _test_pf != float("inf") else 99.0,
            test_r=_test_r,
            recovery_days=_recovery_for_gate,
            n_test=_test_n,
        )
        if _gates:
            _reasons_md = "\n".join(f"  • {r}" for r in _gates)
            st.error(
                "🚫 **This cell FAILS deployment quality gates — "
                "do NOT promote to live.**\n\n"
                f"{_reasons_md}\n\n"
                f"_Thresholds: overall PF ≥ {_GATE_T['min_overall_pf']}, "
                f"TEST PF ≥ {_GATE_T['min_test_pf']}, "
                f"TEST avg_R ≥ {_GATE_T['min_test_avg_r']:+.2f}, "
                f"recovery ≤ {_GATE_T['max_recovery_days']:.0f}d, "
                f"n_test ≥ {_GATE_T['min_n_test']}._  "
                f"All gates evaluated on OOS (test 40%) slice — same "
                f"methodology as Composer + Strategy Library.",
                icon="🚫",
            )
        else:
            st.success(
                f"✅ **Quality gates PASSED** (OOS) — TEST PF "
                f"{_test_pf:.2f}, TEST avg_R {_test_r:+.3f}, recovery "
                f"{('not yet' if _recovery_for_gate is None else f'{_recovery_for_gate:.0f}d')}, "
                f"n_test {_test_n}. Same OOS methodology as the "
                f"catalog Score in Composer / Strategy Library — if "
                f"that says ✓ this also says ✓.",
                icon="✅",
            )

    # ── Full-stats grid (DD %, recovery, R:R, expectancy, CAGR) ──────────
    if n > 0:
        stats = compute_full_stats(r, starting_balance=meta["balance"])
        st.markdown("**📊 Full stats**")
        s1 = st.columns(5)
        s1[0].metric("Max DD",
                       f"{stats.max_dd_pct:.1f}%",
                       delta=f"-${stats.max_dd_dollars:,.0f}",
                       delta_color="inverse")
        s1[1].metric("DD duration", f"{stats.max_dd_duration_days:.0f}d")
        if stats.recovery_duration_days is None:
            s1[2].metric("Recovery", "not yet",
                            help="Equity hasn't reclaimed previous peak.")
        else:
            s1[2].metric("Recovery", f"{stats.recovery_duration_days:.0f}d")
        s1[3].metric("Max consec wins", stats.max_consec_wins)
        s1[4].metric("Max consec losses", stats.max_consec_losses,
                        delta_color="inverse")

        s2 = st.columns(5)
        s2[0].metric("Avg win", f"${stats.avg_win_dollars:+,.2f}")
        s2[1].metric("Avg loss", f"${stats.avg_loss_dollars:+,.2f}",
                        delta_color="inverse")
        rr = stats.risk_reward_ratio
        s2[2].metric("R:R ratio",
                       "inf" if rr == float("inf") else f"{rr:.2f}",
                       help="avg_win / |avg_loss|. >1 = wins outsize losses.")
        s2[3].metric("Expectancy",
                       f"${stats.expectancy_dollars:+,.2f}",
                       help="Mean P&L per trade.")
        if stats.cagr_pct is None:
            s2[4].metric("CAGR",
                            f"{(stats.sum_realized / meta['balance'] * 100):+.1f}%",
                            help=f"Span {stats.span_days:.0f}d — "
                                 "too short to annualise; total return shown.")
        else:
            s2[4].metric("CAGR", f"{stats.cagr_pct:+.1f}%",
                            help=f"Annualised over {stats.span_days:.0f} days.")

        # Phase-32: Edge Score for cross-page consistency. Same formula
        # used by Composer + Library + Compare. The number shown HERE
        # should match what the user saw on the page they came from
        # (within rounding). If they diverge by >10 points, something's
        # drifted (cost config / data window / params).
        try:
            from core import edge_score as _es
            # Build the inputs from the freshly-run stats. Note: we
            # use the FULL-sample stats here, not OOS — same as catalog
            # _evaluate_hard_gates path. Recovery uses the freshly-
            # computed value.
            n_trades = max(1, n)
            tpd = (n_trades / max(1.0, stats.span_days)
                    if stats.span_days else 0.0)
            wr_frac = (stats.win_rate_pct or 0.0) / 100.0
            rr = (stats.risk_reward_ratio
                   if stats.risk_reward_ratio not in (
                       float("inf"), float("-inf")) else 1.0)
            es_breakdown = _es.compute(
                expectancy_per_trade=float(stats.expectancy_dollars or 0.0),
                win_rate=wr_frac, risk_reward=float(rr or 1.0),
                trades_per_day=tpd,
                recovery_days=stats.recovery_duration_days,
                sharpe_r=float(getattr(stats, "sharpe_R", 0) or 0.0),
                deploy_safe=(len(_gates) == 0),
            )
            st.markdown("---")
            st.markdown(
                f"### 🎯 Edge Score: **{es_breakdown.total:.1f}** / 100"
            )
            score_cols = st.columns(5)
            score_cols[0].metric(
                "Expectancy", f"{es_breakdown.expectancy_score:.0f}",
                help=f"$/trade × frequency. Raw: "
                      f"${es_breakdown.expectancy_per_trade:.2f}/trade × "
                      f"{tpd:.2f} trades/day. Weight 30%.",
            )
            score_cols[1].metric(
                "Kelly", f"{es_breakdown.kelly_score:.0f}",
                help=f"(WR × R:R − (1−WR)) / R:R = "
                      f"{es_breakdown.kelly_pct:.3f}. Optimal "
                      f"compounding bet size. Negative = mathematically "
                      f"losing. Weight 20%.",
            )
            score_cols[2].metric(
                "Recovery × freq", f"{es_breakdown.recovery_frequency_score:.0f}",
                help=f"trades/day ÷ recovery_days. Frequent + fast = "
                      f"robust. Weight 20%.",
            )
            score_cols[3].metric(
                "Sharpe-R", f"{es_breakdown.sharpe_r_score:.0f}",
                help=f"Risk-adjusted return in R-multiples. Raw: "
                      f"{es_breakdown.sharpe_r:.2f}. Weight 20%.",
            )
            score_cols[4].metric(
                "Deploy-safe", f"{es_breakdown.deploy_safe_bonus:.0f}",
                help=f"Compliance bonus. Cell {'PASSES' if es_breakdown.deploy_safe else 'FAILS'} "
                      f"hard gates. Weight 10%.",
            )
            st.caption(
                "💡 This score should match what Composer / Library / "
                "Compare showed for this cell. Divergence > 10 points "
                "= cost config or data window has drifted since the "
                "catalog was last rebaselined."
            )
            # Shared Edge Score explainer (same component used on
            # Composer / Library / Compare so the explanation is
            # identical everywhere)
            from dashboards.components import edge_score_explainer
            edge_score_explainer.render_explainer(expanded=False)
        except Exception as _e:
            st.caption(f"(Edge Score unavailable: {_e})")

        st.caption(
            f"Largest single win: ${stats.largest_win_dollars:+,.2f}  ·  "
            f"largest single loss: ${stats.largest_loss_dollars:+,.2f}  ·  "
            f"avg trade duration: {stats.avg_trade_bars:.1f} bars  ·  "
            f"sharpe-on-R: {stats.sharpe_R:.2f}"
        )

    # Sizing summary
    if meta.get("sizing_mode") == "risk %":
        if n > 0:
            avg_lots = sum(t.lots for t in r.trades) / n
            avg_risk_dollars = sum(t.initial_dollar_risk for t in r.trades) / n
            avg_risk_pct = avg_risk_dollars / meta["balance"] * 100 if meta["balance"] else 0
            st.caption(
                f"📐 **Dynamic sizing** target {meta['risk_pct']:.2f}% per trade "
                f"→ avg lots = {avg_lots:.2f}, "
                f"avg $ at risk = ${avg_risk_dollars:,.0f} "
                f"({avg_risk_pct:.2f}% of starting balance)."
            )
        else:
            st.caption(f"📐 Dynamic sizing target {meta['risk_pct']:.2f}% per trade.")
    else:
        st.caption(f"📐 Fixed sizing: {meta.get('lots', 0)} lots per trade.")

    # Train/Test
    train, test = run["train"], run["test"]
    st.markdown("**Train / Test partition (by entry bar):**")
    pcol1, pcol2 = st.columns(2)
    for col, m in [(pcol1, train), (pcol2, test)]:
        pf_t = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        col.markdown(
            f"**{m.label.upper()}** &nbsp; n={m.n_trades} &nbsp; "
            f"PF={pf_t} &nbsp; avg R={m.avg_R:+.3f} &nbsp; "
            f"win%={m.win_rate:.1f} &nbsp; $={m.sum_pnl:+,.2f}",
            unsafe_allow_html=True,
        )

    # Equity + drawdown
    df = run["candles"]
    split_time = df["time"].iloc[run["split_idx"]] if 0 < run["split_idx"] < len(df) else None
    title = f"{meta['ticker']} {meta['tf']} • {meta['strat']} — equity"
    st.plotly_chart(equity_figure(r.equity_curve, split_time,
                                    meta["balance"], title),
                     width="stretch")
    st.plotly_chart(drawdown_figure(r.equity_curve),
                     width="stretch")

    # AlgoTest-style trade-level views: cumulative P&L, win/loss donut,
    # win/loss streak bars. Only render if there are trades.
    if n > 0:
        st.markdown("**📈  Trade-level views**")
        st.plotly_chart(cumulative_pnl_figure(r.trades),
                         width="stretch")
        v_cols = st.columns([1, 2])
        with v_cols[0]:
            st.plotly_chart(win_loss_donut(r.trades),
                             width="stretch")
        with v_cols[1]:
            st.plotly_chart(streak_figure(r.trades),
                             width="stretch")

    # Trade tape + reasons
    if n > 0:
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown("**Trade tape** (sortable)")
            st.dataframe(trades_to_dataframe(r.trades, df),
                          width="stretch", height=360)
        with c2:
            st.plotly_chart(trade_reasons_figure(r.trades),
                             width="stretch")
    else:
        st.info("No trades produced — try different params or a longer history.")

    # Promote / parity buttons
    st.markdown("---")
    pcol1, pcol2, pcol3 = st.columns(3)
    if pcol1.button("📤  Promote to Strategy Studio", type="secondary",
                      width="stretch", key="bt_promote_studio"):
        st.session_state["_studio_compare_left"] = {
            "ticker": meta["ticker"], "tf": meta["tf"], "strat": meta["strat"],
            "params": meta.get("params_obj"),
        }
        st.success("Pushed to Strategy Studio (Page 2 → 'Side A' slot).")

    if pcol2.button("📡  Promote to Paper Portfolio", type="secondary",
                      width="stretch", key="bt_promote_paper"):
        portfolio = st.session_state.get(KEY_PROMOTE_TO_PAPER, [])
        portfolio.append({
            "ticker": meta["ticker"], "tf": meta["tf"], "strat": meta["strat"],
            "lots": meta["lots"], "mpu": meta["mpu"],
            "params": meta.get("params_obj"),
            "enforce_weekend": meta["enforce_weekend"],
            "enforce_daily": meta["enforce_daily"],
        })
        st.session_state[KEY_PROMOTE_TO_PAPER] = portfolio
        st.success(f"Added to paper portfolio ({len(portfolio)} entries). "
                    "Open Page 3 → Tab B to start.")

    if pcol3.button("🔬  Run Replay-Parity",
                      type="primary",
                      width="stretch",
                      key="bt_run_parity",
                      help="Required before live deployment. Compares "
                            "backtest P&L with bar-by-bar replay engine "
                            "— must match within $0.01."):
        _run_parity_for_current_backtest(meta, run)

    # ── Direct-deploy row — creates an entry in deployments.json so the
    # runner picks it up on the next tick. Until now the user had to
    # bounce through the Composer or the Operations page to deploy a
    # cell they'd just backtested; this is the missing 1-click path.
    n_trades_for_deploy = len(run["result"].trades)
    if n_trades_for_deploy > 0:
        st.markdown("##### 🚀 Deploy this cell to your active account")
        _render_deploy_to_account_buttons(meta, run)


def _render_deploy_to_account_buttons(meta: dict, run: dict) -> None:
    """Two buttons: Deploy to Paper / Deploy to LIVE. Both write the
    same Deployment row to deployments.json — the runner picks it up
    on the next tick. LIVE is blocked if the cell fails the hard
    quality gates (parity is also enforced server-side at the runner
    via the parity_gate)."""
    from core import account_manager
    from core import deployment as dep_mod
    from core.deployment import Deployment

    # Resolve active account — same pattern as scripts/run_deployments.py
    try:
        accounts = account_manager.list_accounts()
        active = accounts[0] if accounts else None
    except Exception:
        active = None
    if active is None:
        st.warning("⚠ No MT5 account configured — cannot deploy. "
                    "Add one on the Account Risk page.")
        return

    # Re-evaluate gates so the LIVE button knows whether to block.
    # (Banner above already shows the same status; this is for button state.)
    from core.edge_catalog import _evaluate_hard_gates as _gate_check
    from core.backtest import BacktestResult as _BR
    r = run["result"]
    split_idx = run.get("split_idx", 0)
    test_trades = [t for t in r.trades if t.entry_bar_idx >= split_idx]
    try:
        _oos_eq = compute_full_stats(_BR(
            trades=test_trades,
            equity_curve=r.equity_curve,
            starting_balance=meta["balance"],
            ending_balance=meta["balance"]
                            + sum(t.realized_pnl for t in test_trades),
            sum_realized_pnl=sum(t.realized_pnl for t in test_trades),
            equity_curve_pnl=sum(t.realized_pnl for t in test_trades),
            reconciles=True,
        ), starting_balance=meta["balance"])
        recov_for_gate = _oos_eq.recovery_duration_days
    except Exception:
        recov_for_gate = compute_full_stats(
            r, starting_balance=meta["balance"]
        ).recovery_duration_days
    test_pf = run["test"].profit_factor
    test_r = run["test"].avg_R
    test_n = run["test"].n_trades
    gates_failed = _gate_check(
        overall_pf=test_pf if test_pf != float("inf") else 99.0,
        test_pf=test_pf if test_pf != float("inf") else 99.0,
        test_r=test_r,
        recovery_days=recov_for_gate,
        n_test=test_n,
        strategy_name=meta["strat"],
    )
    cell_passes = not gates_failed

    # Build the Deployment payload that both buttons will use.
    # Slug uses (strategy, ticker, tf) so re-deploys overwrite cleanly.
    params_obj = meta.get("params_obj")
    params_dict = {}
    if params_obj is not None:
        try:
            from dataclasses import asdict as _asdict, is_dataclass as _isdc
            if _isdc(params_obj):
                params_dict = _asdict(params_obj)
        except Exception:
            pass
    sname = meta["strat"]
    deployment_id = Deployment.slug(sname, meta["ticker"], meta["tf"])
    risk_pct_default = float(meta.get("risk_pct") or 0.05)

    cols = st.columns([2, 2, 3])
    cols[0].markdown(
        f"**Account:** `{active.alias}` (#{active.login})  \n"
        f"**Cell:** `{sname}` × `{meta['ticker']}` × `{meta['tf']}`  \n"
        f"**Risk %/trade:** `{risk_pct_default}`%"
    )

    if cols[1].button("📡  Deploy to PAPER", type="secondary",
                        width="stretch",
                        key=f"bt_deploy_paper_{deployment_id}",
                        help="Persists this cell to deployments.json and "
                              "starts paper-trading on the next runner tick. "
                              "No real orders sent."):
        try:
            dep = Deployment(
                deployment_id=deployment_id,
                strategy=sname, ticker=meta["ticker"], tf=meta["tf"],
                long_only=False, params=params_dict,
                risk_pct=risk_pct_default, status="paper",
            )
            dep_mod.upsert_deployment(active.login, dep)
            dep_mod.update_status(active.login, deployment_id, "paper")
            st.success(
                f"📡 Deployed `{deployment_id}` as PAPER on account "
                f"#{active.login}. Runner will pick it up on next tick "
                f"(within ~5s). Watch the Paper page for activity."
            )
        except Exception as e:
            st.error(f"⛔ Deploy-paper failed: {e!r}")

    if not cell_passes:
        cols[2].button("🚫  Deploy to LIVE — BLOCKED",
                        type="secondary", disabled=True,
                        width="stretch",
                        key=f"bt_deploy_live_blocked_{deployment_id}",
                        help="Cell failed hard quality gates. See banner "
                              "above for which gates failed. Run the cell "
                              "with different params or choose a different "
                              "cell from the catalog.")
    else:
        if cols[2].button("🟢  Deploy to LIVE",
                            type="primary", width="stretch",
                            key=f"bt_deploy_live_{deployment_id}",
                            help="Persists this cell as LIVE. Runner WILL "
                                  "send real orders to the broker. Run "
                                  "Replay-Parity first if you haven't yet "
                                  "— the runner enforces parity-pass < 24h "
                                  "before opening any live position."):
            try:
                dep = Deployment(
                    deployment_id=deployment_id,
                    strategy=sname, ticker=meta["ticker"], tf=meta["tf"],
                    long_only=False, params=params_dict,
                    risk_pct=risk_pct_default, status="live",
                )
                dep_mod.upsert_deployment(active.login, dep)
                dep_mod.update_status(active.login, deployment_id, "live")
                st.success(
                    f"🟢 Deployed `{deployment_id}` as LIVE on account "
                    f"#{active.login}. The runner will block opens until "
                    f"replay-parity is recorded — run it from the button "
                    f"above if you haven't yet."
                )
            except Exception as e:
                st.error(f"⛔ Deploy-live failed: {e!r}")


def _run_parity_for_current_backtest(meta: dict, run: dict) -> None:
    """Run replay-parity for the strategy currently shown on the page.
    Shows result inline as a banner + 4-metric grid.

    IMPORTANT: builds the TimeGuardCfg from the FORM values (meta) — not
    from the global RiskConfig — so the replay sees exactly the same
    guard set the backtest in `run["result"]` was produced with.
    """
    import time
    from core.parity_gate import ParityGate
    from core.replay import replay_run
    from core.symbol_info_loader import try_load as try_load_symbol_info
    from core.time_guards import TimeGuardCfg

    cfg = load_config()
    strats = discover_strategies()
    sname = meta["strat"]
    if sname not in strats:
        st.error(f"⛔ Strategy `{sname}` not registered.")
        return
    StratCls, ParamsCls = strats[sname]
    params_obj = meta.get("params_obj")
    try:
        strat = StratCls() if params_obj is None else StratCls(params_obj)
    except Exception as e:
        st.error(f"⛔ Could not instantiate strategy: {e}")
        return

    sym_info = try_load_symbol_info(meta["ticker"])
    df = run["candles"]
    bt = run["result"]
    tg_cfg = TimeGuardCfg(
        weekend_flat_all=bool(meta.get("enforce_weekend", False)),
        daily_close_flat_classes=cfg.daily_close_flat_classes
            if meta.get("enforce_daily", False) else (),
        us_session_close_hhmm=cfg.us_session_close_utc,
        flat_buffer_minutes=cfg.flat_buffer_minutes,
        no_entry_minutes_before_close=int(meta.get("no_entry_minutes", 0)),
        asset_class_overrides=cfg.asset_class_overrides,
    )

    # Match the SAME sizing mode the backtest used. Pre-fix this always
    # passed `lots=meta["lots"]` — but when the backtest used risk%-sizing
    # mode, `meta["lots"]` is hard-coded to 0.0 (the engine ignores lots
    # in that mode). Replay's PaperExecutor rejects lots<=0 → BadSignalGeometry.
    # Detect risk% mode via meta and pass risk_pct + symbol_info instead.
    sizing_mode = meta.get("sizing_mode", "fixed lots")
    bt_lots = float(meta.get("lots", 0.0) or 0.0)
    bt_risk_pct = meta.get("risk_pct")
    if sizing_mode == "risk %" and bt_risk_pct:
        if sym_info is None:
            # Risk% sizing requires symbol_info to compute lots from
            # equity × risk%. If absent, fall back to a fixed lot so
            # parity at least runs (will diverge slightly from the
            # backtest, but the failure mode is "informative" not
            # "BadSignalGeometry crash").
            st.warning(
                f"⚠ symbol_info missing for `{meta['ticker']}` — replay "
                f"cannot reproduce risk%-sizing exactly. Falling back "
                f"to fixed 0.1 lots; expect small divergence. Run "
                f"`make refresh-symbol-info` to fix."
            )
            replay_lots_arg = 0.1
            replay_risk_pct = None
        else:
            # Dynamic sizing — replay sizes each trade off equity × risk%
            replay_lots_arg = 0.0           # signals dynamic
            replay_risk_pct = float(bt_risk_pct)
    else:
        # Fixed-lot mode — use the form's lots (with a sane fallback)
        replay_lots_arg = bt_lots if bt_lots > 0 else 0.1
        replay_risk_pct = None
    t0 = time.time()
    with st.spinner(f"Running replay-parity for {sname}..."):
        rp = replay_run(
            df, strat, symbol=meta["ticker"], tf=meta["tf"],
            starting_balance=meta["balance"],
            lots=replay_lots_arg,
            money_per_unit_price=meta.get("mpu", 1.0),
            commission_per_trade=meta.get(
                "comm", cost_defaults.DEFAULT_COMMISSION_USD),
            slippage_per_fill_atr_frac=meta.get(
                "slip", cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC),
            time_guard_cfg=tg_cfg, symbol_info=sym_info,
            risk_pct=replay_risk_pct,
        )
    elapsed = time.time() - t0
    div = abs(rp.sum_realized_pnl - bt.sum_realized_pnl)
    if div < 0.01:
        gate = ParityGate(REPO / "data" / "v2.db")
        gate.record_pass(sname, divergence_dollars=div)
        st.success(
            f"✅  **Replay-parity PASSED** in {elapsed:.2f}s.  "
            f"Divergence ${div:.6f}. `{sname}` is now cleared for live."
        )
    else:
        st.error(
            f"⛔  **Replay-parity FAILED** — divergence ${div:.4f} > $0.01. "
            f"Live deployment of `{sname}` is BLOCKED until the engine "
            f"drift is fixed."
        )
    cols = st.columns(4)
    cols[0].metric("BT trades", bt.n_trades)
    cols[1].metric("Replay trades", rp.n_trades,
                      delta=rp.n_trades - bt.n_trades)
    cols[2].metric("BT $", f"${bt.sum_realized_pnl:+,.4f}")
    cols[3].metric("Replay $", f"${rp.sum_realized_pnl:+,.4f}",
                      delta=f"${div:.6f} divergence",
                      delta_color=("normal" if div < 0.01 else "inverse"))


def render_sweep_section(strategies, data_index, cfg):
    """In-process sweep with st.progress — no subprocess."""
    st.markdown("---")
    st.markdown("### 🧮  Sweep (multi-cell evaluation)")

    if not data_index:
        return

    tickers = st.multiselect("tickers", sorted(data_index.keys()),
                              default=sorted(data_index.keys())[:3],
                              key="sw_tickers")
    all_tfs = sorted({tf for tfs in data_index.values() for tf in tfs.keys()})
    tfs = st.multiselect("timeframes", all_tfs, default=["D1"],
                          key="sw_tfs")
    snames = st.multiselect("strategies", sorted(strategies.keys()),
                              default=sorted(strategies.keys())[:3],
                              key="sw_strats")

    cols = st.columns(4)
    comm = float(cols[0].number_input("comm $/trade", value=4.0, step=0.5,
                                         format="%.2f", key="sw_comm"))
    slip = float(cols[1].number_input("slip × ATR", value=0.05, step=0.05,
                                         format="%.3f", key="sw_slip"))
    train_pct = float(cols[2].slider("train frac", 0.1, 0.95, 0.6, 0.05,
                                        key="sw_train"))
    enforce_flats = cols[3].checkbox("Enforce time guards", value=True,
                                       key="sw_flats")

    if not st.button("▶  Run sweep", type="primary", key="sw_run"):
        return
    if not (tickers and tfs and snames):
        st.warning("Select at least one ticker, tf, and strategy.")
        return

    rows = []
    failed = []
    total = len(tickers) * len(tfs) * len(snames)
    prog = st.progress(0.0, text="Running sweep...")
    done = 0
    for ticker in tickers:
        mpu = resolve_money_per_unit(ticker)
        lots_t = DEFAULT_LOTS.get(ticker, 0.1)
        for tf in tfs:
            if tf not in data_index.get(ticker, {}):
                done += len(snames)
                prog.progress(done / total, text=f"skip {ticker} {tf}")
                continue
            try:
                df = load_parquet(data_index[ticker][tf])
            except Exception:
                done += len(snames)
                continue
            for sname in snames:
                done += 1
                prog.progress(done / total, text=f"{ticker} {tf} {sname}")
                StratCls, ParamsCls = strategies[sname]
                try:
                    strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
                except Exception:
                    continue
                try:
                    sigs = strat.signals(df)
                    r = run_backtest(
                        df, sigs, starting_balance=91_400, lots=lots_t,
                        money_per_unit_price=mpu,
                        commission_per_trade=comm,
                        slippage_per_fill_atr_frac=slip,
                        symbol=ticker,
                        enforce_weekend_flat=enforce_flats and cfg.weekend_flat_all,
                        enforce_daily_flat=enforce_flats,
                    )
                except Exception:
                    continue
                if not r.reconciles:
                    failed.append(f"{ticker}/{tf}/{sname} div=${r.reconcile_divergence:+.2f}")
                    continue
                train, test = partition_train_test(r, train_pct, n_bars=len(df))
                # IMPORTANT: compute_full_stats uses BOTH the trades list
                # AND the equity_curve. We pass the FULL backtest result
                # so all derived metrics (win%, max DD, DD days, recovery,
                # consec streaks, R:R, avg win/loss) are coherent — they
                # all describe the same equity path.
                #
                # Train/test split is still visible via the train_PF /
                # test_PF / train_R / test_R / test_ret_pct columns
                # which use partition_train_test (which DOES re-derive
                # PnL on the trade-list slice, no equity-curve mixing).
                stats = compute_full_stats(r, starting_balance=91_400)
                rows.append({
                    "ticker": ticker, "tf": tf, "strategy": sname,
                    "n_train": train.n_trades,
                    "n_test": test.n_trades,
                    "win%": round(stats.win_rate_pct, 1),
                    "train_PF": round(train.profit_factor, 2)
                                   if train.profit_factor != float("inf")
                                   else 9.99,
                    "test_PF": round(test.profit_factor, 2)
                                  if test.profit_factor != float("inf")
                                  else 9.99,
                    "train_R": round(train.avg_R, 3),
                    "test_R": round(test.avg_R, 3),
                    "netP&L$": round(stats.sum_realized),
                    "maxDD%": round(stats.max_dd_pct, 1),
                    "maxDD$": round(stats.max_dd_dollars),
                    "DDdays": round(stats.max_dd_duration_days),
                    "recovD": (None if stats.recovery_duration_days is None
                                else round(stats.recovery_duration_days)),
                    "consL": stats.max_consec_losses,
                    "consW": stats.max_consec_wins,
                    "rr": round(stats.risk_reward_ratio, 2)
                            if stats.risk_reward_ratio != float("inf")
                            else 9.99,
                    "avgWin$": round(stats.avg_win_dollars),
                    "avgLoss$": round(stats.avg_loss_dollars),
                    "test_ret_pct": round(test.sum_pnl / 91_400 * 100.0, 2),
                })
    prog.empty()
    if failed:
        st.error("⛔  Cells that failed reconciliation (divergence shown):\n\n"
                  + "\n".join(f"- {f}" for f in failed[:20]))
    if not rows:
        st.warning("No cells produced results.")
        return
    df_grid = pd.DataFrame(rows).sort_values("test_R", ascending=False)
    df_grid["test_PF"] = df_grid["test_PF"].replace(float("inf"), 9.99)
    df_grid["train_PF"] = df_grid["train_PF"].replace(float("inf"), 9.99)
    st.markdown(f"**{len(df_grid)} cells** sorted by test_R")
    st.dataframe(df_grid, width="stretch", height=380)
    if len(df_grid) >= 2:
        st.plotly_chart(heatmap_test_R(df_grid), width="stretch")


def main():
    st.set_page_config(page_title="Backtest", page_icon="📊", layout="wide")
    st.title("📊  Backtest")
    st.caption("Single-strategy reconciliation-enforced backtest. Time guards "
                "are opt-in here; a published 'survivor' should reconcile WITH "
                "guards on so paper/live behaves the same.")
    cfg = load_config()
    strats = discover_strategies()
    data = discover_data()
    render_backtest_section(strats, data, cfg)
    render_sweep_section(strats, data, cfg)


if __name__ == "__main__":
    main()
else:
    main()
