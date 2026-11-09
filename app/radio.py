"""Infinite Radio station logic: settings, lookahead queue, library."""

from __future__ import annotations

import json
import random
import struct
import threading
import time
import uuid
from pathlib import Path

from engine import APP_DIR, AudioCppEngine, EngineError
from lyrics import LyricGenerator, instrumental_title
from genre import derive_genre
from lyricwriter import LANGUAGE_NAMES, LANGUAGES, LyricWriter, Written, apply_language
from themeseeds import SeedPicker

DATA_DIR = APP_DIR / "data"
OUTPUT_DIR = APP_DIR / "output"
STATION_FILE = DATA_DIR / "station.json"
LIBRARY_FILE = DATA_DIR / "library.json"

SEMANTIC_FPS = 25  # YuE2 semantic frames per second of audio

DEFAULT_STATION = {
    "name": "Infinite Radio",
    "style": "English, dreamy synth pop, warm female vocal, soft drums, shimmering synths, late night drive",
    "mood": "dreamy",
    "lyric_mode": "auto",  # auto | instrumental | custom-only
    "themes": "",
    "target_seconds": 120,
    "planning": False,  # cot=full when true
    "lookahead": 2,
    "quality_steps": 8,
    "language": "English",
}

PRESETS = [
    {"name": "Neon Drive", "mood": "dreamy", "style": "English, synthwave, retro 80s, pulsing bass, glossy synths, breathy female vocal, night city"},
    {"name": "Sunrise Indie", "mood": "euphoric", "style": "English, indie pop, bright acoustic guitar, soft drums, warm lead vocal, polished demo mix"},
    {"name": "Late Lounge", "mood": "chill", "style": "English, lo-fi soul, Rhodes piano, brushed drums, relaxed male vocal, warm tape"},
    {"name": "Open Highway", "mood": "nostalgic", "style": "English, heartland rock, jangly guitars, steady drums, raspy male vocal, anthemic chorus"},
    {"name": "Velvet Night", "mood": "romantic", "style": "English, R&B ballad, piano, lush pads, smooth vocal harmonies, slow groove"},
    {"name": "Static Floor", "mood": "energetic", "style": "English, dance pop, four on the floor, punchy synth bass, powerful female vocal, festival"},
    {"name": "Afterhours", "mood": "dark", "style": "English, dark electronic, trip hop, deep bass, haunting female vocal, cinematic"},
    {"name": "Porch Light", "mood": "melancholic", "style": "English, folk, fingerpicked acoustic guitar, intimate vocal, strings, gentle"},
]

MOODS = ["dreamy", "euphoric", "chill", "nostalgic", "romantic", "energetic", "dark", "melancholic"]


def wav_duration_seconds(data: bytes) -> float | None:
    """Parse a RIFF/WAVE header for duration without external libraries."""
    try:
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        pos = 12
        byte_rate = None
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
            if cid == b"fmt ":
                byte_rate = struct.unpack("<I", data[pos + 16:pos + 20])[0]
            elif cid == b"data" and byte_rate:
                return size / byte_rate
            pos += 8 + size + (size & 1)
    except Exception:
        return None
    return None


class Library:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.tracks: list[dict] = []
        if LIBRARY_FILE.exists():
            try:
                self.tracks = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            except Exception:
                self.tracks = []
        # Backfill display metadata for songs made before it was stored (genre from the style prompt,
        # language from the leading language name the app prepends to the style).
        changed = False
        for t in self.tracks:
            if "genre" not in t:
                t["genre"] = derive_genre(t.get("style", ""), set(LANGUAGE_NAMES)); changed = True
            if "language" not in t:
                first = (t.get("style") or "").split(",")[0].strip().lower()
                t["language"] = LANGUAGE_NAMES.get(first); changed = True
        if changed:
            try:
                self._save()
            except Exception:
                pass

    def _save(self) -> None:
        tmp = LIBRARY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tracks, indent=2), encoding="utf-8")
        tmp.replace(LIBRARY_FILE)

    def add(self, track: dict) -> None:
        with self.lock:
            self.tracks.append(track)
            self._save()

    def get(self, track_id: str) -> dict | None:
        with self.lock:
            return next((t for t in self.tracks if t["id"] == track_id), None)

    def update(self, track_id: str, **fields) -> dict | None:
        with self.lock:
            for t in self.tracks:
                if t["id"] == track_id:
                    t.update(fields)
                    self._save()
                    return t
        return None

    def delete(self, track_id: str) -> bool:
        with self.lock:
            before = len(self.tracks)
            self.tracks = [t for t in self.tracks if t["id"] != track_id]
            if len(self.tracks) != before:
                self._save()
                path = OUTPUT_DIR / f"{track_id}.wav"
                if path.exists():
                    path.unlink()
                return True
        return False

    def all(self) -> list[dict]:
        with self.lock:
            return list(self.tracks)

    @staticmethod
    def audio_path(track_id: str) -> Path:
        return OUTPUT_DIR / f"{track_id}.wav"


