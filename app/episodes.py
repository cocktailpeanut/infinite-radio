"""Episode renderer: tracklist -> MP4 + thumbnail + description + tracklist.json."""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

from engine import APP_DIR
from mixer import format_timestamp, mix_tracks
from scene import FPS, H, W, Scene, find_ffmpeg, load_font

DATA_DIR = APP_DIR / "data"
OUTPUT_DIR = APP_DIR / "output"
EPISODES_DIR = OUTPUT_DIR / "episodes"
JOBS_FILE = DATA_DIR / "episodes.json"

INTRO_SECONDS = 6.0
CARD_FADE_IN, CARD_FADE_OUT = 0.6, 0.8


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _slug(text: str) -> str:
    return (re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "episode")[:48]


class EpisodeRenderer:
    def __init__(self, library, scene: Scene, station_getter):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        EPISODES_DIR.mkdir(parents=True, exist_ok=True)
        self.library = library
        self.scene = scene
        self.station_getter = station_getter  # callable -> settings dict
        self.lock = threading.Lock()
        self.jobs: list[dict] = []
        if JOBS_FILE.exists():
            try:
                self.jobs = json.loads(JOBS_FILE.read_text())
            except Exception:
                self.jobs = []
        for j in self.jobs:
            if j["status"] not in ("done", "error"):
                j["status"] = "error"
                j["error"] = "interrupted by restart"
        self._save()
        self.queue: queue.Queue[str] = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._cancel: set[str] = set()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------- persistence ----------
    def _save(self) -> None:
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.jobs, indent=2))
        tmp.replace(JOBS_FILE)

    def _update(self, job_id: str, **fields) -> None:
        with self.lock:
            for j in self.jobs:
                if j["id"] == job_id:
                    j.update(fields)
                    break
            self._save()

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            j = next((x for x in self.jobs if x["id"] == job_id), None)
            return dict(j) if j else None

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in self.jobs]

    # ---------- API ----------
    def create(self, name: str, tracks: list[dict], crossfade: float = 2.0, set_id: str | None = None) -> dict:
        """tracks: [{track_id, display_title?}] in playback order."""
        entries = []
        for t in tracks:
            lib = self.library.get(t.get("track_id"))
            if not lib or not self.library.audio_path(lib["id"]).exists():
                continue
            entries.append({
                "track_id": lib["id"],
                "title": (t.get("display_title") or lib["title"])[:80],
                "style": lib.get("style", ""),
                "duration": lib.get("duration"),
                "start": None,
            })
        if not entries:
            raise ValueError("no playable tracks in the tracklist")
        name = (name or "").strip() or f"Episode {time.strftime('%b %d')}"
        job = {
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "slug": f"{_slug(name)}-{time.strftime('%Y%m%d-%H%M%S')}",
            "set_id": set_id,
            "status": "queued",
            "step": "Queued",
            "progress": 0.0,
            "created": time.time(),
            "finished": None,
            "crossfade": float(min(6.0, max(0.0, crossfade))),
            "tracks": entries,
            "mood": (self.station_getter() or {}).get("mood", "dreamy"),
            "station": (self.station_getter() or {}).get("name", "Infinite Radio"),
            "outputs": {},
            "duration": None,
            "error": None,
        }
        with self.lock:
            self.jobs.insert(0, job)
            self._save()
        self.queue.put(job["id"])
        return dict(job)

    def cancel(self, job_id: str) -> bool:
        j = self.get(job_id)
        if not j or j["status"] in ("done", "error"):
            return False
        self._cancel.add(job_id)
        proc = self._proc
        if proc and proc.poll() is None and j["status"] == "encoding":
            proc.terminate()
        self._update(job_id, status="error", step="Cancelled", error="cancelled")
        return True

    def delete(self, job_id: str) -> bool:
        j = self.get(job_id)
        if not j:
            return False
        if j["status"] not in ("done", "error"):
            self.cancel(job_id)
        folder = EPISODES_DIR / j["slug"]
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        with self.lock:
            self.jobs = [x for x in self.jobs if x["id"] != job_id]
            self._save()
        return True

    def folder(self, job: dict) -> Path:
        return EPISODES_DIR / job["slug"]

    # ---------- worker ----------
    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            if job_id in self._cancel:
                self._cancel.discard(job_id)
                continue
            try:
                self._render(job_id)
            except Exception as e:  # noqa: BLE001
                print(f"[episodes] {job_id} failed: {e!r}", flush=True)
                if job_id not in self._cancel:
                    self._update(job_id, status="error", step="Failed", error=str(e)[:500])
            finally:
                self._cancel.discard(job_id)

    def _check_cancel(self, job_id: str) -> None:
        if job_id in self._cancel:
            raise RuntimeError("cancelled")

    def _render(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found. Run Install again from the Pinokio menu.")
        folder = self.folder(job)
        work = folder / "_work"
        work.mkdir(parents=True, exist_ok=True)

        # 1. Mix audio
        self._update(job_id, status="mixing", step="Mixing audio", progress=0.02)
        paths = [self.library.audio_path(t["track_id"]) for t in job["tracks"]]
        mix = mix_tracks(paths, work / "episode.wav", job["crossfade"],
                         progress=lambda f: self._update(job_id, progress=0.02 + 0.18 * f))
        tracks = job["tracks"]
        for t, start, dur in zip(tracks, mix["starts"], mix["durations"]):
            t["start"] = round(start, 2)
            t["duration"] = round(dur, 2)
        total = mix["duration"]
        self._update(job_id, tracks=tracks, duration=round(total, 2))
        self._check_cancel(job_id)

        # 2. Scene loop
        self._update(job_id, status="scene", step="Rendering scene loop", progress=0.2)
        loop_path, loop_seconds = self.scene.ensure_loop(job["mood"], job["name"],
                                                         progress=lambda f: self._update(job_id, progress=0.2 + 0.3 * f))
        self._check_cancel(job_id)

        # 3. Cards
        self._update(job_id, status="cards", step="Drawing title cards", progress=0.5)
        cards = self._render_cards(job, tracks, total, work)
        self._check_cancel(job_id)

        # 4. Encode
        self._update(job_id, status="encoding", step="Encoding video", progress=0.52)
        out_mp4 = folder / "episode.mp4"
        self._encode(ffmpeg, job_id, loop_path, work / "episode.wav", cards, total, out_mp4)
        self._check_cancel(job_id)

        # 5. Thumbnail + text outputs
        self._update(job_id, step="Writing thumbnail and description", progress=0.97)
        job["duration"] = total
        thumb = self._thumbnail(ffmpeg, job, loop_path, folder / "thumbnail.png")
        desc = self._description(job, tracks, total)
        (folder / "description.txt").write_text(desc, encoding="utf-8")
        (folder / "tracklist.json").write_text(json.dumps({
            "name": job["name"], "duration": round(total, 2), "crossfade": job["crossfade"],
            "tracks": tracks, "created": time.time(),
        }, indent=2), encoding="utf-8")
        shutil.rmtree(work, ignore_errors=True)
        outputs = {
            "video": str(out_mp4.relative_to(OUTPUT_DIR)),
            "thumbnail": str(thumb.relative_to(OUTPUT_DIR)) if thumb else None,
            "description": str((folder / "description.txt").relative_to(OUTPUT_DIR)),
            "tracklist": str((folder / "tracklist.json").relative_to(OUTPUT_DIR)),
            "folder": str(folder),
            "size": out_mp4.stat().st_size,
        }
        self._update(job_id, status="done", step="Done", progress=1.0, finished=time.time(), outputs=outputs)
        print(f"[episodes] done '{job['name']}' {total:.0f}s -> {out_mp4}", flush=True)

    # ---------- pieces ----------
    def _render_cards(self, job: dict, tracks: list[dict], total: float, work: Path) -> list[dict]:
        """Returns [{path, start, end}] including the intro card and a persistent lockup."""
        cards = []
        f_kicker, f_title, f_sub = load_font(26), load_font(54), load_font(26)
        n = len(tracks)
        for i, t in enumerate(tracks):
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            x0, y0 = 72, H - 232
            title = t["title"]
            tw = d.textlength(title, font=f_title)
            tags = ", ".join([s.strip() for s in t.get("style", "").split(",")[1:5] if s.strip()])
            box_w = int(max(560, min(W - 144, max(tw, d.textlength(tags, font=f_sub)) + 72)))
            d.rounded_rectangle([x0, y0, x0 + box_w, y0 + 160], radius=16, fill=(8, 8, 16, 165))
            d.text((x0 + 32, y0 + 24), f"NOW PLAYING  ·  {i + 1:02d} / {n:02d}", font=f_kicker, fill=(34, 211, 238, 255))
            d.text((x0 + 32, y0 + 58), title, font=f_title, fill=(245, 245, 250, 255))
            d.text((x0 + 32, y0 + 120), tags[:90], font=f_sub, fill=(176, 178, 196, 255))
            p = work / f"card{i:03d}.png"
            img.save(p)
            start = t["start"] if i else INTRO_SECONDS
            end = tracks[i + 1]["start"] if i + 1 < n else total
            if end - start > 2.0:
                cards.append({"path": p, "start": start, "end": end})
        # Intro: set name large, first seconds (also what YouTube tends to pick as a thumbnail)
        intro = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(intro)
        d.rectangle([0, 0, W, H], fill=(5, 5, 12, 110))
        f_big, f_small = load_font(110), load_font(34)
        name = job["name"]
        tw = d.textlength(name, font=f_big)
        d.text(((W - tw) / 2, H / 2 - 110), name, font=f_big, fill=(248, 248, 252, 255))
        sub = f"{job['station']}  ·  {_plural(n, 'song')}  ·  {format_timestamp(total)}"
        sw = d.textlength(sub, font=f_small)
        d.text(((W - sw) / 2, H / 2 + 30), sub, font=f_small, fill=(190, 192, 210, 255))
        p = work / "intro.png"
        intro.save(p)
        cards.insert(0, {"path": p, "start": 0.0, "end": INTRO_SECONDS})
        # Lockup: small brand, always on
        lock = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(lock)
        f_lock = load_font(24)
        text = f"INFINITE RADIO  ·  {job['station']}"
        lw = d.textlength(text, font=f_lock)
        d.text((W - lw - 64, 48), text, font=f_lock, fill=(230, 230, 240, 215))
        p = work / "lockup.png"
        lock.save(p)
        cards.append({"path": p, "start": 0.0, "end": total, "static": True})
        return cards

    def _encode(self, ffmpeg: str, job_id: str, loop: Path, wav: Path, cards: list[dict], total: float, out: Path) -> None:
        inputs = [ffmpeg, "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                  "-stream_loop", "-1", "-i", str(loop), "-i", str(wav)]
        filters = []
        prev = "[0:v]"
        for k, c in enumerate(cards):
            idx = 2 + k
            inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(c["path"])]
            lbl = f"[c{k}]"
            if c.get("static"):
                filters.append(f"[{idx}:v]format=rgba{lbl}")
                filters.append(f"{prev}{lbl}overlay=0:0:shortest=0[v{k}]")
            else:
                st, en = c["start"], c["end"]
                fo = max(st + CARD_FADE_IN, en - CARD_FADE_OUT)
                filters.append(f"[{idx}:v]format=rgba,fade=t=in:st={st:.3f}:d={CARD_FADE_IN}:alpha=1,fade=t=out:st={fo:.3f}:d={CARD_FADE_OUT}:alpha=1{lbl}")
                filters.append(f"{prev}{lbl}overlay=0:0:shortest=0:enable='between(t,{st:.3f},{en:.3f})'[v{k}]")
            prev = f"[v{k}]"
        filters.append(f"{prev}format=yuv420p[vout]")
        cmd = inputs + ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "1:a",
                        "-t", f"{total:.3f}", "-r", str(FPS),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._proc = proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        ms = int(line.split("=", 1)[1])
                        frac = min(1.0, (ms / 1_000_000) / max(total, 1))
                        self._update(job_id, progress=0.52 + 0.45 * frac, step=f"Encoding video {int(frac * 100)}%")
                    except ValueError:
                        pass
            err = proc.stderr.read()
            code = proc.wait()
        finally:
            self._proc = None
        if job_id in self._cancel:
            raise RuntimeError("cancelled")
        if code != 0:
            raise RuntimeError(f"ffmpeg encode failed: {err[-600:]}")

    def _thumbnail(self, ffmpeg: str, job: dict, loop: Path, out: Path) -> Path | None:
        frame = out.with_name("_frame.png")
        res = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", "2", "-i", str(loop), "-frames:v", "1", str(frame)],
                             capture_output=True, text=True)
        if res.returncode != 0 or not frame.exists():
            return None
        img = Image.open(frame).convert("RGBA").resize((1280, 720), Image.LANCZOS)
        shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(shade)
        d.rectangle([0, 0, 1280, 720], fill=(5, 5, 12, 95))
        img = Image.alpha_composite(img, shade)
        d = ImageDraw.Draw(img)
        f_big, f_small = load_font(84), load_font(28)
        name = job["name"]
        # Wrap long names onto two lines
        words, lines, cur = name.split(), [], ""
        for w_ in words:
            trial = (cur + " " + w_).strip()
            if d.textlength(trial, font=f_big) > 1120 and cur:
                lines.append(cur)
                cur = w_
            else:
                cur = trial
        lines.append(cur)
        y = 360 - 48 * len(lines)
        for ln in lines[:3]:
            tw = d.textlength(ln, font=f_big)
            d.text(((1280 - tw) / 2, y), ln, font=f_big, fill=(248, 248, 252, 255))
            y += 96
        sub = f"{_plural(len(job['tracks']), 'song')}  ·  {format_timestamp(job['duration'] or 0)}  ·  {job['station']}"
        sw = d.textlength(sub, font=f_small)
        d.text(((1280 - sw) / 2, y + 10), sub, font=f_small, fill=(200, 202, 220, 255))
        img.convert("RGB").save(out, "PNG")
        frame.unlink(missing_ok=True)
        return out

    def _description(self, job: dict, tracks: list[dict], total: float) -> str:
        lines = [job["name"], "",
                 f"{_plural(len(tracks), 'original song')} composed and sung by a local AI radio station, {format_timestamp(total)} total.", "",
                 "Tracklist"]
        # YouTube chapters: first stamp must be 0:00, at least three, each 10 s or more.
        lines.append(f"0:00 {tracks[0]['title']}")
        for t in tracks[1:]:
            lines.append(f"{format_timestamp(t['start'])} {t['title']}")
        lines += ["",
                  "Made with Infinite Radio: https://github.com/cocktailpeanut/infinite-radio",
                  "Music generated locally with YuE2 (m-a-p) running on audio.cpp (0xShug0).",
                  "Lyrics and vocals are AI generated. This video contains AI-generated audio.",
                  "YuE2 weights are licensed CC BY-NC 4.0 (non-commercial).", ""]
        return "\n".join(lines)
