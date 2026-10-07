"""NoteKarLo web server: serves the UI, receives browser audio over a WebSocket, exposes a small JSON API."""

import argparse
import asyncio
import json
import logging
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import claude_cli
from .config import MEETINGS_DIR, MODELS, UPDATE_SPEEDS, WEB_DIR, load_profile, save_profile
from .stt import STT_CHOICES
from .engine import Engine
from .notes import SECTIONS
from .sysaudio import SystemAudio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("notekarlo")

engine = Engine()
engine.sysaudio = SystemAudio(engine.hub)
CHANNELS = {0: "mic", 1: "meeting"}
SETTINGS = {"lan": False, "port": 8765}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await engine.startup()
    yield
    if engine.meeting and engine.meeting.active:
        await engine.stop_meeting(make_mom=False)
    await engine.sysaudio.stop()


app = FastAPI(title="NoteKarLo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
VIEWER_PATHS = {"/view", "/api/state", "/favicon.ico"}


def is_local(client) -> bool:
    return bool(client) and client.host in LOCAL_HOSTS


@app.middleware("http")
async def lan_viewers_are_read_only(request: Request, call_next):
    # With --lan, other devices may only open the read-only viewer.
    path = request.url.path
    if not is_local(request.client) and not (path in VIEWER_PATHS or path.startswith("/static/")):
        return JSONResponse({"detail": "Only the read-only /view page is available from other devices."}, status_code=403)
    response = await call_next(request)
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # always pick up a new version after an update
    return response


def lan_url() -> str | None:
    if not SETTINGS["lan"]:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{SETTINGS['port']}/view"
    except Exception:
        return None


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/view")
async def viewer():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def state(request: Request):
    m = engine.current_or_last()
    local = is_local(request.client)
    return {
        "profile": load_profile() if local else {},
        "models": MODELS,
        "speeds": list(UPDATE_SPEEDS),
        "capabilities": {
            "platform": "mac" if sys.platform == "darwin" else ("linux" if sys.platform.startswith("linux") else sys.platform),
            "system_audio": engine.sysaudio.available(),
            "stt_backend": engine.transcriber.backend,
            "stt_choices": STT_CHOICES if engine.transcriber.backend == "faster" else {},
            "system_audio_reason": "" if engine.sysaudio.available() else engine.sysaudio.unavailable_reason(),
            "claude": bool(claude_cli.CLAUDE_BIN),
            "lan_url": lan_url(),
        },
        "status": engine.status(),
        "meeting": m.snapshot() if m else None,
    }


@app.put("/api/profile")
async def put_profile(request: Request):
    data = await request.json()
    profile = save_profile(data)
    if engine.meeting and engine.meeting.active:
        engine.meeting.profile.update(profile)
    if profile.get("stt_model") != engine.transcriber.choice:
        asyncio.create_task(engine.set_stt_model(profile["stt_model"]))
    return profile


@app.post("/api/meeting/start")
async def start(request: Request):
    data = await request.json()
    sources = data.get("sources") or {"mic": True, "meeting": "none"}
    m = await engine.start_meeting(data.get("title", ""), data.get("agenda", ""), sources)
    return m.snapshot()


@app.post("/api/meeting/stop")
async def stop():
    m = await engine.stop_meeting(make_mom=True)
    return {"ok": True, "id": m.id if m else None}


@app.post("/api/pin")
async def pin():
    m = engine.meeting
    if not m or not m.active:
        raise HTTPException(400, "No meeting running")
    p = m.add_pin(source="button")
    await engine.hub.send({"type": "pin", "t": p["t"]})
    return {"ok": True}


@app.post("/api/notes/refresh")
async def refresh():
    m = engine.current_or_last()
    if not m:
        raise HTTPException(400, "No meeting")
    asyncio.create_task(m.update_notes(engine.hub))
    return {"ok": True}


async def _broadcast_notes(m, changed=None):
    m.save_notes()
    await engine.hub.send({"type": "notes", "notes": m.notes.to_dict(), "changed": changed or []})


@app.post("/api/notes/item")
async def add_item(request: Request):
    m = engine.current_or_last()
    if not m:
        raise HTTPException(400, "No meeting")
    data = await request.json()
    section = data.get("section") if data.get("section") in SECTIONS else "points"
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Empty note")
    item = m.notes.add(section, text, data.get("owner", ""), data.get("due", ""), data.get("important", False), by_user=True)
    await _broadcast_notes(m, [item["id"]])
    return item


@app.patch("/api/notes/item/{item_id}")
async def edit_item(item_id: str, request: Request):
    m = engine.current_or_last()
    if not m:
        raise HTTPException(400, "No meeting")
    item = m.notes.edit_by_user(item_id, await request.json())
    if not item:
        raise HTTPException(404, "Not found")
    await _broadcast_notes(m, [item_id])
    return item


@app.delete("/api/notes/item/{item_id}")
async def delete_item(item_id: str):
    m = engine.current_or_last()
    if not m or not m.notes.delete(item_id):
        raise HTTPException(404, "Not found")
    await _broadcast_notes(m)
    return {"ok": True}


@app.post("/api/fix")
async def fix(request: Request):
    text = ((await request.json()).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Nothing to fix")
    try:
        return {"text": await engine.fix_text(text)}
    except claude_cli.ClaudeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/ask")
async def ask(request: Request):
    q = ((await request.json()).get("question") or "").strip()
    if not q:
        raise HTTPException(400, "Empty question")
    try:
        return {"answer": await engine.ask(q)}
    except claude_cli.ClaudeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/mom")
async def mom():
    m = engine.current_or_last()
    if not m or not m.transcript:
        raise HTTPException(400, "Nothing to summarise yet")
    asyncio.create_task(engine.generate_mom(m))
    return {"ok": True}


@app.post("/api/import")
async def import_audio(request: Request):
    title = request.query_params.get("title", "Imported recording")
    suffix = Path(request.query_params.get("name", "audio.m4a")).suffix or ".m4a"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    async for chunk in request.stream():
        tmp.write(chunk)
    tmp.close()
    asyncio.create_task(engine.import_audio(tmp.name, title))
    return {"ok": True}


@app.get("/api/meetings")
async def meetings():
    out = []
    for d in sorted(MEETINGS_DIR.iterdir(), reverse=True):
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        meta["has_mom"] = (d / "mom.md").exists()
        out.append(meta)
    return out


@app.get("/api/meetings/{meeting_id}")
async def meeting_detail(meeting_id: str):
    d = (MEETINGS_DIR / meeting_id).resolve()
    if d.parent != MEETINGS_DIR.resolve() or not d.exists():
        raise HTTPException(404, "Not found")
    read = lambda name: (d / name).read_text() if (d / name).exists() else ""
    rows = [json.loads(l) for l in read("transcript.jsonl").splitlines() if l.strip()]
    dropped = {r["drop"] for r in rows if "drop" in r}
    transcript = [r for r in rows if "text" in r and r["id"] not in dropped]
    return {
        "meta": json.loads(read("meta.json") or "{}"),
        "notes": json.loads(read("notes.json") or "{}"),
        "mom": read("mom.md"),
        "transcript": transcript,
    }


@app.post("/api/meetings/{meeting_id}/reveal")
async def reveal(meeting_id: str):
    d = (MEETINGS_DIR / meeting_id).resolve()
    if d.parent != MEETINGS_DIR.resolve() or not d.exists():
        raise HTTPException(404, "Not found")
    open_path(str(d))
    return {"ok": True}


def open_path(target: str):
    """Open a folder/URL with the desktop's default app (Finder on Mac, file manager on Linux)."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise HTTPException(501, f"{opener} not available")


@app.post("/api/open-privacy-settings")
async def open_privacy():
    if sys.platform != "darwin":
        return {"ok": False}
    open_path("x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture")
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    engine.hub.clients.add(ws)
    local = is_local(ws.client)
    await ws.send_text(json.dumps(engine.status()))
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data and local:
                channel = CHANNELS.get(data[0])
                if channel and len(data) > 1:
                    pcm = np.frombuffer(data[1:1 + ((len(data) - 1) // 2) * 2], dtype="<i2").astype(np.float32) / 32768.0
                    engine.feed(channel, pcm)
    except WebSocketDisconnect:
        pass
    finally:
        engine.hub.clients.discard(ws)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="NoteKarLo — live Hinglish meeting notes")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lan", action="store_true", help="allow other devices on your Wi-Fi to open the read-only /view page")
    args = parser.parse_args()
    SETTINGS.update(lan=args.lan, port=args.port)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
