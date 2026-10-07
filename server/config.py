"""Paths, defaults and the saved user profile."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
MEETINGS_DIR = ROOT / "meetings"
MODELS_DIR = ROOT / "models"
BIN_DIR = ROOT / "bin"
CLAUDE_WORKDIR = DATA_DIR / "claude-workdir"
PROFILE_PATH = DATA_DIR / "profile.json"

SAMPLE_RATE = 16000

for _d in (DATA_DIR, MEETINGS_DIR, MODELS_DIR, CLAUDE_WORKDIR):
    _d.mkdir(parents=True, exist_ok=True)

# Speech-to-text model (MLX Whisper). 8-bit large-v3-turbo: ~0.9 GB, fast on M-series Macs.
WHISPER_REPO = os.environ.get("NOTEKARLO_WHISPER", "mlx-community/whisper-large-v3-turbo-8bit")

# How often the notes are refreshed while people are talking.
UPDATE_SPEEDS = {
    "fast": 12,      # seconds between Claude updates
    "balanced": 25,
    "saver": 60,
}

MODELS = {
    "sonnet": "Sonnet (best Hinglish, recommended)",
    "haiku": "Haiku (fastest, uses least quota)",
    "opus": "Opus (most careful, slowest)",
}


def _first_name() -> str:
    """The user's first name from the OS account (macOS: id -F, Linux: the GECOS field)."""
    try:
        if sys.platform == "darwin":
            full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=2).stdout.strip()
        else:
            import pwd

            full = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
        return full.split()[0] if full else ""
    except Exception:
        return ""


DEFAULT_PROFILE = {
    "my_name": _first_name() or "Me",
    "people": "",          # e.g. "Manager: Amit, CEO: Priya"
    "glossary": "",        # product names, client names, technical terms (helps spelling)
    "model": "sonnet",
    "speed": "balanced",
    "stt_mode": "hinglish",  # "hinglish" = Roman-script transcript, "auto" = Whisper decides script
    "stt_model": "auto",     # Linux: auto | accurate | fast  (macOS always uses MLX turbo)
}


def load_profile() -> dict:
    profile = dict(DEFAULT_PROFILE)
    if PROFILE_PATH.exists():
        try:
            profile.update(json.loads(PROFILE_PATH.read_text()))
        except Exception:
            pass
    return profile


def save_profile(update: dict) -> dict:
    profile = load_profile()
    for key in DEFAULT_PROFILE:
        if key in update and update[key] is not None:
            profile[key] = update[key]
    write_json(PROFILE_PATH, profile)
    return profile


def write_json(path: Path, data) -> None:
    """Atomic write so a crash mid-meeting never leaves a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)
