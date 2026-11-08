"""Procedural lyric generator for Infinite Radio.

YuE2 needs lyrics with section tags such as [Verse] and [Chorus]. This module
writes short, singable English lyrics from a station's mood and optional themes
without any external model, so the radio can run forever offline.

Lines come from a bank of pre-rhymed couplets and choruses. Only nouns,
adjectives and the listener's themes are substituted, so every line stays
grammatical and every pair still rhymes.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

NOUNS = [
    "the city lights", "the midnight train", "the neon rain", "the open road",
    "a paper moon", "the harbor wind", "the morning tide", "the last streetlight",
    "the summer heat", "the winter coast", "the rooftop view", "an empty stage",
    "the river bend", "a borrowed car", "the skyline", "an old photograph",
    "the back seat", "a lonely frequency", "the corner store", "the static",
]

ADJECTIVES = [
    "electric", "golden", "restless", "velvet", "faded", "wild", "silver",
    "burning", "hollow", "tender", "endless", "crooked", "fearless", "quiet",
]

MOOD_ADJECTIVES = {
    "euphoric": ["golden", "blazing", "weightless", "shining"],
    "melancholic": ["faded", "hollow", "distant", "aching"],
    "chill": ["easy", "lazy", "mellow", "slow"],
    "dark": ["cold", "hidden", "broken", "shadowed"],
    "romantic": ["tender", "velvet", "warm", "trembling"],
    "nostalgic": ["faded", "borrowed", "yesterday's", "sepia"],
    "energetic": ["electric", "restless", "wild", "burning"],
    "dreamy": ["weightless", "hazy", "silver", "endless"],
}

# Rhymed couplets for verses and bridges. Slots: {adj} {noun} {theme} {Theme}
VERSE_COUPLETS = [
    ("Headlights cut across the {adj} night", "{Theme} on my mind and I'm holding on tight"),
    ("I keep {noun} folded in my coat", "A {adj} promise on a half-written note"),
    ("You said the city never sleeps", "I said the {adj} silence keeps what it keeps"),
    ("{Theme} was a word we used to say", "Back when the {adj} summer wouldn't fade away"),
    ("Static on the dial and a voice in the rain", "Singing every line like it could ease the pain"),
    ("We traded all our maps for the {adj} sky", "Learned to spell forever in a single goodbye"),
    ("Counting streetlights down an empty road", "Carrying a {adj} heart like a heavy load"),
    ("Dashboard glowing like a {adj} dream", "Nothing is as simple as it used to seem"),
    ("Pull me from the undertow of {theme}", "Let the morning find us with nothing in between"),
    ("The radio plays a song we never knew", "Every single chorus sounds like me and you"),
    ("{Noun} keeps a secret only we have heard", "A melody that's bigger than a single word"),
    ("I wrote your name in the {adj} dust", "Watched the letters turn to rain and rust"),
    ("Somewhere past the edge of the {adj} town", "The stars are coming up as the lights go down"),
    ("Every window is a {adj} screen", "Playing out the places that we've never been"),
    ("We were loud, we were {adj}, we were young", "Every word we meant was a song unsung"),
    ("Pour the {adj} evening in a paper cup", "Drink it slow and never let the music stop"),
    ("Underneath {noun} we made a vow", "Nothing in the world could hold us back now"),
    ("{Theme} in the rearview, fading out of sight", "Still I feel it burning like a {adj} light"),
]

BRIDGE_COUPLETS = [
    ("If the {adj} morning finds us on the floor", "We will still be humming what we're living for"),
    ("Maybe {noun} knows the way back home", "Maybe we were never really on our own"),
    ("Hush now, let the {adj} engine hum", "Nobody knows the places we come from"),
    ("Slow it down and let the moment breathe", "{Theme} is the only thing I'll never leave"),
    ("Close your eyes and count the {adj} sound", "Every heartbeat turning the world around"),
]

OUTRO_COUPLETS = [
    ("Infinite, infinite, carry us on", "Play it till the {adj} night is gone"),
    ("{Theme}, {theme}, stay a while", "Fade me out in a {adj} smile"),
    ("Turn it down but don't turn it off", "{Theme} on the air, that's enough"),
    ("One more song before the dawn", "Infinite, infinite, carry us on"),
]

# Choruses: four rhymed lines plus a title hook.
CHORUSES = [
    {
        "title": "Never Alone",
        "lines": ["So turn it up and let it play", "We are the signal, we won't fade away",
                  "{Theme}, {theme}, carry me home", "Out on the airwaves, never alone"],
    },
    {
        "title": "Burning Through the Night",
        "lines": ["Hold on to the {adj} light", "We are still burning through the night",
                  "{Theme} calling through the static and the rain", "Sing it back and we will rise again"],
    },
    {
        "title": "Run Until the Morning",
        "lines": ["Take my hand, we'll run until the morning", "Down every {adj} street without a warning",
                  "{Theme} on the radio, turning up the sound", "Nothing left to lose and nowhere to come down"],
    },
    {
        "title": "Afterglow",
        "lines": ["Infinite, infinite, here we go", "Riding on a {adj} afterglow",
                  "{Theme} is the only word I know", "Infinite, infinite, here we go"],
    },
    {
        "title": "The Frequency",
        "lines": ["Louder than the voices in my head", "Brighter than the {adj} words we said",
                  "{Theme}, keep me spinning round", "I am the frequency and you are the sound"],
    },
    {
        "title": "Stay Tuned",
        "lines": ["Stay tuned, don't you touch that dial", "We've been {adj} for a little while",
                  "{Theme} keeps us dancing in the dark", "Every little static is a spark"],
    },
]

TITLE_ADJS = ["Neon", "Velvet", "Electric", "Golden", "Paper", "Silver", "Restless", "Hollow",
              "Infinite", "Quiet", "Wild", "Glass", "Midnight", "Borrowed", "Slow", "Static"]
TITLE_NOUNS = ["Frequency", "Signal", "Horizon", "Tide", "Afterglow", "Satellite", "Orbit", "Echo",
               "Drive", "Pulse", "Skyline", "Halo", "Current", "Transmission", "Bloom", "Hour"]

INSTRUMENTAL_TITLES = [
    "Night Drive", "Low Orbit", "Glass Tides", "Afterglow", "Static Bloom",
    "Signal Fade", "Velvet Hour", "Open Circuit", "Paper Moons", "Slow Current",
    "Neon Drift", "Last Transmission", "Midnight Frequency", "Blue Horizon",
]


@dataclass
class Song:
    title: str
    lyrics: str
    themes: list[str]
    mood: str


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _clean_themes(themes: str | None) -> list[str]:
    if not themes:
        return []
    out = []
    for p in re.split(r"[,\n;/]+", themes):
        p = re.sub(r"\s+", " ", p).strip().strip(".")
        if p and len(p) <= 40:
            out.append(p)
    return out[:6]


class LyricGenerator:
    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)

    def _fill(self, lines: list[str] | tuple[str, ...], themes: list[str], mood: str) -> list[str]:
        out = []
        for line in lines:
            theme = (self.rng.choice(themes) if themes else self.rng.choice(NOUNS)).lower()
            theme = re.sub(r"^(the|a|an) ", "", theme)
            adj_pool = MOOD_ADJECTIVES.get(mood, []) + ADJECTIVES
            noun = self.rng.choice(NOUNS)
            line = (line.replace("{Theme}", _cap(theme)).replace("{theme}", theme)
                        .replace("{adj}", self.rng.choice(adj_pool))
                        .replace("{Noun}", _cap(noun)).replace("{noun}", noun))
            out.append(_cap(re.sub(r"\s+", " ", line).strip()))
        return out

    def generate(self, themes: str | None = None, mood: str = "dreamy",
                 genre: str = "", structure: str = "standard") -> Song:
        theme_list = _clean_themes(themes)
        mood = mood if mood in MOOD_ADJECTIVES else "dreamy"

        couplets = self.rng.sample(VERSE_COUPLETS, 4)
        verse1 = self._fill(couplets[0], theme_list, mood) + self._fill(couplets[1], theme_list, mood)
        verse2 = self._fill(couplets[2], theme_list, mood) + self._fill(couplets[3], theme_list, mood)
        chorus_def = self.rng.choice(CHORUSES)
        chorus = self._fill(chorus_def["lines"], theme_list, mood)
        bridge = self._fill(self.rng.choice(BRIDGE_COUPLETS), theme_list, mood)
        outro = self._fill(self.rng.choice(OUTRO_COUPLETS), theme_list, mood)

        sections = [("[Verse]", verse1), ("[Chorus]", chorus), ("[Verse]", verse2)]
        if structure != "short":
            sections += [("[Bridge]", bridge), ("[Chorus]", chorus)]
        sections += [("[Outro]", outro)]
        body = "\n\n".join(f"{tag}\n" + "\n".join(lines) for tag, lines in sections)

        combo = f"{self.rng.choice(TITLE_ADJS)} {self.rng.choice(TITLE_NOUNS)}"
        if theme_list:
            theme = _cap(re.sub(r"^(the|a|an) ", "", self.rng.choice(theme_list).lower()))
            title = self.rng.choice([theme, f"{theme} {self.rng.choice(TITLE_NOUNS)}", chorus_def["title"], combo])
        else:
            title = self.rng.choice([chorus_def["title"], combo, combo])
        title = re.sub(r"[^A-Za-z0-9' ]", "", title).strip() or "Untitled Signal"
        return Song(title=title, lyrics=body, themes=theme_list, mood=mood)


def instrumental_title(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    if rng.random() < 0.4:
        return rng.choice(INSTRUMENTAL_TITLES)
    return f"{rng.choice(TITLE_ADJS)} {rng.choice(TITLE_NOUNS)}"


if __name__ == "__main__":  # quick manual check
    for seed, themes, mood in [(1, "ocean, late night driving", "nostalgic"), (2, "", "dark"), (3, "first love", "euphoric")]:
        song = LyricGenerator(seed).generate(themes, mood)
        print(f"=== {song.title} ({mood}) ===")
        print(song.lyrics)
        print()
