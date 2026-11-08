"""Infinite Radio web server (FastAPI).

Serves the player UI and a JSON API on 127.0.0.1, drives the audio.cpp YuE2
engine through `radio.py`, and hosts DJ sets, scenes and episode rendering.
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
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import AudioCppEngine, EngineError  # noqa: E402
from episodes import EpisodeRenderer  # noqa: E402
from radio import MOODS, PRESETS, Library, Radio  # noqa: E402
from scene import Scene  # noqa: E402
from sets import SetManager  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
OUTPUT_DIR = APP_DIR / "output"

PORT = 0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Printed only once uvicorn is bound, so the Pinokio launcher opens a live URL.
    print(f"Infinite Radio is on the air at http://127.0.0.1:{PORT}", flush=True)
    yield
    engine.stop()


app = FastAPI(title="Infinite Radio", version="1.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

engine = AudioCppEngine()
library = Library()
radio = Radio(engine, library)
sets = SetManager()
scene = Scene()
episodes = EpisodeRenderer(library, scene, lambda: radio.settings)


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


class SetStart(BaseModel):
    name: str | None = None


class SetCut(BaseModel):
    track_id: str | None = None


class TrackRef(BaseModel):
    track_id: str
    display_title: str | None = None


class SetUpdate(BaseModel):
    name: str | None = None
    tracks: list[TrackRef] | None = None


class RenderRequest(BaseModel):
    crossfade: float | None = 2.0
    name: str | None = None


class EpisodeCreate(BaseModel):
    name: str | None = None
    tracks: list[TrackRef]
    crossfade: float | None = 2.0


class ScenePatch(BaseModel):
    camera: bool | None = None
    weather: str | None = None
    weather_density: float | None = None
    fog: bool | None = None
    flicker: bool | None = None
    grain: bool | None = None
    vignette: bool | None = None
    pingpong: bool | None = None
    loop_seconds: int | None = None


class PlayerEvent(BaseModel):
    track_id: str


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
    st = radio.status()
    cur = sets.current()
    if cur:
        kept = [e for e in cur["tracks"] if e["state"] in ("kept", "playing")]
        cur = {"id": cur["id"], "name": cur["name"], "started_at": cur["started_at"], "kept": len(kept),
               "cut": sum(1 for e in cur["tracks"] if e["state"] == "cut"), "sections": len(cur["sections"]),
               "tracks": cur["tracks"][-12:]}
    st["set"] = cur
    active = [j for j in episodes.all() if j["status"] not in ("done", "error")]
    st["rendering"] = active[0] if active else None
    return st


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
    settings = radio.update_settings(data)
    if "style" in data or "mood" in data:
        sets.steer(settings)
    return {"on": radio.on, "settings": settings}


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
    if track:
        sets.on_next(track)
    st = radio.status()
    return {"track": track, "buffering": track is None, "current": st["current"], "ready_count": len(st["ready"])}


@app.post("/api/player/ended")
def player_ended(ev: PlayerEvent):
    sets.on_ended(ev.track_id)
    return {"ok": True}


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


# ---------- DJ sets ----------
@app.get("/api/sets")
def list_sets():
    return {"sets": sets.all(), "current": sets.current()}


@app.get("/api/sets/current")
def current_set():
    return {"set": sets.current()}


@app.post("/api/sets/start")
def start_set(body: SetStart):
    ok, why = engine.installed()
    if not ok:
        raise HTTPException(409, why)
    try:
        s = sets.start(body.name or "", radio.settings)
    except ValueError as e:
        raise HTTPException(409, str(e))
    radio.set_on(True)
    return s


@app.post("/api/sets/cut")
def cut_set(body: SetCut):
    e = sets.cut(body.track_id)
    if not e:
        raise HTTPException(404, "nothing to cut")
    return e


@app.post("/api/sets/undo")
def undo_cut():
    e = sets.undo_cut()
    if not e:
        raise HTTPException(404, "nothing to undo")
    return e


@app.post("/api/sets/close")
def close_set():
    s = sets.close()
    if not s:
        raise HTTPException(404, "no open set")
    return s


@app.get("/api/sets/{set_id}")
def get_set(set_id: str):
    s = sets.get(set_id)
    if not s:
        raise HTTPException(404, "set not found")
    # Attach library metadata for the tracklist screen
    for e in s["tracks"]:
        t = library.get(e["track_id"])
        e["available"] = bool(t and Library.audio_path(e["track_id"]).exists())
        if t:
            e["style"] = t.get("style")
            e["duration"] = t.get("duration")
    return s


@app.put("/api/sets/{set_id}")
def update_set(set_id: str, body: SetUpdate):
    s = sets.update(set_id, body.name, [t.model_dump() for t in body.tracks] if body.tracks is not None else None)
    if not s:
        raise HTTPException(404, "set not found")
    return s


@app.delete("/api/sets/{set_id}")
def delete_set(set_id: str):
    if not sets.delete(set_id):
        raise HTTPException(404, "set not found or still open")
    return {"ok": True}


@app.post("/api/sets/{set_id}/render")
def render_set(set_id: str, body: RenderRequest):
    s = sets.get(set_id)
    if not s:
        raise HTTPException(404, "set not found")
    if s["status"] == "open":
        raise HTTPException(409, "close the set before rendering")
    kept = [{"track_id": e["track_id"], "display_title": e.get("display_title")} for e in s["tracks"] if e["state"] == "kept"]
    try:
        job = episodes.create(body.name or s["name"], kept, body.crossfade or 2.0, set_id=set_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sets.set_episode(set_id, job["id"])
    return job


# ---------- episodes ----------
@app.get("/api/episodes")
def list_episodes():
    return {"episodes": episodes.all()}


@app.get("/api/episodes/suggest")
def suggest_tracks(source: str = "bookmarked", minutes: int = Query(60, ge=5, le=360), order: str = "chronological"):
    tracks = [t for t in library.all() if Library.audio_path(t["id"]).exists()]
    if source == "bookmarked":
        pool = [t for t in tracks if t.get("liked")]
        if not pool:
            pool, source = tracks, "recent"
    else:
        pool = tracks
    pool = sorted(pool, key=lambda t: t.get("created", 0), reverse=True)
    chosen, total = [], 0.0
    for t in pool:
        if total >= minutes * 60:
            break
        chosen.append(t)
        total += float(t.get("duration") or 0)
    if order == "chronological":
        chosen = sorted(chosen, key=lambda t: t.get("created", 0))
    elif order == "shuffled":
        import random
        random.shuffle(chosen)
    return {"source": source, "minutes": minutes, "duration": round(total, 1),
            "tracks": [{"track_id": t["id"], "display_title": t["title"], "duration": t.get("duration"), "style": t.get("style"), "liked": t.get("liked", False)} for t in chosen]}


@app.post("/api/episodes")
def create_episode(body: EpisodeCreate):
    try:
        return episodes.create(body.name or "", [t.model_dump() for t in body.tracks], body.crossfade or 2.0)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/episodes/{job_id}")
def get_episode(job_id: str):
    j = episodes.get(job_id)
    if not j:
        raise HTTPException(404, "episode not found")
    return j


@app.post("/api/episodes/{job_id}/cancel")
def cancel_episode(job_id: str):
    if not episodes.cancel(job_id):
        raise HTTPException(409, "episode is not rendering")
    return {"ok": True}


@app.delete("/api/episodes/{job_id}")
def delete_episode(job_id: str):
    if not episodes.delete(job_id):
        raise HTTPException(404, "episode not found")
    return {"ok": True}


def _episode_output(job_id: str, key: str) -> Path:
    j = episodes.get(job_id)
    if not j or j["status"] != "done" or not j["outputs"].get(key):
        raise HTTPException(404, "output not available")
    p = OUTPUT_DIR / j["outputs"][key]
    if not p.exists():
        raise HTTPException(404, "file missing")
    return p


@app.get("/api/episodes/{job_id}/file")
def episode_file(job_id: str, download: bool = False):
    j = episodes.get(job_id)
    p = _episode_output(job_id, "video")
    return FileResponse(str(p), media_type="video/mp4", filename=f"{_safe_filename(j['name'])}.mp4" if download else None)


@app.get("/api/episodes/{job_id}/thumbnail")
def episode_thumbnail(job_id: str, download: bool = False):
    j = episodes.get(job_id)
    p = _episode_output(job_id, "thumbnail")
    return FileResponse(str(p), media_type="image/png", filename=f"{_safe_filename(j['name'])}-thumbnail.png" if download else None)


@app.get("/api/episodes/{job_id}/description", response_class=PlainTextResponse)
def episode_description(job_id: str):
    return _episode_output(job_id, "description").read_text(encoding="utf-8")


# ---------- scene ----------
@app.get("/api/scene")
def get_scene():
    return scene.snapshot()


@app.put("/api/scene")
def put_scene(patch: ScenePatch):
    return scene.update_settings({k: v for k, v in patch.model_dump().items() if v is not None})


@app.post("/api/scene/upload")
async def upload_scene(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 300 * 1024 * 1024:
        raise HTTPException(413, "file too large (300 MB max)")
    try:
        return scene.save_upload(file.filename or "scene", data)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/scene")
def clear_scene():
    return scene.clear()


@app.get("/api/scene/preview")
def scene_preview():
    p = scene.preview_path()
    if not p or not p.exists():
        raise HTTPException(404, "no scene file")
    media = "image/png" if p.suffix == ".png" else {"gif": "image/gif", "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime"}.get(p.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(str(p), media_type=media, headers={"Cache-Control": "no-store"})


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
