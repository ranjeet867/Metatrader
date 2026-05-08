#!/usr/bin/env bash
# install_news_refresh_launchagent.sh — schedule daily ForexFactory
# news calendar refresh so the runner's blackout cache stays fresh
# without manual intervention.
#
# Why: the news_blackout gate reads data/news_calendar.json. Stale
# cache (>12h old) triggers an auto-refresh on first read, but during
# vacation the cache may go stale between runner queries. This
# LaunchAgent forces a fresh pull every day at 06:00 local time so
# every trading day starts with current week's events.

set -e
trap 'echo "Aborted."; exit 0' INT TERM

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYBIN="${REPO_DIR}/.venv/bin/python"
SCRIPT="${REPO_DIR}/scripts/refresh_news_calendar.py"
PLIST="${HOME}/Library/LaunchAgents/com.user.mt5_news_refresh.plist"

if [[ ! -x "$PYBIN" ]]; then
  echo "⛔ venv not found at $PYBIN — run 'make setup' first."
  exit 1
fi
if [[ ! -f "$SCRIPT" ]]; then
  echo "⛔ refresh script not found at $SCRIPT"
  exit 1
fi

# TCC-aware shim outside ~/Documents
INSTALL_DIR="${HOME}/Library/Application Support/mt5_quant_trader"
mkdir -p "$INSTALL_DIR"
SHIM="${INSTALL_DIR}/news_refresh_launcher.sh"
cat > "$SHIM" <<SHIMEOF
#!/usr/bin/env bash
# Auto-generated. Refreshes data/news_calendar.json daily.
cd "${REPO_DIR}"
exec "${PYBIN}" "${SCRIPT}" >> /tmp/mt5_news_refresh.log 2>> /tmp/mt5_news_refresh.err
SHIMEOF
chmod +x "$SHIM"

# Schedule: every day at 06:00 local time (before US/EU sessions open)
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.user.mt5_news_refresh</string>
  <key>ProgramArguments</key>
  <array>
    <string>${SHIM}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>6</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/mt5_news_refresh.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/mt5_news_refresh.err</string>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "✅ News-refresh LaunchAgent installed:"
echo "   plist:    $PLIST"
echo "   schedule: every day at 06:00 local time"
echo "   logs:     /tmp/mt5_news_refresh.log, /tmp/mt5_news_refresh.err"
echo ""
echo "Manual trigger (test the wiring):"
echo "  bash $SHIM"
echo ""
echo "Uninstall:"
echo "  launchctl unload \"$PLIST\" && rm \"$PLIST\""
