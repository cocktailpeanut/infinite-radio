"""LLM lyric writer built on llama.cpp (embedded, on demand).

A small instruct model fills a JSON object with five lyric blocks under a JSON
schema (constrained decoding), and this module assembles the sections YuE2
expects, repeating the chorus itself. The model only ever chooses words.
Each call spawns a short `llama-completion` (or `llama-cli`) process and exits,
so nothing stays resident next to YuE2. On any failure the caller falls back
to the template generator in `lyrics.py`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from engine import APP_DIR

MODELS_DIR = APP_DIR / "models" / "lyrics"
BIN_DIR = APP_DIR / "bin" / "llama"
MODEL_FILE = "Qwen3-1.7B-Q4_K_M.gguf"

# (name, tier). Tiers describe what is known about YuE2 *singing* the language;
# the lyric model writes all of them.
#   evaluated  : evaluated by the YuE2 authors
#   demo       : offered on the official YuE2 demo page
#   reported   : community reports of working vocals
#   untested   : covered by the transcriber used in training; no reports yet
LANGUAGES = [
    ("English", "evaluated"), ("Chinese", "evaluated"),
    ("Japanese", "demo"), ("Korean", "demo"), ("Spanish", "demo"), ("Russian", "demo"),
    ("Portuguese", "reported"), ("French", "reported"), ("Polish", "reported"),
    ("Romanian", "reported"), ("Greek", "reported"), ("Arabic", "reported"),
    ("German", "untested"), ("Italian", "untested"), ("Dutch", "untested"), ("Turkish", "untested"),
    ("Vietnamese", "untested"), ("Indonesian", "untested"), ("Thai", "untested"), ("Hindi", "untested"),
    ("Malay", "untested"), ("Swedish", "untested"), ("Danish", "untested"), ("Finnish", "untested"),
    ("Czech", "untested"), ("Filipino", "untested"), ("Persian", "untested"), ("Hungarian", "untested"),
    ("Cantonese", "untested"),
]
LANGUAGE_NAMES = {n.lower(): n for n, _ in LANGUAGES}
NATIVE_NAME = {"Japanese": "日本語", "Chinese": "中文", "Cantonese": "粵語", "Korean": "한국어", "Russian": "русский",
               "Arabic": "العربية", "Greek": "Ελληνικά", "Thai": "ไทย", "Hindi": "हिन्दी", "Persian": "فارسی",
               "Spanish": "español", "French": "français", "German": "Deutsch", "Portuguese": "português",
               "Italian": "italiano", "Polish": "polski", "Romanian": "română", "Dutch": "Nederlands",
               "Turkish": "Türkçe", "Vietnamese": "Tiếng Việt", "Indonesian": "Bahasa Indonesia"}
TIER_LABEL = {"evaluated": "evaluated by the YuE2 authors", "demo": "on the official YuE2 demo",
              "reported": "community-reported vocals", "untested": "untested vocals, may be accented or garbled"}

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 60},
        "verse1": {"type": "array", "items": {"type": "string", "maxLength": 90}, "minItems": 2, "maxItems": 4},
        "chorus": {"type": "array", "items": {"type": "string", "maxLength": 90}, "minItems": 2, "maxItems": 4},
        "verse2": {"type": "array", "items": {"type": "string", "maxLength": 90}, "minItems": 2, "maxItems": 4},
        "bridge": {"type": "array", "items": {"type": "string", "maxLength": 90}, "minItems": 2, "maxItems": 4},
        "outro": {"type": "array", "items": {"type": "string", "maxLength": 90}, "minItems": 1, "maxItems": 3},
    },
    "required": ["title", "verse1", "chorus", "verse2", "bridge", "outro"],
}

SYSTEM = (
    "/no_think\n"
    "You write original song lyrics for a singer. Write ONLY in the requested language: no translations, "
    "no romanization, no English unless English is requested. Each array item is one short sung line. "
    "Do not put section names or brackets inside lines. Make verse2 different from verse1. Keep the chorus "
    "catchy and the bridge a change of perspective. Match the style, mood and themes."
)



@dataclass
class Written:
    title: str
    lyrics: str
    language: str
    seconds: float
    attempt: int


def apply_language(style: str, language: str) -> str:
    """Make the style prompt name the vocal language (YuE2 reads it from there).

    A leading language name is replaced; otherwise the language is prepended.
    """
    parts = [p.strip() for p in (style or "").split(",")]
    if parts and parts[0].lower() in LANGUAGE_NAMES:
        parts[0] = language
    else:
        parts.insert(0, language)
    return ", ".join(p for p in parts if p)


BAD_TITLE = re.compile(r"\[|\]|\b(verse|chorus|bridge|outro|intro|title)\d*\b|\d+:\d+|^\W*$", re.I)


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def _shorten_title(t: str) -> str:
    """A title, not a lyric line: cut at the first natural break, then cap length.

    Hangul and other spaced CJK scripts are capped by characters too, since a
    whole sung line has many words but each word is short.
    """
    cjk = bool(_CJK.search(t))
    limit_chars = 16 if cjk else 36
    if len(t) <= limit_chars and len(t.split()) <= 6:
        return t
    head = re.split(r"[,，、。.!?！？;；:：·…—–-]\s*", t)[0].strip()
    if 2 <= len(head) <= limit_chars and len(head.split()) <= 6:
        return head
    words = head.split() if " " in head else [head]
    out = ""
    for w in words:
        trial = (out + " " + w).strip()
        if len(trial) > limit_chars or len(trial.split()) > 5:
            break
        out = trial
    out = out or head[:limit_chars]
    if not cjk:  # a cut title should not end on filler ("The Weight of Choice Is")
        words = out.split()
        while len(words) > 1 and words[-1].lower() in _TRAILING_FILLER:
            words.pop()
        out = " ".join(words)
    return out


_TRAILING_FILLER = {"is", "are", "was", "the", "a", "an", "of", "and", "to", "in", "on", "with", "than", "for", "my",
                    "your", "our", "at", "by", "from", "until", "when", "while", "as", "but", "so", "or", "if", "that",
                    "into", "because", "de", "la", "el", "y", "que", "der", "die", "das", "und", "le", "et"}


def _clean_title(title: str, fallback_lines: list[str]) -> str:
    """Small models sometimes put section labels or timings in the title; derive one from the hook instead."""
    t = re.sub(r"\s+", " ", title).strip().strip("\"“”'.")
    if not t or BAD_TITLE.search(t) or len(t) < 2:
        hook = (fallback_lines[0] if fallback_lines else "Untitled").strip().rstrip(",.!?;:")
        words = hook.split()
        t = (" ".join(words[:5]) if len(words) > 1 else hook[:24]).rstrip(",.!?;:、。")
    t = _shorten_title(t)
    if t and t.islower() and re.search(r"[A-Za-z]", t):
        t = " ".join(w if w in ("de", "la", "del", "y", "the", "of", "and", "der", "die", "das", "und") else w[:1].upper() + w[1:] for w in t.split())
        t = t[:1].upper() + t[1:]
    return t[:60]


def _script_class(ch: str) -> str:
    name = unicodedata.name(ch, "")
    for key, cls in (("HIRAGANA", "ja"), ("KATAKANA", "ja"), ("CJK", "cjk"), ("HANGUL", "ko"), ("CYRILLIC", "cyr"),
                     ("ARABIC", "ar"), ("GREEK", "el"), ("THAI", "th"), ("DEVANAGARI", "hi"), ("HEBREW", "he")):
        if key in name:
            return cls
    return "latin"


EXPECTED_SCRIPT = {
    "Japanese": {"ja", "cjk"}, "Chinese": {"cjk"}, "Cantonese": {"cjk"}, "Korean": {"ko"}, "Russian": {"cyr"},
    "Arabic": {"ar"}, "Persian": {"ar"}, "Greek": {"el"}, "Thai": {"th"}, "Hindi": {"hi"},
}


def script_matches(text: str, language: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    counts: dict[str, int] = {}
    for c in letters:
        k = _script_class(c)
        counts[k] = counts.get(k, 0) + 1
    total = len(letters)
    expected = EXPECTED_SCRIPT.get(language, {"latin"})
    share = sum(v for k, v in counts.items() if k in expected) / total
    if language == "Japanese" and counts.get("ja", 0) == 0:
        return False  # pure kanji would be Chinese
    return share > (0.7 if language == "Japanese" else 0.85)


class LyricWriter:
    def __init__(self):
        self.binary = self._find_binary()
        self.model = MODELS_DIR / MODEL_FILE if (MODELS_DIR / MODEL_FILE).exists() else self._any_gguf()
        self.last: dict = {"mode": "template", "error": None, "seconds": None, "language": None}
        self.calls = 0
        self.failures = 0

    # ---------- discovery ----------
    @staticmethod
    def _find_binary() -> Path | None:
        runtime = APP_DIR / "runtime.json"
        if runtime.exists():
            try:
                rel = json.loads(runtime.read_text(encoding="utf-8")).get("llama")
                if rel and (APP_DIR / rel).exists():
                    return APP_DIR / rel
            except Exception:
                pass
        if BIN_DIR.exists():
            for name in ("llama-completion", "llama-cli"):
                exe = name + (".exe" if os.name == "nt" else "")
                hits = sorted(BIN_DIR.rglob(exe), key=lambda p: len(p.parts))
                if hits:
                    return hits[0]
        return None

    @staticmethod
    def _any_gguf() -> Path | None:
        if MODELS_DIR.exists():
            ggufs = sorted(MODELS_DIR.glob("*.gguf"))
            return ggufs[0] if ggufs else None
        return None

    def available(self) -> bool:
        return bool(self.binary and self.model and self.binary.exists() and self.model.exists())

    def snapshot(self) -> dict:
        return {
            "available": self.available(),
            "binary": str(self.binary.relative_to(APP_DIR)) if self.binary else None,
            "model": self.model.name if self.model else None,
            "calls": self.calls,
            "failures": self.failures,
            "last": dict(self.last),
        }

    # ---------- generation ----------
    def write(self, style: str, mood: str, themes: str, language: str, target_seconds: int, seed: int) -> Written | None:
        if not self.available():
            return None
        native = NATIVE_NAME.get(language)
        lang_label = f"{language} ({native})" if native else language
        prompt = (f"Language: {lang_label}\nStyle: {style}\nMood: {mood}\n"
                  f"Themes: {themes or 'free choice'}\nSong length: about {max(1, round(target_seconds / 60))} minute(s)\n"
                  f"Every line must be written in {lang_label}. Write the lyrics now.")
        for attempt in (1, 2):
            t0 = time.time()
            try:
                raw = self._run(prompt, seed + attempt * 7919, repeat_penalty=1.1 if attempt == 1 else 1.35,
                                temperature=0.9 if attempt == 1 else 1.0)
                data = self._parse(raw)
                lyrics, problems = self._assemble_and_check(data, language)
                secs = time.time() - t0
                self.calls += 1
                if not problems:
                    self.last = {"mode": "llm", "error": None, "seconds": round(secs, 1), "language": language}
                    return Written(title=data["title"].strip()[:60], lyrics=lyrics, language=language, seconds=secs, attempt=attempt)
                self.last = {"mode": "llm", "error": "; ".join(problems), "seconds": round(secs, 1), "language": language}
                print(f"[lyrics] attempt {attempt} rejected: {problems}", flush=True)
            except Exception as e:  # noqa: BLE001
                self.calls += 1
                self.last = {"mode": "llm", "error": str(e)[:200], "seconds": round(time.time() - t0, 1), "language": language}
                print(f"[lyrics] attempt {attempt} failed: {e}", flush=True)
        self.failures += 1
        return None

    def _run(self, prompt: str, seed: int, repeat_penalty: float, temperature: float) -> str:
        # Note: --log-disable would also silence the generated text in this build, so logs stay on (stderr).
        cmd = [str(self.binary), "-m", str(self.model), "--jinja", "--single-turn",
               "--no-warmup", "-sys", SYSTEM, "-p", prompt, "-j", json.dumps(SCHEMA), "-n", "600",
               "--temp", str(temperature), "--repeat-penalty", str(repeat_penalty), "-s", str(seed % (2**31 - 1)),
               "-c", "2048", "-ngl", "auto"]
        env = dict(os.environ)
        bin_dir = str(self.binary.parent)
        if sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = bin_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        elif sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = bin_dir + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
        # Always decode as UTF-8: on Windows the default is the ANSI code page, which garbles CJK lyrics.
        res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=180, cwd=bin_dir,
                             env=env, stdin=subprocess.DEVNULL)
        if res.returncode != 0 and "{" not in res.stdout:
            raise RuntimeError(f"llama exited {res.returncode}: {res.stderr[-300:]}")
        return res.stdout

    @staticmethod
    def _parse(raw: str) -> dict:
        # stdout echoes the rendered prompt first; the model's answer follows the last "assistant" marker.
        marker = raw.rfind("\nassistant")
        text = raw[marker:] if marker >= 0 else raw
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        text = text.replace("[end of text]", "")
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in model output")
        return json.loads(text[start:end + 1])

    @staticmethod
    def _assemble_and_check(d: dict, language: str) -> tuple[str, list[str]]:
        def clean(lines):
            out = []
            for l in lines or []:
                l = re.sub(r"\s+", " ", str(l)).strip().strip("\"“”")
                l = re.sub(r"^\[[^\]]*\]\s*", "", l)  # stray [Verse] inside a line
                if l:
                    out.append(l)
            return out
        blocks = {k: clean(d.get(k)) for k in ("verse1", "chorus", "verse2", "bridge", "outro")}
        problems = []
        for k, v in blocks.items():
            if len(v) < (1 if k == "outro" else 2):
                problems.append(f"{k} too short")
        d["title"] = _clean_title(str(d.get("title", "")), blocks["chorus"] or blocks["verse1"])
        all_lines = sum(blocks.values(), [])
        if any(len(l) > 90 for l in all_lines):
            problems.append("overlong line")
        if any("[" in l or "]" in l for l in all_lines):
            problems.append("bracket in line")
        if blocks["verse1"] and blocks["verse2"] and set(map(str.lower, blocks["verse1"])) == set(map(str.lower, blocks["verse2"])):
            problems.append("verse2 repeats verse1")
        distinct = len({l.lower() for l in all_lines})
        if all_lines and distinct < 0.6 * len(all_lines):
            problems.append("too repetitive")
        if not script_matches(" ".join(all_lines), language):
            problems.append("wrong language/script")
        order = [("[Verse]", blocks["verse1"]), ("[Chorus]", blocks["chorus"]), ("[Verse]", blocks["verse2"]),
                 ("[Bridge]", blocks["bridge"]), ("[Chorus]", blocks["chorus"]), ("[Outro]", blocks["outro"])]
        lyrics = "\n\n".join(tag + "\n" + "\n".join(lines) for tag, lines in order if lines)
        return lyrics, problems


if __name__ == "__main__":  # manual check: python lyricwriter.py Japanese
    lang = sys.argv[1] if len(sys.argv) > 1 else "English"
    w = LyricWriter()
    print("available:", w.available(), w.binary, w.model)
    r = w.write("city pop, groovy bass, female vocal", "nostalgic", "neon rain", lang, 120, 42)
    print(r.title if r else None); print(r.lyrics if r else w.last)
