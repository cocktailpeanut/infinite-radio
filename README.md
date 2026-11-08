# Infinite Radio

A never-ending, fully local AI radio station. Describe a vibe, press **Tune in**, and
[YuE2](https://huggingface.co/audio-cpp/Yue2-3B-GGUF) (running on
[audio.cpp](https://github.com/0xShug0/audio.cpp)) writes, sings and mixes new
songs back to back while you listen. No cloud APIs, no accounts, no telemetry.

![Infinite Radio icon](icon.png)

## What it does

- **Infinite playback.** The station keeps a buffer of finished songs ahead of the
  player. When one ends, the next starts. Generation runs continuously in the
  background and the UI shows what is being composed, its phase and an ETA.
- **Your vibe, your lyrics.** Pick a preset or write a style prompt, choose a mood,
  optional lyric themes, target length and vocals mode (auto lyrics, instrumental,
  or only your own requests). A built-in lyric writer produces rhymed, singable
  verses offline.
- **Requests.** Paste your own lyrics (with `[Verse]` / `[Chorus]` tags) and the
  station plays them next.
- **Library.** Every song is saved as a 48 kHz stereo WAV. Bookmark, replay, download or
  delete songs from the Library tab.
- **Dark neon player.** Generated album art per song, live waveform visualizer,
  keyboard shortcuts (space = play/pause, `n` or → = skip, `b` = bookmark).
- **Cross-platform, hardware aware.** The installer picks the right audio.cpp build:
  Apple Metal on macOS, CUDA on NVIDIA (Windows/Linux), Vulkan on other GPUs,
  and CPU as a fallback.

### Honest expectations

YuE2 is a 3B-parameter autoregressive song model. Whether it can out-run playback
depends on your GPU:

| Hardware (published by the audio.cpp authors) | Generation speed |
|---|---|
| RTX 5090, Q8 model | ~5× faster than realtime (195 s of audio in 39 s) |
| RTX 4060 Laptop 8 GB, Q4 model | ~1.8× faster than realtime |
| Apple M1 Max 64 GB (Metal), Q8 model, measured while building this | ~1.4× faster than realtime (90 s song in 62–64 s, model load included) |
| CPU only | much slower than realtime |

Speed is not constant: on the M1 Max, four back-to-back 90 s songs took 62, 64, 93 and
~110 s as the chip warmed up, so a laptop may drift from 1.4× to about 1× realtime
under sustained load. When generation is slower than playback the station still works: it buffers songs
ahead (default 2), shows a "buffering" state between tracks, and the Engine tab
reports the measured realtime factor so you can lower the target length or buffer
more songs. Memory: Q8 peaks around 9 GB of GPU memory, Q4 around 8 GB.

YuE2 sings in **English only** and its weights are licensed **CC BY-NC 4.0**
(non-commercial). Songs are generated audio; lengths are approximate.

## Install and run (Pinokio)

In Pinokio, choose **Download from URL** and paste `https://github.com/cocktailpeanut/infinite-radio`.

1. Open the launcher and click **Install**. Choose **Q8** (recommended,
   ~4.5 GB download) or **Q4** (low VRAM, ~3 GB). The installer downloads the
   audio.cpp v0.9.4 release for your platform/GPU, installs the Python web app in a
   virtual environment, and fetches the YuE2 GGUF package from Hugging Face.
2. Click **Start**. The **Open Radio** tab appears once the server is up.
3. Press **Tune in**. The first song takes longer because the model has to load.

Menu items: **Start**, **Songs** (opens the WAV folder), **Update** (pulls the
launcher and refreshes Python deps), **Reinstall** (switch Q8/Q4), **Reset**
(removes binaries, model and venv; keeps your songs and library).

### What gets installed where

```
infinite_radio/
├── app/
│   ├── server.py        # FastAPI web server + JSON API
│   ├── radio.py         # station scheduler, lookahead queue, library
│   ├── engine.py        # supervises audiocpp_server, runs generations
│   ├── lyrics.py        # offline lyric writer
│   ├── static/          # dark neon web UI
│   ├── tools/extract_bin.py
│   ├── bin/             # audio.cpp release (downloaded)
│   ├── models/Yue2-3B-GGUF/  # YuE2 GGUF package (downloaded)
│   ├── output/          # generated WAV files
│   └── data/            # station.json, library.json, server config
├── install.js  start.js  update.js  reset.js  pinokio.js  pinokio.json
└── README.md
```

## How it works

1. `start.js` launches `python server.py` in the venv. The server picks a free port on
   `127.0.0.1` and prints its URL, which Pinokio captures for the **Open Radio** tab.
2. `engine.py` writes an audio.cpp server config (model directory, selected GGUF
   files, `yue2.mem_saver=true`) and starts `audiocpp_server --no-ui` on another
   free local port with the detected backend. If the process dies it is restarted
   on the next job.
3. `radio.py` runs a worker: while the station is on and fewer than *lookahead*
   songs are ready, it builds a job (auto lyrics, instrumental, or a user request),
   calls `POST /v1/tasks/run` on audio.cpp with the lyrics in `request.text` and the
   style prompt plus sampling options in `request.options`, saves the WAV, and
   appends it to the ready queue. Target length maps to
   `semantic_max_tokens = seconds × 25`.
4. The browser polls `/api/status`, pulls the next song with `/api/next`, streams the
   WAV from `/api/tracks/{id}/audio`, and asks for the next one when playback ends.

## API

Base URL: the address shown in the Pinokio terminal (for example
`http://127.0.0.1:52813`). Interactive docs: `/api/docs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Station state, settings, ready queue, current job (phase, elapsed, ETA), engine info, stats |
| GET | `/api/station` / PUT `/api/station` | Read / update station settings (`style`, `mood`, `lyric_mode`, `themes`, `target_seconds`, `planning`, `lookahead`, `quality_steps`) |
| POST | `/api/station/on`, `/api/station/off` | Turn the generator on or off |
| GET | `/api/next` | Pop the next ready song (`track` is `null` while buffering) |
| POST | `/api/requests` | Queue your own lyrics `{lyrics, title?, style?}` |
| DELETE | `/api/requests/{id}` | Remove a pending request |
| GET | `/api/library?liked=1` | All songs, newest first (`liked=1` returns bookmarked songs only) |
| GET | `/api/tracks/{id}` | Song metadata |
| GET | `/api/tracks/{id}/audio?download=1` | WAV audio (add `download=1` for an attachment) |
| POST | `/api/tracks/{id}/like` | Toggle bookmark (stored as the `liked` flag) |
| POST | `/api/tracks/{id}/requeue` | Put a song back at the front of the queue |
| DELETE | `/api/tracks/{id}` | Delete a song and its WAV |
| GET | `/api/presets` | Style presets and moods |
| GET | `/api/engine/log`, POST `/api/engine/restart` | audio.cpp log tail / restart |

### curl

```bash
BASE=http://127.0.0.1:52813   # use the URL from the terminal

# set the vibe and switch the station on
curl -X PUT $BASE/api/station -H 'Content-Type: application/json' \
  -d '{"style":"English, synthwave, retro 80s, glossy synths, breathy female vocal","mood":"dreamy","target_seconds":90}'
curl -X POST $BASE/api/station/on

# queue your own lyrics
curl -X POST $BASE/api/requests -H 'Content-Type: application/json' \
  -d '{"title":"Harbor Lights","lyrics":"[Verse]\nMorning sunlight fills the sky.\n[Chorus]\nSing together, you and I."}'

# wait for a song, then fetch it
curl $BASE/api/status | jq '.current, .ready[].title'
ID=$(curl -s $BASE/api/next | jq -r '.track.id')
curl -o song.wav "$BASE/api/tracks/$ID/audio"
```

### Python

```python
import requests, time
BASE = "http://127.0.0.1:52813"

requests.put(f"{BASE}/api/station", json={"style": "English, lo-fi soul, Rhodes piano, relaxed male vocal", "mood": "chill"})
requests.post(f"{BASE}/api/station/on")

while True:
    nxt = requests.get(f"{BASE}/api/next").json()
    if nxt["track"]:
        break
    cur = nxt.get("current") or {}
    print("composing", cur.get("title"), "eta", cur.get("eta_seconds"))
    time.sleep(5)

track = nxt["track"]
wav = requests.get(f"{BASE}/api/tracks/{track['id']}/audio").content
open(f"{track['title']}.wav", "wb").write(wav)
print(track["title"], track["duration"], "seconds")
```

### JavaScript

```js
const BASE = "http://127.0.0.1:52813";

await fetch(`${BASE}/api/station`, {
  method: "PUT", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ style: "English, indie pop, bright acoustic guitar, warm lead vocal", mood: "euphoric" }),
});
await fetch(`${BASE}/api/station/on`, { method: "POST" });

