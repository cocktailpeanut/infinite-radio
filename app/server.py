"""Infinite Radio web server (FastAPI).

Serves the dark neon player UI and a small JSON API on 127.0.0.1, and drives the
audio.cpp YuE2 engine through `radio.py`.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import AudioCppEngine, EngineError  # noqa: E402
from radio import MOODS, PRESETS, Library, Radio  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

PORT = 0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Printed only once uvicorn is bound, so the Pinokio launcher opens a live URL.
    print(f"Infinite Radio is on the air at http://127.0.0.1:{PORT}", flush=True)
    yield
    engine.stop()


app = FastAPI(title="Infinite Radio", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

engine = AudioCppEngine()
library = Library()
radio = Radio(engine, library)


def _boot_engine() -> None:
    try:
        engine.start()
    except EngineError as e:
        print(f"[engine] {e}", flush=True)


threading.Thread(target=_boot_engine, daemon=True).start()


# ---------- models ----------
class StationPatch(BaseModel):
    name: str | None = None
    style: str | None = None
    mood: str | None = None
    lyric_mode: str | None = None
    themes: str | None = None
    target_seconds: int | None = None
    planning: bool | None = None
    lookahead: int | None = None
    quality_steps: int | None = None


class SongRequest(BaseModel):
    lyrics: str
    style: str | None = None
    title: str | None = None


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ---------- status ----------
@app.get("/api/health")
def health():
    ok, why = engine.installed()
    return {"ok": True, "installed": ok, "detail": why, "engine": engine.state}


@app.get("/api/status")
def status():
    return radio.status()


@app.get("/api/presets")
def presets():
    return {"presets": PRESETS, "moods": MOODS}


@app.get("/api/engine/log")
def engine_log():
    return {"lines": list(engine.log)}


@app.post("/api/engine/restart")
def engine_restart():
    engine.stop()
    try:
        engine.start()
    except EngineError as e:
        raise HTTPException(500, str(e))
    return engine.snapshot()


# ---------- station ----------
@app.get("/api/station")
def get_station():
    return {"on": radio.on, "settings": radio.settings}


@app.put("/api/station")
def put_station(patch: StationPatch):
    data = {k: v for k, v in patch.model_dump().items() if v is not None}
    return {"on": radio.on, "settings": radio.update_settings(data)}


@app.post("/api/station/on")
def station_on():
    ok, why = engine.installed()
    if not ok:
        raise HTTPException(409, why)
    radio.set_on(True)
    return {"on": True}


@app.post("/api/station/off")
def station_off():
    radio.set_on(False)
    return {"on": False}


@app.get("/api/next")
def next_track():
    track = radio.next_track()
    st = radio.status()
    return {"track": track, "buffering": track is None, "current": st["current"], "ready_count": len(st["ready"])}


@app.post("/api/requests")
def add_request(req: SongRequest):
    if not req.lyrics.strip() and not req.title:
        raise HTTPException(400, "lyrics required")
    return radio.add_request(req.lyrics, req.style, req.title)


@app.delete("/api/requests/{req_id}")
def delete_request(req_id: str):
    if not radio.remove_request(req_id):
        raise HTTPException(404, "request not found")
    return {"ok": True}


# ---------- library ----------
@app.get("/api/library")
def get_library(liked: bool = False, limit: int = Query(200, ge=1, le=2000)):
    tracks = library.all()
    if liked:
        tracks = [t for t in tracks if t.get("liked")]
    tracks = sorted(tracks, key=lambda t: t.get("created", 0), reverse=True)
    return {"tracks": tracks[:limit], "total": len(tracks)}


@app.get("/api/tracks/{track_id}")
def get_track(track_id: str):
    t = library.get(track_id)
    if not t:
        raise HTTPException(404, "track not found")
    return t


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip() or "track"
    return name[:60]


@app.get("/api/tracks/{track_id}/audio")
def get_audio(track_id: str, download: bool = False):
    t = library.get(track_id)
    path = Library.audio_path(track_id)
    if not t or not path.exists():
        raise HTTPException(404, "audio not found")
    headers = {"Cache-Control": "no-store"}
    filename = f"{_safe_filename(t['title'])}.wav" if download else None
    return FileResponse(str(path), media_type="audio/wav", headers=headers, filename=filename)


@app.post("/api/tracks/{track_id}/like")
def toggle_like(track_id: str):
    t = library.get(track_id)
    if not t:
        raise HTTPException(404, "track not found")
    return library.update(track_id, liked=not t.get("liked", False))


@app.post("/api/tracks/{track_id}/requeue")
def requeue(track_id: str):
    if not radio.requeue(track_id):
        raise HTTPException(404, "track not found")
    return {"ok": True}


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: str):
    if not library.delete(track_id):
        raise HTTPException(404, "track not found")
    return {"ok": True}


@app.exception_handler(EngineError)
def engine_error_handler(_, exc: EngineError):
    return JSONResponse(status_code=500, content={"error": str(exc)})


def pick_port() -> int:
    env_port = os.environ.get("PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    PORT = pick_port()
    print(f"Starting Infinite Radio on port {PORT}...", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
