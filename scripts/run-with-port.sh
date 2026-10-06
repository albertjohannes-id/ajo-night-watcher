#!/bin/bash
# run-with-port.sh — run any command on a desired port, with a friendly
# conflict modal when the port is already taken.
#
# Usage:
#   ./scripts/run-with-port.sh [options] <port> -- <command...>
#
# Examples:
#   ./scripts/run-with-port.sh 3000 -- npm run dev
#   ./scripts/run-with-port.sh 3000 -- python3 -m http.server '$PORT'
#   ./scripts/run-with-port.sh --next 3000 -- npm run dev   # auto-use next free
#   ./scripts/run-with-port.sh --kill 3000 -- npm run dev   # auto-terminate occupant
#
# Interactive behaviour (default --ask):
#   Port 3000 is currently used by:
#     PID 1234 — node server.js
#   [T]erminate :3000 / [U]se :3001 (first free, checked) / [C]ancel
#
# On macOS with a GUI session it shows a native dialog modal via osascript;
# otherwise (SSH, CI, --no-gui) it falls back to a terminal prompt.
# Non-interactive flags (--kill / --next) are CI-safe and never prompt.
#
# Only stdlib/macOS built-ins: bash, lsof, ps, kill, osascript. No deps.
set -euo pipefail

MODE="ask"
USE_GUI="auto"
MAX_BUMP=20

usage() {
  cat <<'EOF'
run-with-port.sh — run any command on a desired port, with a friendly
conflict modal when the port is already taken.

Usage:
  ./scripts/run-with-port.sh [options] <port> -- <command...>

Examples:
  ./scripts/run-with-port.sh 3000 -- npm run dev
  ./scripts/run-with-port.sh 3000 -- python3 -m http.server '$PORT'
  ./scripts/run-with-port.sh --next 3000 -- npm run dev   # auto-use next free
  ./scripts/run-with-port.sh --kill 3000 -- npm run dev   # auto-terminate occupant
EOF
  echo ""
  echo "Options:"
  echo "  --ask        interactive modal/prompt (default)"
  echo "  --kill       non-interactive: terminate occupant, then run"
  echo "  --next       non-interactive: run on next free port, then run"
  echo "  --gui        force macOS dialog modal"
  echo "  --no-gui     force terminal prompt (for SSH/CI)"
  echo "  -h, --help   show this help"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ask) MODE="ask"; shift ;;
    --kill) MODE="kill"; shift ;;
    --next) MODE="next"; shift ;;
    --gui) USE_GUI="yes"; shift ;;
    --no-gui) USE_GUI="no"; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) break ;;
  esac
done

