"""Meeting orchestration: audio -> segments -> transcript -> Claude notes -> UI + disk."""

import asyncio
import difflib
import json
import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import claude_cli, prompts
from .config import MEETINGS_DIR, SAMPLE_RATE, UPDATE_SPEEDS, load_profile, write_json
from .notes import NotesState
from .segmenter import FRAME_SEC, Segmenter
from .stt import Transcriber, decode_file

log = logging.getLogger("notekarlo.engine")

# Phrases that mean "write this down" — they auto-pin the moment.
TRIGGER_RE = re.compile(
    r"\bnote\s*(kar|kr|karo|karlo|kar\s*lo|kar\s*lena|kar\s*le|down|it|this|that|kijiye|karna)\b"
    r"|\blikh\s*(lo|lena|le|lijiye)\b|\bwrite\s+(it|this|that)\s+down\b|\bmake\s+a\s+note\b"
    r"|\byaad\s*rakh|\bremember\s+(this|that)\b|\bnoted\b"
    r"|नोट\s*कर|लिख\s*लो|लिख\s*लेना|याद\s*रख",
    re.IGNORECASE,
)

PIN_WINDOW = 45  # seconds a pin keeps influencing note updates
PIN_SETTLE = 4   # wait for the speaker to finish the sentence
MAX_BATCH_SEC = 24  # when transcription falls behind, merge queued clips up to this length
ECHO_WINDOW = 6     # seconds: a "Me" line that repeats a "Them" line this close is speaker echo


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "meeting"


def hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


