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
                self.jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
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
        tmp.write_text(json.dumps(self.jobs, indent=2), encoding="utf-8")
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
    # Card geometry: every card image shares one canvas so they can form a single
    # variable-frame-rate sequence (ffmpeg's concat demuxer needs equal sizes).
    CARD_X, CARD_H = 72, 160
    CARD_W = W - 144
    CARD_Y = H - 232

    def _render_cards(self, job: dict, tracks: list[dict], total: float, work: Path) -> dict:
        """Draw the intro card, the lockup, and one VFR card sequence for all songs.

        Returns {"intro": path, "lockup": path, "cards": ffconcat path, "lockup_x": int, "lockup_y": int}.
        Instead of one full-frame input per song (which ffmpeg decodes on every
        output frame), the cards are small images with the fades baked in, listed
        in an ffconcat file with explicit durations, so ffmpeg decodes a new
        image only when the picture actually changes.
        """
        f_kicker, f_title, f_sub = load_font(26), load_font(54), load_font(26)
        n = len(tracks)
        cards_dir = work / "cards"
        cards_dir.mkdir(exist_ok=True)
        fade_in_frames = int(round(CARD_FADE_IN * FPS))
        fade_out_frames = int(round(CARD_FADE_OUT * FPS))
        frame_dt = 1.0 / FPS
        empty = Image.new("RGBA", (self.CARD_W, self.CARD_H), (0, 0, 0, 0))
        empty_path = cards_dir / "empty.png"
        empty.save(empty_path)
        lines = ["ffconcat version 1.0"]
        cursor = 0.0

        def add(path: Path, duration: float) -> None:
            nonlocal cursor
            if duration <= 0:
                return
            lines.append(f"file '{path.as_posix()}'")
            lines.append(f"duration {duration:.4f}")
            cursor += duration

        def faded(base: Image.Image, alpha: float) -> Image.Image:
            out = base.copy()
            out.putalpha(out.getchannel("A").point(lambda v: int(v * alpha)))
            return out

        for i, t in enumerate(tracks):
            base = Image.new("RGBA", (self.CARD_W, self.CARD_H), (0, 0, 0, 0))
            d = ImageDraw.Draw(base)
            title = t["title"]
            tw = d.textlength(title, font=f_title)
            tags = ", ".join([x.strip() for x in t.get("style", "").split(",")[1:5] if x.strip()])
            box_w = int(max(560, min(self.CARD_W, max(tw, d.textlength(tags, font=f_sub)) + 72)))
            d.rounded_rectangle([0, 0, box_w, self.CARD_H], radius=16, fill=(8, 8, 16, 165))
            d.text((32, 24), f"NOW PLAYING  ·  {i + 1:02d} / {n:02d}", font=f_kicker, fill=(34, 211, 238, 255))
            d.text((32, 58), title, font=f_title, fill=(245, 245, 250, 255))
            d.text((32, 120), tags[:90], font=f_sub, fill=(176, 178, 196, 255))
            start = t["start"] if i else INTRO_SECONDS
            end = tracks[i + 1]["start"] if i + 1 < n else total
            if end - start <= 2.0:
                continue
            # gap before this card (transparent)
            add(empty_path, start - cursor)
            # fade in, hold, fade out
            for k in range(fade_in_frames):
                p = cards_dir / f"c{i:03d}_in{k:02d}.png"
                faded(base, (k + 1) / fade_in_frames).save(p)
                add(p, frame_dt)
            hold_path = cards_dir / f"c{i:03d}_hold.png"
            base.save(hold_path)
            fade_out_start = max(start + CARD_FADE_IN, end - CARD_FADE_OUT)
            add(hold_path, fade_out_start - cursor)
            for k in range(fade_out_frames):
                p = cards_dir / f"c{i:03d}_out{k:02d}.png"
                faded(base, (fade_out_frames - k) / fade_out_frames).save(p)
                add(p, frame_dt)
        # trailing transparent segment to the end; the last entry is repeated so
        # the concat demuxer honours its duration.
        add(empty_path, max(frame_dt, total + 1.0 - cursor))
        lines.append(f"file '{empty_path.as_posix()}'")
        concat_path = work / "cards.ffconcat"
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
        intro_path = work / "intro.png"
        intro.save(intro_path)

        # Lockup: small brand image, placed top-right by the overlay
        f_lock = load_font(24)
        text = f"INFINITE RADIO  ·  {job['station']}"
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        lw = int(probe.textlength(text, font=f_lock)) + 8
        lock = Image.new("RGBA", (max(lw, 8), 40), (0, 0, 0, 0))
        ImageDraw.Draw(lock).text((4, 4), text, font=f_lock, fill=(230, 230, 240, 215))
        lock_path = work / "lockup.png"
        lock.save(lock_path)
        return {"intro": intro_path, "lockup": lock_path, "cards": concat_path, "lockup_x": W - lw - 64, "lockup_y": 44}

    # ---------- encoder selection ----------
    _encoder_cache: dict[str, list[str]] = {}

    def _video_encoder(self, ffmpeg: str, allow_hw: bool) -> list[str]:
        """Return encoder args. Hardware encoders when available and allowed, else libx264."""
        key = f"{ffmpeg}:{allow_hw}"
        if key in self._encoder_cache:
            return self._encoder_cache[key]
        software = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p"]
        chosen = software
        if allow_hw:
            try:
                listing = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, encoding="utf-8", errors="replace", timeout=30).stdout
            except Exception:
                listing = ""
            candidates = [
                ("h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-b:v", "6M", "-maxrate", "8M", "-bufsize", "16M", "-profile:v", "high", "-pix_fmt", "yuv420p"]),
                ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "6M", "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p"]),
                ("h264_amf", ["-c:v", "h264_amf", "-quality", "balanced", "-b:v", "6M", "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p"]),
                ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "medium", "-b:v", "6M", "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "nv12"]),
            ]
            for name, args in candidates:
                if name not in listing:
                    continue
                # Probe: encode a few frames so a listed-but-unusable encoder falls through.
                probe = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30", "-t", "0.5",
                                        *args, "-f", "null", "-"], capture_output=True, encoding="utf-8", errors="replace", timeout=60)
                if probe.returncode == 0:
                    chosen = args
                    print(f"[episodes] using hardware encoder {name}", flush=True)
                    break
        self._encoder_cache[key] = chosen
        return chosen

    def _encode(self, ffmpeg: str, job_id: str, loop: Path, wav: Path, cards: dict, total: float, out: Path) -> None:
        settings = self.scene.snapshot().get("settings", {})
        enc = self._video_encoder(ffmpeg, bool(settings.get("hw_encode", True)))
        intro_fo = max(CARD_FADE_IN, INTRO_SECONDS - CARD_FADE_OUT)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               "-stream_loop", "-1", "-i", str(loop),                                   # 0: scene loop
               "-i", str(wav),                                                           # 1: mix
               "-loop", "1", "-framerate", str(FPS), "-t", f"{INTRO_SECONDS:.3f}", "-i", str(cards["intro"]),  # 2: intro (6 s only)
               "-safe", "0", "-f", "concat", "-i", str(cards["cards"]),                 # 3: card sequence (VFR)
               "-loop", "1", "-framerate", "1", "-i", str(cards["lockup"]),              # 4: lockup (1 fps)
               "-filter_complex",
               f"[2:v]format=rgba,fade=t=in:st=0:d={CARD_FADE_IN}:alpha=1,fade=t=out:st={intro_fo:.3f}:d={CARD_FADE_OUT}:alpha=1[intro];"
               f"[3:v]format=rgba[cards];[4:v]format=rgba[lock];"
               f"[0:v][intro]overlay=0:0:eof_action=pass[v1];"
               f"[v1][cards]overlay={self.CARD_X}:{self.CARD_Y}:eof_action=pass[v2];"
               f"[v2][lock]overlay={cards['lockup_x']}:{cards['lockup_y']}:eof_action=repeat,format=yuv420p[vout]",
               "-map", "[vout]", "-map", "1:a", "-t", f"{total:.3f}", "-r", str(FPS),
               *enc, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
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
                             capture_output=True, encoding="utf-8", errors="replace")
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