if [[ $# -lt 1 ]]; then
  echo "Missing <port>." >&2; usage >&2; exit 2
fi
WANT_PORT="$1"; shift
if [[ "$WANT_PORT" =~ ^[0-9]+$ ]] && (( WANT_PORT >= 1 && WANT_PORT <= 65535 )); then
  :
else
  echo "Invalid port: $WANT_PORT (expected 1-65535)." >&2; exit 2
fi
if [[ $# -gt 0 && "$1" == "--" ]]; then shift; fi
if [[ $# -eq 0 ]]; then
  echo "Missing <command> — usage: run-with-port.sh $WANT_PORT -- <command...>" >&2; exit 2
fi

port_pids() {
  # Print one PID per line listening on TCP $1. Empty when free.
  lsof -ti "TCP:$1" -sTCP:LISTEN 2>/dev/null || true
}

port_is_free() {
  [[ -z "$(port_pids "$1")" ]]
}

describe_occupant() {
  # Human-readable "PID … — cmd args" lines for a port.
  local port="$1" pid line comm args
  local out=""
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    # ps output is "comm args…"; fall back to lsof command name if ps fails
    # (process may have exited between lsof and ps).
    if line="$(ps -p "$pid" -o comm= -o args= 2>/dev/null)"; then
      comm="$(echo "$line" | awk '{print $1}')"
      args="$(echo "$line" | cut -d' ' -f2-)"
      [[ -n "$args" ]] || args="$comm"
      out+="  PID $pid — $args"$'\n'
    else
      out+="  PID $pid — (exited)"$'\n'
    fi
  done < <(port_pids "$port")
  printf '%s' "$out"
}

find_next_free() {
  # Echo first free port starting at $1, scanning up to $1+$MAX_BUMP.
  local p="$1" i
  for (( i = 0; i <= MAX_BUMP; i++ )); do
    if port_is_free "$p"; then echo "$p"; return 0; fi
    p=$(( p + 1 ))
    if (( p > 65535 )); then break; fi
  done
  return 1
}

terminate_port() {
  # SIGTERM, wait, then SIGKILL fallback. Returns 0 when port is free.
  local port="$1" pid tries
  local pids
  pids="$(port_pids "$port")"
  [[ -n "$pids" ]] || return 0
  echo "Terminating process(es) on port $port: $(echo "$pids" | tr '\n' ' ')"
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    if [[ "$pid" == "1" ]]; then
      echo "Refusing to kill PID 1." >&2; return 1
    fi
    kill "$pid" 2>/dev/null || true
  done <<< "$pids"
  for (( tries = 0; tries < 10; tries++ )); do
    sleep 0.3
    pids="$(port_pids "$port")"
    [[ -n "$pids" ]] || return 0
  done
  # Still busy — escalate.
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill -9 "$pid" 2>/dev/null || true
  done <<< "$pids"
  sleep 0.5
  if port_is_free "$port"; then return 0; fi
  echo "Port $port is still busy after terminate." >&2
  describe_occupant "$port" >&2
  return 1
}

gui_available() {
  # True when a macOS GUI dialog can be shown.
  [[ "$(uname)" == "Darwin" ]] || return 1
  command -v osascript >/dev/null || return 1
  # No GUI in SSH sessions without a window server.
  [[ -n "${SSH_TTY:-}${SSH_CLIENT:-}${SSH_CONNECTION:-}" ]] && return 1
  return 0
}

ask_gui() {
  # macOS dialog modal. Echoes one of: kill / next / cancel.
  # $1 = wanted port, $2 = occupant description, $3 = next free port (or "").
  local port="$1" occupant="$2" next="$3"
  local use_label="Use $next"
  [[ -n "$next" && "$next" != "$port" ]] || use_label="Use next free port"
  # Escape for AppleScript string literal.
  local msg="Port $port is currently used by:\n$occupant\nDo you want to terminate that process, or run on another port?"
  msg="${msg//\\/\\\\}"
  msg="${msg//\"/\\\"}"
  msg="$(printf '%s' "$msg" | tr '\n' '|')" # occupant already has newlines; keep readable
  local result
  if result="$(osascript -e "set msg to \"$msg\"
set msg to my replace(msg, \"|\", return)
set b to button returned of (display dialog msg buttons {\"Cancel\", \"$use_label\", \"Terminate $port\"} default button \"$use_label\" cancel button \"Cancel\" with title \"Port already in use\" with icon caution)
if b = \"Terminate $port\" then return \"kill\"
if b = \"$use_label\" then return \"next\"
return \"cancel\"
on replace(t, s, r)
  set AppleScript's text item delimiters to s
  set parts to text items of t
  set AppleScript's text item delimiters to r
  return parts as string
end replace" 2>/dev/null)"; then
    echo "$result" | tr '[:upper:]' '[:lower:]'
  else
    echo "cancel"
  fi
}

ask_terminal() {
  # Terminal prompt. Echoes one of: kill / next / cancel.
  local port="$1" occupant="$2" next="$3"
  echo "" >&2
  echo "Port $port is currently used by:" >&2
  printf '%s' "$occupant" >&2
  [[ "$occupant" != *$'\n' ]] && echo "" >&2
  local choice=""
  if [[ -n "$next" && "$next" != "$port" ]]; then
    echo "  [T]erminate :$port   [U]se :$next (first free, checked)   [C]ancel" >&2
    printf 'Choose [T/u/c] (default U): ' >&2
    read -r choice < /dev/tty || choice=""
    case "${choice:-U}" in
      [Tt]*) echo "kill" ;;
      [Uu]*|"") echo "next" ;;
      *) echo "cancel" ;;
    esac
  else
    echo "  [T]erminate :$port   [C]ancel (no free port nearby)" >&2
    printf 'Choose [T/c] (default C): ' >&2
    read -r choice < /dev/tty || choice=""
    case "$choice" in
      [Tt]*) echo "kill" ;;
      *) echo "cancel" ;;
    esac
  fi
}

run_on() {
  local port="$1"; shift
  export PORT="$port"
  echo "Starting on port $port: $* (PORT=$port)"
  exec "$@"
}

# --- main ---
if port_is_free "$WANT_PORT"; then
  run_on "$WANT_PORT" "$@"
fi

OCCUPANT="$(describe_occupant "$WANT_PORT")"
[[ -n "$OCCUPANT" ]] || OCCUPANT="  (unknown process — it may have just exited; retrying)"$'\n'
NEXT="$(find_next_free $(( WANT_PORT + 1 )) || true)"

case "$MODE" in
  kill)
    echo "Port $WANT_PORT is currently used by:" >&2
    printf '%s\n' "$OCCUPANT" >&2
    terminate_port "$WANT_PORT"
    run_on "$WANT_PORT" "$@"
    ;;
  next)
    if [[ -z "$NEXT" ]]; then
      echo "Port $WANT_PORT is busy and no free port found up to $(( WANT_PORT + MAX_BUMP ))." >&2
      printf '%s\n' "$OCCUPANT" >&2
      exit 1
    fi
    echo "Port $WANT_PORT is busy; using next free port $NEXT." >&2
    run_on "$NEXT" "$@"
    ;;
  ask)
    DECISION="cancel"
    if [[ "$USE_GUI" == "yes" ]] || { [[ "$USE_GUI" == "auto" ]] && gui_available && [[ -t 0 || -t 1 ]]; }; then
      DECISION="$(ask_gui "$WANT_PORT" "$OCCUPANT" "$NEXT")"
      # Dialog dismissed / Cancel button → osascript errors; ask_gui maps to cancel.
      # If stdout isn't a TTY we still honour the dialog choice.
      if [[ "$DECISION" == "cancel" && "$USE_GUI" == "yes" ]]; then
        echo "Cancelled." >&2; exit 130
      fi
      # If dialog couldn't decide (e.g. headless), fall through to terminal when possible.
      if [[ "$DECISION" == "cancel" ]] && [[ -t 0 ]]; then
        DECISION="$(ask_terminal "$WANT_PORT" "$OCCUPANT" "$NEXT")"
      fi
    elif [[ -t 0 ]]; then
      DECISION="$(ask_terminal "$WANT_PORT" "$OCCUPANT" "$NEXT")"
    else
      echo "Port $WANT_PORT is currently used by:" >&2
      printf '%s\n' "$OCCUPANT" >&2
      if [[ -n "$NEXT" ]]; then
        echo "Non-interactive shell: re-run with --kill (terminate :$WANT_PORT) or --next (use :$NEXT)." >&2
      else
        echo "Non-interactive shell: re-run with --kill to terminate :$WANT_PORT." >&2
      fi
      exit 1
    fi

    case "$DECISION" in
      kill)
        terminate_port "$WANT_PORT"
        run_on "$WANT_PORT" "$@"
        ;;
      next)
        if [[ -z "$NEXT" ]]; then
          echo "No free port found near $WANT_PORT." >&2; exit 1
        fi
        run_on "$NEXT" "$@"
        ;;
      *) echo "Cancelled — port $WANT_PORT left untouched." >&2; exit 130 ;;
    esac
    ;;
esac
