"""Captures everything the computer is playing (Zoom/Teams/Meet desktop apps) and streams it into the engine.

  * macOS  — the native `notekarlo-sysaudio` helper (Core Audio process tap).
  * Linux  — PulseAudio / PipeWire "monitor" of the default output device, via `parec`
             (or `ffmpeg -f pulse` if parec is missing). No permission prompt needed.

Every backend writes 16 kHz mono float32 PCM to stdout.
"""

import asyncio
import logging
import shutil
import subprocess
import sys
import time

import numpy as np

from .config import BIN_DIR

log = logging.getLogger("notekarlo.sysaudio")
HELPER = BIN_DIR / "notekarlo-sysaudio"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

MAC_HINT = (
    "System audio is silent. If people are talking, give permission: System Settings → "
    "Privacy & Security → Screen & System Audio Recording → 'System Audio Recording Only' → "
    "turn on the app you started NoteKarLo from (e.g. Terminal), then restart NoteKarLo. "
    "Or use 'Chrome tab' capture instead."
)
LINUX_HINT = (
    "System audio is silent. If people are talking, make sure the meeting app plays through your "
    "default output device (Settings → Sound → Output). With headphones, select them as the default "
    "output before starting. Or use 'Chrome tab' capture instead."
)


def linux_monitor_source() -> str:
    """Name of the monitor source for the default speakers/headphones."""
    if shutil.which("pactl"):
        try:
            sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=3).stdout.strip()
            if not sink:
                info = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=3).stdout
                sink = next((l.split(":", 1)[1].strip() for l in info.splitlines() if l.startswith("Default Sink:")), "")
            if sink:
                return f"{sink}.monitor"
        except Exception:
            pass
    return "@DEFAULT_MONITOR@"


def capture_command() -> list[str] | None:
    if IS_MAC:
        return [str(HELPER)] if HELPER.exists() else None
    if IS_LINUX:
        source = linux_monitor_source()
        if shutil.which("parec"):
            return ["parec", "-d", source, "--format=float32le", "--rate=16000", "--channels=1", "--raw", "--latency-msec=100"]
        if shutil.which("ffmpeg"):
            return ["ffmpeg", "-loglevel", "error", "-f", "pulse", "-i", source, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    return None


class SystemAudio:
    def __init__(self, hub):
        self.hub = hub
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.started_at = 0.0
        self.heard_sound = False
        self.warned = False

    @staticmethod
    def available() -> bool:
        if IS_MAC:
            return HELPER.exists()
        if IS_LINUX:
            return bool(shutil.which("parec") or shutil.which("ffmpeg"))
        return False

    @staticmethod
    def unavailable_reason() -> str:
        if IS_MAC:
            return "System audio helper is not built (needs macOS 14.2+ and Xcode command line tools). Run ./start.sh again."
        if IS_LINUX:
            return "Install PulseAudio tools for system audio: sudo apt install pulseaudio-utils"
        return "System audio capture is not supported on this OS — use Chrome tab capture."

    async def start(self, on_samples):
        await self.stop()
        cmd = capture_command()
        if not cmd:
            await self.hub.send({"type": "status", "error": self.unavailable_reason()})
            return
        log.info("system audio: %s", " ".join(cmd))
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.started_at = time.time()
        self.heard_sound = False
        self.warned = False
        self.task = asyncio.create_task(self._pump(on_samples))
        asyncio.create_task(self._watch_stderr())
        if not IS_MAC:  # parec/ffmpeg print nothing on success
            await self.hub.send({"type": "status", "sysaudio": "running"})

    async def _watch_stderr(self):
        proc = self.proc
        if not proc:
            return
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            log.info("sysaudio: %s", line)
            if line.startswith("READY"):
                await self.hub.send({"type": "status", "sysaudio": "running"})
            else:
                msg = line[6:] if line.startswith("ERROR") else line
                await self.hub.send({"type": "status", "error": "System audio: " + msg})

    async def _pump(self, on_samples):
        proc = self.proc
        leftover = b""
        try:
            while proc and proc.stdout:
                chunk = await proc.stdout.read(6400)  # 100 ms
                if not chunk:
                    break
                data = leftover + chunk
                usable = len(data) - len(data) % 4
                leftover = data[usable:]
                samples = np.frombuffer(data[:usable], dtype=np.float32)
                if not self.heard_sound and samples.size and np.abs(samples).max() > 1e-4:
                    self.heard_sound = True
                if not self.heard_sound and not self.warned and time.time() - self.started_at > 20:
                    self.warned = True
                    event = {"type": "status", "warning": MAC_HINT if IS_MAC else LINUX_HINT}
                    if IS_MAC:
                        event["action"] = "privacy"
                    await self.hub.send(event)
                on_samples(samples)
        except Exception:
            log.exception("system audio pump failed")
        finally:
            await self.hub.send({"type": "status", "sysaudio": "stopped"})

    async def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 3)
            except Exception:
                proc.kill()
        if self.task:
            self.task.cancel()
            self.task = None
