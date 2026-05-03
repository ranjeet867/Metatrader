# Publishing checklist

Step-by-step from a clean shell to a public GitHub repo + announcement.

## 0. One-time prereqs

```bash
# GitHub CLI (if not already)
brew install gh
gh auth login           # follow the SSO prompt
```

## 1. Run the publisher

```bash
cd ~/Documents/mt5_quant_trader_v2
./scripts/publish/publish_to_github.sh
```

What it does:
1. Removes any stale `.git/index.lock`
2. Untracks personal data (parquets, SQLite DB, accounts/, optimization
   results, screening reports) — files stay on your disk, just not
   in git
3. Sanity-checks that no obviously-personal files are still staged
4. Commits the open-source-ready snapshot
5. Creates a public GitHub repo named `mt5_quant_trader_v2`
6. Pushes
7. Replaces `REPLACE_OWNER` in README badges with your real username
8. Adds discoverability topics: `mt5`, `metatrader5`, `quant-trading`,
   `ftmo`, `prop-firm`, `algorithmic-trading`, `backtest`, `streamlit`,
   `python`

After it finishes, watch the CI run:
`https://github.com/<YOU>/mt5_quant_trader_v2/actions`

## 2. Capture screenshots

The README references four images under `docs/screenshots/`. Capture
them once the dashboard is running:

```bash
cd ~/Documents/mt5_quant_trader_v2
mkdir -p docs/screenshots
make restart-dashboard
```

Then in your browser at `localhost:8502`:

1. **Operations page** — `localhost:8502/Operations`
   Save as `docs/screenshots/01_operations.png`
   *(Before screenshotting, blur your account login number, alias, and
   any P&L numbers if you want privacy.)*

2. **Strategy Library** — `localhost:8502/Strategy_Library`
   Save as `docs/screenshots/02_strategy_library.png`

3. **Backtest dialog** — Operations → click 📊 Backtest on a card
   Save as `docs/screenshots/03_backtest_dialog.png`

4. **Data Manager** — `localhost:8502/Data_Manager`
   Save as `docs/screenshots/04_data_manager.png`

Then commit + push:

```bash
git add docs/screenshots/
git commit -m "docs: add dashboard screenshots"
git push
```

## 3. Announce

Open `scripts/publish/announcement.md`. Pick one of the three lengths
(short / medium / thread). Replace `REPLACE_OWNER` with your GitHub
username.

### Manual posting

Paste into X/Twitter directly. Done.

### Via Make.com

If you have a Make.com webhook bound to X:

```bash
# Replace HOOK_ID with your Make.com webhook ID
curl -X POST https://hook.eu1.make.com/HOOK_ID \
  -H "Content-Type: application/json" \
  -d @scripts/publish/announcement.json
```

(Generate `announcement.json` by wrapping the chosen text into
`{"text": "..."}` — or hit the hook with whatever JSON shape your
Make scenario expects.)

I don't have your webhook ID or any Twitter/X creds, so this part is
on you.

## Privacy verification

Before pushing, verify nothing personal is in the public repo:

```bash
# Confirm no broker login appears anywhere
git ls-files | xargs grep -l '5031019095\|531019095' 2>/dev/null || echo "✓ login not found"

# Confirm no parquets, no SQLite, no account dirs
git ls-files | grep -E '\.parquet$|\.db$|^data/accounts/' && echo "⛔ FOUND" || echo "✓ clean"
```

Both should print `✓` lines.
