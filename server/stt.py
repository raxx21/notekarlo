"""Local speech-to-text with Whisper — audio never leaves the computer.

Two engines behind one interface:
  * "mlx"    — MLX Whisper on Apple-silicon Macs (GPU).
  * "faster" — faster-whisper (CTranslate2) on Linux/Windows/Intel Macs: CPU int8, or CUDA if present.
Override with NOTEKARLO_STT=mlx|faster and NOTEKARLO_WHISPER_MODEL=<name>.
"""

import difflib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .config import MODELS_DIR, SAMPLE_RATE, WHISPER_REPO

log = logging.getLogger("notekarlo.stt")

# Whisper writes Hindi in Roman letters when it is told the language is English and is shown
# a Roman-Hinglish example. English speech stays English. This is the "hinglish" mode.
HINGLISH_STYLE = (
    "Haan theek hai, toh demo mein login flow thoda slow tha. Usko fix karo, "
    "aur note kar lo, Friday tak dashboard ka naya version chahiye."
)

HALLUCINATIONS = [
    "thank you for watching", "thanks for watching", "please subscribe", "like and subscribe",
    "subscribe to", "see you in the next", "subtitles by", "transcribed by", "amara.org",
    "धन्यवाद", "शुक्रिया", "सब्सक्राइब",
]
SHORT_FILLERS = {"thank you.", "thank you", "thanks.", "you", "bye.", "bye", "okay.", "hmm.", "so"}


def pick_backend() -> str:
    forced = os.environ.get("NOTEKARLO_STT", "").strip().lower()
    if forced in ("mlx", "faster"):
        return forced
    if sys.platform == "darwin" and platform.machine() == "arm64":
        try:
            import mlx_whisper  # noqa: F401
            return "mlx"
        except ImportError:
            pass
    return "faster"


BACKEND = pick_backend()
# faster-whisper models (Linux). "auto" = large-v3-turbo if this computer runs it fast enough for live
# notes, else "small" (~5x faster on CPU; Claude cleans up its extra mistakes). "medium" is skipped on
# purpose: in tests it hallucinated on Hinglish with the Roman-script prompt.
ACCURATE_MODEL = "large-v3-turbo"
FAST_MODEL = "small"
AUTO_LIMIT_SEC = 3.5  # a turbo pass slower than this can't feel "live" on this CPU
STT_CHOICES = {
    "auto": "Auto (recommended)",
    "accurate": "Accurate — large-v3-turbo",
    "fast": "Fast — small (for slow laptops)",
}


def forced_model() -> str | None:
    return os.environ.get("NOTEKARLO_WHISPER_MODEL") or None


def models_for(choice: str) -> list[str]:
    if forced_model():
        return [forced_model()]
    return {"accurate": [ACCURATE_MODEL], "fast": [FAST_MODEL]}.get(choice, [ACCURATE_MODEL, FAST_MODEL])


def resolve_model(repo: str = WHISPER_REPO) -> str:
    """MLX: download the model once and return a local folder mlx_whisper can load.

    Newer mlx-community repos ship `model.safetensors`; mlx_whisper expects
    `weights.safetensors`, so we expose the snapshot through a folder of symlinks.
    """
    from huggingface_hub import snapshot_download

    snap = Path(snapshot_download(repo_id=repo))
    if (snap / "weights.safetensors").exists() or (snap / "weights.npz").exists():
        return str(snap)
    local = MODELS_DIR / repo.split("/")[-1]
    local.mkdir(parents=True, exist_ok=True)
    for src, dst in (("config.json", "config.json"), ("model.safetensors", "weights.safetensors")):
        link = local / dst
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(snap / src)
    return str(local)


def prepare_model(choice: str | None = None) -> str:
    """Download the speech model(s) for this computer (used by start.sh before the server starts)."""
    if BACKEND == "mlx":
        return f"MLX · {resolve_model()}"
    from faster_whisper.utils import download_model

    if choice is None:
        from .config import load_profile

        choice = load_profile().get("stt_model", "auto")
    names = models_for(choice)
    for name in names:
        download_model(name)
    return "faster-whisper · " + " + ".join(names)


def faster_device() -> tuple[str, str]:
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "int8_float16"
    except Exception:
        pass
    return "cpu", "int8"


def cpu_threads() -> int:
    """Physical cores, roughly. Hyper-threads / efficiency cores make Whisper slower, not faster."""
    if os.environ.get("NOTEKARLO_CPU_THREADS"):
        return max(1, int(os.environ["NOTEKARLO_CPU_THREADS"]))
    n = os.cpu_count() or 4
    return max(2, min(8, n // 2 if n >= 8 else n))


def decode_file(path: str) -> np.ndarray:
    """Any audio/video file -> 16 kHz mono float32. Uses ffmpeg if installed, else PyAV (bundled with faster-whisper)."""
    if shutil.which("ffmpeg"):
        raw = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, check=True,
        ).stdout
        return np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype=np.float32)
    import av  # PyAV ships with faster-whisper

    chunks = []
    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)


