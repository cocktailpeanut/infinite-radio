"""Extract every audio.cpp release archive found in app/bin, then delete it.

Cross-platform replacement for `tar -xzf` / `unzip`, so the Pinokio install
script does not need platform-specific shell commands. Also records the backend
implied by the archive name (metal / cuda / vulkan / cpu / rocm) in
app/runtime.json so the server knows which `--backend` to use.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
BIN_DIR = APP_DIR / "bin"
RUNTIME_FILE = APP_DIR / "runtime.json"


def backend_from_name(name: str) -> str | None:
    n = name.lower()
    if "cudart" in n:
        return None  # companion runtime archive, not the main build
    for key, backend in (("metal", "metal"), ("cuda", "cuda"), ("rocm", "hip"), ("hip", "hip"), ("vulkan", "vulkan"), ("cpu", "cpu")):
        if key in n:
            return backend
    return None


def _mark_executable(path: Path) -> None:
    if os.name == "nt":
        return
    for p in path.rglob("*"):
        if p.is_file() and (p.suffix == "" or p.suffix in (".so", ".dylib")):
            try:
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="")
    args = ap.parse_args()

    if not BIN_DIR.exists():
        print(f"no bin dir at {BIN_DIR}")
        return 1
    archives = sorted(list(BIN_DIR.glob("*.tar.gz")) + list(BIN_DIR.glob("*.tgz")) + list(BIN_DIR.glob("*.zip")))
    llama_dir = BIN_DIR / "llama"
    llama_archives = sorted(list(llama_dir.glob("*.tar.gz")) + list(llama_dir.glob("*.zip"))) if llama_dir.exists() else []
    runtime = {}
    if RUNTIME_FILE.exists():
        try:
            runtime = json.loads(RUNTIME_FILE.read_text())
        except Exception:
            runtime = {}
    if not archives:
        print("no archives to extract")
    for a in archives:
        print(f"extracting {a.name}")
        if a.suffix == ".zip":
            with zipfile.ZipFile(a) as z:
                z.extractall(BIN_DIR)
        else:
            with tarfile.open(a, "r:*") as t:
                if hasattr(tarfile, "data_filter"):
                    t.extractall(BIN_DIR, filter="data")
                else:
                    t.extractall(BIN_DIR)
        backend = backend_from_name(a.name)
        if backend:
            runtime["backend"] = backend
            runtime["archive"] = a.name
        a.unlink()
    for a in llama_archives:
        print(f"extracting {a.name} (llama.cpp)")
        if a.suffix == ".zip":
            with zipfile.ZipFile(a) as z:
                z.extractall(llama_dir)
        else:
            with tarfile.open(a, "r:*") as t:
                if hasattr(tarfile, "data_filter"):
                    t.extractall(llama_dir, filter="data")
                else:
                    t.extractall(llama_dir)
        a.unlink()
    _mark_executable(BIN_DIR)
    if llama_dir.exists():
        # Prefer the one-shot completion tool when the build ships it; fall back to llama-cli.
        for name in ("llama-completion", "llama-cli"):
            exe_name = name + (".exe" if os.name == "nt" else "")
            found = sorted(llama_dir.rglob(exe_name), key=lambda p: len(p.parts))
            if found:
                runtime["llama"] = str(found[0].relative_to(APP_DIR))
                print(f"llama.cpp: {found[0]}")
                break
        else:
            print("WARNING: no llama-cli/llama-completion found; lyric writer will use templates")
    exe = "audiocpp_server.exe" if os.name == "nt" else "audiocpp_server"
    hits = sorted(BIN_DIR.rglob(exe), key=lambda p: len(p.parts))
    if not hits:
        print("ERROR: audiocpp_server not found after extraction")
        return 2
    if args.version:
        runtime["version"] = args.version
    runtime["server"] = str(hits[0].relative_to(APP_DIR))
    RUNTIME_FILE.write_text(json.dumps(runtime, indent=2))
    print(f"audio.cpp server: {hits[0]}")
    print(f"runtime: {json.dumps(runtime)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
