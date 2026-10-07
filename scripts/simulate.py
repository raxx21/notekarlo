"""Stream an audio file into a running NoteKarLo exactly like the browser does (for testing).

    .venv/bin/python scripts/simulate.py tests/audio/meeting_sample.wav --stop
    .venv/bin/python scripts/simulate.py them.wav --mic me.wav --speed 2 --stop

The file is sent over the same WebSocket as live audio, in real time (or faster with --speed),
then the script prints the notes and (with --stop) the final MoM.
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.stt import decode_file  # noqa: E402  (ffmpeg if installed, else PyAV)


def api(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def decode(path: str) -> np.ndarray:
    audio = decode_file(path)
    return (np.clip(audio, -1, 1) * 32767).astype("<i2")


async def stream(ws, pcm: np.ndarray, code: int, speed: float):
    step = 1600  # 100 ms
    t0 = time.time()
    for i in range(0, pcm.size, step):
        await ws.send(bytes([code]) + pcm[i:i + step].tobytes())
        target = t0 + (i + step) / 16000 / speed
        await asyncio.sleep(max(0, target - time.time()))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="meeting audio (sent as the 'Them' channel)")
    ap.add_argument("--mic", help="optional second file sent as the 'Me' channel")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--title", default="Simulated meeting")
    ap.add_argument("--agenda", default="")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--stop", action="store_true", help="stop the meeting at the end and wait for the MoM")
    ap.add_argument("--tail", type=float, default=4.0, help="seconds of silence to send after the audio")
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    them = decode(args.audio)
    me = decode(args.mic) if args.mic else None
    tail = np.zeros(int(16000 * args.tail), dtype="<i2")
    them = np.concatenate([them, tail])
    if me is not None:
        me = np.concatenate([me, tail])

    api(base, "POST", "/api/meeting/start", {
        "title": args.title, "agenda": args.agenda,
        "sources": {"mic": me is not None, "meeting": "tab"},
    })
    started = time.time()
    async with websockets.connect(f"ws://127.0.0.1:{args.port}/ws", max_size=None) as ws:
        async def drain():
            async for msg in ws:
                ev = json.loads(msg)
                if ev["type"] == "line":
                    l = ev["line"]
                    print(f"  [{l['who']}] {l['text']}")
                elif ev["type"] == "notes":
                    took = f", {ev['took']}s" if ev.get("took") else ""
                    print(f"  ↳ notes updated ({len(ev['notes']['items'])} items{took})")
                elif ev["type"] == "status" and ev.get("error"):
                    print("  !", ev["error"])

        reader = asyncio.create_task(drain())
        jobs = [stream(ws, them, 1, args.speed)]
        if me is not None:
            jobs.append(stream(ws, me, 0, args.speed))
        await asyncio.gather(*jobs)
        print(f"audio sent in {time.time() - started:.1f}s")
        await asyncio.sleep(3)
        if args.stop:
            print("stopping meeting (final notes + MoM)…")
            await asyncio.to_thread(api, base, "POST", "/api/meeting/stop")
            for _ in range(240):
                st = (await asyncio.to_thread(api, base, "GET", "/api/state"))["meeting"]
                if st and st.get("mom"):
                    break
                await asyncio.sleep(1)
        reader.cancel()

    st = api(base, "GET", "/api/state")["meeting"]
    notes = st["notes"]
    print("\n=== TOPIC:", notes.get("topic"))
    print("=== SUMMARY:", notes.get("summary"))
    for item in notes["items"]:
        flags = " ★" if item["important"] else ""
        extra = ", ".join(x for x in [item.get("owner") and f"owner {item['owner']}", item.get("due") and f"due {item['due']}"] if x)
        print(f"- [{item['section']}] {item['text']}{f' ({extra})' if extra else ''}{flags}")
    if st.get("mom"):
        print("\n=== MoM ===\n" + st["mom"])


if __name__ == "__main__":
    asyncio.run(main())
