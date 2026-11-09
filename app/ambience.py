"""Ambience: a quiet bed of sound (rain, café, subway...) under the music.

Three sources feed one "pool" of takes:
  generate : Stable Audio 3 Small SFX via audiocpp_cli. The model (1.7 GB) is
             downloaded inside the app the first time someone chooses Generate.
  upload   : any audio (or video) file the user provides.
  scene    : the audio track of the scene video.

A generated pool is ~10 takes; unsteady takes and tone outliers are dropped,
the rest are matched in loudness, tone and stereo width so crossfades between
them are inaudible. Playback chains takes in shuffled order with 3 s
equal-power crossfades and never repeats a take back to back, so there is no
fixed loop period. A pool is built once per ambience choice and reused.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

from engine import APP_DIR
from scene import find_ffmpeg

DATA_DIR = APP_DIR / "data" / "ambience"
POOLS_DIR = DATA_DIR / "pools"
SETTINGS_FILE = DATA_DIR / "settings.json"

MODEL_REPO = "audio-cpp/audio.cpp-gguf"
MODEL_REPO_DIR = "Stable-Audio-3-Small-SFX-GGUF"
MODEL_NAME = "stable-audio-3-small-sfx-q8_0.gguf"
MODEL_DIR = APP_DIR / "models" / "stable-audio-sfx"
MODEL_FILE = MODEL_DIR / MODEL_NAME
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{MODEL_REPO_DIR}/{MODEL_NAME}"
MODEL_TREE_URL = f"https://huggingface.co/api/models/{MODEL_REPO}/tree/main/{MODEL_REPO_DIR}"
MODEL_SIZE_HINT = 1_683_576_512
LICENSE = {
    "name": "Stability AI Community License",
    "url": "https://huggingface.co/stabilityai/stable-audio-3-small-sfx/blob/main/LICENSE.md",
    "summary": "Free for personal use and for businesses under USD 1M yearly revenue. Commercial use requires "
               "registering with Stability AI and showing \"Powered by Stability AI\".",
}

SR = 48000
TARGET_RMS_DB = -30.0
GEN_TAKES = 10       # first batch
TOP_UP = 4           # extra takes per batch when too few pass the filters
MAX_TAKES = 18       # hard cap on generated takes per pool
MIN_KEEP = 5         # aim for at least this many matched takes
KEEP_TAKES = 6
STEADY_WINDOW_S = 2.0  # rain and crowds flutter second to second; judge steadiness over 2 s
TAKE_SECONDS = 18
TRIM_SECONDS = 2.0
XF_SECONDS = 3.0
MAX_UPLOAD_SECONDS = 300
STEADY_SUFFIX = "continuous, steady, uninterrupted, constant background ambience, no music"
BANDS = [(20, 150), (150, 500), (500, 2000), (2000, 6000), (6000, 16000)]

PRESETS = [
    {"id": "rain", "name": "Rain on window", "prompt": "steady rain falling on a window, constant even hiss, cozy room, no thunder"},
    {"id": "cafe", "name": "Café", "prompt": "coffee shop ambience, quiet distant chatter, soft clinking cups, espresso machine"},
    {"id": "subway", "name": "Subway", "prompt": "subway train interior, low rumble and steady rail rhythm"},
    {"id": "vinyl", "name": "Vinyl crackle", "prompt": "warm vinyl record crackle and soft surface hiss"},
    {"id": "city", "name": "Night city", "prompt": "city at night heard from an apartment window, distant traffic, soft wind"},
    {"id": "fire", "name": "Fireplace", "prompt": "crackling fireplace, soft embers, quiet cabin"},
    {"id": "ocean", "name": "Ocean", "prompt": "gentle ocean waves on a sandy beach, soft wind"},
    {"id": "forest", "name": "Forest night", "prompt": "forest at night, crickets, soft breeze in the leaves"},
]

DEFAULT_SETTINGS = {"enabled": False, "pool_id": None, "level_db": -18, "duck": True}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- audio helpers
def rms_db(a: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(a, dtype=np.float64))) + 1e-9))


def band_db(x: np.ndarray) -> np.ndarray:
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.array([10 * np.log10(spec[(f >= lo) & (f < hi)].mean() + 1e-12) for lo, hi in BANDS])


def tone_shape(a: np.ndarray) -> np.ndarray:
    b = band_db(a.mean(1))
    return b - b.mean()


def width_db(a: np.ndarray) -> float:
    return rms_db((a[:, 0] - a[:, 1]) / 2) - rms_db((a[:, 0] + a[:, 1]) / 2)


def level_spread(a: np.ndarray, window_s: float = STEADY_WINDOW_S) -> float:
    m, w = a.mean(1), int(SR * window_s)
    lv = [rms_db(m[i:i + w]) for i in range(0, len(m) - w + 1, w)]
    return max(lv) - min(lv) if lv else 99.0


def normalize(a: np.ndarray, target: float = TARGET_RMS_DB) -> np.ndarray:
    return (a * 10 ** ((target - rms_db(a)) / 20)).astype(np.float32)


def eq_toward(a: np.ndarray, target_shape: np.ndarray) -> np.ndarray:
    """Gentle per-band EQ (max ±6 dB) so a take's tone matches the pool median."""
    gains = np.clip(target_shape - tone_shape(a), -6, 6)
    spec = np.fft.rfft(a, axis=0)
    f = np.fft.rfftfreq(len(a), 1 / SR)
    centers = np.array([np.sqrt(max(lo, 1) * hi) for lo, hi in BANDS])
    curve = np.interp(np.log10(np.maximum(f, 1)), np.log10(centers), gains)
    spec *= (10 ** (curve / 20))[:, None]
    return normalize(np.fft.irfft(spec, n=len(a), axis=0).astype(np.float32))


