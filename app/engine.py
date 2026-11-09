"""audio.cpp engine wrapper.

Supervises an `audiocpp_server` child process that serves the YuE2 model and
exposes a blocking `generate()` call. The server is restarted automatically if
it exits (for example after a host-memory assertion inside ggml).
"""

from __future__ import annotations

import base64
import collections
import glob
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BIN_DIR = APP_DIR / "bin"
MODELS_DIR = APP_DIR / "models"
DATA_DIR = APP_DIR / "data"
MODEL_DIRNAME = "Yue2-3B-GGUF"
MODEL_ID = "yue2"

# Preferred main-model quantization order (whatever is installed wins).
MAIN_GGUF_PREFERENCE = ["yue2-3b-q8_0.gguf", "yue2-3b-q4_0.gguf", "yue2-3b-bf16.gguf", "yue2-3b-ios-q4_0.gguf"]
VAE_GGUF_PREFERENCE = ["yue2-vae-f16.gguf", "yue2-vae-f32.gguf"]

# Stage markers from `audiocpp_server --log` ([TIMING] lines), in pipeline order.
# Only these exact keys are matched so config echoes such as `yue2.vae_gguf`
# do not trigger a phase change. Phases never move backwards within a job.
PHASE_ORDER = ["loading", "planning", "composing", "rendering", "decoding"]
PHASE_PATTERNS = [
    (re.compile(r"yue2\.abc\.", re.I), "planning"),
    (re.compile(r"yue2\.ar\.prefill\.", re.I), "composing"),
    (re.compile(r"yue2\.semantic_ms", re.I), "rendering"),
    (re.compile(r"yue2\.vae_decode|oobleck_audio_vae\.decode", re.I), "decoding"),
]


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class EngineError(RuntimeError):
    pass


