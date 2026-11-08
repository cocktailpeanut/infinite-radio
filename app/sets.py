"""DJ Set mode.

A set is a listening session that becomes an episode. Every song that plays
through while the set is open is added; songs the listener cuts are dropped.
Style or mood changes during the set are recorded as sections.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from engine import APP_DIR

DATA_DIR = APP_DIR / "data"
SETS_FILE = DATA_DIR / "sets.json"


def slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:48] or "set"


class SetManager:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.sets: list[dict] = []
        if SETS_FILE.exists():
            try:
                self.sets = json.loads(SETS_FILE.read_text())
            except Exception:
                self.sets = []
        # A set left open by a crash is closed on startup.
        for s in self.sets:
            if s.get("status") == "open":
                s["status"] = "closed"
                s["closed_at"] = s.get("closed_at") or time.time()
        self._save()

    # ---------- persistence ----------
    def _save(self) -> None:
        tmp = SETS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sets, indent=2))
        tmp.replace(SETS_FILE)

    def _open(self) -> dict | None:
        return next((s for s in self.sets if s.get("status") == "open"), None)

    # ---------- lifecycle ----------
    def start(self, name: str, station: dict) -> dict:
        with self.lock:
            if self._open():
                raise ValueError("a set is already open")
            name = (name or "").strip() or f"{station.get('name', 'Infinite Radio')} · {time.strftime('%b %d')}"
            s = {
                "id": uuid.uuid4().hex[:10],
                "name": name,
                "slug": f"{slugify(name)}-{time.strftime('%Y%m%d-%H%M')}",
                "status": "open",
                "started_at": time.time(),
                "closed_at": None,
                "tracks": [],      # entries: {track_id, title, display_title, started_at, state: playing|kept|cut, cut_at}
                "sections": [{"at": time.time(), "style": station.get("style"), "mood": station.get("mood"), "label": "Opening"}],
                "requests": 0,
                "episode_id": None,
            }
            self.sets.insert(0, s)
            self._save()
            return dict(s)

    def current(self) -> dict | None:
        with self.lock:
            s = self._open()
            return dict(s) if s else None

    def get(self, set_id: str) -> dict | None:
        with self.lock:
            s = next((x for x in self.sets if x["id"] == set_id), None)
            return dict(s) if s else None

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(s) for s in self.sets]

    # ---------- playback events ----------
    def on_next(self, track: dict) -> None:
        """A new song starts playing. The previous unresolved song is kept."""
        with self.lock:
            s = self._open()
            if not s:
                return
            for e in s["tracks"]:
                if e["state"] == "playing":
                    e["state"] = "kept"
            s["tracks"].append({
                "track_id": track["id"],
                "title": track["title"],
                "display_title": track["title"],
                "duration": track.get("duration"),
                "started_at": time.time(),
                "state": "playing",
                "kind": track.get("kind", "auto"),
            })
            if track.get("kind") == "request":
                s["requests"] += 1
            self._save()

    def on_ended(self, track_id: str) -> None:
        with self.lock:
            s = self._open()
            if not s:
                return
            for e in reversed(s["tracks"]):
                if e["track_id"] == track_id and e["state"] == "playing":
                    e["state"] = "kept"
                    break
            self._save()

    def cut(self, track_id: str | None = None) -> dict | None:
        """Drop the song that is playing (or the given one) from the set."""
        with self.lock:
            s = self._open()
            if not s:
                return None
            target = None
            for e in reversed(s["tracks"]):
                if (track_id is None and e["state"] == "playing") or (track_id and e["track_id"] == track_id and e["state"] != "cut"):
                    target = e
                    break
            if not target:
                return None
            target["state"] = "cut"
            target["cut_at"] = time.time()
            self._save()
            return dict(target)

    def undo_cut(self) -> dict | None:
        with self.lock:
            s = self._open()
            if not s:
                return None
            cuts = [e for e in s["tracks"] if e["state"] == "cut"]
            if not cuts:
                return None
            last = max(cuts, key=lambda e: e.get("cut_at", 0))
            last["state"] = "kept"
            last.pop("cut_at", None)
            self._save()
            return dict(last)

    def steer(self, station: dict, label: str | None = None) -> None:
        """Record a style/mood change as a new section."""
        with self.lock:
            s = self._open()
            if not s:
                return
            prev = s["sections"][-1]
            if prev.get("style") == station.get("style") and prev.get("mood") == station.get("mood"):
                return
            s["sections"].append({
                "at": time.time(), "style": station.get("style"), "mood": station.get("mood"),
                "label": label or f"Section {len(s['sections']) + 1}",
            })
            self._save()

    def close(self) -> dict | None:
        with self.lock:
            s = self._open()
            if not s:
                return None
            for e in s["tracks"]:
                if e["state"] == "playing":
                    e["state"] = "kept"
            s["status"] = "closed"
            s["closed_at"] = time.time()
            self._save()
            return dict(s)

    # ---------- tracklist editing ----------
    def update(self, set_id: str, name: str | None = None, tracks: list[dict] | None = None) -> dict | None:
        """Reorder, remove, or rename entries of a closed set.

        `tracks` is the full desired list of {track_id, display_title}; entries
        omitted are dropped (state=cut), order is taken from the list.
        """
        with self.lock:
            s = next((x for x in self.sets if x["id"] == set_id), None)
            if not s:
                return None
            if name is not None and name.strip():
                s["name"] = name.strip()
                s["slug"] = f"{slugify(s['name'])}-{time.strftime('%Y%m%d-%H%M', time.localtime(s['started_at']))}"
            if tracks is not None:
                by_id = {e["track_id"]: e for e in s["tracks"]}
                new_list = []
                for item in tracks:
                    e = by_id.get(item.get("track_id"))
                    if not e:
                        continue
                    e["state"] = "kept"
                    if item.get("display_title"):
                        e["display_title"] = str(item["display_title"])[:80]
                    new_list.append(e)
                kept_ids = {e["track_id"] for e in new_list}
                dropped = [e for e in s["tracks"] if e["track_id"] not in kept_ids]
                for e in dropped:
                    e["state"] = "cut"
                s["tracks"] = new_list + dropped
            self._save()
            return dict(s)

    def kept_tracks(self, set_id: str) -> list[dict]:
        s = self.get(set_id)
        if not s:
            return []
        return [e for e in s["tracks"] if e["state"] == "kept"]

    def set_episode(self, set_id: str, episode_id: str) -> None:
        with self.lock:
            s = next((x for x in self.sets if x["id"] == set_id), None)
            if s:
                s["episode_id"] = episode_id
                self._save()

    def delete(self, set_id: str) -> bool:
        with self.lock:
            n = len(self.sets)
            self.sets = [x for x in self.sets if x["id"] != set_id or x.get("status") == "open"]
            changed = len(self.sets) != n
            if changed:
                self._save()
            return changed