def width_toward(a: np.ndarray, target: float) -> np.ndarray:
    mid, side = (a[:, 0] + a[:, 1]) / 2, (a[:, 0] - a[:, 1]) / 2
    side = side * 10 ** (np.clip(target - width_db(a), -6, 6) / 20)
    return normalize(np.stack([mid + side, mid - side], 1).astype(np.float32))


def decode(path: Path, max_seconds: float | None = None) -> np.ndarray:
    """Any audio/video file -> float32 (n, 2) at 48 kHz, via ffmpeg."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    cmd = [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(SR)]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-f", "s16le", "-"]
    res = subprocess.run(cmd, capture_output=True, timeout=600)
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError("could not read audio from that file: " + res.stderr.decode("utf-8", "replace")[-200:])
    return (np.frombuffer(res.stdout, dtype="<i2").astype(np.float32) / 32768).reshape(-1, 2)


def write_wav(path: Path, a: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        return a.reshape(-1, w.getnchannels())


def encode_m4a(src: Path, dst: Path) -> bool:
    """Small AAC copy of a take for the browser player (phones on the network)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    res = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(src), "-c:a", "aac", "-b:a", "160k", str(dst)],
                         capture_output=True, timeout=120)
    return res.returncode == 0 and dst.exists()


# ---------------------------------------------------------------- bed (streaming)
class Bed:
    """Endless ambience: takes chained in shuffled order with equal-power crossfades."""

    def __init__(self, takes: list[np.ndarray], seed: int = 0, xf_s: float = XF_SECONDS):
        self.takes = takes
        self.rng = random.Random(seed)
        self.xf = int(SR * xf_s)
        self.bag: list[int] = []
        self.last = -1
        self.buf = np.zeros((0, 2), np.float32)
        self.tail: np.ndarray | None = None
        t = np.linspace(0, 1, self.xf, dtype=np.float32)[:, None]
        self.fi, self.fo = np.sqrt(t), np.sqrt(1 - t)

    def _next_index(self) -> int:
        if len(self.takes) == 1:
            return 0
        if not self.bag:
            self.bag = list(range(len(self.takes)))
            self.rng.shuffle(self.bag)
            if self.bag[0] == self.last:  # never the same take twice in a row
                self.bag.append(self.bag.pop(0))
        self.last = self.bag.pop(0)
        return self.last

    def _append_take(self) -> None:
        b = self.takes[self._next_index()]
        xf = min(self.xf, len(b) // 3)
        if self.tail is None:
            piece = b[:-xf]
        else:
            xf = min(xf, len(self.tail))
            if xf == self.xf:
                fi, fo = self.fi, self.fo
            else:
                t = np.linspace(0, 1, xf, dtype=np.float32)[:, None]
                fi, fo = np.sqrt(t), np.sqrt(1 - t)
            piece = np.concatenate([self.tail[-xf:] * fo + b[:xf] * fi, b[xf:-xf]], 0)
        self.tail = b[-xf:]
        self.buf = np.concatenate([self.buf, piece], 0)

    def read(self, n: int) -> np.ndarray:
        while len(self.buf) < n:
            self._append_take()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


# ---------------------------------------------------------------- manager
class Ambience:
    def __init__(self, engine, gpu_lock: threading.Lock):
        self.engine = engine
        self.gpu_lock = gpu_lock
        self.lock = threading.Lock()
        self.protected = lambda: set()  # pool ids still needed by queued episode jobs (set by server)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        POOLS_DIR.mkdir(parents=True, exist_ok=True)
        self.settings = dict(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                self.settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        if self.settings.get("pool_id") and not self.pool_exists(self.settings["pool_id"]):
            self.settings.update({"pool_id": None, "enabled": False})
        self.job: dict | None = None
        self._cancel = threading.Event()

    # ---------- persistence / state ----------
    def _save(self) -> None:
        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def pool_dir(self, pool_id: str) -> Path:
        return POOLS_DIR / pool_id

    def pool_exists(self, pool_id: str | None) -> bool:
        return bool(pool_id) and (self.pool_dir(pool_id) / "manifest.json").exists()

    def manifest(self, pool_id: str | None) -> dict | None:
        if not self.pool_exists(pool_id):
            return None
        try:
            return json.loads((self.pool_dir(pool_id) / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def model_status(self) -> dict:
        part = MODEL_FILE.with_suffix(MODEL_FILE.suffix + ".part")
        return {
            "installed": MODEL_FILE.exists(),
            "size": MODEL_FILE.stat().st_size if MODEL_FILE.exists() else MODEL_SIZE_HINT,
            "partial": part.stat().st_size if part.exists() else 0,
            "license": LICENSE,
        }

    def brief(self) -> dict:
        man = self.manifest(self.settings.get("pool_id"))
        job = dict(self.job) if self.job else None
        return {
            "enabled": bool(self.settings.get("enabled")) and man is not None,
            "pool_id": man["id"] if man else None,
            "label": man["label"] if man else None,
            "level_db": self.settings.get("level_db", -18),
            "duck": bool(self.settings.get("duck", True)),
            "job": job,
        }

    def snapshot(self) -> dict:
        man = self.manifest(self.settings.get("pool_id"))
        return {
            "settings": dict(self.settings),
            "pool": ({**man, "takes": [{"index": i, "seconds": t["seconds"], "url": f"/api/ambience/pools/{man['id']}/takes/{i}"}
                                       for i, t in enumerate(man["takes"])]} if man else None),
            "model": self.model_status(),
            "job": dict(self.job) if self.job else None,
            "presets": PRESETS,
        }

    def update_settings(self, patch: dict) -> dict:
        with self.lock:
            if "enabled" in patch:
                self.settings["enabled"] = bool(patch["enabled"]) and self.pool_exists(self.settings.get("pool_id"))
            if "level_db" in patch:
                self.settings["level_db"] = int(max(-36, min(-6, int(patch["level_db"]))))
            if "duck" in patch:
                self.settings["duck"] = bool(patch["duck"])
            self._save()
        return self.snapshot()

    def clear(self) -> dict:
        with self.lock:
            self.settings.update({"enabled": False, "pool_id": None})
            self._save()
        return self.snapshot()

    def job_snapshot(self) -> dict | None:
        """What an episode render should use, frozen at job creation."""
        man = self.manifest(self.settings.get("pool_id"))
        if not (self.settings.get("enabled") and man):
            return None
        return {"pool_id": man["id"], "label": man["label"], "source": man["source"],
                "level_db": self.settings.get("level_db", -18), "duck": bool(self.settings.get("duck", True)),
                "seed": random.randrange(1 << 30)}

    # ---------- jobs ----------
    def busy(self) -> bool:
        return bool(self.job and self.job["state"] in ("running", "waiting"))

    def _start(self, kind: str, target, *args) -> dict:
        with self.lock:
            if self.busy():
                raise RuntimeError("an ambience job is already running")
            self._cancel.clear()
            self.job = {"id": uuid.uuid4().hex[:8], "kind": kind, "state": "running", "step": "Starting",
                        "progress": 0.0, "error": None, "started": time.time()}
            job = dict(self.job)
        threading.Thread(target=self._run, args=(target, args), daemon=True).start()
        return job

    def _run(self, target, args) -> None:
        try:
            target(*args)
        except Cancelled:
            self._job(state="cancelled", step="Cancelled")
        except Exception as e:  # noqa: BLE001
            print(f"[ambience] {self.job and self.job['kind']} failed: {e}", flush=True)
            self._job(state="error", step="Failed", error=str(e)[:300])

    def _job(self, **fields) -> None:
        with self.lock:
            if self.job:
                self.job.update(fields)

    def _check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def cancel(self) -> bool:
        if not self.busy():
            return False
        self._cancel.set()
        return True

    # ---------- model download ----------
    def download_model(self, then_generate: dict | None = None) -> dict:
        return self._start("download", self._download, then_generate)

    def _download(self, then_generate: dict | None) -> None:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        part = MODEL_FILE.with_suffix(MODEL_FILE.suffix + ".part")
        size, sha = MODEL_SIZE_HINT, None
        try:
            with urllib.request.urlopen(MODEL_TREE_URL, timeout=20) as r:
                for x in json.loads(r.read().decode("utf-8")):
                    if x.get("path", "").endswith(MODEL_NAME):
                        size = int(x.get("size") or size)
                        sha = (x.get("lfs") or {}).get("oid")
        except Exception as e:  # metadata is a nice-to-have; size hint still guards
            print(f"[ambience] could not read model metadata: {e}", flush=True)
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "infinite-radio", **({"Range": f"bytes={have}-"} if have else {})})
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        self._job(step="Downloading the ambience model" + (" (resuming)" if have else ""), progress=have / size)
        t0, last = time.time(), 0.0
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:  # server ignored the range; start over
                have = 0
            with open(part, "ab" if have else "wb") as f:
                done = have
                while True:
                    self._check()
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last > 0.5:
                        speed = (done - have) / max(1e-3, now - t0)
                        self._job(progress=min(0.99, done / size),
                                  step=f"Downloading the ambience model · {done / 1e9:.2f} / {size / 1e9:.2f} GB · {speed / 1e6:.0f} MB/s")
                        last = now
        if part.stat().st_size != size:
            raise RuntimeError(f"download incomplete ({part.stat().st_size} of {size} bytes); press Generate to resume")
        if sha:
            self._job(step="Verifying the download", progress=0.995)
            h = hashlib.sha256()
            with open(part, "rb") as f:
                for block in iter(lambda: f.read(1 << 22), b""):
                    self._check()
                    h.update(block)
            if h.hexdigest() != sha:
                part.unlink()
                raise RuntimeError("the downloaded model failed its checksum; it was deleted, please try again")
        os.replace(part, MODEL_FILE)
        print(f"[ambience] model installed ({size / 1e9:.2f} GB)", flush=True)
        self._job(state="done", step="Model installed", progress=1.0)
        if then_generate:
            with self.lock:
                self.job = None
            self.generate(then_generate.get("prompt", ""), then_generate.get("preset"))

    def remove_model(self) -> bool:
        if self.busy():
            raise RuntimeError("wait for the current ambience job to finish")
        removed = False
        for p in (MODEL_FILE, MODEL_FILE.with_suffix(MODEL_FILE.suffix + ".part")):
            if p.exists():
                p.unlink()
                removed = True
        return removed

    # ---------- pool building ----------
    def generate(self, prompt: str, preset: str | None = None) -> dict:
        if not MODEL_FILE.exists():
            raise FileNotFoundError("model not installed")
        p = next((x for x in PRESETS if x["id"] == preset), None)
        text = (p["prompt"] if p and not prompt.strip() else prompt.strip())
        if not text:
            raise ValueError("describe a sound or pick a preset")
        # "New takes" resends the stored prompt; keep the preset's name in that case
        same = p or next((x for x in PRESETS if x["prompt"] == text), None)
        label = same["name"] if same and same["prompt"] == text else text[:40]
        return self._start("generate", self._generate, text, label)

    def from_file(self, path: Path, label: str, source: str) -> dict:
        return self._start(source, self._from_file, path, label, source)

    def _cli(self) -> Path:
        name = "audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli"
        if not self.engine.server_bin:
            raise RuntimeError("audio.cpp is not installed")
        cli = self.engine.server_bin.parent / name
        if not cli.exists():
            raise RuntimeError("audiocpp_cli not found next to audiocpp_server")
        return cli

    def _gen_take(self, cli: Path, prompt: str, seed: int, out: Path, backend: str) -> None:
        env = dict(os.environ)
        bin_dir = str(cli.parent)
        if sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = bin_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        elif sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = bin_dir + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
        res = subprocess.run([str(cli), "--task", "gen", "--family", "stable_audio", "--model", str(MODEL_FILE),
                              "--backend", backend, "--text", prompt, "--duration-seconds", str(TAKE_SECONDS),
                              "--seed", str(seed), "--out", str(out)],
                             capture_output=True, encoding="utf-8", errors="replace", cwd=bin_dir, env=env, timeout=600)
        if res.returncode != 0 or not out.exists():
            raise RuntimeError((res.stderr or res.stdout or "generation failed")[-300:])

    def _generate(self, prompt: str, label: str) -> None:
        cli = self._cli()
        full = f"{prompt}, {STEADY_SUFFIX}"
        pool_id = uuid.uuid4().hex[:10]
        tmp = POOLS_DIR / f"_{pool_id}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            self._job(state="waiting", step="Waiting for the current song to finish rendering")
            takes: list[np.ndarray] = []
            with self.gpu_lock:  # never share the GPU with a YuE2 render
                self._job(state="running")
                backend = self.engine.backend or "cpu"
                base = random.randrange(1 << 30)
                made, target = 0, GEN_TAKES
                while True:
                    while made < target:
                        self._check()
                        self._job(step=f"Generating take {made + 1} of {target}", progress=0.05 + 0.8 * made / MAX_TAKES)
                        out = tmp / f"raw_{made:02d}.wav"
                        try:
                            self._gen_take(cli, full, base + made * 7919, out, backend)
                        except RuntimeError:
                            if backend == "cpu":
                                raise
                            print(f"[ambience] {backend} failed, retrying on cpu", flush=True)
                            backend = "cpu"
                            self._gen_take(cli, full, base + made * 7919, out, backend)
                        a = decode(out)
                        t = int(SR * TRIM_SECONDS)
                        if len(a) > 4 * t:
                            a = a[t:-t]
                        if rms_db(a) >= -65:
                            takes.append(normalize(a))
                        made += 1
                    kept = len(self._pick(takes)) if takes else 0
                    if kept >= MIN_KEEP or made >= MAX_TAKES:
                        break
                    print(f"[ambience] only {kept} usable takes from {made}; generating {TOP_UP} more", flush=True)
                    target = min(MAX_TAKES, made + TOP_UP)
            self._job(step="Matching takes", progress=0.9)
            takes = self._select_and_match(takes)
            self._write_pool(pool_id, tmp, takes, label=label, source="generate", prompt=prompt)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _select_and_match(self, takes: list[np.ndarray]) -> list[np.ndarray]:
        if not takes:
            raise RuntimeError("the model produced silence; try describing the sound differently")
        chosen = [takes[i] for i in self._pick(takes, strict=False)]
        # match tone, then stereo width, then loudness
        shapes = [tone_shape(a) for a in chosen]
        median = np.median(np.stack(shapes), 0)
        wmed = float(np.median([width_db(a) for a in chosen]))
        return [width_toward(eq_toward(a, median), wmed) for a in chosen]

    def _pick(self, takes: list[np.ndarray], strict: bool = True) -> list[int]:
        """Indices of the takes to keep: steady, then closest in tone (at most KEEP_TAKES).

        strict=True only counts takes that pass both filters (used to decide whether to
        generate more); strict=False falls back to the best available, so a pool always
        gets built once generation is finished."""
        spreads = [level_spread(a) for a in takes]
        steady = [i for i, sp in enumerate(spreads) if sp <= 4.0]
        if strict and len(steady) < MIN_KEEP:
            return steady
        if not strict:
            for limit in (4.0, 6.0, 8.0):  # relax until at least 3 takes pass
                steady = [i for i, sp in enumerate(spreads) if sp <= limit]
                if len(steady) >= 3:
                    break
            else:
                order = sorted(range(len(takes)), key=lambda i: spreads[i])
                steady = order[:3]
                if spreads[order[0]] > 12:
                    raise RuntimeError("this sound came out too uneven to loop; describe it as a steady background "
                                       "(for example 'constant rain' rather than 'thunderstorm')")
        shapes = [tone_shape(takes[i]) for i in steady]
        median = np.median(np.stack(shapes), 0)
        dev = {i: float(np.sqrt(np.mean((sh - median) ** 2))) for i, sh in zip(steady, shapes)}
        order = sorted(steady, key=lambda i: dev[i])
        keep = [i for i in order if dev[i] <= 3.0][:KEEP_TAKES]
        if not strict and len(keep) < 3:
            keep = order[:min(3, len(order))]
        return keep

    def _from_file(self, path: Path, label: str, source: str) -> None:
        pool_id = uuid.uuid4().hex[:10]
        tmp = POOLS_DIR / f"_{pool_id}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            self._job(step="Reading audio", progress=0.2)
            a = decode(path, MAX_UPLOAD_SECONDS)
            self._check()
            edge = int(SR * 0.25)
            if len(a) > 4 * edge:
                a = a[edge:-edge]
            if len(a) < SR * (2 * XF_SECONDS + 2):
                raise RuntimeError(f"that audio is too short; use at least {int(2 * XF_SECONDS + 2)} seconds")
            if rms_db(a) < -65:
                raise RuntimeError("that file has no audible sound")
            self._job(step="Preparing the loop", progress=0.6)
            self._write_pool(pool_id, tmp, [normalize(a)], label=label, source=source, prompt=None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if source == "upload":
                Path(path).unlink(missing_ok=True)

    def _write_pool(self, pool_id: str, tmp: Path, takes: list[np.ndarray], label: str, source: str, prompt: str | None) -> None:
        d = self.pool_dir(pool_id)
        d.mkdir(parents=True, exist_ok=True)
        entries = []
        for i, a in enumerate(takes):
            wav = d / f"take_{i:02d}.wav"
            write_wav(wav, a)
            encode_m4a(wav, d / f"take_{i:02d}.m4a")
            entries.append({"file": wav.name, "seconds": round(len(a) / SR, 2)})
        man = {"id": pool_id, "label": label, "source": source, "prompt": prompt, "created": time.time(),
               "sample_rate": SR, "rms_db": TARGET_RMS_DB, "takes": entries,
               "model": "stable-audio-3-small-sfx" if source == "generate" else None}
        (d / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
        with self.lock:
            self.settings.update({"pool_id": pool_id, "enabled": True})
            self._save()
        self._prune()
        self._job(state="done", step=f"Ready · {label} · {len(entries)} take{'s' if len(entries) != 1 else ''}", progress=1.0,
                  pool_id=pool_id)
        print(f"[ambience] pool '{label}' ready: {len(entries)} takes from {source}", flush=True)

    def _prune(self, keep: int = 5) -> None:
        current = self.settings.get("pool_id")
        protected = set(self.protected()) | {current}
        pools = sorted((p for p in POOLS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in pools[keep:]:
            if p.name not in protected:
                shutil.rmtree(p, ignore_errors=True)

    def take_path(self, pool_id: str, index: int, browser: bool = False) -> Path | None:
        man = self.manifest(pool_id)
        if not man or not (0 <= index < len(man["takes"])):
            return None
        wav = self.pool_dir(pool_id) / man["takes"][index]["file"]
        if browser:
            m4a = wav.with_suffix(".m4a")
            if m4a.exists():
                return m4a
        return wav

    # ---------- episode mixing ----------
    def mix_into(self, episode_wav: Path, out_wav: Path, amb: dict, lead_s: float = 4.0, tail_s: float = 6.0,
                 progress=None) -> dict:
        """Stream the episode mix and add the ambience bed.

        The bed starts `lead_s` before the first song (fading in) and keeps going
        `tail_s` after the last one (fading out). With ducking, the bed dips by
        up to 6 dB while the music is at its usual level and comes back in quiet
        passages and between songs.
        """
        man = self.manifest(amb["pool_id"])
        if not man:
            raise RuntimeError("the ambience used by this episode no longer exists")
        takes = [read_wav(self.pool_dir(man["id"]) / t["file"]) for t in man["takes"]]
        bed = Bed(takes, seed=int(amb.get("seed", 0)))
        block = SR // 10  # 100 ms
        # pass 1: music loudness
        with wave.open(str(episode_wav)) as w:
            n_total = w.getnframes()
            acc, cnt = 0.0, 0
            while True:
                raw = w.readframes(SR * 10)
                if not raw:
                    break
                x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
                acc += float(np.sum(np.square(x, dtype=np.float64)))
                cnt += x.size
        music_db = 10 * np.log10(acc / max(1, cnt) + 1e-12)
        gain = 10 ** ((music_db + float(amb["level_db"]) - TARGET_RMS_DB) / 20)
        duck_db = 6.0 if amb.get("duck") else 0.0
        lead, tail = int(SR * lead_s), int(SR * tail_s)
        with wave.open(str(out_wav), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(SR)

            def emit(a):
                out.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())

            # lead-in: bed alone, fading in over 2 s
            b = bed.read(lead) * gain
            ramp = np.minimum(1.0, np.arange(lead, dtype=np.float32) / (SR * 2.0))[:, None]
            emit(b * ramp)
            # body: music + ducked bed
            env_db = music_db - 30.0
            prev_g = 1.0
            with wave.open(str(episode_wav)) as w:
                done = 0
                while True:
                    raw = w.readframes(block)
                    if not raw:
                        break
                    m = (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768).reshape(-1, 2)
                    lvl = rms_db(m)
                    coef = 0.5 if lvl > env_db else 0.08  # ~0.15 s attack, ~1.2 s release at 100 ms blocks
                    env_db += coef * (lvl - env_db)
                    amount = np.clip((env_db - (music_db - 15.0)) / 15.0, 0.0, 1.0)
                    g = 10 ** (-duck_db * amount / 20)
                    curve = np.linspace(prev_g, g, len(m), dtype=np.float32)[:, None]
                    prev_g = g
                    emit(m + bed.read(len(m)) * gain * curve)
                    done += len(m)
                    if progress and done % (block * 50) < block:
                        progress(done / max(1, n_total))
            # tail: bed alone, fading out
            b = bed.read(tail) * gain * prev_g
            ramp = np.linspace(1.0, 0.0, tail, dtype=np.float32)[:, None] ** 1.5
            emit(b * ramp)
        return {"lead": lead_s, "tail": tail_s, "music_db": round(music_db, 1), "bed_gain_db": round(20 * np.log10(gain), 1)}