let track = null;
while (!track) {
  const r = await (await fetch(`${BASE}/api/next`)).json();
  track = r.track;
  if (!track) await new Promise((res) => setTimeout(res, 5000));
}
const audio = new Audio(`${BASE}/api/tracks/${track.id}/audio`);
audio.play();
```

## Running without Pinokio

```bash
cd app
python -m venv env && . env/bin/activate      # Windows: env\Scripts\activate
pip install -r requirements.txt
# put an audio.cpp release in app/bin and the YuE2 GGUF package in app/models/Yue2-3B-GGUF
python tools/extract_bin.py --version v0.9.4
python server.py
```

## Troubleshooting

- **"YuE2 main GGUF not found"**: the model download did not finish. Run
  **Reinstall** from the menu.
- **Engine state `error` / "audio.cpp server exited"**: check the Engine tab log
  and `logs/api/start.js/latest`. Host RAM pressure is the most common cause on
  16 GB machines; try the Q4 model and a shorter target length.
- **First song never finishes on CPU**: expected to be very slow. Use a GPU build
  or keep target length short.
- **Browser says press play**: autoplay policy. Click the play button once.

## Credits and licenses

- [audio.cpp](https://github.com/0xShug0/audio.cpp) by 0xShug0 and contributors
  (runtime, MIT-licensed; see `app/bin/LICENSE` after install).
- [YuE2](https://huggingface.co/audio-cpp/Yue2-3B-GGUF) by the m-a-p team, GGUF
  packaging by audio-cpp. Weights: CC BY-NC 4.0, non-commercial.
- Infinite Radio launcher and app: MIT (see `LICENSE`).
