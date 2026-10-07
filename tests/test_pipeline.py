"""Offline tests (no Claude calls, no Whisper): segmentation, echo detection, filters, notes ops.

    .venv/bin/python -m unittest tests.test_pipeline -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from server import engine as engine_mod
from server.engine import TRIGGER_RE, Meeting
from server.notes import NotesState
from server.segmenter import Segmenter
from server.stt import HINGLISH_STYLE, clean_text, collapse_repeats, decode_file

AUDIO = Path(__file__).parent / "audio"


def load(name: str) -> np.ndarray:
    return decode_file(str(AUDIO / name))


def segment_all(audio: np.ndarray, channel="x"):
    seg = Segmenter(channel)
    out = []
    for i in range(0, audio.size, 1600):
        out += seg.push(audio[i:i + 1600])
    return out + seg.flush()


class SegmenterTest(unittest.TestCase):
    def test_one_segment_per_utterance(self):
        noise = np.random.default_rng(0).normal(0, 0.003, 1).astype(np.float32)
        them = load("them.wav") + noise
        me = load("me.wav") + noise
        self.assertEqual(len(segment_all(them)), 8)
        self.assertEqual(len(segment_all(me)), 3)

    def test_silence_gives_nothing(self):
        silence = np.random.default_rng(1).normal(0, 0.002, 16000 * 10).astype(np.float32)
        self.assertEqual(segment_all(silence), [])

    def test_long_monologue_is_cut(self):
        rng = np.random.default_rng(2)
        t = np.arange(16000 * 40) / 16000
        # 40 s of talking: syllable-like bursts with short (0.3 s) breaths every 2 s, never a long pause
        envelope = (np.sin(2 * np.pi * 4 * t) > -0.3) * ((t % 2.0) < 1.7)
        speech = (0.2 * np.sin(2 * np.pi * 180 * t) * envelope).astype(np.float32)
        speech += rng.normal(0, 0.002, speech.size).astype(np.float32)
        segs = segment_all(speech)
        self.assertGreaterEqual(len(segs), 3)
        self.assertTrue(all(s.end - s.start <= 14.1 for s in segs))


class EchoTest(unittest.TestCase):
    def test_speaker_echo_is_skipped_but_own_voice_kept(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine_mod, "MEETINGS_DIR", Path(tmp)):
            m = Meeting("echo test", "", {"mic": True, "meeting": "tab"}, live=False)
            them, mic = load("them.wav"), load("me_with_echo.wav")
            kept = {"mic": [], "meeting": []}
            for i in range(0, min(them.size, mic.size), 1600):
                kept["meeting"] += m.push_audio("meeting", them[i:i + 1600])
                kept["mic"] += m.push_audio("mic", mic[i:i + 1600])
            starts = [round(s.start, 1) for s in kept["mic"]]
            self.assertEqual(len(kept["meeting"]), 8)
            self.assertEqual(len(kept["mic"]), 3, f"mic clips kept at {starts}")

    def test_room_mode_never_drops(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine_mod, "MEETINGS_DIR", Path(tmp)):
            m = Meeting("room", "", {"mic": True, "meeting": "none"}, live=False)
            mic = load("me_with_echo.wav")
            segs = []
            for i in range(0, mic.size, 1600):
                segs += m.push_audio("mic", mic[i:i + 1600])
            self.assertGreaterEqual(len(segs), 10)


class FilterTest(unittest.TestCase):
    def test_hallucinations_removed(self):
        self.assertEqual(clean_text("Thank you for watching!"), "")
        self.assertEqual(clean_text("Thank you."), "")
        self.assertEqual(clean_text(HINGLISH_STYLE, HINGLISH_STYLE), "")
        self.assertEqual(clean_text("Login flow slow tha."), "Login flow slow tha.")

    def test_repeat_collapse(self):
        self.assertEqual(collapse_repeats("haan haan haan haan theek hai"), "haan theek hai")
        self.assertEqual(collapse_repeats("a b a b a b c"), "a b c")
        self.assertEqual(collapse_repeats("no repeats in this one"), "no repeats in this one")

    def test_trigger_phrases(self):
        for text in ["Haan, aur note kar lo, Friday tak", "isko likh lo", "please note down the numbers",
                     "Yaad rakhna yeh", "नोट कर लो", "write this down"]:
            self.assertTrue(TRIGGER_RE.search(text), text)
        for text in ["notebook mein dekho", "the dashboard is slow"]:
            self.assertFalse(TRIGGER_RE.search(text), text)


class LinuxSystemAudioTest(unittest.TestCase):
    def test_parec_records_default_sink_monitor(self):
        from server import sysaudio

        def which(name):
            return f"/usr/bin/{name}" if name in ("pactl", "parec") else None

        fake = mock.Mock(stdout="alsa_output.pci-0000_00_1f.3.analog-stereo\n")
        with mock.patch.object(sysaudio, "IS_MAC", False), mock.patch.object(sysaudio, "IS_LINUX", True), \
                mock.patch.object(sysaudio.shutil, "which", side_effect=which), \
                mock.patch.object(sysaudio.subprocess, "run", return_value=fake):
            cmd = sysaudio.capture_command()
            self.assertTrue(sysaudio.SystemAudio.available())
        self.assertEqual(cmd[:3], ["parec", "-d", "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"])
        self.assertIn("--format=float32le", cmd)
        self.assertIn("--rate=16000", cmd)

    def test_ffmpeg_fallback_and_unavailable(self):
        from server import sysaudio

        with mock.patch.object(sysaudio, "IS_MAC", False), mock.patch.object(sysaudio, "IS_LINUX", True):
            with mock.patch.object(sysaudio.shutil, "which", side_effect=lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None):
                cmd = sysaudio.capture_command()
                self.assertEqual(cmd[:5], ["ffmpeg", "-loglevel", "error", "-f", "pulse"])
                self.assertIn("@DEFAULT_MONITOR@", cmd)
            with mock.patch.object(sysaudio.shutil, "which", return_value=None):
                self.assertIsNone(sysaudio.capture_command())
                self.assertFalse(sysaudio.SystemAudio.available())
                self.assertIn("pulseaudio-utils", sysaudio.SystemAudio.unavailable_reason())


class NotesTest(unittest.TestCase):
    def test_ops(self):
        n = NotesState()
        changed = n.apply_ops([
            {"op": "add", "section": "actions", "text": "Login flow fix karna hai", "owner": "Rajesh"},
            {"op": "add", "section": "feedback", "text": "Demo accha tha"},
            {"op": "add", "section": "actions", "text": "Login flow fix karna hai"},  # duplicate
        ])
        self.assertEqual(changed, ["a1", "f1"])
        n.apply_ops([{"op": "update", "id": "a1", "due": "Friday tak", "important": True}])
        self.assertEqual(n.get("a1")["due"], "Friday tak")
        self.assertTrue(n.get("a1")["important"])
        n.edit_by_user("f1", {"text": "Demo bahut accha tha"})
        n.apply_ops([{"op": "update", "id": "f1", "text": "changed by AI"}, {"op": "remove", "id": "f1"}])
        self.assertEqual(n.get("f1")["text"], "Demo bahut accha tha")  # user edits are protected
        n.apply_ops([{"op": "remove", "id": "a1"}])
        self.assertIsNone(n.get("a1"))
        self.assertIn("Demo bahut accha tha", n.to_markdown("T", "d"))


if __name__ == "__main__":
    unittest.main()
