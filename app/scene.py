"""Scene: the visual behind an episode.

The user supplies one file: an image (we add procedural motion), or a GIF /
video loop (we normalize and repeat it). With no file, a typographic card in
the mood palette is used. Everything renders to a seamless 1920x1080 30 fps
loop that ffmpeg repeats for the whole episode.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from engine import APP_DIR

DATA_DIR = APP_DIR / "data"
SCENE_DIR = DATA_DIR / "scene"
CACHE_DIR = SCENE_DIR / "cache"
SETTINGS_FILE = DATA_DIR / "scene.json"

W, H, FPS = 1920, 1080, 30
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".gif", ".mp4", ".webm", ".mov", ".m4v", ".mkv"}

DEFAULT_SETTINGS = {
    "camera": True,          # slow push and drift
    "weather": "rain",       # none | rain | snow | leaves | dust
    "weather_density": 0.6,  # 0..1
    "fog": True,
    "flicker": True,
    "grain": True,
    "vignette": True,
    "pingpong": False,       # video loops only
    "loop_seconds": 24,
    "hw_encode": True,       # use VideoToolbox / NVENC / AMF / QSV when available
}

MOOD_PALETTES = {
    "dreamy": ("#8b5cf6", "#22d3ee", "#0b0b18"),
    "euphoric": ("#f59e0b", "#f43f7a", "#140a12"),
    "chill": ("#34d399", "#0ea5e9", "#07131a"),
    "nostalgic": ("#d97706", "#c084fc", "#140d08"),
    "romantic": ("#f43f7a", "#a855f7", "#140810"),
    "energetic": ("#22d3ee", "#f43f7a", "#070b14"),
    "dark": ("#6366f1", "#111827", "#05060a"),
    "melancholic": ("#60a5fa", "#475569", "#070a12"),
}


def find_ffmpeg() -> str | None:
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p
    candidates = []
    for env in ("CONDA_PREFIX", "PINOKIO_HOME"):
        v = os.environ.get(env)
        if v:
            candidates += [Path(v) / "bin" / "ffmpeg", Path(v) / "bin" / "miniconda" / "bin" / "ffmpeg",
                           Path(v) / "Library" / "bin" / "ffmpeg.exe", Path(v) / "bin" / "miniconda" / "Library" / "bin" / "ffmpeg.exe"]
    home = Path.home()
    candidates += [home / "pinokio" / "bin" / "miniconda" / "bin" / "ffmpeg", Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf",
        "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


class Scene:
    def __init__(self):
        SCENE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.state = {"kind": "none", "file": None, "original_name": None, "settings": dict(DEFAULT_SETTINGS)}
        if SETTINGS_FILE.exists():
            try:
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                self.state.update({k: saved[k] for k in ("kind", "file", "original_name") if k in saved})
                self.state["settings"].update(saved.get("settings", {}))
            except Exception:
                pass
        if self.state["file"] and not (SCENE_DIR / self.state["file"]).exists():
            self.state.update({"kind": "none", "file": None, "original_name": None})

    def _save(self) -> None:
        SETTINGS_FILE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def snapshot(self) -> dict:
        with self.lock:
            s = dict(self.state)
            s["settings"] = dict(self.state["settings"])
            s["ffmpeg"] = find_ffmpeg() is not None
            s["defaults"] = DEFAULT_SETTINGS
            return s

    def update_settings(self, patch: dict) -> dict:
        with self.lock:
            for k, v in patch.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                if k == "weather" and v not in ("none", "rain", "snow", "leaves", "dust"):
                    continue
                if k == "weather_density":
                    v = float(min(1.0, max(0.0, float(v))))
                if k == "loop_seconds":
                    v = int(min(60, max(8, int(v))))
                if isinstance(DEFAULT_SETTINGS[k], bool):
                    v = bool(v)
                self.state["settings"][k] = v
            self._save()
        return self.snapshot()

    def save_upload(self, filename: str, data: bytes) -> dict:
        ext = Path(filename).suffix.lower()
        if ext in IMAGE_EXT:
            kind = "image"
        elif ext in VIDEO_EXT:
            kind = "video"
        else:
            raise ValueError("unsupported file type; use an image (jpg, png, webp) or a loop (gif, mp4, webm, mov)")
        if kind == "image":
            # Re-encode through Pillow so odd formats and EXIF rotation are handled once.
            img = Image.open(__import__("io").BytesIO(data))
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            img = img.convert("RGB")
            dest = SCENE_DIR / "scene.png"
            img.save(dest, "PNG")
        else:
            dest = SCENE_DIR / f"scene{ext}"
            dest.write_bytes(data)
        with self.lock:
            for old in SCENE_DIR.glob("scene.*"):
                if old != dest:
                    old.unlink(missing_ok=True)
            self.state.update({"kind": kind, "file": dest.name, "original_name": filename})
            self._save()
        return self.snapshot()

    def clear(self) -> dict:
        with self.lock:
            for old in SCENE_DIR.glob("scene.*"):
                old.unlink(missing_ok=True)
            self.state.update({"kind": "none", "file": None, "original_name": None})
            self._save()
        return self.snapshot()

    def preview_path(self) -> Path | None:
        with self.lock:
            if self.state["file"]:
                return SCENE_DIR / self.state["file"]
        return None

    # ---------- loop rendering ----------
    def loop_key(self, mood: str, title: str) -> str:
        with self.lock:
            src = SCENE_DIR / self.state["file"] if self.state["file"] else None
            stamp = f"{src.stat().st_mtime_ns}:{src.stat().st_size}" if src and src.exists() else "none"
            visual = {k: v for k, v in self.state["settings"].items() if k != "hw_encode"}
            payload = json.dumps({"kind": self.state["kind"], "stamp": stamp, "settings": visual,
                                  "mood": mood if self.state["kind"] == "none" else "", "title": title if self.state["kind"] == "none" else ""}, sort_keys=True)
        return hashlib.sha1(payload.encode()).hexdigest()[:16]

    def ensure_loop(self, mood: str, title: str, progress=None) -> tuple[Path, float]:
        """Return (loop.mp4 path, loop seconds), rendering if not cached."""
        key = self.loop_key(mood, title)
        out = CACHE_DIR / f"loop-{key}.mp4"
        meta = CACHE_DIR / f"loop-{key}.json"
        if out.exists() and meta.exists():
            try:
                return out, float(json.loads(meta.read_text(encoding="utf-8"))["seconds"])
            except Exception:
                pass
        with self.lock:
            kind, file, settings = self.state["kind"], self.state["file"], dict(self.state["settings"])
        # Keep a few recent loops; never touch one another render may be reading.
        cached = sorted(CACHE_DIR.glob("loop-*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in cached[3:]:
            for p in (stale, stale.with_suffix(".json")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        if kind == "video":
            seconds = _normalize_video_loop(SCENE_DIR / file, out, settings, progress)
        else:
            base = Image.open(SCENE_DIR / file).convert("RGB") if kind == "image" else _fallback_card(mood, title)
            seconds = render_motion_loop(base, out, settings, mood, progress)
        meta.write_text(json.dumps({"seconds": seconds}), encoding="utf-8")
        return out, seconds


# ---------- helpers ----------
def _fallback_card(mood: str, title: str) -> Image.Image:
    c1, c2, bg = MOOD_PALETTES.get(mood, MOOD_PALETTES["dreamy"])
    img = Image.new("RGB", (W, H), _hex(bg))
    glow = Image.new("RGB", (W, H), _hex(bg))
    d = ImageDraw.Draw(glow)
    d.ellipse([W * 0.05, H * 0.1, W * 0.65, H * 1.1], fill=_hex(c1))
    d.ellipse([W * 0.45, -H * 0.2, W * 1.05, H * 0.7], fill=_hex(c2))
    glow = glow.filter(ImageFilter.GaussianBlur(220))
    img = Image.blend(img, glow, 0.55)
    d = ImageDraw.Draw(img)
    f1, f2 = load_font(96), load_font(30)
    d.text((W * 0.08, H * 0.40), title[:40], font=f1, fill=(245, 245, 250))
    d.text((W * 0.08, H * 0.40 + 118), "INFINITE RADIO", font=f2, fill=(200, 200, 215))
    return img


def _fit_oversized(base: Image.Image, oversize: float = 1.12) -> Image.Image:
    scale = max((W * oversize) / base.width, (H * oversize) / base.height)
    return base.resize((int(round(base.width * scale)), int(round(base.height * scale))), Image.LANCZOS)


def _periodic_fog(rng: np.random.Generator) -> np.ndarray:
    small = rng.uniform(0, 1, (10, 20)).astype(np.float32)
    tiled = np.concatenate([small, small, small], axis=1)
    img = Image.fromarray((tiled * 255).astype(np.uint8)).resize((W * 3, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(70))
    arr = np.asarray(img).astype(np.float32) / 255.0
    return arr[:, W:2 * W]  # one period, seamless when wrapped


class _Particles:
    """Loop-periodic particle field (rain, snow, leaves, dust)."""

    def __init__(self, kind: str, density: float, loop: float, rng: np.random.Generator):
        self.kind = kind
        pad = 120
        self.pad = pad
        counts = {"rain": 480, "snow": 220, "leaves": 60, "dust": 140}
        n = int(counts[kind] * (0.35 + density))
        self.n = n
        self.x0 = rng.uniform(0, W, n).astype(np.float32)
        self.y0 = rng.uniform(0, H + pad, n).astype(np.float32)
        span = H + pad
        # Integer cycles per loop keep the field seamless.
        if kind == "rain":
            cyc = rng.integers(3, 6, n)
            self.len = rng.uniform(20, 48, n).astype(np.float32)
            self.bright = rng.uniform(0.25, 0.55, n).astype(np.float32)
            self.slant = -0.14
            self.radius = 0
        elif kind == "snow":
            cyc = rng.integers(1, 3, n)
            self.len = np.zeros(n, np.float32)
            self.bright = rng.uniform(0.35, 0.8, n).astype(np.float32)
            self.slant = 0.0
            self.radius = 2
        elif kind == "leaves":
            cyc = rng.integers(1, 2, n)
            self.len = np.zeros(n, np.float32)
            self.bright = rng.uniform(0.3, 0.6, n).astype(np.float32)
            self.slant = 0.25
            self.radius = 3
        else:  # dust
            cyc = rng.integers(1, 2, n)
            self.len = np.zeros(n, np.float32)
            self.bright = rng.uniform(0.12, 0.3, n).astype(np.float32)
            self.slant = 0.0
            self.radius = 1
        self.speed = (cyc * span / loop).astype(np.float32)  # px per second
        self.wobble_phase = rng.uniform(0, 2 * math.pi, n).astype(np.float32)
        self.wobble_amp = rng.uniform(6, 26, n).astype(np.float32) if kind in ("snow", "leaves", "dust") else np.zeros(n, np.float32)
        self.loop = loop
        self.span = span
        self.color = {"rain": (225, 238, 255), "snow": (250, 250, 255), "leaves": (235, 160, 90), "dust": (255, 235, 200)}[kind]

    def layer(self, t: float) -> np.ndarray:
        """Returns float32 alpha accumulation (H, W) in [0, 1]."""
        acc = np.zeros((H, W), np.float32)
        direction = 1.0 if self.kind != "dust" else 0.35
        y = (self.y0 + self.speed * t * direction) % self.span - self.pad / 2
        wob = self.wobble_amp * np.sin(2 * math.pi * (t / self.loop) * 2 + self.wobble_phase)
        x = (self.x0 + y * self.slant + wob) % W
        if self.kind == "rain":
            steps = 24
            k = np.linspace(0, 1, steps, dtype=np.float32)[:, None]
            xs = (x[None, :] - self.len[None, :] * self.slant * k).ravel()
            ys = (y[None, :] - self.len[None, :] * k).ravel()
            br = np.repeat(self.bright[None, :], steps, axis=0).ravel()
        else:
            xs, ys, br = x, y, self.bright
        r = self.radius
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if dx * dx + dy * dy > r * r + r:
                    continue
                xi = (xs + dx).astype(np.int32)
                yi = (ys + dy).astype(np.int32)
                m = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
                np.add.at(acc, (yi[m], xi[m]), br[m])
        return np.clip(acc, 0, 1)


def render_motion_loop(base: Image.Image, out: Path, settings: dict, mood: str, progress=None) -> float:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    loop = float(settings.get("loop_seconds", 24))
    n_frames = int(loop * FPS)
    rng = np.random.default_rng(1234)
    src = _fit_oversized(base)
    BW, BH = src.size

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    rad = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    vignette = np.clip(1.0 - 0.38 * np.clip(rad - 0.5, 0, 1) ** 1.5, 0, 1)[..., None].astype(np.float32) if settings.get("vignette", True) else None
    fog = _periodic_fog(rng) if settings.get("fog", True) else None
    fog_mask = np.exp(-((yy - H * 0.55) / (H * 0.3)) ** 2).astype(np.float32) if fog is not None else None
    weather = settings.get("weather", "rain")
    particles = _Particles(weather, float(settings.get("weather_density", 0.6)), loop, rng) if weather != "none" else None
    pcolor = np.array(particles.color, np.float32) if particles else None
    camera = bool(settings.get("camera", True))
    flicker_on = bool(settings.get("flicker", True))
    grain_on = bool(settings.get("grain", True))

    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(n_frames):
            t = i / FPS
            ph = 2 * math.pi * t / loop
            if camera:
                z = 1.035 + 0.03 * math.sin(ph)
                cw, ch = int(W / z), int(H / z)
                cx = (BW - cw) / 2 + 28 * math.sin(ph + 0.9)
                cy = (BH - ch) / 2 + 12 * math.sin(2 * ph + 2.1)
            else:
                cw, ch = int(W / 1.06), int(H / 1.06)
                cx, cy = (BW - cw) / 2, (BH - ch) / 2
            frame = src.crop((int(cx), int(cy), int(cx) + cw, int(cy) + ch)).resize((W, H), Image.BILINEAR)
            a = np.asarray(frame).astype(np.float32)
            if flicker_on:
                f = 1.0 + 0.022 * math.sin(ph * 11) + 0.01 * math.sin(ph * 37 + 0.7)
                a *= np.array([f * 1.008, f, f * 0.988], np.float32)
            if fog is not None:
                off = int((t / loop) * W) % W
                band = np.concatenate([fog[:, off:], fog[:, :off]], axis=1)
                a += (band * fog_mask * 20)[..., None]
            if particles is not None:
                alpha = particles.layer(t)
                if weather == "rain":
                    alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))).astype(np.float32) / 255.0
                alpha = (alpha * (0.55 if weather == "rain" else 0.8))[..., None]
                a = a * (1 - alpha) + pcolor * alpha
            if grain_on:
                a += rng.normal(0, 3.0, (H, W, 1)).astype(np.float32)
            if vignette is not None:
                a *= vignette
            proc.stdin.write(np.clip(a, 0, 255).astype(np.uint8).tobytes())
            if progress and i % 15 == 0:
                progress(i / n_frames)
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        code = proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed while encoding the scene loop: {err[-400:]}")
    return loop


def _probe_duration(ffmpeg: str, path: Path) -> float:
    ffprobe = str(Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe")))
    try:
        res = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def _normalize_video_loop(src: Path, out: Path, settings: dict, progress=None) -> float:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    vf = [f"scale={W}:{H}:force_original_aspect_ratio=increase", f"crop={W}:{H}", f"fps={FPS}", "format=yuv420p"]
    if settings.get("grain", True):
        vf.append("noise=alls=6:allf=t")
    if settings.get("vignette", True):
        vf.append("vignette=PI/5")
    chain = ",".join(vf)
    if settings.get("pingpong", False):
        filt = f"[0:v]{chain},split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]"
    else:
        filt = f"[0:v]{chain}[v]"
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    if src.suffix.lower() == ".gif":
        cmd += ["-ignore_loop", "0", "-t", "60"]  # stop an infinite GIF after one minute of material
    cmd += ["-i", str(src), "-filter_complex", filt, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-movflags", "+faststart", str(out)]
    res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to prepare the loop: {res.stderr[-400:]}")
    if progress:
        progress(1.0)
    return _probe_duration(ffmpeg, out)
