#!/usr/bin/env bash
# keep_mt5_alive.sh — keep MetaTrader 5 running on macOS.
#
# Why MT5 keeps closing on macOS:
#   1. App Nap — macOS suspends apps without visible windows.
#   2. Energy Saver — battery / lid-close puts the system to sleep.
#   3. Memory-pressure killer — OS kills "non-essential" GUI apps under RAM stress.
#   4. No auto-restart — if MT5 crashes there's nothing to restart it.
#
# What this script does:
#   • Disables App Nap for MetaTrader 5 (one-time defaults write)
#   • Runs `caffeinate -dimsu` to prevent display + system sleep while it's alive
#   • Restarts MT5 every 30s if it's not running
#   • Stops cleanly on Ctrl+C
#
# USAGE:
#   ./scripts/keep_mt5_alive.sh                 # uses default MT5 path
#   ./scripts/keep_mt5_alive.sh --no-relaunch   # only disable nap + caffeinate
#   ./scripts/keep_mt5_alive.sh --check         # just print current state
#   ./scripts/keep_mt5_alive.sh --setup-only    # disable App Nap, then exit
#
# To run forever in the background:
#   nohup ./scripts/keep_mt5_alive.sh > /tmp/mt5_alive.log 2>&1 &
#
# Or set up a LaunchAgent (best — survives reboots):
#   ./scripts/keep_mt5_alive.sh --install-launchagent

set -e
trap 'echo "Stopping…"; exit 0' INT TERM

MT5_APP_NAME="MetaTrader 5"
MT5_BUNDLE_ID="com.metaquotes.MetaTrader.5"
MT5_APP_PATH="/Applications/${MT5_APP_NAME}.app"
CHECK_INTERVAL=30
RELAUNCH=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-relaunch) RELAUNCH=false; shift ;;
    --check) MODE="check"; shift ;;
    --setup-only) MODE="setup"; shift ;;
    --install-launchagent) MODE="launchagent"; shift ;;
    --interval) CHECK_INTERVAL="$2"; shift 2 ;;
    --path) MT5_APP_PATH="$2"; shift 2 ;;
    -h|--help)
      sed -n '1,30p' "$0" | grep '^#'
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ----- Locate MT5 -----
if [[ ! -d "$MT5_APP_PATH" ]]; then
  # Try alternative install paths
  for p in \
    "/Applications/MetaTrader 5.app" \
    "/Applications/MetaTrader 5/MetaTrader 5.app" \
    "$HOME/Applications/MetaTrader 5.app"; do
    if [[ -d "$p" ]]; then MT5_APP_PATH="$p"; break; fi
  done
fi
if [[ ! -d "$MT5_APP_PATH" ]]; then
  echo "⛔ MetaTrader 5.app not found. Pass --path /full/path/to/MetaTrader\ 5.app"
  exit 1
fi

echo "📍 MT5 location: $MT5_APP_PATH"

# ----- check mode -----
if [[ "$MODE" == "check" ]]; then
  if pgrep -f "MetaTrader 5" >/dev/null; then
    PID=$(pgrep -f "MetaTrader 5" | head -1)
    echo "✅ MT5 is running (pid=$PID)"
    NAP=$(defaults read "$MT5_BUNDLE_ID" NSAppSleepDisabled 2>/dev/null || echo "?")
    echo "   App Nap disabled: $NAP (1=yes, 0=no)"
  else
    echo "⛔ MT5 is NOT running."
  fi
  exit 0
fi

# ----- Disable App Nap -----
echo "⚙  Disabling App Nap for ${MT5_BUNDLE_ID}…"
defaults write "${MT5_BUNDLE_ID}" NSAppSleepDisabled -bool YES 2>/dev/null \
  || defaults write "${MT5_APP_NAME}" NSAppSleepDisabled -bool YES 2>/dev/null \
  || true
echo "   (must restart MT5 once for App Nap setting to take effect)"

if [[ "$MODE" == "setup" ]]; then
  echo "✅ Setup complete. Exit."
  exit 0
fi

# ----- LaunchAgent install -----
if [[ "$MODE" == "launchagent" ]]; then
  PLIST="$HOME/Library/LaunchAgents/com.user.keep_mt5_alive.plist"
  # IMPORTANT: macOS Privacy controls block LaunchAgents from accessing
  # ~/Documents, ~/Desktop, and ~/Downloads without explicit user grant.
  # If we point the plist at $REPO/scripts/keep_mt5_alive.sh (which lives
  # under ~/Documents), the agent fails at startup with
  #   "Operation not permitted"
  # in /tmp/mt5_alive.err. So we COPY the script to a non-protected
  # location and reference THAT path. Re-installing each time picks up
  # any edits to the source script.
  INSTALL_DIR="$HOME/Library/Application Support/mt5_quant_trader"
  mkdir -p "$INSTALL_DIR"
  INSTALLED_SCRIPT="$INSTALL_DIR/keep_mt5_alive.sh"
  cp "$(realpath "$0")" "$INSTALLED_SCRIPT"
  chmod +x "$INSTALLED_SCRIPT"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.keep_mt5_alive</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALLED_SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/mt5_alive.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mt5_alive.err</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "✅ LaunchAgent installed: $PLIST"
  echo "   Script copied to: $INSTALLED_SCRIPT"
  echo "   (Required — macOS blocks LaunchAgents from ~/Documents.)"
  echo "   It will start at login and restart on crash."
  echo "   To uninstall: launchctl unload \"$PLIST\" && rm \"$PLIST\""
  echo "                  rm -rf \"$INSTALL_DIR\""
  exit 0
fi

# ----- Foreground keep-alive loop -----
echo "🛡  Keep-alive loop starting (check every ${CHECK_INTERVAL}s)."
echo "    Press Ctrl+C to stop."
echo "    Tip: laptop lid CLOSED while plugged-in is OK"
echo "    (caffeinate -dim handles that)."

# caffeinate keeps display + system + idle awake while THIS script is alive.
# -d  prevent display sleep
# -i  prevent idle sleep
# -m  prevent disk sleep
# -s  prevent system sleep on AC
# -u  declare user is active (5s default)
caffeinate -dimsu &
CAF_PID=$!
trap "kill $CAF_PID 2>/dev/null; echo Stopped.; exit 0" INT TERM

while true; do
  if pgrep -f "MetaTrader 5" >/dev/null; then
    : # all good, do nothing
  else
    if $RELAUNCH; then
      echo "$(date '+%H:%M:%S') ⚠ MT5 not running — launching $MT5_APP_PATH"
      open -a "$MT5_APP_PATH"
    else
      echo "$(date '+%H:%M:%S') ⚠ MT5 not running (--no-relaunch set)."
    fi
  fi
  sleep "$CHECK_INTERVAL"
done
