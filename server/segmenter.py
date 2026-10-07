"""Energy-based voice activity detection.

Turns a live 16 kHz mono stream into utterance-sized chunks (roughly one sentence each)
so Whisper always gets whole phrases instead of words cut in half.
"""

from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import SAMPLE_RATE

FRAME = 480  # 30 ms at 16 kHz
FRAME_SEC = FRAME / SAMPLE_RATE


@dataclass
class Segment:
    channel: str
    start: float  # seconds on the channel clock
    end: float
    audio: np.ndarray


class Segmenter:
    def __init__(
        self,
        channel: str,
        end_silence: float = 0.7,   # pause that ends an utterance
        min_speech: float = 0.5,    # ignore coughs / clicks shorter than this
        max_len: float = 14.0,      # force a cut in long monologues
        preroll: float = 0.3,       # keep a little audio before speech starts
    ):
        self.channel = channel
        self.end_silence_frames = int(end_silence / FRAME_SEC)
        self.min_speech_frames = int(min_speech / FRAME_SEC)
        self.max_frames = int(max_len / FRAME_SEC)
        self.preroll: deque = deque(maxlen=int(preroll / FRAME_SEC))
        self.pending = np.zeros(0, np.float32)
        self.frames: list[np.ndarray] = []
        self.energies: list[float] = []
        self.flags: list[bool] = []  # was each frame speech *at the time it arrived*
        self.in_speech = False
        self.onset = 0
        self.silence_run = 0
        self.speech_count = 0
        self.noise = 0.002
        self.pos = 0          # frames consumed so far
        self.seg_start_pos = 0
        self.level = 0.0      # recent RMS, for the UI meter
        self.history: deque = deque(maxlen=4000)  # per-frame RMS of the last ~2 min (echo detection)

    def _threshold(self) -> float:
        return max(self.noise * 2.8, 0.006)

    def push(self, samples: np.ndarray) -> list[Segment]:
        out: list[Segment] = []
        if samples.size == 0:
            return out
        data = np.concatenate([self.pending, samples.astype(np.float32, copy=False)])
        n = data.size // FRAME
        self.pending = data[n * FRAME:]
        for i in range(n):
            frame = data[i * FRAME:(i + 1) * FRAME]
            seg = self._frame(frame)
            if seg is not None:
                out.append(seg)
        return out

    def _frame(self, frame: np.ndarray):
        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
        self.history.append(rms)
        self.level = max(rms, self.level * 0.85)
        thr = self._threshold()
        speech = rms > thr
        self.pos += 1

        if not speech:
            self.noise = min(max(0.95 * self.noise + 0.05 * rms, 0.0005), 0.05)
        else:
            self.noise = min(0.9995 * self.noise + 0.0005 * rms, 0.05)

        if not self.in_speech:
            self.preroll.append((frame, rms, speech))
            self.onset = self.onset + 1 if speech else 0
            if self.onset >= 3:  # 90 ms of speech in a row
                self.in_speech = True
                self.frames = [f for f, _, _ in self.preroll]
                self.energies = [e for _, e, _ in self.preroll]
                self.flags = [sp for _, _, sp in self.preroll]
                self.seg_start_pos = self.pos - len(self.frames)
                self.preroll.clear()
                self.silence_run = 0
                self.speech_count = 3
            return None

        self.frames.append(frame)
        self.energies.append(rms)
        self.flags.append(speech)
        if speech:
            self.silence_run = 0
            self.speech_count += 1
        else:
            self.silence_run += 1

        if self.silence_run >= self.end_silence_frames:
            keep = len(self.frames) - self.silence_run + 8  # keep ~240 ms of tail
            return self._emit(keep, reset=True)
        if len(self.frames) >= self.max_frames:
            # Cut at the quietest moment in the last 2.5 s so we don't split a word.
            window = int(2.5 / FRAME_SEC)
            tail = self.energies[-window:]
            cut = len(self.frames) - window + int(np.argmin(tail)) + 1
            return self._emit(cut, reset=False)
        return None

    def _emit(self, upto: int, reset: bool):
        frames, self.frames = self.frames[:upto], self.frames[upto:]
        self.energies = self.energies[upto:]
        flags, self.flags = self.flags[:upto], self.flags[upto:]
        start_pos = self.seg_start_pos
        self.seg_start_pos += upto
        speech_frames = sum(flags)
        if reset:
            self.in_speech = False
            self.onset = 0
            self.silence_run = 0
            self.frames, self.energies, self.flags = [], [], []
        else:
            self.silence_run = 0
        self.speech_count = 0
        if speech_frames < self.min_speech_frames or not frames:
            return None
        audio = np.concatenate(frames)
        return Segment(self.channel, start_pos * FRAME_SEC, (start_pos + len(frames)) * FRAME_SEC, audio)

    def envelope(self, f0: int, f1: int) -> np.ndarray:
        """Per-frame RMS for frame indices [f0, f1) that are still in history."""
        first = self.pos - len(self.history)
        f0, f1 = max(f0, first), min(f1, self.pos)
        if f1 <= f0:
            return np.zeros(0, np.float32)
        hist = self.history
        return np.array([hist[i - first] for i in range(f0, f1)], np.float32)

    def flush(self):
        """Finish whatever is buffered (called when the meeting stops)."""
        if self.in_speech and self.frames:
            seg = self._emit(len(self.frames), reset=True)
            return [seg] if seg else []
        return []