class Transcriber:
    def __init__(self):
        self.backend = BACKEND
        self.model_path: str | None = None
        self.model = None
        self.model_name = ACCURATE_MODEL
        self.choice = "auto"
        self.note = ""  # why auto picked what it picked
        self.device = "gpu" if BACKEND == "mlx" else "cpu"
        self.ready = threading.Event()
        self.error: str | None = None

    @property
    def description(self) -> str:
        if self.backend == "mlx":
            return "Whisper large-v3-turbo (MLX, Mac GPU)"
        text = f"Whisper {self.model_name} (faster-whisper, {self.device.upper()})"
        return f"{text} — {self.note}" if self.note else text

    def load(self, choice: str = "auto"):
        """Blocking. Call from the STT worker thread. `choice`: auto | accurate | fast (Linux only)."""
        self.ready.clear()
        self.error = None
        self.choice = choice
        try:
            if self.backend == "mlx":
                import mlx_whisper

                self.model_path = resolve_model()
                mlx_whisper.transcribe(np.zeros(16000, np.float32), path_or_hf_repo=self.model_path, language="en")
            else:
                self._load_faster(choice)
            self.ready.set()
            log.info("Speech model ready: %s", self.description)
        except Exception as e:  # surfaced in the UI
            self.error = f"Speech model failed to load: {e}"
            log.exception("whisper load failed")

    def _load_faster(self, choice: str):
        from faster_whisper import WhisperModel

        self.device, compute = faster_device()
        threads = cpu_threads()
        names = models_for(choice)
        self.note = ""
        if len(names) > 1 and self.device == "cuda":
            names = names[:1]  # a GPU runs the accurate model live with ease
        self.model = None
        for i, name in enumerate(names):
            self.model = WhisperModel(name, device=self.device, compute_type=compute, cpu_threads=threads)
            self.model_name = name
            took = self._benchmark()
            log.info("%s: one pass takes %.1fs on %s (%d threads)", name, took, self.device, threads)
            if i == len(names) - 1 or took <= AUTO_LIMIT_SEC:
                break
            self.note = f"auto-picked: this CPU needs {took:.0f}s per pass for {name}"
            self.model = None
        if choice == "auto" and self.model_name == names[0]:
            self.note = ""

    def _benchmark(self) -> float:
        """Time one realistic pass (Whisper always encodes a 30 s window, so content barely matters)."""
        rng = np.random.default_rng(0)
        audio = (rng.normal(0, 0.02, 16000 * 8)).astype(np.float32)
        list(self.model.transcribe(audio[:16000], language="en", beam_size=1)[0])  # warm-up
        start = time.time()
        list(self.model.transcribe(audio, language="en", beam_size=1, without_timestamps=True)[0])
        return time.time() - start

    def transcribe(self, audio: np.ndarray, mode: str = "hinglish", glossary: str = "") -> str:
        if not self.ready.is_set():
            raise RuntimeError(self.error or "speech model not loaded")
        if audio.size < 8000:
            audio = np.pad(audio, (0, 8000 - audio.size))
        prompt = None
        language = None
        if mode == "hinglish":
            language = "en"
            prompt = HINGLISH_STYLE
        if glossary.strip():
            terms = glossary.strip().replace("\n", ", ")[:300]
            prompt = f"{prompt or ''} Names and terms: {terms}.".strip()
        if self.backend == "mlx":
            segments = self._run_mlx(audio, language, prompt)
        else:
            segments = self._run_faster(audio, language, prompt)
        pieces = []
        for seg in segments:
            text = seg["text"].strip()
            if not text:
                continue
            if seg["no_speech_prob"] > 0.6 and seg["avg_logprob"] < -0.8:
                continue
            if seg["compression_ratio"] > 2.4:
                continue
            pieces.append(text)
        return clean_text(" ".join(pieces), prompt or "")

    def _run_mlx(self, audio, language, prompt) -> list[dict]:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model_path,
            language=language,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            temperature=(0.0, 0.2, 0.4),
            compression_ratio_threshold=2.2,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
        )
        return [
            {
                "text": s.get("text", ""),
                "no_speech_prob": s.get("no_speech_prob", 0.0),
                "avg_logprob": s.get("avg_logprob", 0.0),
                "compression_ratio": s.get("compression_ratio", 0.0),
            }
            for s in result.get("segments", [])
        ]

    def _run_faster(self, audio, language, prompt) -> list[dict]:
        segments, _info = self.model.transcribe(
            audio,
            language=language,
            initial_prompt=prompt,
            beam_size=1 if self.device == "cpu" else 5,  # greedy is ~2x faster on CPU, near-identical here
            temperature=[0.0, 0.2, 0.4],
            compression_ratio_threshold=2.2,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,  # we already cut audio into utterances
        )
        return [
            {
                "text": s.text,
                "no_speech_prob": s.no_speech_prob,
                "avg_logprob": s.avg_logprob,
                "compression_ratio": s.compression_ratio,
            }
            for s in segments
        ]


def clean_text(text: str, prompt: str = "") -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    low = text.lower()
    if any(h in low for h in HALLUCINATIONS):
        return ""
    if low in SHORT_FILLERS:
        return ""
    # Whisper sometimes echoes its own prompt back on near-silent audio.
    if prompt and difflib.SequenceMatcher(None, low, prompt.lower()).ratio() > 0.6:
        return ""
    text = collapse_repeats(text)
    return text


def collapse_repeats(text: str) -> str:
    """'abc abc abc abc' -> 'abc' (classic Whisper loop)."""
    words = text.split()
    for n in range(1, 6):
        out = []
        i = 0
        while i < len(words):
            chunk = words[i:i + n]
            j = i + n
            reps = 1
            while len(chunk) == n and words[j:j + n] == chunk:
                reps += 1
                j += n
            if reps >= 3:
                out.extend(chunk)
                i = j
            else:
                out.append(words[i])
                i += 1
        words = out
    return " ".join(words)
