"""Derive a short, publishable genre label from a song's style prompt.

The style prompt is a production brief ("james blake style slow minimal dubstep, sparse
minimalism, heavy sub-bass"). On screen we want only the genre ("minimal dubstep"): no
artist references, no mix notes. This is deterministic on purpose; a small LLM proved
unreliable at it (it copied examples and invented words).
"""

from __future__ import annotations

import re

# Multi-word genres first so "bossa nova" wins over "nova".
GENRES = [
    "drum and bass", "drum n bass", "hip hop", "trip hop", "lo-fi hip hop", "bossa nova", "city pop", "dream pop",
    "synth pop", "synthpop", "dance pop", "indie pop", "art pop", "k-pop", "j-pop", "j-rock", "post rock", "post-rock",
    "uk garage", "future bass", "deep house", "tech house", "progressive house", "french house", "acid house",
    "neo soul", "neo-soul", "nu disco", "nu-disco", "big band", "smooth jazz", "acid jazz", "jazz hop", "jazz-hop",
    "shibuya-kei", "shibuya kei", "r&b", "rnb", "edm", "idm", "dnb", "ukg", "ebm",
    "pop", "rock", "techno", "house", "dubstep", "jazz", "folk", "soul", "funk", "disco", "metal", "punk", "indie",
    "electronica", "electronic", "electro", "trap", "garage", "grime", "reggae", "reggaeton", "blues", "country",
    "classical", "orchestral", "cinematic", "ambient", "trance", "synthwave", "vaporwave", "chillwave", "chillhop",
    "lofi", "lo-fi", "downtempo", "breakbeat", "breaks", "jungle", "ballad", "gospel", "latin", "bachata", "salsa",
    "afrobeats", "afrobeat", "amapiano", "shoegaze", "emo", "grunge", "ska", "drill", "phonk", "hyperpop", "industrial",
    "darkwave", "new wave", "post-punk", "krautrock", "bluegrass", "americana", "flamenco", "tango", "samba", "chanson",
    "enka", "kayokyoku", "cumbia", "dancehall", "soundtrack", "musical", "opera", "acapella", "a cappella", "minimalism",
]
_GENRE_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(g) for g in sorted(GENRES, key=len, reverse=True)) + r")(?![\w-])", re.I)

# Words that describe tempo, mood or mix rather than genre; dropped from the front of a label.
DESCRIPTORS = {
    "slow", "fast", "upbeat", "uptempo", "mellow", "soft", "gentle", "relaxing", "sad", "happy", "emotional", "sparse",
    "heavy", "warm", "bright", "energetic", "calm", "cozy", "quiet", "loud", "epic", "beautiful", "dreamy", "moody",
    "groovy", "smooth", "lush", "raw", "intimate", "atmospheric", "catchy", "polished", "modern", "classic", "retro",
    "vintage", "chill", "laid-back", "laidback", "melodic", "minimalist",
}
UPPER = {"r&b": "R&B", "rnb": "R&B", "edm": "EDM", "idm": "IDM", "dnb": "DnB", "uk garage": "UK garage", "ukg": "UKG",
         "ebm": "EBM", "k-pop": "K-pop", "j-pop": "J-pop", "j-rock": "J-rock"}


def _strip_artist_refs(segment: str) -> str:
    s = segment
    s = re.sub(r"\b(in the )?style of [^,]+", "", s, flags=re.I)          # "in the style of X"
    s = re.sub(r"^.*?\b[\w&'.\-]+(?:\s+[\w&'.\-]+){0,3}?[\s-]+style\b", "", s, flags=re.I)  # "X style ..." / "X-style ..."
    s = re.sub(r"\b(like|inspired by|reminiscent of|a la|à la)\b.*$", "", s, flags=re.I)  # "like X", "inspired by X"
    s = re.sub(r"\b\w+-esque\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,.-")


def derive_genre(style: str, languages: set[str] | None = None) -> str:
    """Up to three words ending at the last genre term in the first segment that names a genre."""
    if not style:
        return ""
    for raw in [p.strip() for p in style.split(",") if p.strip()]:
        if languages and raw.lower() in languages:
            continue  # the language name the app prepends
        seg = _strip_artist_refs(raw)
        matches = list(_GENRE_RE.finditer(seg))
        if not matches:
            continue
        end = matches[-1].end()
        words = seg[:end].split()
        label_words = words[-3:]
        while len(label_words) > 1 and label_words[0].lower() in DESCRIPTORS:
            label_words = label_words[1:]
        label = " ".join(label_words).lower()
        for k, v in UPPER.items():
            label = re.sub(rf"(?<![\w-]){re.escape(k)}(?![\w-])", v, label)
        return label[:28]
    return ""


if __name__ == "__main__":
    for s in ["English, james blake style slow minimal dubstep, sparse minimalism, heavy sub-bass",
              "Japanese, city pop, groovy bass, female vocal", "dark hard techno", "bossa nova electronica, shibuya-kei",
              "Spanish, acoustic folk, fingerpicked guitar, intimate vocal", "Gesaffelstein style dark techno, distorted bass",
              "in the style of Nujabes, jazz hop, vinyl crackle", "English, dreamy synth pop, warm female vocal",
              "gesaffelstein-style dark techno", "lo-fi soul, Rhodes piano", "warm vocal, soft drums", "R&B ballad, piano",
              "English, uplifting house music like Avicii", "K-pop ballad"]:
        print(f"{s[:60]:60s} -> {derive_genre(s, {'english', 'japanese', 'spanish', 'korean'})!r}")
