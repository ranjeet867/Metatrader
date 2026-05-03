# `data/` directory

This folder is **deliberately empty** in the public repo. Everything that
lives here at runtime is either (a) per-account broker data that must
not leak (FTMO login, journal, SQLite) or (b) cached price parquets
that can be regenerated from the bridge in seconds.

After cloning, populate it via:

    make refresh-data       # pull every parquet from your MT5 bridge

Per-account artifacts (`accounts.json`, `accounts/<login>/`) are
created automatically when you add an account through the dashboard's
**🔍 Auto-detect from MT5** flow.

See `docs/RUNBOOK.md` for the full operational procedure.