class Radio:
    def __init__(self, engine: AudioCppEngine, library: Library):
        self.engine = engine
        self.library = library
        self.lock = threading.Lock()
        self.on = False
        self.settings = dict(DEFAULT_STATION)
        if STATION_FILE.exists():
            try:
                self.settings.update(json.loads(STATION_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        # Songs generated but not yet handed to a player. Unplayed songs from a
        # previous run are restored so a restart does not waste them.
        self.ready: list[str] = [
            t["id"] for t in sorted(self.library.all(), key=lambda t: t.get("created", 0))
            if not t.get("played") and self.library.audio_path(t["id"]).exists()
        ]
        self.requests: list[dict] = []  # custom lyric requests
        self.current: dict | None = None  # job in progress
        self.errors: list[str] = []
        self.rtf_history: list[float] = []
        self.songs_generated = 0
        self.rng = random.Random()
        self.writer = LyricWriter()
        self.gpu_lock = threading.Lock()  # YuE2 generation and the lyric model never run at the same time
        self.lyric_cache: list[tuple[str, Written]] = []  # (settings signature, lyrics)
        self.seeds = SeedPicker(self.rng)  # inspiration words when the station has no themes
        self.lyric_mode_used = "template"
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._prefetch_wake = threading.Event()
        threading.Thread(target=self._prefetch_loop, daemon=True).start()

    # ---------- settings ----------
    def update_settings(self, patch: dict) -> dict:
        allowed = set(DEFAULT_STATION)
        with self.lock:
            for k, v in patch.items():
                if k not in allowed:
                    continue
                if k == "target_seconds":
                    v = int(max(30, min(360, int(v))))
                if k == "lookahead":
                    v = int(max(1, min(4, int(v))))
                if k == "quality_steps":
                    v = int(max(4, min(16, int(v))))
                if k == "mood" and v not in MOODS:
                    continue
                if k == "lyric_mode" and v not in ("auto", "instrumental", "custom-only"):
                    continue
                if k == "language" and v not in {n for n, _ in LANGUAGES}:
                    continue
                self.settings[k] = v
            STATION_FILE.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
            snapshot = dict(self.settings)
        self._wake.set()
        self._prefetch_wake.set()
        return snapshot

    def set_on(self, on: bool) -> None:
        with self.lock:
            self.on = on
        self._wake.set()
        self._prefetch_wake.set()

    def add_request(self, lyrics: str, style: str | None, title: str | None) -> dict:
        req = {
            "id": uuid.uuid4().hex[:10],
            "lyrics": lyrics.strip(),
            "style": (style or "").strip() or None,
            "title": (title or "").strip() or None,
            "created": time.time(),
        }
        with self.lock:
            self.requests.append(req)
        self._wake.set()
        return req

    def remove_request(self, req_id: str) -> bool:
        with self.lock:
            n = len(self.requests)
            self.requests = [r for r in self.requests if r["id"] != req_id]
            return len(self.requests) != n

    def next_track(self) -> dict | None:
        with self.lock:
            if not self.ready:
                return None
            tid = self.ready.pop(0)
        track = self.library.update(tid, played=True, played_at=time.time())
        self._wake.set()
        return track

    def requeue(self, track_id: str) -> bool:
        track = self.library.get(track_id)
        if not track:
            return False
        with self.lock:
            if track_id not in self.ready:
                self.ready.insert(0, track_id)
        return True

    # ---------- status ----------
    def status(self) -> dict:
        with self.lock:
            ready_ids = list(self.ready)
            requests = list(self.requests)
            current = dict(self.current) if self.current else None
            errors = list(self.errors[-5:])
            on = self.on
            settings = dict(self.settings)
        tracks = {t["id"]: t for t in self.library.all()}
        ready = [tracks[t] for t in ready_ids if t in tracks]
        avg_rtf = sum(self.rtf_history[-5:]) / len(self.rtf_history[-5:]) if self.rtf_history else None
        if current:
            current["elapsed"] = time.time() - current["started_at"]
            if avg_rtf:
                est = settings["target_seconds"] * avg_rtf
                if not self.engine.model_loaded:
                    est += 45
                current["eta_seconds"] = max(0.0, est - current["elapsed"])
            else:
                current["eta_seconds"] = None
        return {
            "on": on,
            "settings": settings,
            "ready": ready,
            "requests": requests,
            "current": current,
            "errors": errors,
            "lyrics": {
                "writer": "llm" if self.writer.available() else "template",
                "last_mode": self.lyric_mode_used,
                "ready": len(self.lyric_cache),
                "language": settings.get("language", "English"),
                **self.writer.snapshot(),
            },
            "stats": {
                "songs_generated": self.songs_generated,
                "avg_rtf": avg_rtf,
                "realtime_factor": (1 / avg_rtf) if avg_rtf else None,
            },
            "engine": self.engine.snapshot(),
        }

    # ---------- worker ----------
    def _loop(self) -> None:
        backoff = 3
        while True:
            job = self._next_job()
            if job is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            # Give the lyric prefetcher the idle gap: if no set is cached for an auto song,
            # it is written now (about 5 s) rather than while YuE2 renders.
            if job["kind"] == "auto" and self.settings["lyric_mode"] == "auto" and self.writer.available():
                with self.lock:
                    sig = self._signature(self.settings)
                    cached = any(k == sig for k, _ in self.lyric_cache)
                if not cached:
                    self._prefetch_wake.set()
                    time.sleep(0.2)
            try:
                self._run_job(job)
                backoff = 3
            except EngineError as e:
                self._record_error(str(e))
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:  # pragma: no cover - defensive
                self._record_error(f"Unexpected error: {e!r}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                with self.lock:
                    self.current = None

    def _record_error(self, msg: str) -> None:
        print(f"[radio] error: {msg}", flush=True)
        with self.lock:
            self.errors.append(msg)
            self.errors = self.errors[-20:]

    def _next_job(self) -> dict | None:
        with self.lock:
            if not self.on:
                return None
            if self.requests:
                req = self.requests.pop(0)
                return {"kind": "request", "request": req}
            if len(self.ready) >= int(self.settings["lookahead"]):
                return None
            if self.settings["lyric_mode"] == "custom-only":
                return None
            return {"kind": "auto"}

    def _signature(self, s: dict) -> str:
        return json.dumps([s.get("style"), s.get("mood"), s.get("themes"), s.get("language", "English"), int(s.get("target_seconds", 120))])

    def _write_lyrics(self, s: dict) -> Written | None:
        themes = (s.get("themes") or "").strip()
        return self.writer.write(apply_language(s["style"], s.get("language", "English")), s["mood"], themes,
                                 s.get("language", "English"), int(s["target_seconds"]), self.rng.randrange(1 << 30),
                                 inspiration=None if themes else self.seeds.pick(2))

    def _auto_lyrics(self, s: dict) -> tuple[str, str, str, str]:
        """Returns (lyrics, title, mode, theme) for an auto song: cached LLM set, fresh LLM set, or templates."""
        sig = self._signature(s)
        with self.lock:
            for i, (k, w) in enumerate(self.lyric_cache):
                if k == sig:
                    self.lyric_cache.pop(i)
                    self._prefetch_wake.set()
                    return w.lyrics, w.title, "llm", w.theme
        if self.writer.available():
            w = self._write_lyrics(s)
            if w:
                return w.lyrics, w.title, "llm", w.theme
        song = LyricGenerator(self.rng.randrange(1 << 30)).generate(s.get("themes", ""), s["mood"])
        if s.get("language", "English") != "English":
            self._record_error(f"Lyric model unavailable or failed; wrote English template lyrics instead of {s.get('language')}")
        return song.lyrics, song.title, "template", (s.get("themes") or "").strip()

    def _prefetch_loop(self) -> None:
        """Keep up to two LLM lyric sets ready for the current settings so songs never wait on text."""
        while True:
            self._prefetch_wake.wait(timeout=5.0)
            self._prefetch_wake.clear()
            if not self.writer.available():
                continue
            with self.lock:
                active = self.on and self.settings["lyric_mode"] == "auto"
                s = dict(self.settings)
                sig = self._signature(s)
                self.lyric_cache = [(k, w) for k, w in self.lyric_cache if k == sig]
                have = len(self.lyric_cache)
                rendering = self.current is not None
            if not active or have >= 2 or rendering:
                continue
            # Never share the GPU with YuE2: the lock is held for the whole lyric call, and a song
            # cannot start while it is held (see _run_job). If a song is rendering, skip this round.
            if not self.gpu_lock.acquire(blocking=False):
                continue
            try:
                w = self._write_lyrics(s)
            finally:
                self.gpu_lock.release()
            if w:
                with self.lock:
                    if self._signature(self.settings) == sig:
                        self.lyric_cache.append((sig, w))
                print(f"[lyrics] prefetched '{w.title}' ({w.language}, theme: {w.theme}) in {w.seconds:.1f}s", flush=True)
                self._prefetch_wake.set()  # top up to two
            else:
                time.sleep(10)

    def _build(self, job: dict) -> tuple[str, str, str, dict, dict]:
        s = dict(self.settings)
        language = s.get("language", "English")
        style = apply_language(s["style"], language)
        mood = s["mood"]
        theme = ""
        self.lyric_mode_used = "custom"
        if job["kind"] == "request":
            req = job["request"]
            lyrics = req["lyrics"]
            title = req["title"] or (lyrics.strip().splitlines()[0][:40] if lyrics.strip() else "Requested Track")
            title = title.lstrip("[").rstrip("]")
            style = req["style"] or style
        elif s["lyric_mode"] == "instrumental":
            lyrics = ""
            title = instrumental_title(self.rng)
            self.lyric_mode_used = "instrumental"
        else:
            lyrics, title, self.lyric_mode_used, theme = self._auto_lyrics(s)
        title = self._unique_title(title)
        target = int(s["target_seconds"])
        # Score planning (ABC) is reported to break pronunciation for non-English lyrics.
        planning = bool(s["planning"]) and language == "English"
        options = {
            "cot": "full" if planning else "off",
            "seed": self.rng.randrange(1, 2**31 - 1),
            "num_inference_steps": int(s["quality_steps"]),
            "semantic_max_tokens": int(target * SEMANTIC_FPS),
            "semantic_min_tokens": int(max(200, target * SEMANTIC_FPS * 0.5)),
        }
        return lyrics, style, title, options, {"genre": derive_genre(style, set(LANGUAGE_NAMES)), "language": language,
                                                "theme": theme}

    _ROMAN = ["II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]

    def _unique_title(self, title: str) -> str:
        """Avoid identical titles in the library (e.g. 'Afterglow', 'Afterglow II')."""
        existing = {t["title"].lower() for t in self.library.all()}
        if title.lower() not in existing:
            return title
        for suffix in self._ROMAN:
            cand = f"{title} {suffix}"
            if cand.lower() not in existing:
                return cand
        return f"{title} {uuid.uuid4().hex[:4]}"

    def _run_job(self, job: dict) -> None:
        lyrics, style, title, options, meta_in = self._build(job)
        track_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.current = {
                "id": track_id, "title": title, "style": style, "lyrics": lyrics,
                "started_at": time.time(), "kind": job["kind"],
            }
        print(f"[radio] generating '{title}' seed={options['seed']} cot={options['cot']}", flush=True)
        with self.gpu_lock:  # waits for an in-flight lyric call (a few seconds) rather than overlapping it
            wav, meta = self.engine.generate(lyrics, style, options)
        path = self.library.audio_path(track_id)
        path.write_bytes(wav)
        duration = wav_duration_seconds(wav) or 0.0
        elapsed = float(meta.get("elapsed") or 0.0)
        rtf = (elapsed / duration) if duration else None
        if rtf and self.engine.model_loaded and self.songs_generated >= 1:
            self.rtf_history.append(rtf)
        elif rtf and not self.rtf_history:
            self.rtf_history.append(rtf)
        track = {
            "id": track_id,
            "title": title,
            "style": style,
            "lyrics": lyrics,
            "mood": self.settings.get("mood"),
            "genre": meta_in.get("genre", ""),
            "language": meta_in.get("language", "English"),
            "theme": meta_in.get("theme", ""),
            "seed": options["seed"],
            "cot": options["cot"],
            "duration": round(duration, 2),
            "gen_seconds": round(elapsed, 1),
            "rtf": round(rtf, 3) if rtf else None,
            "sample_rate": meta.get("sample_rate"),
            "created": time.time(),
            "liked": False,
            "played": False,
            "kind": job["kind"],
            "size": len(wav),
        }
        self.library.add(track)
        with self.lock:
            self.ready.append(track_id)
            self.songs_generated += 1
        self._prefetch_wake.set()
        print(f"[radio] ready '{title}' {duration:.1f}s audio in {elapsed:.1f}s (rtf {rtf:.2f})" if rtf else f"[radio] ready '{title}'", flush=True)
