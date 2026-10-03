#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${ADB:-}" ]]; then
  ADB="$ADB"
elif command -v adb >/dev/null 2>&1; then
  ADB="$(command -v adb)"
elif [[ -n "${ANDROID_HOME:-}" && -x "$ANDROID_HOME/platform-tools/adb" ]]; then
  ADB="$ANDROID_HOME/platform-tools/adb"
elif [[ -x "$HOME/Library/Android/sdk/platform-tools/adb" ]]; then
  ADB="$HOME/Library/Android/sdk/platform-tools/adb"
else
  echo "adb was not found. Set ADB or ANDROID_HOME." >&2
  exit 1
fi
PACKAGE="ai.openphone.agent"
CAPTURE_DIR="${CAPTURE_DIR:-captures}"

state() {
  "$ADB" shell run-as "$PACKAGE" cat files/display-state.json 2>/dev/null
}

display_id() {
  state | sed -n 's/.*"displayId": \([-0-9]*\).*/\1/p'
}

require_display() {
  local id displays
  id="$(display_id)"
  if [[ -z "$id" || "$id" == "-1" ]]; then
    echo "Agent display is not running. Open the app and tap Start agent display." >&2
    exit 1
  fi
  displays="$("$ADB" shell cmd display get-displays 2>/dev/null)"
  if [[ "$displays" != *"Display id $id:"* ]]; then
    echo "Agent display $id no longer exists. Open the app and tap Start agent display again." >&2
    exit 1
  fi
  printf '%s' "$id"
}

case "${1:-help}" in
  state)
    state
    ;;
  open-browser|open-chrome)
    id="$(require_display)"
    url="${2:-https://www.wikipedia.org/}"
    "$ADB" shell am start \
      --display "$id" \
      --activity-clear-top \
      -n "$PACKAGE/.AgentBrowserActivity" \
      -a android.intent.action.VIEW \
      -d "$url"
    ;;
  tap)
    id="$(require_display)"
    "$ADB" shell input -d "$id" tap "${2:?x required}" "${3:?y required}"
    ;;
  swipe)
    id="$(require_display)"
    "$ADB" shell input -d "$id" swipe \
      "${2:?x1 required}" "${3:?y1 required}" \
      "${4:?x2 required}" "${5:?y2 required}" "${6:-300}"
    ;;
  text)
    id="$(require_display)"
    "$ADB" shell input -d "$id" text "${2:?text required}"
    ;;
  key)
    id="$(require_display)"
    "$ADB" shell input -d "$id" keyevent "${2:?keycode required}"
    ;;
  capture)
    mkdir -p "$CAPTURE_DIR"
    target="$CAPTURE_DIR/agent-display-$(date +%Y%m%d-%H%M%S).png"
    "$ADB" shell run-as "$PACKAGE" cat files/display/latest.png > "$target"
    echo "$target"
    ;;
  help|*)
    cat <<'EOF'
Usage: ./scripts/agentctl.sh COMMAND

  state                         Show display ID and status
  open-browser [URL]            Launch the isolated browser on the agent display
  open-chrome [URL]             Backward-compatible alias for open-browser
  tap X Y                       Tap only the agent display
  swipe X1 Y1 X2 Y2 [DURATION] Swipe only the agent display
  text TEXT                     Type on the agent display
  key KEYCODE                   Send a key event to the agent display
  capture                       Save the latest agent-display screenshot
EOF
    ;;
esac
