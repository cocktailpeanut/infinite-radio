"""DJ Set mode.

A set is a listening session that becomes an episode. Each song gets a
verdict: kept (in the episode), cut (dropped) or review (undecided, settled in
the tracklist before rendering). The listener gives verdicts with Keep and Cut,
which also move on to the next song. A song that plays to the end gets the
set's play-through verdict (keep by default, or review), and a song that is
interrupted without a verdict (page reload, closing mid-song) goes to review.
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
VERDICT_STATE = {"keep": "kept", "cut": "cut", "review": "review"}
PLAY_THROUGH = ("keep", "review")


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
                self.sets = json.loads(SETS_FILE.read_text(encoding="utf-8"))
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
        tmp.write_text(json.dumps(self.sets, indent=2), encoding="utf-8")
        tmp.replace(SETS_FILE)

    def _open(self) -> dict | None:
        return next((s for s in self.sets if s.get("status") == "open"), None)

    # ---------- lifecycle ----------
    def start(self, name: str, station: dict, planned_minutes: int = 0, play_through: str = "keep") -> dict:
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
                "tracks": [],      # entries: {track_id, title, display_title, started_at, state: playing|kept|cut|review}
                "sections": [{"at": time.time(), "style": station.get("style"), "mood": station.get("mood"), "label": "Opening"}],
                "requests": 0,
                "episode_id": None,
                "planned_minutes": int(max(0, min(360, planned_minutes or 0))),
                "play_through": play_through if play_through in PLAY_THROUGH else "keep",
                "history": [],     # verdict log for undo: {track_id, prev, at}
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
    @staticmethod
    def counts(s: dict) -> dict:
        c = {"kept": 0, "cut": 0, "review": 0, "playing": 0}
        for e in s["tracks"]:
            c[e["state"]] = c.get(e["state"], 0) + 1
        return c

    @staticmethod
    def _entry(track: dict, state: str) -> dict:
        return {
            "track_id": track["id"],
            "title": track["title"],
            "display_title": track["title"],
            "duration": track.get("duration"),
            "started_at": time.time(),
            "state": state,
            "kind": track.get("kind", "auto"),
        }

    def on_next(self, track: dict) -> None:
        """A new song starts playing. A previous song still marked playing was
        interrupted without a verdict, so it goes to review."""
        with self.lock:
            s = self._open()
            if not s:
                return
            for e in s["tracks"]:
                if e["state"] == "playing":
                    e["state"] = "review"
            s["tracks"].append(self._entry(track, "playing"))
            if track.get("kind") == "request":
                s["requests"] += 1
            self._save()

    def on_ended(self, track_id: str) -> None:
        """The song played to the end: apply the set's play-through verdict."""
        with self.lock:
            s = self._open()
            if not s:
                return
            for e in reversed(s["tracks"]):
                if e["track_id"] == track_id and e["state"] == "playing":
                    e["state"] = "kept" if s.get("play_through", "keep") == "keep" else "review"
                    break
            self._save()

    def verdict(self, track_id: str | None, verdict: str, track: dict | None = None) -> dict | None:
        """Keep, cut or send to review the playing song (track_id None) or a given one.

        Works on the open set; returns the updated entry, or None if there is no match.
        `track` is the library record of a song that began before the set started:
        it has no entry yet, so the verdict adds one."""
        state = VERDICT_STATE.get(verdict)
        if not state:
            raise ValueError("verdict must be keep, cut or review")
        with self.lock:
            s = self._open()
            if not s:
                return None
            target = None
            for e in reversed(s["tracks"]):
                if (track_id is None and e["state"] == "playing") or (track_id and e["track_id"] == track_id):
                    target = e
                    break
            if not target and track and track.get("id") == track_id:
                target = self._entry(track, "playing")
                s["tracks"].append(target)
            if not target:
                return None
            hist = s.setdefault("history", [])
            hist.append({"track_id": target["track_id"], "prev": target["state"], "at": time.time()})
            del hist[:-50]
            target["state"] = state
            target["decided_at"] = time.time()
            self._save()
            return dict(target)

    def cut(self, track_id: str | None = None) -> dict | None:
        """Drop the song that is playing (or the given one) from the set."""
        return self.verdict(track_id, "cut")

    def undo(self) -> dict | None:
        """Revert the most recent verdict. A song that was playing when it got its
        verdict has moved on since, so it returns to review rather than playing."""
        with self.lock:
            s = self._open()
            if not s or not s.get("history"):
                return None
            last = s["history"].pop()
            e = next((x for x in reversed(s["tracks"]) if x["track_id"] == last["track_id"]), None)
            if not e:
                return None
            e["state"] = "review" if last["prev"] == "playing" else last["prev"]
            self._save()
            return dict(e)

    # Back-compat name for the old endpoint.
    undo_cut = undo

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
                if e["state"] == "playing":  # closed mid-song: let the tracklist decide
                    e["state"] = "review"
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
                    if item.get("subtitle") is not None:  # "" hides the subtitle for this song
                        e["subtitle"] = str(item["subtitle"])[:60]
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
