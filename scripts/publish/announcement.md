# Announcement copy — pick one

## SHORT (X / Twitter — under 280 chars)

> Just open-sourced mt5_quant_trader_v2 — production-grade MetaTrader 5
> quant platform. FTMO-aware, 13 invariants enforced, 539 tests, autonomous
> portfolio optimizer, Streamlit dashboard. MIT.
>
> https://github.com/REPLACE_OWNER/mt5_quant_trader_v2
> 
> #algotrading #ftmo #quant

(266 chars when REPLACE_OWNER is "yourname")

---

## MEDIUM (LinkedIn / Reddit)

> **mt5_quant_trader_v2 — production-grade MT5 quant platform, now open source**
>
> After months of iteration I've open-sourced the full FTMO-aware research
> + execution platform I've been building.
>
> What's in it:
> • Reconciliation-gated backtester ($0.01 tolerance, hard fail otherwise)
> • Autonomous (strategy × ticker × timeframe × R:R) portfolio optimizer
>   with 30-day FTMO Monte-Carlo
> • Streamlit dashboard with multi-account support, st.dialog modals
>   for backtest/Go-Live, position manager, equity history, activity log
> • 8 pre-flight gates before a single live order goes out (incl. market-open check)
> • Time guards (weekend-flat, daily-close-flat, FTMO pre-close in user TZ)
> • 539+ passing tests, 13 hard invariants, ruff-clean
>
> MIT licensed. Not financial advice. Past backtest performance does not
> predict live results.
>
> https://github.com/REPLACE_OWNER/mt5_quant_trader_v2
>
> #algorithmictrading #ftmo #propfirm #quant #python #metatrader5

---

## LONG (thread, 5 tweets)

**1/5** I just open-sourced mt5_quant_trader_v2 — a production-grade
MetaTrader 5 quant trading research platform built around one rule:
every dollar the dashboard shows must reconcile with the equity curve.
If they don't match, the result is rejected. 🧵

**2/5** Out of that one invariant fall twelve more: replay-parity,
single PnL calculator, idempotency, no-silent-fallbacks, 8 pre-flight
gates before live, reproducibility (seeded RNG), time-based forced
exits, account isolation, arithmetic risk floors.

**3/5** The autonomous optimizer sweeps every (strategy × ticker × tf
× R:R variant), runs each through the reconciliation-gated backtester,
computes drawdown / recovery days / R:R / max consec losses / CAGR,
plus a 30-day FTMO Monte-Carlo. Top D1 cells: 90-98% pass rate.

**4/5** Dashboard: Streamlit on :8502. Auto-detects MT5 account, shows
"+X% to win" when below FTMO baseline (so you know what climb you need),
detects manual closes done in MT5 terminal, never auto-closes phantom
positions. Real st.dialog modals for backtest with equity + DD curves.

**5/5** 539 tests pass in <6s. Synthetic fixtures for everything
broker-related so the suite runs offline. MIT license. Not financial
advice; trading is risky; past backtests don't predict live results.

🔗 https://github.com/REPLACE_OWNER/mt5_quant_trader_v2

#quant #algotrading #ftmo #python #metatrader5

---

## Hashtag set (use what fits)

- Primary: #algotrading #algorithmictrading #quant #quantitativetrading
- Platform: #metatrader5 #mt5 #python #streamlit
- Audience: #ftmo #propfirm #propfirmtrading #fundedaccount
- Discovery: #opensource #fintech #tradingstrategy

---

## How to actually post via Make.com

If you have a Make.com webhook for X/Twitter, hit it with:

```bash
curl -X POST https://hook.eu1.make.com/<YOUR_HOOK_ID> \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Just open-sourced mt5_quant_trader_v2 — production-grade MetaTrader 5 quant platform. FTMO-aware, 13 invariants enforced, 539 tests, autonomous portfolio optimizer, Streamlit dashboard. MIT.\n\nhttps://github.com/REPLACE_OWNER/mt5_quant_trader_v2\n\n#algotrading #ftmo #quant"
  }'
```

I don't have your webhook URL or auth — populate the URL above and the
post fires. If you'd rather post manually, just paste any of the
versions above into X/Twitter directly.
