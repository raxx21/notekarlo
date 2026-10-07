"""Builds a synthetic two-channel Hinglish demo-review meeting with macOS voices (for testing).

Writes tests/audio/them.wav (manager/CEO side) and tests/audio/me.wav (the user's mic),
time-aligned so they can be streamed together with scripts/simulate.py.
"""

import subprocess
import tempfile
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "audio"
SR = 16000

SCRIPT = [
    ("them", "Lekha", "चलो राजेश, डेमो शुरू करो। आज हम नया ऑनबोर्डिंग फ्लो देखेंगे।"),
    ("me", "Aman", "Sure sir, I will share my screen. This is the new onboarding flow with three steps."),
    ("them", "Lekha", "ठीक है। पहली बात, साइनअप पेज पर बहुत सारे फील्ड्स हैं। यूज़र से सिर्फ़ ईमेल और फ़ोन नंबर लो, बाकी बाद में पूछो।"),
    ("them", "Rishi", "Also the loading time is too high. The dashboard took almost five seconds to load. That is not acceptable for the client demo."),
    ("them", "Lekha", "हाँ, और नोट कर लो, शुक्रवार तक डैशबोर्ड का लोडिंग टाइम दो सेकंड से कम होना चाहिए।"),
    ("me", "Lekha", "जी सर, मैं एपीआई में कैशिंग लगा दूँगा और इमेजेस को कंप्रेस कर दूँगा।"),
    ("them", "Rishi", "Good. The color theme looks nice, I like the new design. Keep that."),
    ("them", "Lekha", "एक और चीज़, प्राइसिंग पेज पर कस्टमर टेस्टिमोनियल्स ऐड करो। प्रिया ने बोला है कि लॉन्च से पहले यह ज़रूरी है।"),
    ("them", "Lekha", "और फ़ैसला यह हुआ कि लॉन्च पंद्रह अक्टूबर को होगा, उससे पहले क्लाइंट को एक बार फिर डेमो देंगे।"),
    ("me", "Aman", "Sir, should I also add the Hindi language option in onboarding?"),
    ("them", "Lekha", "वो अभी पक्का नहीं है, मैं प्रिया से पूछ के बताता हूँ।"),
]


def synth(voice: str, text: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "x.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        raw = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", str(aiff), "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
            capture_output=True, check=True,
        ).stdout
    return np.frombuffer(raw, dtype="<i2")


def main():
    OUT.mkdir(exist_ok=True)
    tracks = {"them": [], "me": []}
    gap = np.zeros(int(SR * 0.9), dtype="<i2")
    for who, voice, text in SCRIPT:
        clip = synth(voice, text)
        silence = np.zeros_like(clip)
        for ch in tracks:
            tracks[ch].append(clip if ch == who else silence)
            tracks[ch].append(gap)
    for ch, parts in tracks.items():
        audio = np.concatenate(parts)
        path = OUT / f"{ch}.wav"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-", str(path)],
            input=audio.tobytes(), check=True,
        )
        print(f"{path}  {audio.size / SR:.1f}s")


if __name__ == "__main__":
    main()