class AudioCppEngine:
    def __init__(self):
        self.lock = threading.Lock()  # one generation at a time
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.state = "stopped"  # stopped | starting | ready | loading | generating | error
        self.message = ""
        self.phase = ""
        self.model_loaded = False
        self.log = collections.deque(maxlen=400)
        self.version = ""
        self.restarts = 0
        self.runtime = self._load_runtime()
        self.backend = self.runtime.get("backend") or self._detect_backend()
        self.server_bin = self._find_binary()
        self.model_dir = MODELS_DIR / MODEL_DIRNAME
        self.main_gguf = self._pick(self.model_dir, MAIN_GGUF_PREFERENCE)
        self.vae_gguf = self._pick(self.model_dir, VAE_GGUF_PREFERENCE)
        self.last_timing: dict = {}
        self._reader: threading.Thread | None = None

    # ---------- discovery ----------
    @staticmethod
    def _load_runtime() -> dict:
        p = APP_DIR / "runtime.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    @staticmethod
    def _detect_backend() -> str:
        if sys.platform == "darwin":
            return "metal"
        names = " ".join(p.name.lower() for p in BIN_DIR.rglob("*")) if BIN_DIR.exists() else ""
        if "cudart" in names or "cublas" in names:
            return "cuda"
        if "vulkan" in names:
            return "vulkan"
        return "cpu"

    @staticmethod
    def _find_binary() -> Path | None:
        exe = "audiocpp_server.exe" if os.name == "nt" else "audiocpp_server"
        if not BIN_DIR.exists():
            return None
        hits = sorted(BIN_DIR.rglob(exe), key=lambda p: len(p.parts))
        return hits[0] if hits else None

    @staticmethod
    def _pick(model_dir: Path, prefs: list[str]) -> str | None:
        for name in prefs:
            if (model_dir / name).exists():
                return name
        return None

    def installed(self) -> tuple[bool, str]:
        if not self.server_bin:
            return False, "audio.cpp server binary not found under app/bin"
        if not self.main_gguf:
            return False, f"YuE2 main GGUF not found in app/models/{MODEL_DIRNAME}"
        if not self.vae_gguf:
            return False, f"YuE2 VAE GGUF not found in app/models/{MODEL_DIRNAME}"
        sidecars = self.model_dir / "sidecars"
        if not sidecars.exists():
            return False, "YuE2 sidecars folder missing"
        return True, "ok"

    def hardware_label(self) -> str:
        labels = {"metal": "Apple Metal", "cuda": "NVIDIA CUDA", "vulkan": "Vulkan", "hip": "AMD ROCm", "cpu": "CPU"}
        return labels.get(self.backend, self.backend)

    # ---------- lifecycle ----------
    def write_config(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg = {
            "host": "127.0.0.1",
            "port": self.port,
            "backend": self.backend,
            "device": 0,
            "threads": max(2, min(8, os.cpu_count() or 4)),
            "lazy_load": True,
            "max_loaded_models": 1,
            "busy_timeout_ms": 0,
            "ui": False,
            "ui_management": False,
            "models": [
                {
                    "id": MODEL_ID,
                    "family": "yue2",
                    "path": str(self.model_dir),
                    "task": "gen",
                    "mode": "offline",
                    "busy_timeout_ms": 0,
                    "session_options": {
                        "yue2.model_gguf": self.main_gguf,
                        "yue2.vae_gguf": self.vae_gguf,
                        "yue2.mem_saver": True,
                    },
                    "default_request_options": {
                        "cot": "off",
                        "num_inference_steps": 8,
                    },
                }
            ],
        }
        path = DATA_DIR / "audiocpp-server.json"
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return path

    def start(self) -> None:
        ok, why = self.installed()
        if not ok:
            self.state = "error"
            self.message = why
            raise EngineError(why)
        self.state = "starting"
        self.message = "Starting audio.cpp server"
        self.port = free_port()
        cfg = self.write_config()
        env = dict(os.environ)
        bin_dir = str(self.server_bin.parent)
        if sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = bin_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        elif sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = bin_dir + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
        if os.name != "nt":
            try:
                os.chmod(self.server_bin, 0o755)
            except OSError:
                pass
        cmd = [str(self.server_bin), "--config", str(cfg), "--no-ui", "--log"]
        self._log(f"$ {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd, cwd=bin_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._wait_health()

    def _pump(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip("\n")
            self._log(line)
            if "[TIMING" in line:
                for pat, phase in PHASE_PATTERNS:
                    if pat.search(line):
                        cur = PHASE_ORDER.index(self.phase) if self.phase in PHASE_ORDER else -1
                        if PHASE_ORDER.index(phase) > cur:
                            self.phase = phase
                        break
        code = proc.wait()
        self._log(f"[engine] audiocpp_server exited with code {code}")
        if self.state != "stopped":
            self.state = "error"
            self.message = f"audio.cpp server exited (code {code})"

    NOISY = ("SERVER_HTTP_DEBUG", "[TRACE ", "http.headers", "http.body_ready", "compile_pipeline", "ggml_metal_library")
    # Only stage boundaries and problems reach the terminal; Pinokio rebuilds terminal state per chunk,
    # so relaying every timing line costs real CPU on the host.
    RELAY = ("semantic_ms", "nar_ms", "vae_decode_ms", "session.wall_ms", "listening on", "exited", "error", "failed", "Error")

    def _log(self, line: str) -> None:
        if any(tag in line for tag in self.NOISY):
            return
        # Never relay a raw URL: the Pinokio launcher captures the first
        # "http://host:port" printed, which must be this app's own address.
        line = line.replace("http://", "").replace("https://", "")
        self.log.append(line)
        if "[TIMING" in line and not any(k in line for k in self.RELAY):
            return
        if "[TIMING" not in line and not any(k in line for k in self.RELAY):
            return
        print(f"[audiocpp] {line}", flush=True)

    def _wait_health(self, timeout: float = 90.0) -> None:
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{self.port}/health"
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise EngineError("audio.cpp server exited during startup; see terminal log")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    data = json.loads(r.read().decode())
                    self.version = str(data.get("version", ""))
                    self.state = "ready"
                    self.message = "Ready"
                    return
            except Exception:
                time.sleep(0.5)
        raise EngineError("audio.cpp server did not become healthy in time")

    def stop(self) -> None:
        self.state = "stopped"
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def ensure_running(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            if self.proc is not None:
                self.restarts += 1
                self.model_loaded = False
            self.start()

    # ---------- generation ----------
    def generate(self, lyrics: str, style: str, options: dict | None = None) -> tuple[bytes, dict]:
        """Blocking. Returns (wav_bytes, meta)."""
        with self.lock:
            self.ensure_running()
            opts = {"style": style}
            if options:
                opts.update(options)
            body = {"model": MODEL_ID, "request": {"text": lyrics, "options": opts}}
            self.state = "generating" if self.model_loaded else "loading"
            self.message = "Generating" if self.model_loaded else "Loading YuE2 weights (first song takes longer)"
            self.phase = "loading" if not self.model_loaded else "planning"
            started = time.time()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/tasks/run",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=6 * 3600) as r:
                    payload = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                self.state = "ready"
                self.phase = ""
                raise EngineError(f"audio.cpp returned HTTP {e.code}: {detail[:500]}")
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                self.state = "error"
                self.message = f"Lost connection to audio.cpp server: {e}"
                raise EngineError(self.message)
            finally:
                elapsed = time.time() - started
            audio_b64 = payload.get("audio")
            if not audio_b64:
                self.state = "ready"
                raise EngineError(f"audio.cpp result had no audio: {str(payload)[:300]}")
            if audio_b64.startswith("data:"):
                audio_b64 = audio_b64.split(",", 1)[1]
            wav = base64.b64decode(audio_b64)
            timing = payload.get("timing") or {}
            self.last_timing = timing
            self.model_loaded = True
            self.state = "ready"
            self.message = "Ready"
            self.phase = ""
            meta = {
                "sample_rate": payload.get("sample_rate"),
                "channels": payload.get("channels"),
                "timing": timing,
                "elapsed": elapsed,
            }
            return wav, meta

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "phase": self.phase,
            "backend": self.backend,
            "hardware": self.hardware_label(),
            "version": self.version or self.runtime.get("version", ""),
            "model": self.main_gguf,
            "vae": self.vae_gguf,
            "model_loaded": self.model_loaded,
            "restarts": self.restarts,
            "platform": platform.system(),
            "last_log": list(self.log)[-12:],
        }
