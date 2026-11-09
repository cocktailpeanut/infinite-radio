"""Inspiration words for songs whose station has no lyric themes.

Left without a topic, a small lyric model keeps returning the same few
abstractions (love, time, freedom, echoes); shown its earlier titles, it copies
them. So the variety comes from here: each song gets two concrete words drawn
at random, and the model builds a specific theme around them. Words used
recently are skipped so neighbouring songs never share one.
"""

from __future__ import annotations

import random
from collections import deque

SEEDS = sorted(set("""
umbrella harbor lighthouse postcard balcony streetcar bakery typewriter orchard laundromat rooftop ferry
greenhouse snow-globe ticket-stub ice-cream-truck bookstore night-market jukebox fireflies paper-lantern
bicycle swimming-pool vending-machine polaroid suitcase seashell garden-hose phone-booth thunderstorm
late-bus rice-field museum diner neon-sign airport-gate lemon-tree fountain-pen moth porch-light
cassette-tape fishing-boat apartment-hallway carnival subway-map sunflower-field moving-boxes old-sweater
wedding-ring chalk-drawing campfire skyline kite laundry-line hotel-lobby piano-bar first-snow canal
tea-house sand-dune train-window highway-motel radio-static parking-lot vinyl-shop lost-glove street-musician
corner-store city-bus last-train rain-gutter window-seat stairwell elevator payphone crosswalk billboard
overpass gas-station harbor-crane bridge-lights river-bank pier boardwalk arcade bowling-alley drive-in
rooftop-garden fire-escape alleyway night-shift taxi-meter hospital-corridor school-gym locker graduation
birthday-cake candle matchbox lighter cigarette-smoke coffee-cup tea-kettle morning-toast breakfast-table
kitchen-radio refrigerator-note grocery-list apron rolling-pin flour wooden-spoon dinner-plate wine-glass
cherry-blossom maple-leaves autumn-wind winter-coat scarf mittens snowman frozen-lake ice-skates
summer-heat cicadas sprinkler lemonade beach-towel sunscreen sandcastle tide-pool driftwood sea-glass
seagull fishing-net anchor sailboat ocean-liner island-ferry volcano palm-tree hammock coconut
desert-road cactus mirage tumbleweed cowboy-boots rodeo pickup-truck dusty-radio motel-pool neon-cactus
mountain-cabin pine-forest hiking-trail compass map campground tent sleeping-bag stargazing telescope
comet satellite moon-landing astronaut planetarium meteor-shower northern-lights aurora
library-card bookmark handwritten-letter envelope stamp mailbox carrier-pigeon telegram fax-machine
pager answering-machine flip-phone text-message voicemail selfie photo-album slideshow home-video
film-camera darkroom projector cinema-seat popcorn movie-ticket credits drive-in-screen
violin cello trumpet saxophone drum-kit bass-guitar piano-keys metronome sheet-music record-player
headphones mixtape radio-dial microphone karaoke stage-lights encore backstage tour-bus
paint-brush canvas easel sketchbook pottery-wheel clay sculpture gallery museum-guard
train-station platform-bench conductor timetable luggage passport boarding-pass jet-lag hotel-key
postcard-rack souvenir foreign-coins street-food night-train sleeper-car border-crossing
garden-gate rose-bush ivy-wall birdhouse bird-feeder wind-chime rocking-chair front-porch
attic basement old-trunk grandfather-clock pocket-watch hourglass calendar sticky-note
paper-airplane origami paper-boat kite-string balloon confetti fireworks parade marching-band
carousel ferris-wheel cotton-candy roller-coaster funhouse-mirror fortune-teller tarot-card
chess-board dice playing-cards jigsaw-puzzle crossword video-game arcade-token pinball
basketball-court soccer-field swimming-lane finish-line running-shoes skateboard surfboard
snow-plow street-sweeper night-guard lighthouse-keeper baker florist tailor barber mechanic
hospital-window waiting-room train-delay traffic-jam detour roadside-diner truck-stop
bus-shelter rain-puddle wet-pavement streetlamp flickering-sign shop-window mannequin
""".split()))


def human(seed: str) -> str:
    return seed.replace("-", " ")


class SeedPicker:
    def __init__(self, rng: random.Random | None = None, recent: int = 40):
        self.rng = rng or random.Random()
        self.recent: deque[str] = deque(maxlen=recent)

    def pick(self, k: int = 2) -> list[str]:
        pool = [s for s in SEEDS if s not in self.recent] or list(SEEDS)
        chosen = self.rng.sample(pool, k)
        self.recent.extend(chosen)
        return [human(s) for s in chosen]
