#!/bin/bash
# NoteKarLo launcher for macOS and Linux. First run sets everything up (a few minutes, once);
# later runs start in seconds.
#   ./start.sh          → opens http://127.0.0.1:8765 in Chrome
#   ./start.sh --lan    → also lets other devices on your Wi-Fi open the read-only /view page
# Options (environment variables):
#   PORT=8777                          use another port
#   NOTEKARLO_WHISPER_MODEL=small      faster speech model for slow CPUs (Linux; default large-v3-turbo)
#   NOTEKARLO_NO_BROWSER=1             don't open the browser
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
URL="http://127.0.0.1:$PORT"
OS="${NOTEKARLO_FORCE_OS:-$(uname -s)}"   # Darwin | Linux
PLATFORM_TAG="$OS-$(uname -m)"
if [ "$OS" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then FLAVOR=mac; else FLAVOR=linux; fi

say() { printf '%s\n' "$*"; }

open_browser() {
  [ -n "$NOTEKARLO_NO_BROWSER" ] && return 0
  if [ "$OS" = "Darwin" ]; then
    open -a "Google Chrome" "$URL" 2>/dev/null || open "$URL"
    return 0
  fi
  for b in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge brave-browser; do
    if command -v "$b" >/dev/null 2>&1; then
      nohup "$b" "$URL" >/dev/null 2>&1 &
      return 0
    fi
  done
  xdg-open "$URL" >/dev/null 2>&1 || say "Open $URL in Chrome."
}

pick_python() {
  for p in python3.12 python3.11 python3.13 python3.10 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      command -v "$p"
      return 0
    fi
  done
  return 1
}

is_up() {
  "$1" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1)' "$URL/api/state" >/dev/null 2>&1
}

# 0) A .venv copied from another computer/OS will not run here — rebuild it.
if [ -d .venv ] && [ "$(cat .venv/.notekarlo-platform 2>/dev/null)" != "$PLATFORM_TAG-$FLAVOR" ]; then
  if [ -x .venv/bin/python ] && [ ! -f .venv/.notekarlo-platform ] && [ "$FLAVOR" = mac ] && [ "$OS" = "Darwin" ]; then
    echo "$PLATFORM_TAG-$FLAVOR" > .venv/.notekarlo-platform   # created by an older start.sh on this Mac
  else
    say "▶ Rebuilding the Python environment for this computer…"
    rm -rf .venv
  fi
fi

# 1) Python environment
if [ ! -x .venv/bin/python ]; then
  say "▶ First-time setup: creating the Python environment ($FLAVOR)…"
  if command -v uv >/dev/null 2>&1 && [ -z "$NOTEKARLO_NO_UV" ]; then
    uv venv --python 3.12 .venv
    PIP=(uv pip install --python .venv/bin/python)
  else
    PY="$(pick_python)" || { say "✗ Python 3.9+ not found. Install it (Ubuntu: sudo apt install python3 python3-venv)."; exit 1; }
    if ! "$PY" -m venv .venv; then
      rm -rf .venv
      say "✗ Could not create a virtual environment."
      say "  On Ubuntu/Debian run:  sudo apt install python3-venv   (or python3.X-venv for your version)"
      exit 1
    fi
    .venv/bin/python -m pip install -q --upgrade pip
    PIP=(.venv/bin/python -m pip install)
  fi
  "${PIP[@]}" -r requirements.txt -r "requirements-$FLAVOR.txt"
  if [ "$FLAVOR" = mac ]; then
    "${PIP[@]}" --no-deps "mlx-whisper==0.4.3"
  fi
  echo "$PLATFORM_TAG-$FLAVOR" > .venv/.notekarlo-platform
fi

# 2) System-audio capture (Zoom/Teams desktop apps). Optional — Chrome tab capture always works.
if [ "$OS" = "Darwin" ]; then
  if [ ! -x bin/notekarlo-sysaudio ] || [ native/SystemAudioTap.swift -nt bin/notekarlo-sysaudio ]; then
    if xcrun --find swiftc >/dev/null 2>&1; then
      say "▶ Building the Mac system-audio helper (one time, ~1 min)…"
      mkdir -p bin
      xcrun swiftc -O -o bin/notekarlo-sysaudio native/SystemAudioTap.swift \
        || say "  (build failed — the 'System audio' option will be disabled; Chrome tab capture still works)"
    else
      say "  (Xcode command line tools not found — 'System audio' disabled. Install with: xcode-select --install)"
    fi
  fi
else
  if ! command -v parec >/dev/null 2>&1 && ! command -v ffmpeg >/dev/null 2>&1; then
    say "  (For the 'System audio' option install PulseAudio tools: sudo apt install pulseaudio-utils)"
  fi
fi

# 3) Claude CLI
if ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ] && [ ! -x "$HOME/.npm-global/bin/claude" ]; then
  say "⚠  Claude CLI not found. Install Claude Code and run 'claude' once to log in — notes need it."
fi

# 4) Already running? Just open it.
if is_up .venv/bin/python; then
  say "NoteKarLo is already running → opening $URL"
  open_browser
  exit 0
fi

# 5) Speech model (downloaded once, then cached: ~0.9 GB on Mac, ~0.8-1.6 GB on Linux)
say "▶ Checking the speech model (first run downloads it — can take a few minutes)…"
.venv/bin/python -c "from server.stt import prepare_model; print('▶ Speech model ready:', prepare_model())"

( for _ in $(seq 1 120); do sleep 0.5; is_up .venv/bin/python && break; done; open_browser ) &

say ""
say "  ✍  NoteKarLo is running at $URL"
say "     Keep this window open during the meeting. Ctrl+C to quit."
say ""
exec .venv/bin/python -m server.app --port "$PORT" "$@"
