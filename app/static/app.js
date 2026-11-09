/* Infinite Radio player, DJ sets and episodes */
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
    libSelectMode: false,
    libSelected: new Set(),
    libTracks: [],
    presets: [],
    moods: [],
    activeTab: "queue",
    lastError: null,
    set: null,          // open set summary from /api/status
    scene: null,
    episodes: [],
    sets: [],
    editor: null,       // {kind: 'set'|'custom', setId, name, tracks:[{track_id, display_title, duration, style, available}]}
    setup: false,       // inline set setup open
  };

  // ---------- helpers ----------
  const fmt = (s) => {
    if (!isFinite(s) || s < 0) s = 0;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = Math.floor(s % 60);
    return h ? `${h}:${m.toString().padStart(2, "0")}:${r.toString().padStart(2, "0")}` : `${m}:${r.toString().padStart(2, "0")}`;
  };
  const hueFor = (id) => { let h = 0; for (const ch of id || "x") h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h % 360; };
  const colorsFor = (id) => { const h = hueFor(id); return [`hsl(${h} 85% 62%)`, `hsl(${(h + 70) % 360} 90% 60%)`]; };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const bmSvg = '<svg class="bm" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>';

  let toastTimer;
  const toast = (msg, info = false, action = null) => {
    const t = $("toast"), a = $("toast-action");
    $("toast-text").textContent = msg;
    t.className = "toast" + (info ? " info" : "");
    if (action) { a.hidden = false; a.textContent = action.label; a.onclick = () => { t.hidden = true; action.run(); }; }
    else { a.hidden = true; a.onclick = null; }
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), action ? 7000 : 4500);
  };
  const api = async (path, opts = {}) => {
    const headers = opts.body instanceof FormData ? {} : { "Content-Type": "application/json" };
    const res = await fetch(path, { headers, ...opts, body: opts.body instanceof FormData ? opts.body : (opts.body ? JSON.stringify(opts.body) : undefined) });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      const err = new Error(typeof detail === "string" ? detail : (detail.code || "request failed"));
      err.status = res.status; err.detail = detail;
      throw err;
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("json") ? res.json() : res.text();
  };

  // ---------- visualizer ----------
  let actx, analyser, srcNode;
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
    requestAnimationFrame(draw);
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
      ctx.globalAlpha = 0.35 + v * 0.65;
      ctx.beginPath(); ctx.roundRect(i * (bw + gap), (h - bh) / 2, bw, bh, 3); ctx.fill();
    }
    ctx.globalAlpha = 1;
  };
  draw();

  // ---------- now playing ----------
  const setArt = (id) => { const [c1, c2] = colorsFor(id); $("art").style.setProperty("--c1", c1); $("art").style.setProperty("--c2", c2); };
  const renderNowPlaying = () => {
    const t = state.track, like = $("btn-like"), dl = $("btn-download"), skip = $("btn-skip");
    const inSet = !!state.set;
    skip.classList.toggle("cut", inSet);
    skip.title = inSet ? "Cut from the set (skips too)" : "Skip";
    if (!t) {
      $("np-kicker").textContent = state.listening ? (state.buffering ? "Buffering" : "On air") : "Nothing playing";
      $("np-title").textContent = state.listening ? "Your first song is being composed…" : "Tune in to start the station";
      $("np-meta").innerHTML = ""; $("lyrics").textContent = "—";
      like.setAttribute("aria-disabled", "true"); dl.setAttribute("aria-disabled", "true");
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
    like.setAttribute("aria-disabled", "false"); dl.setAttribute("aria-disabled", "false");
    dl.href = `/api/tracks/${t.id}/audio?download=1`;
    skip.setAttribute("aria-disabled", "false");
    setArt(t.id);
    if (state.set) renderStage();
  };

  // ---------- lists ----------
  const trackItem = (t, mode) => {
    const [c1, c2] = colorsFor(t.id);
    const sub = mode === "library"
      ? `${fmt(t.duration)} · ${new Date(t.created * 1000).toLocaleString()}${t.played ? "" : " · unplayed"}`
      : `${fmt(t.duration)} · ${t.style.split(",").slice(1, 4).join(",").trim()}`;
    const selectable = mode === "library" && state.libSelectMode;
    const selected = state.libSelected.has(t.id);
    return `<li class="track ${selectable ? "selectable" : ""} ${selected ? "selected" : ""}" data-id="${t.id}" style="--c1:${c1};--c2:${c2}">
      ${selectable ? `<input type="checkbox" class="sel" data-act="select" ${selected ? "checked" : ""}>` : ""}
      <div class="thumb"></div>
      <div class="info"><div class="title">${esc(t.title)}</div><div class="sub">${esc(sub)}</div></div>
      <div class="actions">
        <button data-act="play" title="Play now">▶</button>
        <button data-act="like" class="${t.liked ? "liked" : ""}" title="Bookmark">${bmSvg}</button>
        <a href="/api/tracks/${t.id}/audio?download=1" download title="Download">⤓</a>
        ${mode === "library" ? `<button data-act="delete" title="Delete">✕</button>` : ""}
      </div></li>`;
  };

  const renderQueue = (st) => {
    const ready = st.ready || [];
    $("queue").innerHTML = ready.map((t) => trackItem(t, "queue")).join("");
    $("queue-empty").hidden = ready.length > 0;
    const cur = st.current, card = $("gen-card");
    if (cur) {
      card.hidden = false;
      const loading = st.engine.state === "loading";
      $("gen-title").textContent = (loading ? "Loading model · " : "Composing · ") + cur.title;
      $("gen-phase").textContent = { loading: "Loading YuE2 weights into memory", planning: "Planning the score", composing: "Composing melody and vocals", rendering: "Rendering the acoustics", decoding: "Decoding audio" }[st.engine.phase] || "Working…";
      $("gen-elapsed").textContent = `${Math.floor(cur.elapsed)}s elapsed`;
      const fill = $("gen-fill");
      if (cur.eta_seconds != null) {
        const total = cur.elapsed + cur.eta_seconds;
        fill.classList.remove("indeterminate");
        fill.style.width = `${Math.min(97, (cur.elapsed / total) * 100)}%`;
        $("gen-eta").textContent = cur.eta_seconds > 1 ? `~${Math.ceil(cur.eta_seconds)}s left` : "finishing…";
      } else { fill.classList.add("indeterminate"); $("gen-eta").textContent = "estimating…"; }
    } else card.hidden = true;
    const s = st.stats || {};
    let speed = "";
    if (s.realtime_factor) {
      const x = s.realtime_factor;
      speed = `Generation speed: ${x.toFixed(2)}× realtime on ${st.engine.hardware}.` + (x >= 1 ? " Faster than playback, the queue stays full." : " Slower than playback, so the station buffers ahead between songs.");
    } else if (st.on) speed = `Measuring generation speed on ${st.engine.hardware}…`;
    $("speed").textContent = speed;
  };

  const renderLibrary = async () => {
    const data = await api(`/api/library?liked=${state.libLikedOnly ? 1 : 0}`);
    state.libTracks = data.tracks;
    $("library").innerHTML = data.tracks.map((t) => trackItem(t, "library")).join("") || `<p class="hint">Nothing here yet.</p>`;
    $("lib-count").textContent = `${data.total} song${data.total === 1 ? "" : "s"}`;
    $("lib-selbar").hidden = !state.libSelectMode;
    $("lib-select").textContent = state.libSelectMode ? "Done" : "Select";
    $("lib-selcount").textContent = `${state.libSelected.size} selected`;
  };

  const renderEngine = (st) => {
    const e = st.engine, pill = $("engine-pill");
    pill.textContent = `${e.hardware} · ${e.state}`;
    pill.className = "status " + (e.state === "ready" ? "ready" : e.state === "error" ? "error" : "busy");
    pill.title = e.message || "";
    if (state.activeTab === "engine") {
      const kv = [["State", `${e.state} — ${e.message}`], ["Backend", e.hardware + (e.backend ? ` (${e.backend})` : "")], ["audio.cpp", e.version || "unknown"],
        ["Model", `${e.model || "—"} + ${e.vae || "—"}`], ["Loaded", e.model_loaded ? "yes" : "no (loads on first song)"], ["Restarts", String(e.restarts)], ["Songs made", String(st.stats.songs_generated)],
        ["Lyric writer", st.lyrics ? (st.lyrics.available ? `${st.lyrics.model} · ${st.lyrics.ready} set${st.lyrics.ready === 1 ? "" : "s"} ready · last ${st.lyrics.last_mode}${st.lyrics.last?.seconds ? ` (${st.lyrics.last.seconds}s)` : ""}${st.lyrics.last?.error ? ` · ${st.lyrics.last.error}` : ""}` : "templates (lyric model not installed)") : "—"]];
      $("engine-kv").innerHTML = kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      $("engine-log").textContent = (e.last_log || []).join("\n") || "(no log yet)";
    }
  };

  const renderRequests = (st) => {
    $("r-list").innerHTML = (st.requests || []).map((r) => `<li><span>${esc(r.title || r.lyrics.split("\n")[0].slice(0, 36))}</span><button data-req="${r.id}">remove</button></li>`).join("");
  };

  const applySettingsToForm = (s) => {
    if (state.settingsDirty) return;
    $("f-style").value = s.style; $("f-mood").value = s.mood; $("f-lyric-mode").value = s.lyric_mode;
    $("f-themes").value = s.themes || ""; $("f-len").value = s.target_seconds; $("f-len-label").textContent = fmt(s.target_seconds);
    $("f-lookahead").value = String(s.lookahead); $("f-planning").checked = !!s.planning;
    if (s.language) $("f-language").value = s.language;
    updateLanguageNotes();
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.style === s.style));
  };

  // ---------- DJ set ----------
  const renderSetBar = () => {
    const s = state.set, bar = $("setbar");
    $("btn-set-start").hidden = !!s || !!state.setup;
    bar.hidden = !s;
    if (!s) return;
    $("setbar-name").textContent = s.name;
    $("setbar-kept").textContent = `${s.kept} kept`;
    $("setbar-cut").textContent = `${s.cut} cut`;
    const el = Math.max(0, Date.now() / 1000 - s.started_at);
    $("setbar-clock").textContent = fmt(el).padStart(5, "0");
    const plan = Number(s.planned_minutes || 0);
    $("setbar-plan").textContent = plan ? ` / ${fmt(plan * 60)}${el > plan * 60 ? " · over" : ""}` : "";
  };
  setInterval(() => { if (state.set) renderSetBar(); }, 1000);

  // ---------- set setup (inline, replaces the old dialog) ----------
  const describeVibe = (st) => {
    if (!st) return "—";
    const vocals = { auto: "auto lyrics", instrumental: "instrumental", "custom-only": "requests only" }[st.lyric_mode] || st.lyric_mode;
    return `${st.language || "English"} · ${st.style.split(",").slice(0, 3).map((x) => x.trim()).join(", ")} · ${st.mood} · ${vocals} · ~${fmt(st.target_seconds)} songs${st.themes ? ` · themes: ${st.themes}` : ""}`;
  };
  const renderSetup = () => {
    const on = !!state.setup;
    $("setup").hidden = !on;
    $("btn-set-start").hidden = on || !!state.set;
    if (!on) return;
    const sc = state.scene;
    $("su-scene").textContent = !sc || sc.kind === "none" ? "none yet (a mood-colored card is used)" : `${sc.kind === "image" ? "image" : "loop"} · ${sc.original_name || sc.file}`;
    $("su-scene-clear").hidden = !sc || sc.kind === "none";
    const ab = state.status?.ambience;
    $("su-amb").textContent = ab && ab.enabled ? `${ab.label} · ${ab.level_db} dB under the music` : "Off";
    const vibe = $("su-vibe");
    vibe.textContent = describeVibe(state.status?.settings) + (state.settingsDirty ? "  (unsaved edits in Station — save to use them)" : "");
    vibe.classList.toggle("unsaved", !!state.settingsDirty);
  };
  const openSetup = async () => {
    state.setup = true;
    $("su-name").value = `${(state.status?.settings?.name) || "Infinite Radio"} · ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
    try { if (!state.scene) state.scene = await api("/api/scene"); } catch {}
    renderSetup(); renderStage(true);
    $("setup").scrollIntoView({ behavior: "smooth", block: "center" });
  };
  const closeSetup = () => { state.setup = false; renderSetup(); renderStage(true); };
  const startSet = openSetup;
  $("su-cancel").onclick = closeSetup;
  $("su-edit").onclick = () => {
    const panel = document.querySelector(".station");
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    panel.classList.remove("flash"); void panel.offsetWidth; panel.classList.add("flash");
    $("f-style").focus({ preventScroll: true });
  };
  $("su-file").onchange = async (ev) => {
    const f = ev.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f, f.name);
    try { state.scene = await api("/api/scene/upload", { method: "POST", body: fd }); renderScene(); renderSetup(); renderStage(true); }
    catch (e) { toast(e.message); }
    ev.target.value = "";
  };
  $("su-scene-clear").onclick = async () => { state.scene = await api("/api/scene", { method: "DELETE" }); renderScene(); renderSetup(); renderStage(true); };
  $("su-start").onclick = async () => {
    const name = $("su-name").value.trim();
    const planned_minutes = Number($("su-length").value);
    try {
      if (state.settingsDirty) await saveSettings();
      await api("/api/sets/start", { method: "POST", body: { name, planned_minutes } });
      state.setup = false; renderSetup();
      await refresh();
      if (!state.listening) await tuneIn();
      toast("Set started. Everything that plays through is in; skip to cut.", true);
    } catch (e) { toast(e.message); }
  };
  const closeSet = async () => {
    try {
      const s = await api("/api/sets/close", { method: "POST" });
      state.set = null; renderSetBar();
      await refresh();
      switchTab("episodes");
      await openSetEditor(s.id);
      toast("Set closed. Review the tracklist, then render.", true);
    } catch (e) { toast(e.message); }
  };
  $("btn-set-start").onclick = startSet;
  $("btn-set-close").onclick = closeSet;

  // ---------- scene ----------
  const isGif = (sc) => !!sc && sc.kind === "video" && /\.gif$/i.test(sc.file || "");
  const renderScene = () => {
    const sc = state.scene; if (!sc) return;
    const prev = $("scene-preview");
    if (sc.kind === "image" || isGif(sc)) prev.innerHTML = `<img src="/api/scene/preview?${Date.now()}" alt="scene">`;
    else if (sc.kind === "video") prev.innerHTML = `<video src="/api/scene/preview?${Date.now()}" muted loop autoplay playsinline></video>`;
    else prev.innerHTML = `<span class="hint">No scene yet. A mood-colored title card is used.</span>`;
    $("scene-clear").hidden = sc.kind === "none";
    const box = $("scene-settings");
    box.className = `scene-settings is-${sc.kind}`;
    box.querySelectorAll("[data-scene]").forEach((el) => {
      const k = el.dataset.scene, v = sc.settings[k];
      if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    });
    if (!sc.ffmpeg) toast("ffmpeg was not found. Run Install again from the Pinokio menu to render episodes.");
  };
  const loadScene = async () => { state.scene = await api("/api/scene"); renderScene(); };
  $("scene-file").onchange = async (ev) => {
    const f = ev.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f, f.name);
    try { state.scene = await api("/api/scene/upload", { method: "POST", body: fd }); renderScene(); renderStage(true); toast("Scene updated.", true); }
    catch (e) { toast(e.message); }
    ev.target.value = "";
  };
  $("scene-clear").onclick = async () => { state.scene = await api("/api/scene", { method: "DELETE" }); renderScene(); renderStage(true); };
  $("scene-settings").addEventListener("change", async (ev) => {
    const el = ev.target.closest("[data-scene]"); if (!el) return;
    const v = el.type === "checkbox" ? el.checked : (el.type === "range" ? Number(el.value) : el.value);
    try { state.scene = await api("/api/scene", { method: "PUT", body: { [el.dataset.scene]: v } }); renderScene(); renderStage(true); renderEditor(); }
    catch (e) { toast(e.message); }
  });

  // ---------- tracklist editor ----------
  // Line under a song title: per-song override, then the chosen source. Never the style prompt.
  const subtitleFor = (t, source) => {
    if (source === "genre") return (t.genre || "").trim();
    if (source === "mood") return t.mood ? t.mood[0].toUpperCase() + t.mood.slice(1) : "";
    if (source === "language") {
      if (t.language) return t.language;
      const first = (t.style || "").split(",")[0].trim().toLowerCase();
      const hit = (state.languages || []).find((l) => l.name.toLowerCase() === first);
      return hit ? hit.name : "";
    }
    return "";
  };
  const subtitleSource = () => (state.scene && state.scene.settings && state.scene.settings.subtitle) || "none";

  const renderEditor = () => {
    const ed = state.editor, sec = $("ep-editor");
    sec.hidden = !ed;
    if (!ed) return;
    $("ed-heading").textContent = ed.kind === "set" ? "Set tracklist" : "Tracklist";
    $("ed-name").value = ed.name;
    $("ed-list").innerHTML = ed.tracks.map((t, i) => `
      <li class="ed-row ${t.available === false ? "unavailable" : ""}" data-i="${i}">
        <span class="num">${String(i + 1).padStart(2, "0")}</span>
        <div><input value="${esc(t.display_title)}" data-act="rename" ${t.available === false ? "disabled" : ""}>
          ${subtitleSource() !== "none" && t.available !== false ? `<input class="ed-subtitle" data-act="subtitle" value="${esc(t.subtitleEdited ? (t.subtitle || "") : subtitleFor(t, subtitleSource()))}" placeholder="no subtitle">` : ""}
          <div class="sub">${fmt(t.duration || 0)}${t.genre ? " · " + esc(t.genre) : t.mood ? " · " + esc(t.mood) : ""}${t.available === false ? " · audio missing" : ""}</div></div>
        <div class="ops"><button data-act="up" title="Move up">↑</button><button data-act="down" title="Move down">↓</button><button data-act="remove" title="Remove">✕</button></div>
      </li>`).join("") || `<li class="hint">No songs left. Add some from the Library or discard.</li>`;
    const total = ed.tracks.reduce((a, t) => a + (t.duration || 0), 0) - Math.max(0, ed.tracks.length - 1) * Number($("ed-xf").value);
    $("ed-total").textContent = `${ed.tracks.length} song${ed.tracks.length === 1 ? "" : "s"} · about ${fmt(total)}`;
    $("ed-render").disabled = ed.tracks.filter((t) => t.available !== false).length === 0;
  };
  $("ed-list").onclick = (ev) => {
    const btn = ev.target.closest("button[data-act]"), row = ev.target.closest(".ed-row");
    if (!btn || !row || !state.editor) return;
    const i = Number(row.dataset.i), tr = state.editor.tracks;
    if (btn.dataset.act === "up" && i > 0) [tr[i - 1], tr[i]] = [tr[i], tr[i - 1]];
    if (btn.dataset.act === "down" && i < tr.length - 1) [tr[i + 1], tr[i]] = [tr[i], tr[i + 1]];
    if (btn.dataset.act === "remove") tr.splice(i, 1);
    renderEditor();
  };
  $("ed-list").addEventListener("input", (ev) => {
    const row = ev.target.closest(".ed-row");
    if (!row || !state.editor) return;
    const t = state.editor.tracks[Number(row.dataset.i)];
    if (ev.target.matches("input[data-act=rename]")) t.display_title = ev.target.value;
    if (ev.target.matches("input[data-act=subtitle]")) { t.subtitle = ev.target.value; t.subtitleEdited = true; }
  });
  $("ed-name").oninput = () => { if (state.editor) state.editor.name = $("ed-name").value; };
  $("ed-xf").onchange = renderEditor;
  $("ed-cancel").onclick = () => { state.editor = null; renderEditor(); };

  const openSetEditor = async (setId) => {
    const s = await api(`/api/sets/${setId}`);
    const kept = s.tracks.filter((e) => e.state === "kept");
    state.editor = { kind: "set", setId, name: s.name, tracks: kept.map((e) => ({ track_id: e.track_id, display_title: e.display_title || e.title, duration: e.duration, style: e.style, available: e.available,
      mood: e.mood, genre: e.genre, language: e.language, subtitle: e.subtitle, subtitleEdited: e.subtitle !== undefined && e.subtitle !== null })) };
    renderEditor();
    $("ep-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const openCustomEditor = (name, tracks) => {
    state.editor = { kind: "custom", setId: null, name, tracks };
    renderEditor();
    switchTab("episodes");
    $("ep-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  };

  $("ed-render").onclick = async () => {
    const ed = state.editor; if (!ed) return;
    const tracks = ed.tracks.filter((t) => t.available !== false).map((t) => ({ track_id: t.track_id, display_title: t.display_title,
      ...(t.subtitleEdited ? { subtitle: t.subtitle || "" } : {}) }));
    const crossfade = Number($("ed-xf").value);
    try {
      if (ed.kind === "set") {
        await api(`/api/sets/${ed.setId}`, { method: "PUT", body: { name: ed.name, tracks } });
        await api(`/api/sets/${ed.setId}/render`, { method: "POST", body: { crossfade, name: ed.name } });
      } else {
        await api("/api/episodes", { method: "POST", body: { name: ed.name, tracks, crossfade } });
      }
      state.editor = null; renderEditor();
      toast("Rendering started. Progress shows under Episodes.", true);
      loadEpisodes();
    } catch (e) { toast(e.message); }
  };

  $("b-suggest").onclick = async () => {
    try {
      const r = await api(`/api/episodes/suggest?source=${$("b-source").value}&minutes=${$("b-minutes").value}&order=${$("b-order").value}`);
      if (!r.tracks.length) return toast("No songs available yet.");
      const label = r.source === "bookmarked" ? "Bookmarks" : "Recent";
      openCustomEditor(`${(state.status?.settings?.name) || "Infinite Radio"} · ${label} mix`, r.tracks.map((t) => ({ ...t, available: true })));
    } catch (e) { toast(e.message); }
  };

  // ---------- episodes list ----------
  const renderEpisodes = () => {
    const eps = state.episodes;
    $("ep-empty").hidden = eps.length > 0;
    $("ep-list").innerHTML = eps.map((j) => {
      const running = !["done", "error"].includes(j.status);
      const cls = j.status === "done" ? "done" : j.status === "error" ? "error" : "";
      const statusText = j.status === "done" ? `${fmt(j.duration || 0)} · ${(j.outputs.size / 1048576).toFixed(0)} MB` : j.status === "error" ? (j.error || "failed") : j.step;
      return `<li class="ep-item" data-id="${j.id}">
        <div class="ep-head"><span class="ep-name">${esc(j.name)}</span><span class="ep-status ${cls}">${esc(statusText)}</span></div>
        ${running ? `<div class="ep-bar"><div style="width:${Math.round(j.progress * 100)}%"></div></div>` : ""}
        <div class="ep-sub">${j.tracks.length} song${j.tracks.length === 1 ? "" : "s"} · ${new Date(j.created * 1000).toLocaleString()}${j.status === "done" ? ` · ${esc(j.outputs.folder)}` : ""}</div>
        ${j.status === "done" ? `<img class="ep-thumb" src="/api/episodes/${j.id}/thumbnail" alt="thumbnail">` : ""}
        <div class="ep-links">
          ${j.status === "done" ? `<a href="/api/episodes/${j.id}/file?download=1">Download MP4</a><a href="/api/episodes/${j.id}/thumbnail?download=1">Thumbnail</a><button data-act="copy">Copy description</button>` : ""}
          ${running ? `<button data-act="cancel">Cancel</button>` : `<button data-act="delete" class="danger">Delete</button>`}
        </div></li>`;
    }).join("");
  };
  const loadEpisodes = async () => {
    const [eps, sets] = await Promise.all([api("/api/episodes"), api("/api/sets")]);
    state.episodes = eps.episodes; state.sets = sets.sets;
    renderEpisodes(); renderSets();
  };
  $("ep-list").onclick = async (ev) => {
    const btn = ev.target.closest("button[data-act]"), item = ev.target.closest(".ep-item");
    if (!btn || !item) return;
    const id = item.dataset.id;
    try {
      if (btn.dataset.act === "copy") {
        const text = await api(`/api/episodes/${id}/description`);
        await navigator.clipboard.writeText(text);
        toast("Description with chapters copied.", true);
      } else if (btn.dataset.act === "cancel") { await api(`/api/episodes/${id}/cancel`, { method: "POST" }); loadEpisodes(); }
      else if (btn.dataset.act === "delete") { if (!confirm("Delete this episode and its files?")) return; await api(`/api/episodes/${id}`, { method: "DELETE" }); loadEpisodes(); }
    } catch (e) { toast(e.message); }
  };

  const renderSets = () => {
    const closed = state.sets.filter((s) => s.status === "closed");
    $("sets-section").hidden = closed.length === 0;
    $("sets-list").innerHTML = closed.slice(0, 8).map((s) => {
      const kept = s.tracks.filter((e) => e.state === "kept").length;
      return `<li class="ep-item" data-id="${s.id}">
        <div class="ep-head"><span class="ep-name">${esc(s.name)}</span><span class="ep-status">${kept} kept · ${s.tracks.filter((e) => e.state === "cut").length} cut</span></div>
        <div class="ep-sub">${new Date(s.started_at * 1000).toLocaleString()}${s.episode_id ? " · rendered" : ""}</div>
        <div class="ep-links"><button data-act="open">Open tracklist</button><button data-act="delete" class="danger">Delete</button></div></li>`;
    }).join("");
  };
  $("sets-list").onclick = async (ev) => {
    const btn = ev.target.closest("button[data-act]"), item = ev.target.closest(".ep-item");
    if (!btn || !item) return;
    try {
      if (btn.dataset.act === "open") await openSetEditor(item.dataset.id);
      else if (btn.dataset.act === "delete") { if (!confirm("Delete this set? Songs stay in the library.")) return; await api(`/api/sets/${item.dataset.id}`, { method: "DELETE" }); loadEpisodes(); }
    } catch (e) { toast(e.message); }
  };

  // ---------- status loop ----------
  const refresh = async () => {
    try {
      const st = await api("/api/status");
      state.status = st;
      $("onair").textContent = st.on ? "On air" : "Off air";
      $("onair").className = "status " + (st.on ? "on" : "off");
      $("btn-tune").hidden = state.listening;
      $("btn-tune").textContent = st.on && !state.listening ? "Tune in (station is on air)" : "Tune in";
      $("btn-stop").hidden = !st.on;
      const hadSet = !!state.set;
      state.set = st.set;
      if (st.set && state.setup) { state.setup = false; }
      renderSetBar(); renderSetup(); onAmbienceStatus(st.ambience);
      if (hadSet !== !!st.set) renderNowPlaying();
      renderStage();
      applySettingsToForm(st.settings);
      renderQueue(st); renderEngine(st); renderRequests(st);
      if (st.errors && st.errors.length && st.errors[st.errors.length - 1] !== state.lastError) { state.lastError = st.errors[st.errors.length - 1]; toast(state.lastError); }
      if (state.listening && !state.track && st.ready.length && !state.pendingNext) playNext();
      if (state.listening && state.buffering) {
        const cur = st.current;
        $("buffering-text").textContent = cur ? (st.engine.state === "loading" ? "Loading the model for the first song…" : `Composing "${cur.title}"${cur.eta_seconds > 1 ? ` · ~${Math.ceil(cur.eta_seconds)}s` : ""}`) : (st.on ? "Waiting for the next song…" : "Station is off air");
      }
      if (state.activeTab === "episodes" && (st.rendering || state.episodes.some((j) => !["done", "error"].includes(j.status)))) loadEpisodes();
    } catch (e) {
      $("engine-pill").textContent = "server unreachable";
      $("engine-pill").className = "status error";
    }
  };

  // ---------- playback ----------
  const setBuffering = (b) => { state.buffering = b; $("buffering").hidden = !b; renderNowPlaying(); };
  const playTrack = async (t) => {
    state.track = t;
    audio.src = `/api/tracks/${t.id}/audio`;
    audio.volume = 1;
    setBuffering(false); renderNowPlaying();
    try { ensureAudioGraph(); if (actx && actx.state === "suspended") await actx.resume(); await audio.play(); }
    catch (e) { toast("Press play to start audio (browser autoplay rule).", true); }
  };
  const playNext = async () => {
    if (state.pendingNext) return;
    state.pendingNext = true;
    try {
      const r = await api("/api/next");
      if (r.track) await playTrack(r.track); else { state.track = null; setBuffering(true); }
    } catch (e) { toast(e.message); } finally { state.pendingNext = false; }
  };
  const cutAndSkip = async () => {
    if (!state.listening) return;
    if (state.set && state.track) {
      try {
        const cut = await api("/api/sets/cut", { method: "POST", body: { track_id: state.track.id } });
        toast(`Cut "${cut.title}" from the set.`, true, { label: "Undo", run: async () => { try { await api("/api/sets/undo", { method: "POST" }); refresh(); toast("Put it back in the set.", true); } catch (e) { toast(e.message); } } });
      } catch (e) { /* not in set anymore */ }
    }
    playNext();
  };

  audio.addEventListener("playing", () => ambSync());
  audio.addEventListener("waiting", () => ambSync());
  audio.addEventListener("ended", () => {
    setTimeout(ambSync, 0);
    if (state.track && state.set) api("/api/player/ended", { method: "POST", body: { track_id: state.track.id } }).catch(() => {});
    if (state.listening) playNext();
  });
  audio.addEventListener("play", () => { $("art").classList.add("playing"); $("btn-play").classList.remove("paused"); $("btn-play").querySelector(".play-icon").textContent = "❚❚"; });
  audio.addEventListener("pause", () => { $("art").classList.remove("playing"); $("btn-play").classList.add("paused"); $("btn-play").querySelector(".play-icon").textContent = "▶"; });
  const FADE = 2.5;
  audio.addEventListener("timeupdate", () => {
    if (audio.duration) {
      const left = audio.duration - audio.currentTime;
      audio.volume = Math.min(1, Math.max(0.05, Math.min(left / FADE, (audio.currentTime + 0.15) / 0.6)));
    }
    $("t-cur").textContent = fmt(audio.currentTime); $("t-dur").textContent = fmt(audio.duration);
    if (audio.duration) $("seek").value = Math.floor((audio.currentTime / audio.duration) * 1000);
  });
  $("seek").addEventListener("input", () => { if (audio.duration) audio.currentTime = ($("seek").value / 1000) * audio.duration; });

  $("btn-play").onclick = async () => {
    ensureAudioGraph(); if (actx && actx.state === "suspended") await actx.resume();
    if (!state.track) { if (!state.listening) return tuneIn(); return playNext(); }
    if (audio.paused) { state.userPaused = false; audio.play(); } else { state.userPaused = true; audio.pause(); }
    ambSync();
  };
  $("btn-skip").onclick = cutAndSkip;
  $("btn-like").onclick = async () => {
    if (!state.track) return;
    const t = await api(`/api/tracks/${state.track.id}/like`, { method: "POST" });
    state.track = t; renderNowPlaying();
    if (state.activeTab === "library") renderLibrary();
  };

  const tuneIn = async () => {
    try {
      ensureAudioGraph(); if (actx && actx.state === "suspended") await actx.resume();
      await api("/api/station/on", { method: "POST" });
      state.listening = true; state.userPaused = false;
      await refresh(); await playNext();
    } catch (e) { toast(e.message); }
  };
  $("btn-tune").onclick = tuneIn;
  $("btn-stop").onclick = async () => { await api("/api/station/off", { method: "POST" }); state.listening = false; setBuffering(false); ambSync(); refresh(); };

  // ---------- settings ----------
  const formPatch = () => ({ style: $("f-style").value.trim(), mood: $("f-mood").value, lyric_mode: $("f-lyric-mode").value, themes: $("f-themes").value.trim(), target_seconds: Number($("f-len").value), lookahead: Number($("f-lookahead").value), planning: $("f-planning").checked, language: $("f-language").value });
  const updateLanguageNotes = () => {
    const lang = $("f-language").value, meta = (state.languages || []).find((l) => l.name === lang);
    $("f-lang-tier").textContent = meta ? `(${meta.label})` : "";
    const note = $("f-lang-note"), writer = state.status?.lyrics;
    if (writer && !writer.available && lang !== "English") { note.textContent = "The lyric model is not installed, so auto lyrics fall back to English templates. Run Install again to add it."; note.className = "field-note warn"; }
    else if (meta && meta.tier === "untested") { note.textContent = "YuE2 was not shown singing this language; expect an accent or garbled words."; note.className = "field-note warn"; }
    else if (meta && meta.tier === "reported") { note.textContent = "Users report YuE2 sings this language acceptably."; note.className = "field-note"; }
    else { note.textContent = ""; note.className = "field-note"; }
    $("f-planning-note").textContent = lang === "English" ? "Slower, more structured songs" : "Slower, more structured songs. Off for non-English lyrics (it garbles pronunciation).";
    $("f-planning").disabled = lang !== "English";
  };
  const saveSettings = async () => {
    try {
      await api("/api/station", { method: "PUT", body: formPatch() });
      state.settingsDirty = false;
      $("save-note").hidden = false; setTimeout(() => ($("save-note").hidden = true), 3000);
      if (state.set) toast("New section in the set: the next songs follow this vibe.", true);
      refresh();
    } catch (e) { toast(e.message); }
  };
  $("save").onclick = saveSettings;
  ["f-style", "f-mood", "f-lyric-mode", "f-themes", "f-len", "f-lookahead", "f-planning", "f-language"].forEach((id) => {
    $(id).addEventListener("input", () => { state.settingsDirty = true; if (id === "f-len") $("f-len-label").textContent = fmt($("f-len").value); if (id === "f-language") updateLanguageNotes(); if (state.setup) renderSetup(); });
  });
  const loadPresets = async () => {
    const p = await api("/api/presets");
    state.presets = p.presets; state.moods = p.moods; state.languages = p.languages || [];
    $("f-mood").innerHTML = p.moods.map((m) => `<option value="${m}">${m[0].toUpperCase() + m.slice(1)}</option>`).join("");
    $("f-language").innerHTML = state.languages.map((l) => `<option value="${l.name}">${l.name}${l.tier === "evaluated" ? "" : l.tier === "demo" ? " · demo" : l.tier === "reported" ? " · reported" : " · untested"}</option>`).join("");
    $("presets").innerHTML = p.presets.map((x) => `<button class="chip" data-style="${esc(x.style)}" data-mood="${x.mood}">${esc(x.name)}</button>`).join("");
    document.querySelectorAll(".chip").forEach((c) => c.onclick = () => { $("f-style").value = c.dataset.style; $("f-mood").value = c.dataset.mood; state.settingsDirty = true; saveSettings(); });
  };

  // ---------- requests ----------
  $("r-send").onclick = async () => {
    const lyrics = $("r-lyrics").value.trim();
    if (!lyrics) return toast("Write some lyrics first.");
    try {
      await api("/api/requests", { method: "POST", body: { lyrics, title: $("r-title").value, style: $("r-style").value } });
      $("r-lyrics").value = ""; $("r-title").value = ""; $("r-style").value = "";
      toast("Request queued. It plays after the current song is ready.", true); refresh();
    } catch (e) { toast(e.message); }
  };
  $("r-list").onclick = async (ev) => { const id = ev.target.dataset.req; if (id) { await api(`/api/requests/${id}`, { method: "DELETE" }); refresh(); } };

  // ---------- library / queue clicks ----------
  const listClick = (mode) => async (ev) => {
    const li = ev.target.closest(".track"); if (!li) return;
    const id = li.dataset.id;
    const sel = ev.target.closest("[data-act=select]");
    if (sel || (mode === "library" && state.libSelectMode && !ev.target.closest(".actions"))) {
      if (state.libSelected.has(id)) state.libSelected.delete(id); else state.libSelected.add(id);
      renderLibrary(); return;
    }
    const btn = ev.target.closest("[data-act]"); if (!btn) return;
    try {
      if (btn.dataset.act === "play") { const t = await api(`/api/tracks/${id}`); if (mode === "queue") await api(`/api/next`); await playTrack(t); }
      else if (btn.dataset.act === "like") { await api(`/api/tracks/${id}/like`, { method: "POST" }); if (state.track && state.track.id === id) { state.track.liked = !state.track.liked; renderNowPlaying(); } if (mode === "library") renderLibrary(); else refresh(); }
      else if (btn.dataset.act === "delete") { if (!confirm("Delete this song permanently?")) return; await api(`/api/tracks/${id}`, { method: "DELETE" }); renderLibrary(); }
    } catch (e) { toast(e.message); }
  };
  $("queue").onclick = listClick("queue");
  $("library").onclick = listClick("library");
  $("lib-liked").onchange = () => { state.libLikedOnly = $("lib-liked").checked; renderLibrary(); };
  $("lib-select").onclick = () => { state.libSelectMode = !state.libSelectMode; if (!state.libSelectMode) state.libSelected.clear(); renderLibrary(); };
  $("lib-selcancel").onclick = () => { state.libSelectMode = false; state.libSelected.clear(); renderLibrary(); };
  $("lib-build").onclick = () => {
    const chosen = state.libTracks.filter((t) => state.libSelected.has(t.id));
    if (!chosen.length) return toast("Select some songs first.");
    chosen.sort((a, b) => a.created - b.created);
    openCustomEditor(`${(state.status?.settings?.name) || "Infinite Radio"} · Selection`, chosen.map((t) => ({ track_id: t.id, display_title: t.title, duration: t.duration, style: t.style, available: true, mood: t.mood, genre: t.genre, language: t.language })));
    state.libSelectMode = false; state.libSelected.clear(); renderLibrary();
  };

  const switchTab = (name) => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    state.activeTab = name;
    ["queue", "library", "episodes", "engine"].forEach((n) => $(`tab-${n}`).hidden = n !== name);
    if (name === "library") renderLibrary();
    if (name === "engine" && state.status) { renderEngine(state.status); loadAmbience(); }
    if (name === "episodes") { loadScene(); loadEpisodes(); loadAmbience(); }
  };
  document.querySelectorAll(".tab").forEach((tab) => tab.onclick = () => switchTab(tab.dataset.tab));
  $("engine-restart").onclick = async () => { try { await api("/api/engine/restart", { method: "POST" }); toast("audio.cpp restarted", true); refresh(); } catch (e) { toast(e.message); } };

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.code === "Space") { e.preventDefault(); $("btn-play").click(); }
    if (e.key === "n" || e.key === "ArrowRight") $("btn-skip").click();
    if (e.key === "l" || e.key === "b") $("btn-like").click();
  });

  // ---------- stage: live preview of the episode look during a set ----------
  const fxCanvas = $("stage-fx"), fxCtx = fxCanvas.getContext("2d");
  let fxParticles = null, fxKind = "none", fxRunning = false, stageSceneKey = "";
  const initFx = (kind, density) => {
    const W = fxCanvas.width = fxCanvas.clientWidth || 760, H = fxCanvas.height = fxCanvas.clientHeight || 428;
    const counts = { rain: 260, snow: 110, leaves: 30, dust: 70 };
    const n = Math.round((counts[kind] || 0) * (0.35 + density));
    fxParticles = Array.from({ length: n }, () => ({ x: Math.random() * W, y: Math.random() * H, v: kind === "rain" ? 7 + Math.random() * 6 : kind === "dust" ? 0.25 + Math.random() * 0.4 : 0.7 + Math.random() * 1.1, len: 10 + Math.random() * 16, a: 0.25 + Math.random() * 0.45, ph: Math.random() * 6.28 }));
    fxKind = kind;
  };
  let fxLast = 0;
  const fxLoop = (ts = 0) => {
    if (!fxRunning) return;
    requestAnimationFrame(fxLoop);
    if (ts - fxLast < 33 || document.hidden) return;  // ~30 fps, idle when hidden
    fxLast = ts;
    const W = fxCanvas.width, H = fxCanvas.height;
    fxCtx.clearRect(0, 0, W, H);
    if (!fxParticles || fxKind === "none") return;
    const t = ts / 1000;
    for (const p of fxParticles) {
      p.y += p.v; if (p.y > H + 20) { p.y = -20; p.x = Math.random() * W; }
      const wob = fxKind === "rain" ? 0 : Math.sin(t * 1.3 + p.ph) * 10;
      const x = p.x + wob;
      if (fxKind === "rain") {
        fxCtx.strokeStyle = `rgba(225,238,255,${p.a * 0.6})`; fxCtx.lineWidth = 1;
        fxCtx.beginPath(); fxCtx.moveTo(x, p.y); fxCtx.lineTo(x - p.len * 0.12, p.y - p.len); fxCtx.stroke();
      } else {
        const r = fxKind === "leaves" ? 3 : fxKind === "snow" ? 2 : 1.2;
        fxCtx.fillStyle = fxKind === "leaves" ? `rgba(235,160,90,${p.a})` : fxKind === "dust" ? `rgba(255,235,200,${p.a * 0.7})` : `rgba(250,250,255,${p.a})`;
        fxCtx.beginPath(); fxCtx.arc(x, p.y, r, 0, 6.28); fxCtx.fill();
      }
    }
  };
  const renderStage = (force = false) => {
    const inSet = !!state.set || !!state.setup, stage = $("stage"), wrap = $("art-wrap");
    wrap.hidden = inSet; stage.hidden = !inSet;
    if (!inSet) { fxRunning = false; $("stage-video").pause(); return; }
    $("stage-station").textContent = (state.status?.settings?.name) || "Infinite Radio";
    const sc = state.scene;
    if (!sc) { api("/api/scene").then((x) => { state.scene = x; renderStage(true); }).catch(() => {}); return; }
    const key = `${sc.kind}:${sc.file}:${JSON.stringify(sc.settings)}`;
    if (force || key !== stageSceneKey) {
      stageSceneKey = key;
      const img = $("stage-img"), vid = $("stage-video"), st = sc.settings;
      stage.classList.toggle("has-scene", sc.kind !== "none");
      stage.classList.toggle("grain", !!st.grain);
      const gif = isGif(sc);
      img.hidden = !(sc.kind === "image" || gif); vid.hidden = sc.kind !== "video" || gif;
      if (sc.kind === "image") {
        img.src = `/api/scene/preview?${Date.now()}`;
        img.className = st.camera ? (st.flicker ? "flicker" : "drift") : (st.flicker ? "flicker" : "");
        initFx(st.weather || "none", Number(st.weather_density ?? 0.6));
      } else if (gif) {
        img.src = `/api/scene/preview?${Date.now()}`;  // GIFs animate on their own; no CSS drift
        img.className = "";
        vid.pause();
        initFx("none", 0);
      } else {
        initFx("none", 0);
        if (sc.kind === "video") { vid.src = `/api/scene/preview?${Date.now()}`; vid.play().catch(() => {}); }
      }
      if (!fxRunning) { fxRunning = true; requestAnimationFrame(fxLoop); }
    }
    const t = state.track, card = $("stage-card");
    const titleStyle = sc.settings.title_style || "minimal";
    card.hidden = !t || !state.set || titleStyle === "none";
    card.className = `stage-card ${titleStyle}`;
    if (t && state.set) {
      const n = String(Math.max(1, state.set.kept || 0)).padStart(2, "0");
      $("stage-num").textContent = n; $("stage-n").textContent = n;
      $("stage-title").textContent = t.title;
      const sub = subtitleFor(t, sc.settings.subtitle || "none");
      $("stage-tags").textContent = sub; $("stage-tags").hidden = !sub;
    }
  };
  window.addEventListener("resize", () => { if (state.set && state.scene) renderStage(true); });

  // ---------- ambience: panel ----------
  const fmtPct = (x) => `${Math.round((x || 0) * 100)}%`;
  const loadAmbience = async () => { try { state.amb = await api("/api/ambience"); renderAmbience(); } catch (e) { /* server restarting */ } };
  const ambBusy = (job) => !!job && (job.state === "running" || job.state === "waiting");
  const renderAmbJob = (job) => {
    const box = $("amb-job"), err = $("amb-error");
    if (ambBusy(job)) {
      box.hidden = false; err.hidden = true;
      $("amb-step").textContent = job.step || "Working…";
      $("amb-fill").style.width = fmtPct(job.progress);
    } else {
      box.hidden = true;
      if (job && job.state === "error" && job.id !== state.ambDismissedJob) { err.hidden = false; err.textContent = job.error || "Something went wrong."; }
      else err.hidden = true;
    }
    $("amb-generate").disabled = ambBusy(job);
  };
  const renderAmbience = () => {
    const a = state.amb; if (!a) return;
    const pool = a.pool, st = a.settings, job = a.job;
    $("amb-enabled").checked = !!(st.enabled && pool);
    $("amb-enabled").disabled = !pool;
    const src = pool ? { generate: "generated", upload: "uploaded", scene: "from the scene video" }[pool.source] || pool.source : "";
    $("amb-label").textContent = pool ? `${pool.label} · ${pool.takes.length} take${pool.takes.length === 1 ? "" : "s"} · ${src}` : "None yet";
    $("amb-preview").hidden = !pool;
    $("amb-preview").textContent = state.ambPreview ? "■ Stop preview" : "▶ Preview";
    $("amb-clear").hidden = !pool;
    $("amb-scene").hidden = !a.scene_has_audio;
    $("amb-regen").hidden = !(pool && pool.source === "generate");
    $("amb-controls").hidden = !pool;
    if (document.activeElement !== $("amb-level")) $("amb-level").value = st.level_db;
    $("amb-level-label").textContent = `${st.level_db} dB`;
    $("amb-duck").checked = !!st.duck;
    $("amb-presets").innerHTML = (a.presets || []).map((p) => `<button class="chip ${pool && pool.source === "generate" && pool.label === p.name ? "active" : ""}" data-preset="${p.id}">${esc(p.name)}</button>`).join("");
    const m = a.model;
    $("amb-size").textContent = `${(m.size / 1e9).toFixed(1)} GB${m.partial ? `, ${(m.partial / 1e9).toFixed(1)} GB already downloaded` : ""}`;
    $("amb-license").href = m.license.url; $("amb-license").textContent = m.license.name;
    $("amb-license-summary").textContent = m.license.summary;
    $("amb-consent").hidden = !state.ambPending;
    $("amb-remove-model").hidden = !m.installed;
    renderAmbJob(job);
  };
  const requestGenerate = async (req) => {
    state.ambDismissedJob = state.amb?.job?.id;
    try { state.amb = await api("/api/ambience/generate", { method: "POST", body: req }); state.ambPending = null; renderAmbience(); }
    catch (e) {
      if (e.status === 409 && e.detail && e.detail.code === "model_required") {
        state.ambPending = req; await loadAmbience();
        $("amb-consent").scrollIntoView({ behavior: "smooth", block: "center" });
      } else toast(e.message);
    }
  };
  $("amb-presets").onclick = (ev) => { const b = ev.target.closest("[data-preset]"); if (b) requestGenerate({ preset: b.dataset.preset, prompt: "" }); };
  $("amb-generate").onclick = () => { const p = $("amb-prompt").value.trim(); if (!p) return toast("Describe a sound, or pick one of the presets."); requestGenerate({ prompt: p }); };
  $("amb-prompt").addEventListener("keydown", (e) => { if (e.key === "Enter") $("amb-generate").click(); });
  $("amb-regen").onclick = () => { const pool = state.amb?.pool; if (pool) requestGenerate({ prompt: pool.prompt || pool.label }); };
  $("amb-consent-cancel").onclick = () => { state.ambPending = null; renderAmbience(); };
  $("amb-consent-ok").onclick = async () => {
    const req = state.ambPending; state.ambPending = null;
    try { state.amb = await api("/api/ambience/model/download", { method: "POST", body: { accept_license: true, then_generate: req } }); renderAmbience(); }
    catch (e) { toast(e.message); }
  };
  $("amb-cancel").onclick = async () => { try { state.amb = await api("/api/ambience/cancel", { method: "POST" }); renderAmbience(); } catch (e) { toast(e.message); } };
  $("amb-file").onchange = async (ev) => {
    const f = ev.target.files[0]; ev.target.value = ""; if (!f) return;
    const fd = new FormData(); fd.append("file", f, f.name);
    try { state.amb = await api("/api/ambience/upload", { method: "POST", body: fd }); renderAmbience(); } catch (e) { toast(e.message); }
  };
  $("amb-scene").onclick = async () => { try { state.amb = await api("/api/ambience/from-scene", { method: "POST" }); renderAmbience(); } catch (e) { toast(e.message); } };
  $("amb-clear").onclick = async () => { state.ambPreview = false; state.amb = await api("/api/ambience", { method: "DELETE" }); renderAmbience(); refresh(); };
  $("amb-enabled").onchange = async () => { state.amb = await api("/api/ambience", { method: "PUT", body: { enabled: $("amb-enabled").checked } }); renderAmbience(); refresh(); };
  $("amb-duck").onchange = async () => { state.amb = await api("/api/ambience", { method: "PUT", body: { duck: $("amb-duck").checked } }); renderAmbience(); refresh(); };
  $("amb-level").addEventListener("input", () => { const v = Number($("amb-level").value); $("amb-level-label").textContent = `${v} dB`; ambSetLevel(v); });
  $("amb-level").addEventListener("change", async () => { state.amb = await api("/api/ambience", { method: "PUT", body: { level_db: Number($("amb-level").value) } }); renderAmbience(); refresh(); });
  $("amb-preview").onclick = async () => {
    ensureAudioGraph(); if (actx && actx.state === "suspended") await actx.resume();
    state.ambPreview = !state.ambPreview; renderAmbience(); ambSync();
  };
  $("amb-remove-model").onclick = async () => {
    if (!confirm("Remove the ambience model (1.7 GB)? Ambience you already generated keeps working; generating new ones downloads it again.")) return;
    try { const r = await api("/api/ambience/model", { method: "DELETE" }); state.amb = r; renderAmbience(); toast("Ambience model removed.", true); } catch (e) { toast(e.message); }
  };
  $("su-amb-edit").onclick = () => { switchTab("episodes"); setTimeout(() => $("amb-section").scrollIntoView({ behavior: "smooth", block: "start" }), 50); };
  const onAmbienceStatus = (brief) => {
    if (!brief) return;
    const prev = state.ambBrief; state.ambBrief = brief;
    const job = brief.job;
    if (ambBusy(job)) { renderAmbJob(job); if (!state.amb || !ambBusy(state.amb.job)) loadAmbience(); }
    else if (prev && ambBusy(prev.job)) loadAmbience();  // a job just finished: refresh pool, controls and player
    if (!prev || prev.pool_id !== brief.pool_id || prev.enabled !== brief.enabled || prev.level_db !== brief.level_db) loadAmbience();
    ambSync();
  };

  // ---------- ambience: live playback (Web Audio) ----------
  // Takes are normalized to -30 dBFS RMS; songs play at roughly -16 dBFS, so "level" is dB under that.
  const AMB_REF_DB = -16, AMB_TAKE_DB = -30, AMB_XF = 3;
  const ambP = { poolId: null, buffers: [], loadingId: null, out: null, duck: null, running: false, next: 0, last: -1, bag: [], timer: null, sources: new Set() };
  const ambLinear = (lvl) => Math.pow(10, (AMB_REF_DB + lvl - AMB_TAKE_DB) / 20);
  const ambSetLevel = (lvl) => { if (ambP.out && actx) ambP.out.gain.setTargetAtTime(ambLinear(lvl), actx.currentTime, 0.3); };
  const ambLoadBuffers = async (pool) => {
    if (ambP.poolId === pool.id && ambP.buffers.length) return true;
    if (ambP.loadingId === pool.id) return false;
    ambP.loadingId = pool.id;
    try {
      const bufs = await Promise.all(pool.takes.map(async (t) => actx.decodeAudioData(await (await fetch(t.url)).arrayBuffer())));
      ambP.buffers = bufs; ambP.poolId = pool.id; ambP.bag = []; ambP.last = -1;
      return true;
    } catch (e) { console.warn("ambience load failed", e); return false; }
    finally { ambP.loadingId = null; }
  };
  const ambNextIndex = () => {
    const n = ambP.buffers.length; if (n === 1) return 0;
    if (!ambP.bag.length) { ambP.bag = [...Array(n).keys()].sort(() => Math.random() - 0.5); if (ambP.bag[0] === ambP.last) ambP.bag.push(ambP.bag.shift()); }
    ambP.last = ambP.bag.shift(); return ambP.last;
  };
  const curve = (up) => { const c = new Float32Array(64); for (let k = 0; k < 64; k++) c[k] = Math.sqrt(up ? k / 63 : 1 - k / 63); return c; };
  const CURVE_UP = curve(true), CURVE_DOWN = curve(false);
  const ambSchedule = () => {
    if (!ambP.running || !actx) return;
    while (ambP.next < actx.currentTime + 8) {
      const b = ambP.buffers[ambNextIndex()];
      const src = actx.createBufferSource(), g = actx.createGain();
      const t0 = Math.max(ambP.next, actx.currentTime + 0.02), dur = b.duration, xf = Math.min(AMB_XF, dur / 3);
      src.buffer = b; g.gain.setValueAtTime(0, t0);
      g.gain.setValueCurveAtTime(CURVE_UP, t0, xf);
      g.gain.setValueCurveAtTime(CURVE_DOWN, t0 + dur - xf, xf);
      src.connect(g); g.connect(ambP.out);
      src.start(t0); src.stop(t0 + dur + 0.05);
      ambP.sources.add(src); src.onended = () => { ambP.sources.delete(src); try { g.disconnect(); } catch {} };
      ambP.next = t0 + dur - xf;
    }
  };
  const ambStart = async () => {
    const pool = state.amb?.pool; if (!pool || !actx) return;
    if (!(await ambLoadBuffers(pool))) return;
    if (ambP.running) return;
    ambP.out = actx.createGain(); ambP.duck = actx.createGain();
    ambP.out.gain.setValueAtTime(0, actx.currentTime);
    ambP.out.gain.linearRampToValueAtTime(ambLinear(state.amb.settings.level_db), actx.currentTime + 1.5);
    ambP.out.connect(ambP.duck); ambP.duck.connect(actx.destination);
    ambP.running = true; ambP.next = actx.currentTime + 0.05;
    ambSchedule(); ambP.timer = setInterval(ambSchedule, 1000);
    ambDuck();
  };
  const ambStop = () => {
    if (!ambP.running) return;
    ambP.running = false; clearInterval(ambP.timer);
    const out = ambP.out, duck = ambP.duck, srcs = [...ambP.sources];
    out.gain.cancelScheduledValues(actx.currentTime); out.gain.setTargetAtTime(0, actx.currentTime, 0.3);
    setTimeout(() => { srcs.forEach((s) => { try { s.stop(); } catch {} }); try { out.disconnect(); duck.disconnect(); } catch {} }, 1500);
    ambP.sources.clear();
  };
  const ambDuck = () => {
    if (!ambP.running || !ambP.duck) return;
    const b = state.ambBrief || {};
    const musicOn = !!state.track && !audio.paused && !audio.ended && audio.readyState >= 3;
    const target = b.duck && musicOn && !state.ambPreview ? Math.pow(10, -6 / 20) : 1;
    ambP.duck.gain.setTargetAtTime(target, actx.currentTime, 0.6);
  };
  const ambSync = () => {
    const b = state.ambBrief || {}, pool = state.amb?.pool;
    const want = !!actx && !!pool && (state.ambPreview || (state.listening && !state.userPaused && b.enabled && b.pool_id === pool.id));
    if (ambP.running && (!want || ambP.poolId !== pool?.id)) ambStop();
    if (want && !ambP.running) ambStart();
    if (ambP.running && state.amb) ambSetLevel(state.amb.settings.level_db);
    ambDuck();
  };
  setInterval(ambDuck, 500);

  // ---------- boot ----------
  loadPresets().then(refresh);
  setInterval(refresh, 1500);
  renderNowPlaying();
})();
