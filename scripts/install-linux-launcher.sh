#!/bin/bash
# Adds "NoteKarLo" to the Linux app menu / dock. It opens a terminal window running start.sh.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/notekarlo.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=NoteKarLo
Comment=Live Hinglish meeting notes
Exec=bash "$DIR/start.sh"
Path=$DIR
Icon=$DIR/web/icon.svg
Terminal=true
Categories=Office;AudioVideo;
DESKTOP
chmod +x "$APPS/notekarlo.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "✓ NoteKarLo added to your app menu (search for 'NoteKarLo')."
