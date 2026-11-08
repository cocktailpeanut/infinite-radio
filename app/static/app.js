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
      throw new Error(detail);
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
        ["Model", `${e.model || "—"} + ${e.vae || "—"}`], ["Loaded", e.model_loaded ? "yes" : "no (loads on first song)"], ["Restarts", String(e.restarts)], ["Songs made", String(st.stats.songs_generated)]];
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
    document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c.dataset.style === s.style));
  };

  // ---------- DJ set ----------
  const renderSetBar = () => {
    const s = state.set, bar = $("setbar");
    $("btn-set-start").hidden = !!s;
    bar.hidden = !s;
    if (!s) return;
    $("setbar-name").textContent = s.name;
    $("setbar-kept").textContent = `${s.kept} kept`;
    $("setbar-cut").textContent = `${s.cut} cut`;
    const el = Math.max(0, Date.now() / 1000 - s.started_at);
    $("setbar-clock").textContent = fmt(el).padStart(5, "0");
  };
  setInterval(() => { if (state.set) renderSetBar(); }, 1000);

  const scenePreviewHtml = (sc) => {
    if (!sc || sc.kind === "none") return `<span class="hint">No scene yet. A mood-colored title card is used.</span>`;
    if (sc.kind === "image") return `<img src="/api/scene/preview?${Date.now()}" alt="scene">`;
    return `<video src="/api/scene/preview?${Date.now()}" muted loop autoplay playsinline></video>`;
  };
  const openSetDialog = async () => {
    $("sd-name").value = `${(state.status?.settings?.name) || "Infinite Radio"} · ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
    $("set-dialog").hidden = false;
    try { if (!state.scene) state.scene = await api("/api/scene"); } catch {}
    $("sd-preview").innerHTML = scenePreviewHtml(state.scene);
    $("sd-name").focus();
  };
  const startSet = openSetDialog;
  $("sd-cancel").onclick = () => { $("set-dialog").hidden = true; };
  $("set-dialog").onclick = (ev) => { if (ev.target === $("set-dialog")) $("set-dialog").hidden = true; };
  $("sd-file").onchange = async (ev) => {
    const f = ev.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f, f.name);
    try { state.scene = await api("/api/scene/upload", { method: "POST", body: fd }); $("sd-preview").innerHTML = scenePreviewHtml(state.scene); renderScene(); renderStage(true); }
    catch (e) { toast(e.message); }
    ev.target.value = "";
  };
  $("sd-start").onclick = async () => {
    const name = $("sd-name").value.trim();
    try {
      await api("/api/sets/start", { method: "POST", body: { name } });
      $("set-dialog").hidden = true;
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
  const renderScene = () => {
    const sc = state.scene; if (!sc) return;
    const prev = $("scene-preview");
    if (sc.kind === "image") prev.innerHTML = `<img src="/api/scene/preview?${Date.now()}" alt="scene">`;
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
    try { state.scene = await api("/api/scene", { method: "PUT", body: { [el.dataset.scene]: v } }); renderScene(); renderStage(true); }
    catch (e) { toast(e.message); }
  });

  // ---------- tracklist editor ----------
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
          <div class="sub">${fmt(t.duration || 0)}${t.style ? " · " + esc(t.style.split(",").slice(1, 3).join(",").trim()) : ""}${t.available === false ? " · audio missing" : ""}</div></div>
        <div class="ops"><button data-act="up" title="Move up">↑</button><button data-act="down" title="Move down">↓</button><button data-act="remove" title="Remove">✕</button></div>
      </li>`).join("") || `<li class="hint">No songs left. Add some from the Library or discard.</li>`;
    const total = ed.tracks.reduce((a, t) => a + (t.duration || 0), 0) - Math.max(0, ed.tracks.length - 1) * Number($("ed-xf").value);
    $("ed-total").textContent = `${ed.tracks.length} songs · about ${fmt(total)}`;
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
    const inp = ev.target.closest("input[data-act=rename]"), row = ev.target.closest(".ed-row");
    if (inp && row && state.editor) state.editor.tracks[Number(row.dataset.i)].display_title = inp.value;
  });
  $("ed-name").oninput = () => { if (state.editor) state.editor.name = $("ed-name").value; };
  $("ed-xf").onchange = renderEditor;
  $("ed-cancel").onclick = () => { state.editor = null; renderEditor(); };

  const openSetEditor = async (setId) => {
    const s = await api(`/api/sets/${setId}`);
    const kept = s.tracks.filter((e) => e.state === "kept");
    state.editor = { kind: "set", setId, name: s.name, tracks: kept.map((e) => ({ track_id: e.track_id, display_title: e.display_title || e.title, duration: e.duration, style: e.style, available: e.available })) };
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
    const tracks = ed.tracks.filter((t) => t.available !== false).map((t) => ({ track_id: t.track_id, display_title: t.display_title }));
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
      renderSetBar();
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

  audio.addEventListener("ended", () => {
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
    if (audio.paused) audio.play(); else audio.pause();
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
      state.listening = true;
      await refresh(); await playNext();
    } catch (e) { toast(e.message); }
  };
  $("btn-tune").onclick = tuneIn;
  $("btn-stop").onclick = async () => { await api("/api/station/off", { method: "POST" }); state.listening = false; setBuffering(false); refresh(); };

  // ---------- settings ----------
  const formPatch = () => ({ style: $("f-style").value.trim(), mood: $("f-mood").value, lyric_mode: $("f-lyric-mode").value, themes: $("f-themes").value.trim(), target_seconds: Number($("f-len").value), lookahead: Number($("f-lookahead").value), planning: $("f-planning").checked });
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
  ["f-style", "f-mood", "f-lyric-mode", "f-themes", "f-len", "f-lookahead", "f-planning"].forEach((id) => {
    $(id).addEventListener("input", () => { state.settingsDirty = true; if (id === "f-len") $("f-len-label").textContent = fmt($("f-len").value); });
  });
  const loadPresets = async () => {
    const p = await api("/api/presets");
    state.presets = p.presets; state.moods = p.moods;
    $("f-mood").innerHTML = p.moods.map((m) => `<option value="${m}">${m[0].toUpperCase() + m.slice(1)}</option>`).join("");
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
    openCustomEditor(`${(state.status?.settings?.name) || "Infinite Radio"} · Selection`, chosen.map((t) => ({ track_id: t.id, display_title: t.title, duration: t.duration, style: t.style, available: true })));
    state.libSelectMode = false; state.libSelected.clear(); renderLibrary();
  };

  const switchTab = (name) => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    state.activeTab = name;
    ["queue", "library", "episodes", "engine"].forEach((n) => $(`tab-${n}`).hidden = n !== name);
    if (name === "library") renderLibrary();
    if (name === "engine" && state.status) renderEngine(state.status);
    if (name === "episodes") { loadScene(); loadEpisodes(); }
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
  const fxLoop = (ts = 0) => {
    if (!fxRunning) return;
    requestAnimationFrame(fxLoop);
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
    const inSet = !!state.set, stage = $("stage"), wrap = $("art-wrap");
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
      img.hidden = sc.kind !== "image"; vid.hidden = sc.kind !== "video";
      if (sc.kind === "image") {
        img.src = `/api/scene/preview?${Date.now()}`;
        img.className = st.camera ? (st.flicker ? "flicker" : "drift") : (st.flicker ? "flicker" : "");
        initFx(st.weather || "none", Number(st.weather_density ?? 0.6));
      } else {
        initFx("none", 0);
        if (sc.kind === "video") { vid.src = `/api/scene/preview?${Date.now()}`; vid.play().catch(() => {}); }
      }
      if (!fxRunning) { fxRunning = true; requestAnimationFrame(fxLoop); }
    }
    const t = state.track, card = $("stage-card");
    card.hidden = !t;
    if (t) {
      $("stage-num").textContent = String((state.set.kept || 0) + 0).padStart(2, "0");
      $("stage-title").textContent = t.title;
      $("stage-tags").textContent = t.style.split(",").slice(1, 5).map((x) => x.trim()).filter(Boolean).join(" · ");
    }
  };
  window.addEventListener("resize", () => { if (state.set && state.scene) renderStage(true); });

  // ---------- boot ----------
  loadPresets().then(refresh);
  setInterval(refresh, 1500);
  renderNowPlaying();
})();
