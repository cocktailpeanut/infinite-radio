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
  // Multipart upload with progress (fetch cannot report upload progress); large videos take a while.
  const uploadFile = (path, file) => new Promise((resolve, reject) => {
    const fd = new FormData(); fd.append("file", file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    const mb = (n) => (n / 1048576).toFixed(0);
    if (file.size > 20 * 1048576) {
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) toast(`Uploading ${file.name}: ${Math.floor((e.loaded / e.total) * 100)}% (${mb(e.loaded)} of ${mb(e.total)} MB)`, true); };
      xhr.upload.onload = () => toast(`Uploaded ${file.name}. Preparing…`, true);
    }
    xhr.onload = () => {
      let body = null; try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
      const detail = body && body.detail !== undefined ? body.detail : xhr.statusText;
      const err = new Error(typeof detail === "string" ? detail : (detail.code || "upload failed"));
      err.status = xhr.status; err.detail = detail; reject(err);
    };
    xhr.onerror = () => reject(new Error("upload failed (connection lost)"));
    xhr.send(fd);
  });

  // ---------- full-screen shell: drawers, idle chrome, lyrics, full screen ----------
  // The backdrop fills the window. Controls (.chrome) fade after a few idle seconds while a
  // song plays and come back on mouse move, a key, or a tap. Drawers slide over the picture,
  // or dock beside it when pinned on wide screens.
  const body = document.body;
  const store = {
    get: (k, d) => { try { const v = localStorage.getItem("ir." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem("ir." + k, JSON.stringify(v)); } catch {} },
  };
  const ui = {
    open: { station: false, side: false },
    pinned: { station: false, side: false, ...store.get("pinned", {}) },
    lyrics: !!store.get("lyrics", false),
    backdrop: store.get("backdrop", "scene"),   // "scene" | "viz"
    overChrome: false, idleTimer: null, pointer: "mouse", wokeByTap: false,
  };
  const wideQuery = window.matchMedia("(min-width: 1100px)");
  const isPinned = (d) => !!ui.pinned[d] && wideQuery.matches;
  const isOpen = (d) => ui.open[d] || isPinned(d);
  const overlayOpen = () => ["station", "side"].some((d) => ui.open[d] && !isPinned(d));
  const renderShell = () => {
    for (const d of ["station", "side"]) { body.classList.toggle(`open-${d}`, isOpen(d)); body.classList.toggle(`pin-${d}`, isPinned(d)); }
    body.classList.toggle("lyrics-open", ui.lyrics);
    $("lyrics-panel").hidden = !ui.lyrics;
    $("btn-lyrics").classList.toggle("on", ui.lyrics);
    document.querySelectorAll("[data-pin]").forEach((b) => b.classList.toggle("on", !!ui.pinned[b.dataset.pin]));
    document.querySelectorAll("[data-open]").forEach((b) => {
      const k = b.dataset.open;
      b.classList.toggle("on", k === "station" ? isOpen("station") : isOpen("side") && (k === "queue" ? ["queue", "library"] : [k]).includes(state.activeTab));
    });
  };
  const openDrawer = (d, tab) => {
    if (!wideQuery.matches) ui.open[d === "station" ? "side" : "station"] = false;  // one at a time on small screens
    ui.open[d] = true;
    if (d === "side") switchTab(tab || state.activeTab);
    renderShell(); wake();
  };
  const closeDrawer = (d) => {
    ui.open[d] = false;
    if (ui.pinned[d]) { ui.pinned[d] = false; store.set("pinned", ui.pinned); }
    renderShell();
  };
  const toggleDrawer = (d, tab) => {
    if (d !== "side") return isOpen(d) ? closeDrawer(d) : openDrawer(d);
    const group = tab === "queue" ? ["queue", "library"] : [tab];
    if (isOpen("side") && group.includes(state.activeTab)) return closeDrawer("side");
    openDrawer("side", group.includes(state.activeTab) ? state.activeTab : tab);
  };
  const togglePin = (d) => { ui.pinned[d] = !ui.pinned[d]; if (ui.pinned[d]) ui.open[d] = true; store.set("pinned", ui.pinned); renderShell(); };
  const closeOverlays = () => { for (const d of ["station", "side"]) if (!isPinned(d)) ui.open[d] = false; renderShell(); };
  const toggleLyrics = (on = !ui.lyrics) => { ui.lyrics = on; store.set("lyrics", on); renderShell(); wake(); };

  const IDLE_MS = 3200;
  const canIdle = () => !!state.track && !audio.paused && !state.setup && !overlayOpen() && !ui.overChrome &&
    !(document.activeElement && document.activeElement.matches("input, textarea, select"));
  const goIdle = () => { clearTimeout(ui.idleTimer); if (canIdle()) body.classList.add("idle"); else ui.idleTimer = setTimeout(goIdle, IDLE_MS); };
  const wake = () => { body.classList.remove("idle"); clearTimeout(ui.idleTimer); ui.idleTimer = setTimeout(goIdle, IDLE_MS); };
  document.addEventListener("pointermove", (e) => { if (!e.pointerType || e.pointerType === "mouse") wake(); }, { passive: true });
  document.addEventListener("pointerdown", (e) => {
    ui.pointer = e.pointerType || "mouse";
    if (ui.pointer !== "mouse" && body.classList.contains("idle")) { wake(); ui.wokeByTap = true; setTimeout(() => { ui.wokeByTap = false; }, 700); }
  }, true);
  document.querySelectorAll(".chrome, .setbar, .drawer").forEach((el) => {
    el.addEventListener("pointerenter", () => { ui.overChrome = true; });
    el.addEventListener("pointerleave", () => { ui.overChrome = false; });
  });
  // Clicking the picture closes floating drawers; on touch screens a tap shows or hides the controls.
  $("main").addEventListener("click", (e) => {
    if (e.target.closest(".chrome, .np, .hero, .setup, .lyrics-panel, .setbar")) return;
    if (ui.wokeByTap) { ui.wokeByTap = false; return; }
    if (overlayOpen()) { closeOverlays(); wake(); return; }
    if (ui.pointer !== "mouse" && canIdle()) { clearTimeout(ui.idleTimer); body.classList.add("idle"); }
  });

  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement;
  const fsOk = !!(document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen);
  const toggleFull = () => {
    if (!fsOk) return;
    if (fsEl()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    else { const el = document.documentElement, r = (el.requestFullscreen || el.webkitRequestFullscreen).call(el); if (r && r.catch) r.catch(() => {}); }
  };
  if (!fsOk) $("btn-full").hidden = true;
  ["fullscreenchange", "webkitfullscreenchange"].forEach((ev) => document.addEventListener(ev, () => $("btn-full").classList.toggle("on", !!fsEl())));

  const sceneShown = () => !!state.scene && state.scene.kind !== "none" && ui.backdrop !== "viz";
  const toggleBackdrop = () => {
    if (!state.scene || state.scene.kind === "none") return toast("Add a scene under Episodes to put a picture or loop behind the player.", true);
    ui.backdrop = ui.backdrop === "viz" ? "scene" : "viz"; store.set("backdrop", ui.backdrop);
    $("scene-live").checked = ui.backdrop !== "viz";
    renderStage(true);
  };

  document.querySelectorAll("[data-open]").forEach((b) => { b.onclick = () => (b.dataset.open === "station" ? toggleDrawer("station") : toggleDrawer("side", b.dataset.open)); });
  document.querySelectorAll("[data-close]").forEach((b) => { b.onclick = () => closeDrawer(b.dataset.close); });
  document.querySelectorAll("[data-pin]").forEach((b) => { b.onclick = () => togglePin(b.dataset.pin); });
  $("btn-lyrics").onclick = () => toggleLyrics();
  $("lyrics-close").onclick = () => toggleLyrics(false);
  $("btn-full").onclick = toggleFull;
  $("next-chip").onclick = () => openDrawer("side", "queue");
  $("engine-pill").onclick = () => openDrawer("side", "engine");
  wideQuery.addEventListener("change", renderShell);

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
    if (!canvas.offsetParent) return;  // a scene is showing instead
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const cw = Math.round(canvas.clientWidth * dpr), ch = Math.round(canvas.clientHeight * dpr);
    if (cw && ch && (canvas.width !== cw || canvas.height !== ch)) { canvas.width = cw; canvas.height = ch; }
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!analyser) return;
    const data = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(data);
    const bars = 64, gap = 4 * dpr, bw = (w - gap * (bars - 1)) / bars;
    const [c1, c2] = state.track ? colorsFor(state.track.id) : ["#8b5cf6", "#22d3ee"];
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, c1); grad.addColorStop(1, c2);
    ctx.fillStyle = grad;
    for (let i = 0; i < bars; i++) {
      const idx = Math.floor((i / bars) * data.length * 0.8);
      const v = audio.paused ? 2 : data[idx] / 255;
      const bh = Math.max(3 * dpr, v * h);
      ctx.globalAlpha = 0.35 + v * 0.65;
      ctx.beginPath(); ctx.roundRect(i * (bw + gap), (h - bh) / 2, bw, bh, 3 * dpr); ctx.fill();
    }
    ctx.globalAlpha = 1;
  };
  draw();

  // ---------- now playing ----------
  const setArt = (id) => { const [c1, c2] = colorsFor(id); $("art").style.setProperty("--c1", c1); $("art").style.setProperty("--c2", c2); };
  // Title style and subtitle: in a set they follow the episode settings, otherwise Minimal with the genre.
  const renderTitle = () => {
    const t = state.track, sc = state.scene, inSet = !!state.set && !!t;
    const style = inSet ? ((sc && sc.settings && sc.settings.title_style) || "minimal") : "minimal";
    $("np").className = `np style-${style}`;
    $("np-n").hidden = !inSet;
    if (inSet) $("np-n").textContent = String((state.set.kept || 0) + 1).padStart(2, "0");  // its place if kept
    let sub = "";
    if (t) {
      const src = inSet ? ((sc && sc.settings && sc.settings.subtitle) || "none") : "genre";
      sub = subtitleFor(t, src) || (inSet ? "" : subtitleFor(t, "mood"));
    }
    $("np-sub").textContent = sub; $("np-sub").hidden = !sub;
  };
  const renderNowPlaying = () => {
    const t = state.track, like = $("btn-like"), dl = $("btn-download"), skip = $("btn-skip");
    body.classList.toggle("has-track", !!t);
    for (const id of ["btn-keep", "btn-cut"]) $(id).setAttribute("aria-disabled", t ? "false" : "true");
    if (!t) {
      $("np-kicker").textContent = state.listening ? (state.buffering ? "Buffering" : "On air") : "Nothing playing";
      $("np-title").textContent = state.listening ? "Your first song is being composed…" : "Tune in to start the station";
      $("np-meta").innerHTML = ""; $("lyrics").textContent = "—";
      like.setAttribute("aria-disabled", "true"); dl.setAttribute("aria-disabled", "true");
      skip.setAttribute("aria-disabled", state.listening ? "false" : "true");
      setArt("idle"); renderTitle();
      document.title = "Infinite Radio";
      return;
    }
    $("np-kicker").textContent = t.kind === "request" ? "Your request" : (t.lyrics ? "Now playing" : "Instrumental");
    $("np-title").textContent = t.title;
    const tags = t.style.split(",").map((s) => s.trim()).filter(Boolean).slice(0, 5);
    $("np-meta").innerHTML = (t.theme ? `<div class="np-theme">Theme: ${esc(t.theme)}</div>` : "") +
      `<div>${esc(tags.join(" · "))}</div>` +
      `<div>${fmt(t.duration)} · seed ${t.seed}${t.rtf ? ` · rendered ${(1 / t.rtf).toFixed(1)}× realtime` : ""}</div>`;
    $("lyrics").textContent = t.lyrics || "(instrumental)";
    like.classList.toggle("liked", !!t.liked);
    like.setAttribute("aria-disabled", "false"); dl.setAttribute("aria-disabled", "false");
    dl.href = `/api/tracks/${t.id}/audio?download=1`;
    skip.setAttribute("aria-disabled", "false");
    setArt(t.id); renderTitle();
    document.title = `${t.title} · Infinite Radio`;
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
    renderNextChip(st);
  };
  const renderNextChip = (st) => {
    const ready = st.ready || [], cur = st.current;
    let text = "", frac = 0;
    if (ready.length) { text = `Next · ${ready[0].title}${ready.length > 1 ? ` +${ready.length - 1}` : ""}`; frac = 1; }
    else if (cur) {
      text = st.engine.state === "loading" ? "Loading the model" : `Composing next · ${cur.title}`;
      if (cur.eta_seconds != null) {
        frac = Math.min(0.97, cur.elapsed / Math.max(1, cur.elapsed + cur.eta_seconds));
        if (cur.eta_seconds > 1) text += ` · ~${Math.ceil(cur.eta_seconds)}s`;
      }
    } else if (st.on) text = "Waiting for the next song";
    $("next-chip").hidden = !text;
    $("next-text").textContent = text;
    $("next-chip").title = `${text} (Q)`;
    $("next-ring").setAttribute("stroke-dasharray", `${Math.round(frac * 100)} 100`);
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
    document.querySelectorAll("#presets .chip").forEach((c) => c.classList.toggle("active", c.dataset.style === s.style));
  };

  // ---------- DJ set ----------
  const renderSetBar = () => {
    const s = state.set, bar = $("setbar");
    $("btn-set-start").hidden = !!s || !!state.setup;
    $("btn-set-top").hidden = !!s || !!state.setup || !state.listening;
    body.classList.toggle("in-set", !!s);
    bar.hidden = !s;
    if (!s) return;
    $("setbar-name").textContent = s.name;
    $("setbar-kept").textContent = `${s.kept} kept`;
    $("setbar-cut").textContent = `${s.cut} cut`;
    $("setbar-review").textContent = s.review ? ` · ${s.review} to review` : "";
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
    $("btn-set-top").hidden = on || !!state.set || !state.listening;
    body.classList.toggle("setup-open", on);
    if (!on) return;
    const sc = state.scene;
    const file = sc && sc.kind !== "none" ? (sc.original_name || sc.file || "") : "";
    $("su-scene").textContent = !file ? "none yet (a mood-colored card is used)" : `${sc.kind === "image" ? "image" : "loop"} · ${shortName(file)}`;
    $("su-scene").title = file;
    $("su-scene-clear").hidden = !sc || sc.kind === "none";
    const ab = state.status?.ambience;
    $("su-amb").textContent = ab && ab.enabled ? `${ab.label} · ${ab.level_db} dB under the music` : "Off";
    const vibe = $("su-vibe");
    vibe.textContent = describeVibe(state.status?.settings) + (state.settingsDirty ? "  (unsaved edits in Station — save to use them)" : "");
    vibe.classList.toggle("unsaved", !!state.settingsDirty);
  };
  // Long file names (often random IDs) are cut in the middle so the extension stays visible.
  const shortName = (name, max = 44) => {
    if (name.length <= max) return name;
    const dot = name.lastIndexOf("."), ext = dot > 0 && name.length - dot <= 6 ? name.slice(dot) : "";
    const keep = max - ext.length - 1, head = Math.ceil(keep * 0.6);
    return `${name.slice(0, head)}…${name.slice(name.length - ext.length - (keep - head), name.length - ext.length)}${ext}`;
  };
  const openSetup = async () => {
    state.setup = true;
    $("su-name").value = `${(state.status?.settings?.name) || "Infinite Radio"} · ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
    try { if (!state.scene) state.scene = await api("/api/scene"); } catch {}
    if (!wideQuery.matches) closeOverlays();
    renderSetup(); renderStage(true); wake();
  };
  const closeSetup = () => { state.setup = false; renderSetup(); renderStage(true); };
  const startSet = openSetup;
  $("su-cancel").onclick = closeSetup;
  $("su-edit").onclick = () => {
    openDrawer("station");
    const panel = $("drawer-station");
    panel.classList.remove("flash"); void panel.offsetWidth; panel.classList.add("flash");
    setTimeout(() => $("f-style").focus({ preventScroll: true }), 340);
  };
  $("su-file").onchange = async (ev) => {
    const f = ev.target.files[0]; if (!f) return;
    try { state.scene = await uploadFile("/api/scene/upload", f); renderScene(); renderSetup(); renderStage(true); }
    catch (e) { toast(e.message); }
    ev.target.value = "";
  };
  $("su-scene-clear").onclick = async () => { state.scene = await api("/api/scene", { method: "DELETE" }); renderScene(); renderSetup(); renderStage(true); };
  $("su-start").onclick = async () => {
    const name = $("su-name").value.trim();
    const planned_minutes = Number($("su-length").value), play_through = $("su-through").value;
    try {
      if (state.settingsDirty) await saveSettings();
      await api("/api/sets/start", { method: "POST", body: { name, planned_minutes, play_through } });
      state.setup = false; renderSetup();
      await refresh();
      if (!state.listening) await tuneIn();
      toast(play_through === "keep" ? "Set started. Keep (K) or Cut (X) moves on; songs that play to the end are kept."
        : "Set started. Keep (K) or Cut (X) moves on; songs that play to the end wait for review.", true);
    } catch (e) { toast(e.message); }
  };
  const closeSet = async () => {
    try {
      const s = await api("/api/sets/close", { method: "POST" });
      state.set = null; renderSetBar(); renderNowPlaying();
      await refresh();
      openDrawer("side", "episodes");
      await openSetEditor(s.id);
      toast("Set closed. Review the tracklist, then render.", true);
    } catch (e) { toast(e.message); }
  };
  $("btn-set-start").onclick = startSet;
  $("btn-set-top").onclick = startSet;
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
    $("scene-live-row").hidden = sc.kind === "none";
    $("scene-live").checked = ui.backdrop !== "viz";
    const box = $("scene-settings");
    box.className = `scene-settings is-${sc.kind}`;
    box.querySelectorAll("[data-scene]").forEach((el) => {
      const k = el.dataset.scene, v = sc.settings[k];
      if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    });
    // Ping-pong reverses the clip in memory, so it is only offered for short clips.
    const pp = box.querySelector("[data-scene=pingpong]"), ppNote = $("pingpong-note");
    if (pp) {
      const ok = sc.pingpong_ok !== false;
      pp.disabled = !ok;
      if (!ok) pp.checked = false;
      if (ppNote) ppNote.textContent = ok ? "" : `only for clips up to ${Math.round(sc.pingpong_max_seconds || 15)} s; this one is ${fmt(sc.duration || 0)}`;
    }
    if (!sc.ffmpeg) toast("ffmpeg was not found. Run Install again from the Pinokio menu to render episodes.");
  };
  const loadScene = async () => { state.scene = await api("/api/scene"); renderScene(); };
  $("scene-file").onchange = async (ev) => {
    const f = ev.target.files[0]; if (!f) return;
    try { state.scene = await uploadFile("/api/scene/upload", f); renderScene(); renderStage(true); toast("Scene updated.", true); }
    catch (e) { toast(e.message); }
    ev.target.value = "";
  };
  $("scene-live").onchange = () => { ui.backdrop = $("scene-live").checked ? "scene" : "viz"; store.set("backdrop", ui.backdrop); renderStage(true); };
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
    const undecided = ed.tracks.filter((t) => t.undecided).length;
    $("ed-review").hidden = !undecided;
    $("ed-review-text").textContent = `${undecided} song${undecided === 1 ? "" : "s"} still need${undecided === 1 ? "s" : ""} a decision: keep (✓) or cut (✕).`;
    $("ed-list").innerHTML = ed.tracks.map((t, i) => `
      <li class="ed-row ${t.available === false ? "unavailable" : ""} ${t.undecided ? "undecided" : ""}" data-i="${i}">
        <span class="num">${String(i + 1).padStart(2, "0")}</span>
        <div><input value="${esc(t.display_title)}" data-act="rename" ${t.available === false ? "disabled" : ""}>
          ${subtitleSource() !== "none" && t.available !== false ? `<input class="ed-subtitle" data-act="subtitle" value="${esc(t.subtitleEdited ? (t.subtitle || "") : subtitleFor(t, subtitleSource()))}" placeholder="no subtitle">` : ""}
          <div class="sub">${fmt(t.duration || 0)}${t.genre ? " · " + esc(t.genre) : t.mood ? " · " + esc(t.mood) : ""}${t.available === false ? " · audio missing" : ""}${t.undecided ? `<span class="badge">Undecided</span>` : ""}</div></div>
        <div class="ops">${t.undecided ? `<button data-act="keep" class="keep-op" title="Keep">✓</button>` : ""}<button data-act="up" title="Move up">↑</button><button data-act="down" title="Move down">↓</button><button data-act="remove" title="${t.undecided ? "Cut" : "Remove"}">✕</button></div>
      </li>`).join("") || `<li class="hint">No songs left. Add some from the Library or discard.</li>`;
    const total = ed.tracks.reduce((a, t) => a + (t.duration || 0), 0) - Math.max(0, ed.tracks.length - 1) * Number($("ed-xf").value);
    $("ed-total").textContent = `${ed.tracks.length} song${ed.tracks.length === 1 ? "" : "s"} · about ${fmt(total)}`;
    $("ed-render").disabled = undecided > 0 || ed.tracks.filter((t) => t.available !== false).length === 0;
    $("ed-render").title = undecided ? "Keep or cut the undecided songs first" : "";
  };
  $("ed-list").onclick = (ev) => {
    const btn = ev.target.closest("button[data-act]"), row = ev.target.closest(".ed-row");
    if (!btn || !row || !state.editor) return;
    const i = Number(row.dataset.i), tr = state.editor.tracks;
    if (btn.dataset.act === "up" && i > 0) [tr[i - 1], tr[i]] = [tr[i], tr[i - 1]];
    if (btn.dataset.act === "down" && i < tr.length - 1) [tr[i + 1], tr[i]] = [tr[i], tr[i + 1]];
    if (btn.dataset.act === "remove") tr.splice(i, 1);
    if (btn.dataset.act === "keep") tr[i].undecided = false;
    renderEditor();
  };
  $("ed-keep-all").onclick = () => { if (!state.editor) return; state.editor.tracks.forEach((t) => { t.undecided = false; }); renderEditor(); };
  $("ed-cut-all").onclick = () => { if (!state.editor) return; state.editor.tracks = state.editor.tracks.filter((t) => !t.undecided); renderEditor(); };
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
    // Kept songs and the ones still awaiting a verdict, in the order they played.
    const kept = s.tracks.filter((e) => e.state === "kept" || e.state === "review" || e.state === "playing");
    state.editor = { kind: "set", setId, name: s.name, tracks: kept.map((e) => ({ track_id: e.track_id, display_title: e.display_title || e.title, duration: e.duration, style: e.style, available: e.available,
      undecided: e.state !== "kept",
      mood: e.mood, genre: e.genre, language: e.language, subtitle: e.subtitle, subtitleEdited: e.subtitle !== undefined && e.subtitle !== null })) };
    renderEditor();
    $("ep-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const openCustomEditor = (name, tracks) => {
    state.editor = { kind: "custom", setId: null, name, tracks };
    renderEditor();
    openDrawer("side", "episodes");
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
      const n = (st) => s.tracks.filter((e) => e.state === st).length, review = n("review") + n("playing");
      return `<li class="ep-item" data-id="${s.id}">
        <div class="ep-head"><span class="ep-name">${esc(s.name)}</span><span class="ep-status">${n("kept")} kept · ${n("cut")} cut${review ? ` · ${review} to review` : ""}</span></div>
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
      // Power button: goes off air while you listen; tunes back in after (the hero covers the no-song case).
      const live = st.on && state.listening, pw = $("btn-stop");
      pw.hidden = !st.on && !state.track;
      pw.classList.toggle("off", !live);
      pw.title = live ? "Go off air" : "Tune in"; pw.setAttribute("aria-label", pw.title);
      body.classList.toggle("on-air", !!st.on);
      $("hero-station").textContent = (st.settings && st.settings.name) || "Infinite Radio";
      $("hero-vibe").textContent = describeVibe(st.settings);
      $("open-episodes").classList.toggle("busy", !!st.rendering);
      const hadSet = !!state.set;
      state.set = st.set;
      if (st.set && state.setup) { state.setup = false; }
      renderSetBar(); renderSetup(); onAmbienceStatus(st.ambience);
      if (hadSet !== !!st.set) renderNowPlaying(); else if (st.set) renderTitle();  // the song number follows the kept count
      renderStage();
      applySettingsToForm(st.settings);
      renderQueue(st); renderEngine(st); renderRequests(st);
      if (st.errors && st.errors.length && st.errors[st.errors.length - 1] !== state.lastError) { state.lastError = st.errors[st.errors.length - 1]; toast(state.lastError); }
      if (state.listening && !state.track && st.ready.length && !state.pendingNext) playNext();
      if (state.listening && state.buffering) {
        const cur = st.current;
        $("buffering-text").textContent = cur ? (st.engine.state === "loading" ? "Loading the model for the first song…" : `Composing "${cur.title}"${cur.eta_seconds > 1 ? ` · ~${Math.ceil(cur.eta_seconds)}s` : ""}`) : (st.on ? "Waiting for the next song…" : "Station is off air");
      }
      if (state.activeTab === "episodes" && isOpen("side") && (st.rendering || state.episodes.some((j) => !["done", "error"].includes(j.status)))) loadEpisodes();
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
  // `ended` names the song that just played to the end, so the set can apply its
  // play-through verdict in the same request that picks the next song.
  const playNext = async (ended) => {
    if (state.pendingNext) return;
    state.pendingNext = true;
    try {
      const r = await api(ended ? `/api/next?ended=${encodeURIComponent(ended)}` : "/api/next");
      if (r.track) await playTrack(r.track); else { state.track = null; setBuffering(true); }
    } catch (e) { toast(e.message); } finally { state.pendingNext = false; }
  };
  // Keep or Cut the playing song, then move on. The toast offers the opposite
  // verdict, in case the wrong button was pressed.
  const decideAndNext = async (verdict) => {
    if (!state.listening || !state.set || !state.track || state.pendingNext) return;
    const t = state.track;
    try {
      const e = await api("/api/sets/verdict", { method: "POST", body: { track_id: t.id, verdict } });
      const title = e.display_title || e.title || t.title, other = verdict === "keep" ? "cut" : "keep";
      toast(verdict === "keep" ? `Kept "${title}".` : `Cut "${title}".`, true, {
        label: verdict === "keep" ? "Cut instead" : "Keep instead",
        run: async () => {
          try { await api("/api/sets/verdict", { method: "POST", body: { track_id: t.id, verdict: other } }); refresh(); toast(other === "keep" ? `Kept "${title}".` : `Cut "${title}".`, true); }
          catch (err) { toast(err.message); }
        },
      });
      if (state.set) { state.set[verdict === "keep" ? "kept" : "cut"] += 1; renderSetBar(); }
    } catch (e) { toast(e.message); return; }
    playNext();
  };
  const skip = () => {
    if (!state.listening) return;
    if (state.set && state.track) toast(`Left "${state.track.title}" for review.`, true);
    playNext();
  };

  audio.addEventListener("playing", () => ambSync());
  audio.addEventListener("waiting", () => ambSync());
  audio.addEventListener("ended", () => {
    setTimeout(ambSync, 0);
    if (state.listening) playNext(state.set && state.track ? state.track.id : undefined);
    else { state.track = null; renderNowPlaying(); }  // off air and the last song ended: back to the tune-in screen
  });
  audio.addEventListener("play", () => { $("art").classList.add("playing"); $("btn-play").classList.remove("paused"); wake(); });
  audio.addEventListener("pause", () => { $("art").classList.remove("playing"); $("btn-play").classList.add("paused"); wake(); });
  const FADE = 2.5;
  audio.addEventListener("timeupdate", () => {
    if (audio.duration) {
      const left = audio.duration - audio.currentTime;
      audio.volume = Math.min(1, Math.max(0.05, Math.min(left / FADE, (audio.currentTime + 0.15) / 0.6)));
    }
    $("t-cur").textContent = fmt(audio.currentTime); $("t-dur").textContent = fmt(audio.duration);
    if (audio.duration) {
      const pct = (audio.currentTime / audio.duration) * 100;
      $("seek").value = Math.floor(pct * 10); $("seek").style.setProperty("--p", `${pct}%`);
      $("hairline-fill").style.width = `${pct}%`;
    }
  });
  $("seek").addEventListener("input", () => { if (audio.duration) audio.currentTime = ($("seek").value / 1000) * audio.duration; });

  $("btn-play").onclick = async () => {
    ensureAudioGraph(); if (actx && actx.state === "suspended") await actx.resume();
    if (!state.track) { if (!state.listening) return tuneIn(); return playNext(); }
    if (audio.paused) { state.userPaused = false; audio.play(); } else { state.userPaused = true; audio.pause(); }
    ambSync();
  };
  $("btn-skip").onclick = skip;
  $("btn-keep").onclick = () => decideAndNext("keep");
  $("btn-cut").onclick = () => decideAndNext("cut");
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
  $("btn-stop").onclick = async () => {
    if ($("btn-stop").classList.contains("off")) return tuneIn();
    await api("/api/station/off", { method: "POST" }); state.listening = false; setBuffering(false); ambSync(); refresh();
  };

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
    document.querySelectorAll("#presets .chip").forEach((c) => c.onclick = () => { $("f-style").value = c.dataset.style; $("f-mood").value = c.dataset.mood; state.settingsDirty = true; saveSettings(); });
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
    renderShell();
  };
  document.querySelectorAll(".tab").forEach((tab) => tab.onclick = () => switchTab(tab.dataset.tab));
  $("engine-restart").onclick = async () => { try { await api("/api/engine/restart", { method: "POST" }); toast("audio.cpp restarted", true); refresh(); } catch (e) { toast(e.message); } };

  document.addEventListener("keydown", (e) => {
    wake();
    if (e.key === "Escape") {
      if (state.setup) closeSetup();
      else if (overlayOpen()) closeOverlays();
      else if (ui.lyrics) toggleLyrics(false);
      if (e.target.matches("input, textarea, select")) e.target.blur();
      return;
    }
    if (e.target.matches("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (e.code === "Space") { e.preventDefault(); $("btn-play").click(); }
    else if (state.set && (k === "k" || e.key === "ArrowRight")) $("btn-keep").click();
    else if (state.set && (k === "x" || e.key === "ArrowLeft")) $("btn-cut").click();
    else if (k === "n" || (!state.set && e.key === "ArrowRight")) skip();  // in a set, N moves on and leaves the song for review
    else if (k === "b") $("btn-like").click();
    else if (k === "s") toggleDrawer("station");
    else if (k === "q") toggleDrawer("side", "queue");
    else if (k === "e") toggleDrawer("side", "episodes");
    else if (k === "l") toggleLyrics();
    else if (k === "f") toggleFull();
    else if (k === "v") toggleBackdrop();
  });

  // ---------- stage: live preview of the episode look during a set ----------
  const fxCanvas = $("stage-fx"), fxCtx = fxCanvas.getContext("2d");
  let fxParticles = null, fxKind = "none", fxRunning = false, stageSceneKey = "";
  const initFx = (kind, density) => {
    const W = fxCanvas.width = fxCanvas.clientWidth || 760, H = fxCanvas.height = fxCanvas.clientHeight || 428;
    const counts = { rain: 480, snow: 220, leaves: 60, dust: 140 };  // episode renderer at 1920x1080
    const area = Math.min(1.5, Math.max(0.2, (W * H) / (1920 * 1080)));
    const n = Math.round((counts[kind] || 0) * (0.35 + density) * area);
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
    const stage = $("stage"), wrap = $("art-wrap"), sc = state.scene;
    if (!sc) {
      if (!state.sceneLoading) {
        state.sceneLoading = true;
        api("/api/scene").then((x) => { state.scene = x; renderScene(); renderStage(true); }).catch(() => {}).finally(() => { state.sceneLoading = false; });
      }
      return;
    }
    const show = sceneShown();
    body.classList.toggle("has-scene", show);
    wrap.hidden = show; stage.hidden = !show;
    $("stage-station").textContent = (state.status?.settings?.name) || "Infinite Radio";
    if (!show) { fxRunning = false; $("stage-video").pause(); stageSceneKey = ""; renderTitle(); return; }
    const key = `${sc.kind}:${sc.file}:${JSON.stringify(sc.settings)}`;
    if (force || key !== stageSceneKey) {
      stageSceneKey = key;
      const img = $("stage-img"), vid = $("stage-video"), st = sc.settings;
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
    renderTitle();
  };
  // Weather particles follow the window size (drawers pinning or unpinning change it too).
  new ResizeObserver(() => {
    const sc = state.scene;
    if (sceneShown() && sc.kind === "image") initFx(sc.settings.weather || "none", Number(sc.settings.weather_density ?? 0.6));
  }).observe($("main"));

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
    try { state.amb = await uploadFile("/api/ambience/upload", f); renderAmbience(); } catch (e) { toast(e.message); }
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
  $("su-amb-edit").onclick = () => { openDrawer("side", "episodes"); setTimeout(() => $("amb-section").scrollIntoView({ behavior: "smooth", block: "start" }), 120); };
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
  renderShell();
  loadPresets().then(refresh);
  setInterval(refresh, 1500);
  renderNowPlaying();
  renderStage();
})();
