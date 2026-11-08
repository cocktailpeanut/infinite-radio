"""Crossfade mixer: joins WAV tracks into one episode WAV, streaming to disk.

Returns the exact start offset of every track so chapter timestamps and title
card timing line up with the audio.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 48000
TARGET_CHANNELS = 2


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a 16-bit PCM WAV into float32 [-1, 1], shape (frames, channels)."""
    with wave.open(str(path), "rb") as w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    data = data.reshape(-1, ch)
    if ch == 1:
        data = np.repeat(data, 2, axis=1)
    elif ch > 2:
        data = data[:, :2]
    if rate != TARGET_RATE:
        data = _resample(data, rate, TARGET_RATE)
    return data, TARGET_RATE


def _resample(data: np.ndarray, src: int, dst: int) -> np.ndarray:
    n_out = int(round(data.shape[0] * dst / src))
    x_old = np.linspace(0.0, 1.0, data.shape[0], endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.stack([np.interp(x_new, x_old, data[:, c]) for c in range(data.shape[1])], axis=1).astype(np.float32)


def _normalize(data: np.ndarray, peak_db: float = -1.0) -> np.ndarray:
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak <= 1e-6:
        return data
    target = 10 ** (peak_db / 20)
    return (data * (target / peak)).astype(np.float32)


def _trim_silence(data: np.ndarray, rate: int, threshold: float = 0.004, keep_ms: int = 150) -> np.ndarray:
    """Trim leading/trailing digital silence so crossfades meet real audio."""
    env = np.max(np.abs(data), axis=1)
    idx = np.where(env > threshold)[0]
    if idx.size == 0:
        return data
    keep = int(rate * keep_ms / 1000)
    start = max(0, idx[0] - keep)
    end = min(data.shape[0], idx[-1] + keep)
    return data[start:end]


def mix_tracks(paths: list[Path], out_path: Path, crossfade_seconds: float = 2.0,
               progress=None) -> dict:
    """Write a crossfaded mix of `paths` to `out_path` (16-bit stereo 48 kHz).

    Returns {"duration": seconds, "starts": [seconds per track], "durations": [...]}.
    A track's chapter starts where its fade-in begins.
    """
    rate = TARGET_RATE
    xf = int(rate * max(0.0, crossfade_seconds))
    fade_in = np.sqrt(np.linspace(0.0, 1.0, xf, endpoint=False, dtype=np.float32))[:, None] if xf else None
    fade_out = np.sqrt(np.linspace(1.0, 0.0, xf, endpoint=False, dtype=np.float32))[:, None] if xf else None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    w = wave.open(str(out_path), "wb")
    w.setnchannels(TARGET_CHANNELS)
    w.setsampwidth(2)
    w.setframerate(rate)

    written = 0
    pending_tail: np.ndarray | None = None
    starts: list[float] = []
    durations: list[float] = []

    def emit(block: np.ndarray) -> None:
        nonlocal written
        if block.size == 0:
            return
        pcm = np.clip(block, -1.0, 1.0)
        w.writeframes((pcm * 32767.0).astype("<i2").tobytes())
        written += block.shape[0]

    total = len(paths)
    for i, p in enumerate(paths):
        data, _ = _read_wav(p)
        data = _normalize(_trim_silence(data, rate))
        durations.append(data.shape[0] / rate)
        use_xf = xf if (xf and data.shape[0] > 3 * xf) else 0
        if pending_tail is not None and use_xf:
            # Chapter starts where the incoming song begins to fade in.
            starts.append(written / rate)
            head = data[:use_xf]
            emit(pending_tail * fade_out[: pending_tail.shape[0]] + head * fade_in[: head.shape[0]])
            body = data[use_xf:]
        elif pending_tail is not None:
            emit(pending_tail)
            starts.append(written / rate)
            body = data
        else:
            starts.append(written / rate)
            body = data
        if use_xf and i < total - 1:
            pending_tail = body[-use_xf:]
            emit(body[:-use_xf])
        else:
            pending_tail = None
            emit(body)
        if progress:
            progress((i + 1) / total)
    if pending_tail is not None:
        emit(pending_tail)
    w.close()
    return {"duration": written / rate, "starts": starts, "durations": durations, "rate": rate}


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def format_timestamp(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
