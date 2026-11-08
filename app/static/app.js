/* Infinite Radio player */
(() => {
  const $ = (id) => document.getElementById(id);
  const audio = $("audio");

  const state = {
    status: null,
    listening: false,
    track: null,
    buffering: false,
    pendingNext: false,
    settingsDirty: false,
    libLikedOnly: false,
    presets: [],
    moods: [],
    activeTab: "queue",
    lastStatusAt: 0,
  };

  // ---------- helpers ----------
  const fmt = (s) => {
    if (!isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60), r = Math.floor(s % 60);
    return `${m}:${r.toString().padStart(2, "0")}`;
  };
  const hueFor = (id) => {
    let h = 0;
    for (const ch of id || "x") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return h % 360;
  };
  const colorsFor = (id) => {
    const h = hueFor(id);
    return [`hsl(${h} 85% 62%)`, `hsl(${(h + 70) % 360} 90% 60%)`];
  };
  let toastTimer;
  const toast = (msg, info = false) => {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast" + (info ? " info" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), 4500);
  };
  const api = async (path, opts = {}) => {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    return res.json();
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- visualizer ----------
  let actx, analyser, srcNode, raf;
  const canvas = $("viz"), ctx = canvas.getContext("2d");
  const ensureAudioGraph = () => {
    if (actx) return;
    try {
      actx = new (window.AudioContext || window.webkitAudioContext)();
      srcNode = actx.createMediaElementSource(audio);
      analyser = actx.createAnalyser();
      analyser.fftSize = 256;
      srcNode.connect(analyser);
      analyser.connect(actx.destination);
    } catch (e) { console.warn("visualizer unavailable", e); }
  };
  let lastDraw = 0;
  const draw = (ts = 0) => {
    raf = requestAnimationFrame(draw);
    // ~30 fps is plenty for bars and keeps GPU time for the model.
    if (ts - lastDraw < 33 || document.hidden) return;
    lastDraw = ts;
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!analyser) return;
    const data = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(data);
    const bars = 64, gap = 4, bw = (w - gap * (bars - 1)) / bars;
    const [c1, c2] = state.track ? colorsFor(state.track.id) : ["#8b5cf6", "#22d3ee"];
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, c1); grad.addColorStop(1, c2);
    ctx.fillStyle = grad;
    for (let i = 0; i < bars; i++) {
      const idx = Math.floor((i / bars) * data.length * 0.8);
      const v = audio.paused ? 2 : data[idx] / 255;
      const bh = Math.max(3, v * h);
      const x = i * (bw + gap);
      ctx.globalAlpha = 0.35 + v * 0.65;
      ctx.beginPath();
      ctx.roundRect(x, (h - bh) / 2, bw, bh, 3);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  };
  draw();

  // ---------- rendering ----------
  const setArt = (id) => {
    const [c1, c2] = colorsFor(id);
    $("art").style.setProperty("--c1", c1);
    $("art").style.setProperty("--c2", c2);
  };

  const renderNowPlaying = () => {
    const t = state.track;
    const like = $("btn-like"), dl = $("btn-download"), skip = $("btn-skip");
    if (!t) {
      $("np-kicker").textContent = state.listening ? (state.buffering ? "Buffering" : "On air") : "Nothing playing";
      $("np-title").textContent = state.listening ? "Your first song is being composed…" : "Tune in to start the station";
      $("np-meta").innerHTML = "";
      $("lyrics").textContent = "—";
      like.setAttribute("aria-disabled", "true");
      dl.setAttribute("aria-disabled", "true");
      skip.setAttribute("aria-disabled", state.listening ? "false" : "true");
      setArt("idle");
      return;
    }
    $("np-kicker").textContent = t.kind === "request" ? "Your request" : (t.lyrics ? "Now playing" : "Instrumental");
    $("np-title").textContent = t.title;
    const tags = t.style.split(",").map((s) => s.trim()).filter(Boolean).slice(0, 7);
    $("np-meta").innerHTML = tags.map((x) => `<span class="tag">${esc(x)}</span>`).join("") +
      `<div style="margin-top:8px">${fmt(t.duration)} · seed ${t.seed}${t.rtf ? ` · rendered ${(1 / t.rtf).toFixed(1)}× realtime` : ""}</div>`;
    $("lyrics").textContent = t.lyrics || "(instrumental)";
    like.classList.toggle("liked", !!t.liked);
    like.setAttribute("aria-disabled", "false");
    dl.setAttribute("aria-disabled", "false");
    dl.href = `/api/tracks/${t.id}/audio?download=1`;
    skip.setAttribute("aria-disabled", "false");
    setArt(t.id);
  };

  const trackItem = (t, mode) => {
    const [c1, c2] = colorsFor(t.id);
    const sub = mode === "library"
      ? `${fmt(t.duration)} · ${new Date(t.created * 1000).toLocaleString()}${t.played ? "" : " · unplayed"}`
      : `${fmt(t.duration)} · ${t.style.split(",").slice(1, 4).join(",").trim()}`;
    return `<li class="track" data-id="${t.id}" style="--c1:${c1};--c2:${c2}">
      <div class="thumb"></div>
      <div class="info"><div class="title">${esc(t.title)}</div><div class="sub">${esc(sub)}</div></div>
      <div class="actions">
        <button data-act="play" title="Play now">▶</button>
        <button data-act="like" class="${t.liked ? "liked" : ""}" title="Bookmark"><svg class="bm" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg></button>
        <a href="/api/tracks/${t.id}/audio?download=1" download title="Download">⤓</a>
        ${mode === "library" ? `<button data-act="delete" title="Delete">✕</button>` : ""}
      </div></li>`;
  };

  const renderQueue = (st) => {
    const ready = st.ready || [];
    $("queue").innerHTML = ready.map((t) => trackItem(t, "queue")).join("");
    $("queue-empty").hidden = ready.length > 0;
    const cur = st.current;
    const card = $("gen-card");
    if (cur) {
      card.hidden = false;
      const loading = st.engine.state === "loading";
      $("gen-title").textContent = (loading ? "Loading model · " : "Composing · ") + cur.title;
      const phaseText = {
        loading: "Loading YuE2 weights into memory",
        planning: "Planning the score",
        composing: "Composing melody and vocals",
        rendering: "Rendering the acoustics",
        decoding: "Decoding audio",
      }[st.engine.phase] || "Working…";
      $("gen-phase").textContent = phaseText;
      $("gen-elapsed").textContent = `${Math.floor(cur.elapsed)}s elapsed`;
      const fill = $("gen-fill");
      if (cur.eta_seconds != null) {
        const total = cur.elapsed + cur.eta_seconds;
        fill.classList.remove("indeterminate");
        fill.style.width = `${Math.min(97, (cur.elapsed / total) * 100)}%`;
        $("gen-eta").textContent = cur.eta_seconds > 1 ? `~${Math.ceil(cur.eta_seconds)}s left` : "finishing…";
      } else {
        fill.classList.add("indeterminate");
        $("gen-eta").textContent = "estimating…";
      }
    } else {
      card.hidden = true;
    }
    const s = st.stats || {};
    let speed = "";
    if (s.realtime_factor) {
      const x = s.realtime_factor;
      speed = `Generation speed: ${x.toFixed(2)}× realtime on ${st.engine.hardware}.`;
      speed += x >= 1 ? " Faster than playback, the queue stays full." : " Slower than playback, so the station buffers ahead between songs.";
    } else if (st.on) {
      speed = `Measuring generation speed on ${st.engine.hardware}…`;
    }
    $("speed").textContent = speed;
  };

  const renderLibrary = async () => {
    const data = await api(`/api/library?liked=${state.libLikedOnly ? 1 : 0}`);
    $("library").innerHTML = data.tracks.map((t) => trackItem(t, "library")).join("") || `<p class="hint">Nothing here yet.</p>`;
    $("lib-count").textContent = `${data.total} song${data.total === 1 ? "" : "s"}`;
  };

  const renderEngine = (st) => {
    const e = st.engine;
    const pill = $("engine-pill");
    pill.textContent = `${e.hardware} · ${e.state}`;
    pill.className = "status " + (e.state === "ready" ? "ready" : e.state === "error" ? "error" : "busy");
    pill.title = e.message || "";
    if (state.activeTab === "engine") {
      const kv = [
        ["State", `${e.state} — ${e.message}`],
        ["Backend", e.hardware + (e.backend ? ` (${e.backend})` : "")],
        ["audio.cpp", e.version || "unknown"],
        ["Model", `${e.model || "—"} + ${e.vae || "—"}`],
        ["Loaded", e.model_loaded ? "yes" : "no (loads on first song)"],
        ["Restarts", String(e.restarts)],
        ["Songs made", String(st.stats.songs_generated)],
      ];
      $("engine-kv").innerHTML = kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      $("engine-log").textContent = (e.last_log || []).join("\n") || "(no log yet)";
    }
  };

  const renderRequests = (st) => {
    $("r-list").innerHTML = (st.requests || []).map((r) =>
      `<li><span>${esc(r.title || r.lyrics.split("\n")[0].slice(0, 36))}</span><button data-req="${r.id}">remove</button></li>`).join("");
  };

  const applySettingsToForm = (s) => {
    if (state.settingsDirty) return;
    $("f-style").value = s.style;
    $("f-mood").value = s.mood;
    $("f-lyric-mode").value = s.lyric_mode;
    $("f-themes").value = s.themes || "";
    $("f-len").value = s.target_seconds;
    $("f-len-label").textContent = fmt(s.target_seconds);
    $("f-lookahead").value = String(s.lookahead);
    $("f-planning").checked = !!s.planning;
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.style === s.style));
  };

  // ---------- status loop ----------
  const refresh = async () => {
    try {
      const st = await api("/api/status");
      state.status = st;
      state.lastStatusAt = Date.now();
      $("onair").textContent = st.on ? "On air" : "Off air";
      $("onair").className = "status " + (st.on ? "on" : "off");
      // "Tune in" is about this browser listening; "Go off air" is about the generator.
      $("btn-tune").hidden = state.listening;
      $("btn-tune").textContent = st.on && !state.listening ? "Tune in (station is on air)" : "Tune in";
      $("btn-stop").hidden = !st.on;
      applySettingsToForm(st.settings);
      renderQueue(st);
      renderEngine(st);
      renderRequests(st);
      if (st.errors && st.errors.length && st.errors[st.errors.length - 1] !== state.lastError) {
        state.lastError = st.errors[st.errors.length - 1];
        toast(state.lastError);
      }
      if (state.listening && !state.track && st.ready.length && !state.pendingNext) {
        playNext();
      }
      if (state.listening && state.buffering) {
        const cur = st.current;
        $("buffering-text").textContent = cur
          ? (st.engine.state === "loading" ? "Loading the model for the first song…" : `Composing "${cur.title}"${cur.eta_seconds > 1 ? ` · ~${Math.ceil(cur.eta_seconds)}s` : ""}`)
          : (st.on ? "Waiting for the next song…" : "Station is off air");
      }
    } catch (e) {
      $("engine-pill").textContent = "server unreachable";
      $("engine-pill").className = "status error";
    }
  };

  // ---------- playback ----------
  const setBuffering = (b) => {
    state.buffering = b;
    $("buffering").hidden = !b;
    renderNowPlaying();
  };

  const playTrack = async (t) => {
    state.track = t;
    audio.src = `/api/tracks/${t.id}/audio`;
    setBuffering(false);
    renderNowPlaying();
    try {
      ensureAudioGraph();
      if (actx && actx.state === "suspended") await actx.resume();
      await audio.play();
    } catch (e) {
      toast("Press play to start audio (browser autoplay rule).", true);
    }
  };

  const playNext = async () => {
    if (state.pendingNext) return;
    state.pendingNext = true;
    try {
      const r = await api("/api/next");
      if (r.track) {
        await playTrack(r.track);
      } else {
        state.track = null;
        setBuffering(true);
      }
    } catch (e) {
      toast(e.message);
    } finally {
      state.pendingNext = false;
    }
  };

  audio.addEventListener("ended", () => { if (state.listening) playNext(); });
  audio.addEventListener("play", () => { $("art").classList.add("playing"); $("btn-play").classList.remove("paused"); $("btn-play").querySelector(".play-icon").textContent = "❚❚"; });
  audio.addEventListener("pause", () => { $("art").classList.remove("playing"); $("btn-play").classList.add("paused"); $("btn-play").querySelector(".play-icon").textContent = "▶"; });
  const FADE = 2.5;
  audio.addEventListener("timeupdate", () => {
    // Capped songs can stop abruptly: ease the edges instead of hard cuts.
    if (audio.duration) {
      const left = audio.duration - audio.currentTime;
      const v = Math.min(1, Math.max(0.05, Math.min(left / FADE, (audio.currentTime + 0.15) / 0.6)));
      audio.volume = v;
    }
    $("t-cur").textContent = fmt(audio.currentTime);
    $("t-dur").textContent = fmt(audio.duration);
    if (audio.duration) $("seek").value = Math.floor((audio.currentTime / audio.duration) * 1000);
  });
  $("seek").addEventListener("input", () => { if (audio.duration) audio.currentTime = ($("seek").value / 1000) * audio.duration; });

  $("btn-play").onclick = async () => {
    ensureAudioGraph();
    if (actx && actx.state === "suspended") await actx.resume();
    if (!state.track) {
      if (!state.listening) return tuneIn();
      return playNext();
    }
    if (audio.paused) audio.play(); else audio.pause();
  };
  $("btn-skip").onclick = () => { if (state.listening) playNext(); };
  $("btn-like").onclick = async () => {
    if (!state.track) return;
    const t = await api(`/api/tracks/${state.track.id}/like`, { method: "POST" });
    state.track = t; renderNowPlaying();
    if (state.activeTab === "library") renderLibrary();
  };

  const tuneIn = async () => {
    try {
      ensureAudioGraph();
      if (actx && actx.state === "suspended") await actx.resume();
      await api("/api/station/on", { method: "POST" });
      state.listening = true;
      await refresh();
      await playNext();
    } catch (e) { toast(e.message); }
  };
  $("btn-tune").onclick = tuneIn;
  $("btn-stop").onclick = async () => {
    await api("/api/station/off", { method: "POST" });
    state.listening = false;
    setBuffering(false);
    refresh();
  };

  // ---------- settings ----------
  const formPatch = () => ({
    style: $("f-style").value.trim(),
    mood: $("f-mood").value,
    lyric_mode: $("f-lyric-mode").value,
    themes: $("f-themes").value.trim(),
    target_seconds: Number($("f-len").value),
    lookahead: Number($("f-lookahead").value),
    planning: $("f-planning").checked,
  });
  const saveSettings = async () => {
    try {
      await api("/api/station", { method: "PUT", body: formPatch() });
      state.settingsDirty = false;
      $("save-note").hidden = false;
      setTimeout(() => ($("save-note").hidden = true), 3000);
      refresh();
    } catch (e) { toast(e.message); }
  };
  $("save").onclick = saveSettings;
  ["f-style", "f-mood", "f-lyric-mode", "f-themes", "f-len", "f-lookahead", "f-planning"].forEach((id) => {
    $(id).addEventListener("input", () => { state.settingsDirty = true; if (id === "f-len") $("f-len-label").textContent = fmt($("f-len").value); });
  });

  const loadPresets = async () => {
    const p = await api("/api/presets");
    state.presets = p.presets; state.moods = p.moods;
    $("f-mood").innerHTML = p.moods.map((m) => `<option value="${m}">${m[0].toUpperCase() + m.slice(1)}</option>`).join("");
    $("presets").innerHTML = p.presets.map((x) => `<button class="chip" data-style="${esc(x.style)}" data-mood="${x.mood}">${esc(x.name)}</button>`).join("");
    document.querySelectorAll(".chip").forEach((c) => c.onclick = () => {
      $("f-style").value = c.dataset.style; $("f-mood").value = c.dataset.mood;
      state.settingsDirty = true; saveSettings();
    });
  };

  // ---------- requests ----------
  $("r-send").onclick = async () => {
    const lyrics = $("r-lyrics").value.trim();
    if (!lyrics) return toast("Write some lyrics first.");
    try {
      await api("/api/requests", { method: "POST", body: { lyrics, title: $("r-title").value, style: $("r-style").value } });
      $("r-lyrics").value = ""; $("r-title").value = ""; $("r-style").value = "";
      toast("Request queued. It plays after the current song is ready.", true);
      refresh();
    } catch (e) { toast(e.message); }
  };
  $("r-list").onclick = async (ev) => {
    const id = ev.target.dataset.req;
    if (id) { await api(`/api/requests/${id}`, { method: "DELETE" }); refresh(); }
  };

  // ---------- lists ----------
  const listClick = (mode) => async (ev) => {
    const btn = ev.target.closest("[data-act]");
    const li = ev.target.closest(".track");
    if (!btn || !li) return;
    const id = li.dataset.id;
    try {
      if (btn.dataset.act === "play") {
        const t = await api(`/api/tracks/${id}`);
        if (mode === "queue") await api(`/api/next`); // consume nothing specific; keep order simple
        await playTrack(t);
      } else if (btn.dataset.act === "like") {
        await api(`/api/tracks/${id}/like`, { method: "POST" });
        if (state.track && state.track.id === id) { state.track.liked = !state.track.liked; renderNowPlaying(); }
        if (mode === "library") renderLibrary(); else refresh();
      } else if (btn.dataset.act === "delete") {
        if (!confirm("Delete this song permanently?")) return;
        await api(`/api/tracks/${id}`, { method: "DELETE" });
        renderLibrary();
      }
    } catch (e) { toast(e.message); }
  };
  $("queue").onclick = listClick("queue");
  $("library").onclick = listClick("library");
  $("lib-liked").onchange = () => { state.libLikedOnly = $("lib-liked").checked; renderLibrary(); };

  document.querySelectorAll(".tab").forEach((tab) => tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    state.activeTab = tab.dataset.tab;
    ["queue", "library", "engine"].forEach((n) => $(`tab-${n}`).hidden = n !== state.activeTab);
    if (state.activeTab === "library") renderLibrary();
    if (state.activeTab === "engine" && state.status) renderEngine(state.status);
  });

  $("engine-restart").onclick = async () => {
    try { await api("/api/engine/restart", { method: "POST" }); toast("audio.cpp restarted", true); refresh(); }
    catch (e) { toast(e.message); }
  };

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.code === "Space") { e.preventDefault(); $("btn-play").click(); }
    if (e.key === "n" || e.key === "ArrowRight") $("btn-skip").click();
    if (e.key === "l" || e.key === "b") $("btn-like").click();
  });

  // ---------- boot ----------
  loadPresets().then(refresh);
  setInterval(refresh, 1500);
  renderNowPlaying();
})();
