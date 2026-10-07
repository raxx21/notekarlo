#!/bin/bash
# Builds dist/notekarlo.zip — the project without anything computer-specific (Python env, models,
# compiled helper, your meetings and profile). Copy it to another computer, unzip, run start.sh.
set -e
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="$(basename "$PROJECT")"
OUT="$PROJECT/dist/notekarlo.zip"
mkdir -p "$PROJECT/dist"
rm -f "$OUT"
cd "$(dirname "$PROJECT")"
zip -r -q "$OUT" "$NAME" \
  -x "$NAME/.venv/*" "$NAME/models/*" "$NAME/bin/*" "$NAME/meetings/*" "$NAME/data/*" "$NAME/dist/*" \
     "$NAME/.claude/*" "$NAME/tests/audio/*" "*/__pycache__/*" "*.DS_Store"
# the two sample tracks used for the self-test (scripts/simulate.py)
zip -q "$OUT" "$NAME/tests/audio/them.wav" "$NAME/tests/audio/me.wav"
echo "✓ $OUT ($(du -h "$OUT" | cut -f1))"