class Hub:
    """Fan-out of JSON events to every open browser tab (capture page and viewers)."""

    def __init__(self):
        self.clients = set()

    async def send(self, event: dict):
        if not self.clients:
            return
        msg = json.dumps(event, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


class Meeting:
    def __init__(self, title: str, agenda: str, sources: dict, live: bool = True):
        self.profile = load_profile()
        self.title = title.strip() or "Meeting"
        self.agenda = agenda.strip()
        self.sources = sources
        self.live = live
        self.started = time.time()
        self.ended: float | None = None
        self.id = time.strftime("%Y-%m-%d_%H%M", time.localtime(self.started)) + "_" + slugify(self.title)
        self.dir = MEETINGS_DIR / self.id
        n = 2
        while self.dir.exists():
            self.dir = MEETINGS_DIR / f"{self.id}-{n}"
            n += 1
        self.id = self.dir.name
        self.dir.mkdir(parents=True)

        has_meeting_audio = sources.get("meeting", "none") != "none"
        self.labels = {"mic": "Me" if has_meeting_audio else "Room", "meeting": "Them", "file": "Room"}
        self.segmenters: dict[str, Segmenter] = {}
        self.clock: dict[str, list] = {}  # channel -> [t0, samples]
        self.transcript: list[dict] = []
        self.line_seq = 0
        self.processed = 0
        self.notes = NotesState()
        self.pins: list[dict] = []
        self.last_update = 0.0
        self.active = True
        self.update_lock = asyncio.Lock()
        self.updating = False
        self.last_error = ""
        self.mom = ""
        self.mom_running = False
        self.usage = {"calls": 0}
        self.audio_seconds = 0.0  # for imported recordings: length of the audio
        self.save_meta()

    # ---- audio -----------------------------------------------------------
    def push_audio(self, channel: str, samples: np.ndarray):
        if not self.active:
            return []
        seg = self.segmenters.get(channel)
        if seg is None:
            seg = self.segmenters[channel] = Segmenter(channel)
            self.clock[channel] = [time.time() if self.live else self.started, 0]
        clock = self.clock[channel]
        if self.live:
            drift = (time.time() - clock[1] / SAMPLE_RATE) - clock[0]
            if drift > 1.5:  # audio paused for a while (tab hidden, device switch) — catch up
                clock[0] += drift
        clock[1] += samples.size
        out = []
        for s in seg.push(samples):
            if channel == "mic" and self.mic_is_echo(s):
                log.info("skipped mic clip %.1fs — it is the meeting audio leaking from speakers", s.end - s.start)
                continue
            out.append(s)
        return out

    def envelope(self, channel: str, wall_a: float, wall_b: float) -> np.ndarray:
        seg = self.segmenters.get(channel)
        if seg is None:
            return np.zeros(0, np.float32)
        t0 = self.clock[channel][0]
        return seg.envelope(int((wall_a - t0) / FRAME_SEC), int((wall_b - t0) / FRAME_SEC))

    def mic_is_echo(self, s) -> bool:
        """True when a mic clip is just the meeting audio re-heard through laptop speakers.

        The meeting app never plays your own voice back, so if the mic's loudness pattern
        follows the meeting channel's pattern (shifted by 0-0.4 s), it is an echo."""
        if self.labels.get("mic") != "Me" or "meeting" not in self.segmenters:
            return False
        a, b = self.wall_time("mic", s.start), self.wall_time("mic", s.end)
        mic = self.envelope("mic", a, b)
        meet = self.envelope("meeting", a - 0.4, b)
        n = mic.size
        if n < 15 or meet.size < n or np.percentile(meet, 90) < 0.006:
            return False
        x = np.log10(mic + 1e-4)
        x = (x - x.mean()) / (x.std() + 1e-6)
        best = 0.0
        for k in range(0, meet.size - n + 1):
            y = np.log10(meet[k:k + n] + 1e-4)
            sd = y.std()
            if sd < 1e-3:
                continue
            best = max(best, float(np.mean(x * (y - y.mean()) / sd)))
        return best > 0.6

    def wall_time(self, channel: str, offset: float) -> float:
        return self.clock.get(channel, [self.started])[0] + offset

    def levels(self) -> dict:
        return {ch: round(s.level, 4) for ch, s in self.segmenters.items()}

    # ---- transcript -------------------------------------------------------
    def is_echo(self, channel: str, t: float, text: str) -> bool:
        """With speakers instead of headphones the mic re-hears the meeting; drop that copy."""
        if channel != "mic" or self.labels.get("mic") != "Me" or len(text) < 12:
            return False
        low = text.lower()
        for other in reversed(self.transcript[-12:]):
            if other["ch"] == "meeting" and abs(other["t"] - t) <= ECHO_WINDOW:
                if difflib.SequenceMatcher(None, low, other["text"].lower()).ratio() > 0.55:
                    return True
        return False

    def find_echo_of(self, line: dict) -> dict | None:
        """A new 'Them' line may arrive after its echoed 'Me' copy."""
        if line["ch"] != "meeting" or self.labels.get("mic") != "Me":
            return None
        low = line["text"].lower()
        for other in reversed(self.transcript[-12:]):
            if other["ch"] == "mic" and not other.get("echo") and abs(other["t"] - line["t"]) <= ECHO_WINDOW and len(other["text"]) >= 12:
                if difflib.SequenceMatcher(None, low, other["text"].lower()).ratio() > 0.55:
                    return other
        return None

    def add_line(self, channel: str, start: float, text: str) -> dict | None:
        t = self.wall_time(channel, start)
        if self.is_echo(channel, t, text):
            log.info("dropped echo line: %s", text[:60])
            return None
        self.line_seq += 1
        line = {
            "id": self.line_seq,
            "ch": channel,
            "who": self.labels.get(channel, "Room"),
            "t": t,
            "text": text,
            "trigger": bool(TRIGGER_RE.search(text)),
        }
        self.transcript.append(line)
        with open(self.dir / "transcript.jsonl", "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        if line["trigger"]:
            self.add_pin(line["t"], source="auto", text=text)
        return line

    def drop_line(self, line: dict):
        idx = self.transcript.index(line)
        self.transcript.pop(idx)
        if idx < self.processed:
            self.processed -= 1
        with open(self.dir / "transcript.jsonl", "a") as f:
            f.write(json.dumps({"drop": line["id"]}) + "\n")

    def add_pin(self, t: float | None = None, source: str = "button", text: str = "") -> dict:
        pin = {"t": t or time.time(), "source": source, "text": text, "made": time.time()}
        self.pins.append(pin)
        return pin

    def pending_pin(self) -> bool:
        now = time.time()
        return any(not p.get("used") and now - p["made"] >= PIN_SETTLE for p in self.pins)

    # ---- notes ------------------------------------------------------------
    def should_update(self) -> bool:
        new = self.transcript[self.processed:]
        if not new or self.updating:
            return False
        chars = sum(len(l["text"]) for l in new)
        if self.pending_pin():
            return True
        interval = UPDATE_SPEEDS.get(self.profile.get("speed"), 25)
        if time.time() - self.last_update >= interval and chars >= 40:
            return True
        return chars >= 1500

    def context_block(self) -> str:
        p = self.profile
        rows = [
            f"Title: {self.title}",
            f"Date: {time.strftime('%A, %d %B %Y', time.localtime(self.started))}",
            f"Me (the user): {p.get('my_name') or 'Me'}",
        ]
        if p.get("people"):
            rows.append(f"People in this meeting: {p['people']}")
        if p.get("glossary"):
            rows.append(f"Glossary (correct spellings of names/terms): {p['glossary']}")
        if self.agenda:
            rows.append(f"Agenda / user's own context: {self.agenda}")
        return "\n".join(rows)

    def _fmt_lines(self, lines) -> str:
        return "\n".join(f"[{hhmmss(l['t'])} {l['who']}] {l['text']}" for l in sorted(lines, key=lambda l: l["t"]))

    def build_update_prompt(self, upto: int) -> tuple[str, list]:
        earlier = self.transcript[: self.processed]
        new = self.transcript[self.processed: upto]
        earlier_text = self._fmt_lines(earlier)
        if len(earlier_text) > 14000:
            earlier_text = "…(older lines omitted — already in the notes)\n" + earlier_text[-14000:]
        now = time.time()
        pins = [p for p in self.pins if now - p["made"] <= PIN_WINDOW or not p.get("used")]
        me = self.profile.get("my_name") or "Me"
        parts = [
            "MEETING CONTEXT",
            self.context_block(),
            "",
            "CURRENT NOTES (ids in brackets)",
            self.notes.for_prompt(me),
            "",
            "EARLIER TRANSCRIPT (already covered by the notes — context only)",
            earlier_text or "(none)",
            "",
            "NEW TRANSCRIPT SINCE LAST UPDATE — update the notes from these lines",
            self._fmt_lines(new),
        ]
        if pins:
            parts += ["", "PINNED MOMENTS (the user signalled: 'note this down!')"]
            for p in pins:
                why = "auto-detected phrase" if p["source"] == "auto" else "user pressed the Note-this button"
                parts.append(f"- around {hhmmss(p['t'])} ({why}): capture exactly what was asked to be noted around this time and mark it important.")
        parts += ["", "Return the ops now."]
        return "\n".join(parts), pins

    async def update_notes(self, hub: Hub, final: bool = False) -> bool:
        async with self.update_lock:
            if self.processed >= len(self.transcript):
                return False
            upto = len(self.transcript)
            # Very long backlog (e.g. imported recording): do it in chunks.
            chars = 0
            for i in range(self.processed, upto):
                chars += len(self.transcript[i]["text"])
                if chars > 9000:
                    upto = i + 1
                    break
            prompt, pins = self.build_update_prompt(upto)
            me = self.profile.get("my_name") or "Me"
            self.updating = True
            await hub.send({"type": "status", "notes": "updating"})
            started = time.time()
            try:
                result = await claude_cli.run_json(
                    prompt,
                    prompts.NOTES_SYSTEM.format(me=me),
                    self.profile.get("model", "sonnet"),
                    schema=prompts.NOTES_SCHEMA,
                    timeout=120,
                )
                data = result.get("structured_output") or json.loads(result.get("result") or "{}")
            except Exception as e:
                self.last_error = str(e)
                self.last_update = time.time()  # back off before retrying
                log.warning("notes update failed: %s", e)
                await hub.send({"type": "status", "notes": "error", "error": f"Notes update failed: {e}"})
                return False
            finally:
                self.updating = False
            self.usage["calls"] += 1
            changed = self.notes.apply_ops(data.get("ops", []))
            if data.get("current_topic"):
                self.notes.topic = data["current_topic"].strip()
            if data.get("summary"):
                self.notes.summary = data["summary"].strip()
            self.processed = upto
            self.last_update = time.time()
            self.last_error = ""
            for p in pins:
                if time.time() - p["made"] >= PIN_SETTLE:
                    p["used"] = True
            self.save_notes()
            await hub.send({
                "type": "notes",
                "notes": self.notes.to_dict(),
                "changed": changed,
                "took": round(time.time() - started, 1),
            })
            await hub.send({"type": "status", "notes": "idle"})
            return True

    # ---- persistence ------------------------------------------------------
    def meta(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "agenda": self.agenda,
            "started": self.started,
            "ended": self.ended,
            "sources": self.sources,
            "live": self.live,
            "profile": {k: self.profile.get(k) for k in ("my_name", "people", "glossary", "model", "stt_mode")},
            "lines": len(self.transcript),
        }

    def save_meta(self):
        write_json(self.dir / "meta.json", self.meta())

    def save_notes(self):
        write_json(self.dir / "notes.json", self.notes.to_dict())
        date = time.strftime("%d %b %Y, %H:%M", time.localtime(self.started))
        (self.dir / "notes.md").write_text(self.notes.to_markdown(self.title, date))

    def snapshot(self) -> dict:
        return {
            "meta": self.meta(),
            "active": self.active,
            "transcript": self.transcript[-400:],
            "notes": self.notes.to_dict(),
            "mom": self.mom,
            "labels": self.labels,
        }

    def transcript_text(self, limit: int = 120000) -> str:
        text = self._fmt_lines(self.transcript)
        return text if len(text) <= limit else "…\n" + text[-limit:]

    def duration_text(self) -> str:
        secs = int((self.ended or time.time()) - self.started)
        if secs < 3600:
            return f"{max(1, round(secs / 60))} min"
        return f"{secs // 3600}h {round((secs % 3600) / 60)}m"


class Engine:
    def __init__(self):
        self.hub = Hub()
        self.transcriber = Transcriber()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")
        self.pending: deque = deque()  # (meeting, Segment) waiting for Whisper
        self.pending_event = asyncio.Event()
        self.meeting: Meeting | None = None
        self.last_meeting: Meeting | None = None
        self.tasks: list[asyncio.Task] = []
        self.sysaudio = None  # set by app (SystemAudio)
        self.stt_busy = False
        self.slow_calls = 0
        self.warned_slow = False

    async def startup(self):
        loop = asyncio.get_running_loop()
        self.tasks.append(asyncio.create_task(self._load_model(loop)))
        self.tasks.append(asyncio.create_task(self._stt_worker(loop)))
        self.tasks.append(asyncio.create_task(self._ticker()))

    async def _load_model(self, loop, choice: str | None = None):
        choice = choice or load_profile().get("stt_model", "auto")
        await self.hub.send({"type": "status", "stt": "loading"})
        await loop.run_in_executor(self.executor, self.transcriber.load, choice)
        await self.hub.send(self.status())

    async def set_stt_model(self, choice: str):
        """Switch the Linux speech model (Settings). Queued clips wait in the executor until it is loaded."""
        if self.transcriber.backend != "faster" or choice == self.transcriber.choice:
            return
        await self._load_model(asyncio.get_running_loop(), choice)

    def status(self) -> dict:
        t = self.transcriber
        return {
            "type": "status",
            "stt": "ready" if t.ready.is_set() else ("error" if t.error else "loading"),
            "stt_error": t.error,
            "stt_model": t.description,
            "queue": len(self.pending),
            "claude": bool(claude_cli.CLAUDE_BIN),
        }

    # ---- audio in --------------------------------------------------------
    def feed(self, channel: str, samples: np.ndarray):
        m = self.meeting
        if not m or not m.active:
            return
        for seg in m.push_audio(channel, samples):
            self.enqueue(m, seg)

    def enqueue(self, meeting, seg):
        self.pending.append((meeting, seg))
        self.pending_event.set()

    async def drain(self):
        """Wait until every queued clip has been transcribed."""
        while self.pending or self.stt_busy:
            await asyncio.sleep(0.2)

    def _next_batch(self):
        """Pop the next clip, plus any later clips of the same speaker channel that are already waiting
        (up to MAX_BATCH_SEC). Whisper costs about the same per call for 5 s or 25 s of audio, so when we
        are behind real time (slow CPU) this catches up several times faster. Each channel keeps its order."""
        meeting, seg = self.pending.popleft()
        parts, total, count = [seg.audio], seg.end - seg.start, 1
        gap = np.zeros(int(0.25 * SAMPLE_RATE), np.float32)
        rest = deque()
        while self.pending:
            item = self.pending.popleft()
            nm, nxt = item
            same = nm is meeting and nxt.channel == seg.channel
            if same and total + (nxt.end - nxt.start) <= MAX_BATCH_SEC:
                parts += [gap, nxt.audio]
                total += nxt.end - nxt.start
                count += 1
                continue
            rest.append(item)
            if same:  # doesn't fit — stop here so this channel's later clips stay in order
                rest.extend(self.pending)
                self.pending.clear()
        self.pending.extend(rest)
        audio = np.concatenate(parts) if count > 1 else seg.audio
        return meeting, seg, audio, total, count

    async def _stt_worker(self, loop):
        while True:
            while not self.pending:
                self.pending_event.clear()
                await self.pending_event.wait()
            if not self.transcriber.ready.is_set():
                if self.transcriber.error:
                    self.pending.clear()
                    continue
                await loop.run_in_executor(None, self.transcriber.ready.wait, 900)
                continue
            self.stt_busy = True
            try:
                meeting, seg, audio, seconds, count = self._next_batch()
                started = time.time()
                text = await loop.run_in_executor(
                    self.executor,
                    self.transcriber.transcribe,
                    audio,
                    meeting.profile.get("stt_mode", "hinglish"),
                    meeting.profile.get("glossary", ""),
                )
                took = time.time() - started
                log.info("stt %.1fs audio (%d clip%s) in %.2fs, %d waiting",
                         seconds, count, "s" if count > 1 else "", took, len(self.pending))
                await self._check_speed(meeting, seconds, took)
                if text:
                    line = meeting.add_line(seg.channel, seg.start, text)
                    if line:
                        await self.hub.send({"type": "line", "line": line, "meeting": meeting.id})
                        echo = meeting.find_echo_of(line)
                        if echo:
                            meeting.drop_line(echo)
                            await self.hub.send({"type": "line_drop", "id": echo["id"], "meeting": meeting.id})
            except Exception as e:
                log.exception("transcription failed")
                await self.hub.send({"type": "status", "error": f"Transcription error: {e}"})
            finally:
                self.stt_busy = False

    async def _check_speed(self, meeting, seconds: float, took: float):
        """Live meetings only: tell the user once if this computer transcribes slower than people talk."""
        if not meeting.live or self.warned_slow:
            return
        self.slow_calls = self.slow_calls + 1 if (took > seconds and len(self.pending) >= 3) else 0
        if self.slow_calls >= 4:
            self.warned_slow = True
            event = {"type": "status", "warning":
                     "Speech-to-text is slower than real time on this computer, so notes will lag behind."}
            if self.transcriber.backend == "faster" and self.transcriber.model_name != "small":
                event["warning"] += " Switch to the fast speech model (a little less accurate — Claude still cleans it up)."
                event["action"] = "fast_model"

    async def _ticker(self):
        """Levels for the meters + decides when to refresh notes."""
        tick = 0
        while True:
            await asyncio.sleep(0.25)
            tick += 1
            m = self.meeting
            if not m:
                continue
            if m.active:
                await self.hub.send({"type": "levels", "levels": m.levels(), "queue": len(self.pending)})
            if tick % 4 == 0 and m.active and m.live and m.should_update():
                asyncio.create_task(m.update_notes(self.hub))

    # ---- meeting lifecycle -------------------------------------------------
    async def start_meeting(self, title: str, agenda: str, sources: dict, live: bool = True) -> Meeting:
        if self.meeting and self.meeting.active:
            await self.stop_meeting(make_mom=False)
        m = Meeting(title, agenda, sources, live=live)
        self.meeting = m
        if live and sources.get("meeting") == "system" and self.sysaudio:
            await self.sysaudio.start(lambda samples: self.feed("meeting", samples))
        await self.hub.send({"type": "meeting", "state": m.snapshot()})
        return m

    async def stop_meeting(self, make_mom: bool = True):
        m = self.meeting
        if not m or not m.active:
            return m
        if self.sysaudio:
            await self.sysaudio.stop()
        for segmenter in list(m.segmenters.values()):
            for seg in segmenter.flush():
                self.enqueue(m, seg)
        m.active = False
        await self.hub.send({"type": "meeting_stopping", "id": m.id})
        await self.drain()
        for _ in range(20):
            if m.processed >= len(m.transcript):
                break
            ok = await m.update_notes(self.hub, final=True)
            if not ok and m.last_error:
                break
        m.ended = m.started + m.audio_seconds if (not m.live and m.audio_seconds) else time.time()
        m.save_meta()
        m.save_notes()
        self.last_meeting = m
        await self.hub.send({"type": "meeting_ended", "state": m.snapshot()})
        if make_mom and m.transcript:
            asyncio.create_task(self.generate_mom(m))
        return m

    async def generate_mom(self, m: Meeting):
        if m.mom_running:
            return
        m.mom_running = True
        me = m.profile.get("my_name") or "Me"
        prompt = "\n".join([
            "MEETING CONTEXT",
            m.context_block(),
            f"Duration: {m.duration_text()}",
            "",
            "LIVE NOTES (already cleaned up)",
            m.notes.for_prompt(me),
            "",
            "FULL TRANSCRIPT (speech-to-text, may contain mistakes)",
            m.transcript_text(),
            "",
            "Write the final MoM now.",
        ])
        await self.hub.send({"type": "mom", "id": m.id, "reset": True})

        async def on_delta(piece):
            await self.hub.send({"type": "mom", "id": m.id, "delta": piece})

        try:
            text = await claude_cli.stream_text(prompt, prompts.MOM_SYSTEM.format(me=me), m.profile.get("model", "sonnet"), on_delta)
            m.mom = text.strip()
            (m.dir / "mom.md").write_text(m.mom)
            await self.hub.send({"type": "mom", "id": m.id, "done": True, "text": m.mom})
        except Exception as e:
            await self.hub.send({"type": "mom", "id": m.id, "error": str(e)})
        finally:
            m.mom_running = False

    def current_or_last(self) -> Meeting | None:
        return self.meeting or self.last_meeting

    # ---- helpers for the side tools ---------------------------------------
    async def fix_text(self, text: str) -> str:
        m = self.current_or_last()
        profile = m.profile if m else load_profile()
        ctx = m.context_block() if m else f"Me: {profile.get('my_name')}\nGlossary: {profile.get('glossary')}"
        prompt = f"MEETING CONTEXT\n{ctx}\n\nTEXT TO FIX:\n{text}"
        result = await claude_cli.run_json(prompt, prompts.FIX_SYSTEM, profile.get("model", "sonnet"), timeout=60)
        return (result.get("result") or "").strip()

    async def ask(self, question: str) -> str:
        m = self.current_or_last()
        if not m or not m.transcript:
            return "Abhi tak meeting mein kuch record nahi hua hai."
        me = m.profile.get("my_name") or "Me"
        prompt = "\n".join([
            "MEETING CONTEXT", m.context_block(), "",
            "NOTES", m.notes.for_prompt(me), "",
            "TRANSCRIPT", m.transcript_text(60000), "",
            f"QUESTION: {question}",
        ])
        result = await claude_cli.run_json(prompt, prompts.ASK_SYSTEM.format(me=me), m.profile.get("model", "sonnet"), timeout=90)
        return (result.get("result") or "").strip()

    # ---- recorded file ---------------------------------------------------
    async def import_audio(self, path: str, title: str):
        """Process a recording (any audio/video file) as if it were a meeting."""
        m = await self.start_meeting(title or "Imported recording", "", {"mic": False, "meeting": "none"}, live=False)
        m.labels["file"] = "Room"
        loop = asyncio.get_running_loop()
        try:
            audio = await loop.run_in_executor(None, decode_file, path)
        except Exception as e:
            log.warning("could not decode %s: %s", path, e)
            audio = np.zeros(0, np.float32)
        total = audio.size
        step = SAMPLE_RATE * 5
        for i in range(0, total, step):
            self.feed("file", audio[i:i + step])
            if i % (step * 12) == 0:
                await self.hub.send({"type": "import", "seconds": round(i / SAMPLE_RATE), "queue": len(self.pending)})
                await asyncio.sleep(0)
        m.audio_seconds = total / SAMPLE_RATE
        if total == 0:
            m.active = False
            await self.hub.send({"type": "status", "error": "Could not read that audio file."})
            return
        # Let notes refresh while the queue drains so long files show progress.
        while self.pending or self.stt_busy:
            await asyncio.sleep(2)
            await self.hub.send({"type": "import", "seconds": round(total / SAMPLE_RATE), "queue": len(self.pending)})
            if sum(len(l["text"]) for l in m.transcript[m.processed:]) > 6000:
                await m.update_notes(self.hub)
        await self.stop_meeting(make_mom=True)
