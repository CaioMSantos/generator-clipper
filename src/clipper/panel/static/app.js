"use strict";

// ---------------------------------------------------------------- config
const ICONS = {
  ready: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  approved: '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
  posted: '<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/>',
  rejected: '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>',
  rendering: '<path d="M21 12a9 9 0 1 1-6.2-8.56"/>',
  failed: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  all: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
};
const STATUSES = [
  { id: "ready", label: "Para revisar", color: "var(--warn)", notify: true },
  { id: "approved", label: "Aprovados", color: "var(--ok)" },
  { id: "posted", label: "Postados", color: "var(--info)" },
  { id: "rejected", label: "Rejeitados", color: "var(--bad)" },
  { id: "rendering", label: "Renderizando", color: "#b9dbff" },
  { id: "failed", label: "Falhas", color: "#ff8b8e", notify: true },
  { id: "", label: "Todos", color: "var(--muted)" },
];
const STATUS_LABEL = {
  ready: "revisar", approved: "aprovado", posted: "postado", rejected: "rejeitado",
  rendering: "renderizando", failed: "falhou", discovered: "na fila", skipped: "pulado",
};
const REFRESH_MS = 15000;
const JOBS_MS = 2500;

// ---------------------------------------------------------------- state
const state = {
  view: "clips", // clips | connections
  connections: [],
  streamers: [],
  status: "ready",
  streamer: "",
  q: "",
  clips: [],
  stats: null,
  jobs: [],
  openIndex: -1,
  hookMode: "text",
};

// ---------------------------------------------------------------- helpers
const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
};
const svg = (paths) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.innerHTML = paths; // static, trusted markup only
  return s;
};
const fmtNum = (n) => new Intl.NumberFormat("pt-BR", { notation: n >= 10000 ? "compact" : "standard" }).format(n);
const fmtDur = (s) => {
  if (!s && s !== 0) return "–";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`;
};
const ago = (iso) => {
  if (!iso) return "–";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "agora";
  if (diff < 3600) return `há ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `há ${Math.floor(diff / 3600)} h`;
  return `há ${Math.floor(diff / 86400)} d`;
};
const initials = (name) => (name || "?").trim().slice(0, 1).toUpperCase();

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", "X-Clipper-Panel": "1" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* keep statusText */ }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function toast(msg, kind = "") {
  const t = el("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 2600);
}

// ---------------------------------------------------------------- data
async function loadStats() {
  try {
    state.stats = await api("/api/stats");
    renderSidebar();
    renderStats();
  } catch (e) {
    $("#last-activity").textContent = "painel sem conexão";
    $("#pulse").className = "pulse";
    $("#live").className = "live";
    $("#live-text").textContent = "OFFLINE";
  }
}

async function loadClips({ keepOpen = true } = {}) {
  const params = new URLSearchParams();
  if (state.status) params.set("status", state.status);
  if (state.streamer) params.set("streamer", state.streamer);
  if (state.q) params.set("q", state.q);
  const openKey = keepOpen && state.openIndex >= 0 ? state.clips[state.openIndex]?.key : null;
  try {
    state.clips = await api(`/api/clips?${params}`);
  } catch (e) {
    toast(`Erro ao carregar: ${e.message}`, "bad");
    return;
  }
  renderGrid();
  if (openKey) {
    const idx = state.clips.findIndex((c) => c.key === openKey);
    if (idx >= 0) { state.openIndex = idx; renderDrawerMeta(); }
  }
}

async function refreshAll() {
  $("#refresh").classList.add("spin");
  await Promise.all([loadStats(), loadClips()]);
  setTimeout(() => $("#refresh").classList.remove("spin"), 500);
}

// ---------------------------------------------------------------- sidebar & stats
function renderSidebar() {
  const counts = state.stats?.by_status || {};
  const total = ["ready", "approved", "posted", "rejected", "rendering", "failed"].reduce((a, k) => a + (counts[k] || 0), 0);
  $("#status-nav").replaceChildren(...STATUSES.map((s) => {
    const n = s.id ? counts[s.id] || 0 : total;
    return el("button", {
      class: `rail-item ${state.view === "clips" && state.status === s.id ? "on" : ""}`,
      style: `--item-color:${s.color}`,
      "data-tip": `${s.label} · ${fmtNum(n)}`,
      "aria-label": s.label,
      onclick: () => { state.status = s.id; setView("clips"); closeDrawer(); closeMobileMenu(); loadClips({ keepOpen: false }); renderSidebar(); },
    },
      el("span", { class: "rail-ico" },
        svg(ICONS[s.id || "all"]),
        s.notify && n > 0 ? el("span", { class: "notif" }) : null),
      el("span", { class: "lbl", text: s.label }),
      el("span", { class: "n", text: fmtNum(n) }),
    );
  }));

  const streamers = state.stats?.streamers || [];
  const pending = streamers.reduce((a, s) => a + s.to_review, 0);
  $("#streamer-list").replaceChildren(
    el("button", {
      class: `rail-item ${state.streamer ? "" : "on"}`, "data-tip": "Todos os streamers", onclick: () => setStreamer(""),
    },
      el("span", { class: "rail-ico" }, el("span", { class: "avatar all", text: "∗" }), pending ? el("span", { class: "notif" }) : null),
      el("span", { class: "lbl", text: "Todos" }),
    ),
    ...streamers.map((s) => el("button", {
      class: `rail-item ${state.streamer === s.login ? "on" : ""}`,
      "data-tip": `${s.name} · ${s.platform}${s.to_review ? ` · ${s.to_review} p/ revisar` : ""}`,
      onclick: () => setStreamer(s.login),
    },
      el("span", { class: "rail-ico" },
        el("span", { class: `avatar ${s.platform}`, text: initials(s.name) }),
        s.to_review ? el("span", { class: "notif" }) : null),
      el("span", { class: "lbl" }, s.name, el("span", { class: "plat", text: s.platform })),
      s.to_review ? el("span", { class: "count-badge", text: String(s.to_review) }) : null,
    )),
  );

  const last = state.stats?.last_activity;
  const ageS = last ? (Date.now() - new Date(last).getTime()) / 1000 : Infinity;
  const live = ageS < 600;
  $("#pulse").className = `pulse ${live ? "live" : "idle"}`;
  $("#live").className = `live ${live ? "on" : last ? "idle" : ""}`;
  $("#live").querySelector(".pulse").className = `pulse ${live ? "live" : last ? "idle" : ""}`;
  $("#live-text").textContent = live ? "LIVE" : last ? "IDLE" : "OFFLINE";
  const activity = !last ? "sem atividade ainda" : ageS < 600 ? `loop ativo · ${ago(last)}` : `última atividade ${ago(last)}`;
  $("#last-activity").textContent = activity;
  $("#activity").dataset.tip = activity;
  $("#output-dir").textContent = state.stats?.output_dir || "";
}

// ---------------------------------------------------------------- sidebar collapse / mobile menu
const COLLAPSE_KEY = "clipper.sidebar.collapsed";
function setCollapsed(collapsed) {
  $(".app").classList.toggle("collapsed", collapsed);
  $("#collapse").dataset.tip = collapsed ? "Expandir menu" : "Recolher menu";
  try { localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0"); } catch { /* private mode */ }
}
function initCollapsed() {
  let saved = null;
  try { saved = localStorage.getItem(COLLAPSE_KEY); } catch { /* private mode */ }
  // Default: collapsed on laptop-sized screens, expanded on wide monitors.
  setCollapsed(saved === null ? window.innerWidth < 1400 : saved === "1");
}
function closeMobileMenu() { $(".app").classList.remove("mobile-open"); }
$("#collapse").addEventListener("click", () => setCollapsed(!$(".app").classList.contains("collapsed")));
$("#menu").addEventListener("click", () => $(".app").classList.add("mobile-open"));
$("#scrim").addEventListener("click", closeMobileMenu);
$("#show-help").addEventListener("click", () => { closeMobileMenu(); $("#help").classList.remove("hidden"); });
$("#copy-folder").addEventListener("click", async () => {
  const path = state.stats?.output_dir;
  if (!path) return;
  try { await navigator.clipboard.writeText(path); toast("caminho da pasta copiado", "ok"); }
  catch { toast(path); }
});

function setStreamer(login) {
  setView("clips");
  state.streamer = state.streamer === login ? "" : login;
  closeDrawer();
  closeMobileMenu();
  renderSidebar();
  loadClips({ keepOpen: false });
}

function renderStats() {
  const c = state.stats?.by_status || {};
  const cards = [
    { label: "revisar", value: c.ready || 0, color: "var(--warn)", sub: "status=ready" },
    { label: "aprovados", value: c.approved || 0, color: "var(--ok)", sub: "status=approved" },
    { label: "postados", value: c.posted || 0, color: "var(--info)", sub: "status=posted" },
    { label: "fila do loop", value: c.discovered || 0, color: "var(--accent)", sub: "status=discovered" },
  ];
  $("#stats").replaceChildren(...cards.map((s) => el("div", { class: "stat", style: `--c:${s.color}` },
    el("div", { class: "stat-label", text: s.label }),
    el("div", { class: "stat-value", text: fmtNum(s.value) }),
    el("div", { class: "stat-sub", text: s.sub }),
  )));
}

// ---------------------------------------------------------------- grid
const lazyVideos = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting && e.target.dataset.src) {
      e.target.src = e.target.dataset.src;
      delete e.target.dataset.src;
      lazyVideos.unobserve(e.target);
    }
  }
}, { rootMargin: "300px" });

function renderGrid() {
  const grid = $("#grid");
  const label = STATUSES.find((s) => s.id === state.status)?.label || "Todos";
  $("#view-title").textContent = label;
  $("#view-count").textContent = fmtNum(state.clips.length);
  $("#empty").classList.toggle("hidden", state.clips.length > 0);

  const busy = new Set(state.jobs.filter((j) => j.state === "queued" || j.state === "running").map((j) => j.key));
  grid.replaceChildren(...state.clips.map((clip, i) => clipCard(clip, i, busy.has(clip.key))));
}

function clipCard(clip, index, busy) {
  let media;
  if (clip.has_video) {
    media = el("video", { muted: true, playsinline: true, loop: true, preload: "metadata" });
    media.muted = true;
    media.dataset.src = `${clip.video_url}#t=0.8`;
    lazyVideos.observe(media);
  } else {
    media = el("div", { class: "no-video", text: clip.status === "failed" ? "Falhou ao renderizar" : "Vídeo não está na pasta" });
  }

  const card = el("article", {
    class: `clip ${index === state.openIndex ? "selected" : ""}`,
    tabindex: "0",
    onclick: () => openDrawer(index),
    onkeydown: (e) => { if (e.key === "Enter") openDrawer(index); },
    onmouseenter: () => { if (media.tagName === "VIDEO" && media.src) media.play().catch(() => {}); },
    onmouseleave: () => { if (media.tagName === "VIDEO") { media.pause(); media.currentTime = 0.8; } },
  },
    media,
    el("div", { class: "shade" }),
    el("div", { class: "top" },
      el("span", { class: `chip ${clip.platform}`, text: clip.platform === "kick" ? "KICK" : "TWITCH" }),
      el("span", { class: `status-pill s-${clip.status}`, text: STATUS_LABEL[clip.status] || clip.status }),
    ),
    el("div", { class: "quick" },
      el("button", { class: "ok", title: "Aprovar", onclick: (e) => { e.stopPropagation(); setStatus(clip, "approved"); } },
        svg('<path d="M5 12l5 5L20 7"/>')),
      el("button", { class: "bad", title: "Rejeitar", onclick: (e) => { e.stopPropagation(); setStatus(clip, "rejected"); } },
        svg('<path d="M6 6l12 12M18 6L6 18"/>')),
    ),
    el("div", { class: "bottom" },
      clip.hook ? el("div", { class: "hook", text: clip.hook }) : null,
      el("div", { class: "title", text: clip.title || "(sem título)" }),
      el("div", { class: "meta" },
        el("span", { class: `avatar ${clip.platform}`, text: initials(clip.streamer_name) }),
        el("span", { class: "who", text: clip.streamer_name }),
      ),
      el("div", { class: "readout" },
        "views=", el("b", { text: fmtNum(clip.views) }),
        " dur=", el("b", { text: fmtDur(clip.duration_s) }),
        " score=", el("b", { text: clip.score.toFixed(1) }),
      ),
    ),
    busy || clip.status === "rendering"
      ? el("div", { class: "progress-ring" }, el("span", { class: "spinner" }), "Renderizando…")
      : null,
  );
  return card;
}

// ---------------------------------------------------------------- status
async function setStatus(clip, status, { advance = false } = {}) {
  if (!clip) return;
  try {
    const updated = await api(`/api/clips/${encodeURIComponent(clip.key)}/status`, { method: "POST", body: { status } });
    const msg = { approved: "aprovado ✓", rejected: "rejeitado ✕", posted: "marcado como postado", ready: "de volta para revisão" }[status];
    toast(msg, status === "rejected" ? "bad" : "ok");
    const idx = state.clips.findIndex((c) => c.key === clip.key);
    const leavesView = state.status && state.status !== status;
    if (idx >= 0) {
      if (leavesView) state.clips.splice(idx, 1);
      else state.clips[idx] = updated;
    }
    if (state.openIndex >= 0) {
      if (leavesView) {
        if (state.clips.length === 0) closeDrawer();
        else openDrawer(Math.min(idx, state.clips.length - 1));
      } else if (advance && state.openIndex < state.clips.length - 1) {
        openDrawer(state.openIndex + 1);
      } else {
        renderDrawerMeta();
      }
    }
    renderGrid();
    loadStats();
  } catch (e) {
    toast(`Não deu: ${e.message}`, "bad");
  }
}

// ---------------------------------------------------------------- drawer
const player = $("#player");

function current() { return state.clips[state.openIndex]; }

function openDrawer(index) {
  if (index < 0 || index >= state.clips.length) return;
  const changed = index !== state.openIndex || !$("#drawer").classList.contains("open");
  state.openIndex = index;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#overlay").classList.remove("hidden");
  if (changed) loadPlayer();
  renderDrawerMeta();
  resetEditor();
  document.querySelectorAll(".clip").forEach((n, i) => n.classList.toggle("selected", i === index));
  document.querySelectorAll(".clip")[index]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function closeDrawer() {
  state.openIndex = -1;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#overlay").classList.add("hidden");
  player.pause();
  document.querySelectorAll(".clip.selected").forEach((n) => n.classList.remove("selected"));
}

function loadPlayer() {
  const clip = current();
  if (!clip) return;
  $("#player-missing").classList.toggle("hidden", clip.has_video);
  if (clip.has_video) {
    if (player.getAttribute("src") !== clip.video_url) {
      player.src = clip.video_url;
      player.play().catch(() => {});
    }
  } else {
    player.removeAttribute("src");
    player.load();
  }
}

function renderDrawerMeta() {
  const clip = current();
  if (!clip) return;
  $("#drawer-pos").textContent = `${state.openIndex + 1} / ${state.clips.length}`;
  const av = $("#d-avatar");
  av.className = `avatar ${clip.platform}`;
  av.textContent = initials(clip.streamer_name);
  $("#d-streamer").textContent = clip.streamer_name;
  $("#d-sub").textContent = `${clip.platform === "kick" ? "Kick" : "Twitch"} · criado ${ago(clip.created_at)}`;
  const st = $("#d-status");
  st.className = `status-pill s-${clip.status}`;
  st.textContent = STATUS_LABEL[clip.status] || clip.status;
  $("#d-title").textContent = clip.hook || clip.title || "(sem título)";
  $("#d-metrics").replaceChildren(...[
    metric("views", fmtNum(clip.views)),
    metric("dur", fmtDur(clip.duration_s)),
    metric("score", clip.score.toFixed(1)),
    clip.cluster_size > 1 ? metric("cluster", `×${clip.cluster_size}`) : null,
    metric("updated", ago(clip.updated_at)),
  ].filter(Boolean));
  $("#d-caption").value = clip.suggested_caption || "";
  $("#d-transcript").textContent = clip.transcript || "Sem transcrição.";
  const dl = $("#d-download");
  dl.href = clip.has_video ? `${clip.video_url.split("?")[0]}?download=true` : "#";
  dl.classList.toggle("hidden", !clip.has_video);
  $("#d-source").href = clip.source_url;
  const err = $("#d-error");
  err.textContent = clip.error || "";
  err.classList.toggle("hidden", !clip.error || clip.status !== "failed");
  document.querySelectorAll(".actions .btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.status !== "ready" && b.dataset.status === clip.status));
  const busy = state.jobs.some((j) => j.key === clip.key && (j.state === "queued" || j.state === "running"));
  $("#rerender").disabled = busy || clip.status === "rendering";
  $("#rerender").textContent = busy ? "renderizando…" : "▶ Re-renderizar";
}

function metric(key, value) {
  return el("span", { class: "metric" }, `${key}=`, el("b", { text: value }));
}

// ---------------------------------------------------------------- editor (hook + trim)
function sourceDuration() {
  const clip = current();
  if (clip?.duration_s) return clip.duration_s;
  return (player.duration || 0) + (clip?.trim_start || 0);
}

function resetEditor() {
  const clip = current();
  $("#hook").value = clip?.hook || "";
  setHookMode(clip?.hook ? "text" : "ai");
  $("#trim-start").value = "";
  $("#trim-end").value = "";
  $("#captions").checked = true;
  fillTemplateSelect($("#rr-template"), clip?.template || "", "— do streamer —");
  updateTrimBar();
}

function setHookMode(mode) {
  state.hookMode = mode;
  document.querySelectorAll("#hook-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  $("#hook").disabled = mode !== "text";
}

function trimValues() {
  const total = sourceDuration();
  const s = parseFloat($("#trim-start").value);
  const e = parseFloat($("#trim-end").value);
  const has = !Number.isNaN(s) || !Number.isNaN(e);
  return { has, start: Number.isNaN(s) ? 0 : s, end: Number.isNaN(e) ? total : e, total };
}

function updateTrimBar() {
  const { has, start, end, total } = trimValues();
  const sel = $("#trim-sel");
  if (!total) { sel.style.left = "0"; sel.style.width = "100%"; $("#trim-len").textContent = ""; return; }
  sel.style.left = `${(start / total) * 100}%`;
  sel.style.width = `${Math.max(((end - start) / total) * 100, 0)}%`;
  $("#trim-len").textContent = has ? `${fmtDur(start)} → ${fmtDur(end)}  (${(end - start).toFixed(1)}s)` : `inteiro · ${fmtDur(total)}`;
  updatePlayhead();
}

function updatePlayhead() {
  const total = sourceDuration();
  const t = (player.currentTime || 0) + (current()?.trim_start || 0);
  $("#trim-playhead").style.left = total ? `${Math.min(t / total, 1) * 100}%` : "0";
}

function markTrim(which) {
  if (!current()) return;
  const t = ((player.currentTime || 0) + (current().trim_start || 0)).toFixed(1);
  $(which === "start" ? "#trim-start" : "#trim-end").value = t;
  updateTrimBar();
  toast(`${which === "start" ? "Início" : "Fim"} em ${t}s`);
}

async function rerender() {
  const clip = current();
  if (!clip) return;
  const body = { captions: $("#captions").checked };
  if ($("#rr-template").value) body.template = $("#rr-template").value;
  if (state.hookMode === "text") body.hook = $("#hook").value.trim();
  else if (state.hookMode === "none") body.hook = "";
  // "ai": omit hook -> backend asks the model
  const { has, start, end } = trimValues();
  if (has) {
    if (end - start < 1) { toast("O corte precisa ter pelo menos 1s", "bad"); return; }
    body.start = Math.max(start, 0);
    body.end = end;
  }
  try {
    const job = await api(`/api/clips/${encodeURIComponent(clip.key)}/rerender`, { method: "POST", body });
    state.jobs = [job, ...state.jobs.filter((j) => j.id !== job.id)];
    toast("re-render enfileirado", "ok");
    renderJobs();
    renderDrawerMeta();
    renderGrid();
    pollJobs();
  } catch (e) {
    toast(`Não deu: ${e.message}`, "bad");
  }
}

// ---------------------------------------------------------------- jobs
let jobsTimer = null;
const seenDone = new Set();

async function pollJobs() {
  clearTimeout(jobsTimer);
  try {
    state.jobs = await api("/api/jobs");
  } catch { /* panel restarting; try again */ }
  let finished = false;
  for (const j of state.jobs) {
    if ((j.state === "done" || j.state === "failed") && !seenDone.has(j.id)) {
      seenDone.add(j.id);
      if (j.finished_at && Date.now() - new Date(j.finished_at).getTime() < 60000) {
        finished = true;
        toast(j.state === "done" ? "re-render pronto ✓" : `re-render falhou: ${j.error || ""}`, j.state === "done" ? "ok" : "bad");
      }
    }
  }
  renderJobs();
  if (finished) {
    await loadClips();
    const clip = current();
    if (clip) { player.removeAttribute("src"); loadPlayer(); renderDrawerMeta(); }
    loadStats();
  }
  const active = state.jobs.some((j) => j.state === "queued" || j.state === "running");
  jobsTimer = setTimeout(pollJobs, active ? JOBS_MS : REFRESH_MS);
}

function renderJobs() {
  const active = state.jobs.filter((j) => j.state === "queued" || j.state === "running");
  $("#jobs-pill").classList.toggle("hidden", active.length === 0);
  $("#jobs-text").textContent = `${active.length} renderizando`;
}

// ---------------------------------------------------------------- events
$("#overlay").addEventListener("click", closeDrawer);
$("#close").addEventListener("click", closeDrawer);
$("#prev").addEventListener("click", () => openDrawer(state.openIndex - 1));
$("#next").addEventListener("click", () => openDrawer(state.openIndex + 1));
$("#refresh").addEventListener("click", refreshAll);
$("#rerender").addEventListener("click", rerender);
$("#set-start").addEventListener("click", () => markTrim("start"));
$("#set-end").addEventListener("click", () => markTrim("end"));
$("#trim-reset").addEventListener("click", () => { $("#trim-start").value = ""; $("#trim-end").value = ""; updateTrimBar(); });
$("#trim-start").addEventListener("input", updateTrimBar);
$("#trim-end").addEventListener("input", updateTrimBar);
$("#trim-bar").addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  const t = ((e.clientX - r.left) / r.width) * sourceDuration() - (current()?.trim_start || 0);
  if (t >= 0 && player.duration) player.currentTime = Math.min(t, player.duration);
});
player.addEventListener("timeupdate", updatePlayhead);
player.addEventListener("loadedmetadata", updateTrimBar);
document.querySelectorAll("#hook-mode button").forEach((b) => b.addEventListener("click", () => setHookMode(b.dataset.mode)));
document.querySelectorAll(".actions .btn").forEach((b) => b.addEventListener("click", () => setStatus(current(), b.dataset.status)));
$("#copy-caption").addEventListener("click", copyCaption);

async function copyCaption() {
  const text = $("#d-caption").value;
  if (!text) { toast("Sem legenda para copiar", "bad"); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast("legenda copiada", "ok");
  } catch {
    $("#d-caption").select();
    document.execCommand("copy");
    toast("legenda copiada", "ok");
  }
}

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); loadClips({ keepOpen: false }); }, 250);
});

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (!$("#calib").classList.contains("hidden") || !$("#tpl-editor").classList.contains("hidden")) { calibKey(e); return; }
  if (e.key === "Escape") {
    if (!$("#help").classList.contains("hidden")) { $("#help").classList.add("hidden"); return; }
    if (typing) { document.activeElement.blur(); return; }
    closeDrawer();
    return;
  }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  const open = state.openIndex >= 0;
  const key = e.key.toLowerCase();
  if (key === "/") { e.preventDefault(); $("#search").focus(); return; }
  if (key === "?") { $("#help").classList.toggle("hidden"); return; }
  if (key === "[") { setCollapsed(!$(".app").classList.contains("collapsed")); return; }
  if (!open) {
    if ((key === "enter" || key === "arrowright") && state.clips.length) { e.preventDefault(); openDrawer(0); }
    return;
  }
  const clip = current();
  switch (key) {
    case "arrowright": case "j": e.preventDefault(); openDrawer(state.openIndex + 1); break;
    case "arrowleft": case "k": e.preventDefault(); openDrawer(state.openIndex - 1); break;
    case "a": setStatus(clip, "approved", { advance: true }); break;
    case "x": setStatus(clip, "rejected", { advance: true }); break;
    case "p": setStatus(clip, "posted", { advance: true }); break;
    case "u": setStatus(clip, "ready"); break;
    case "c": copyCaption(); break;
    case "i": markTrim("start"); break;
    case "o": markTrim("end"); break;
    case " ": e.preventDefault(); player.paused ? player.play() : player.pause(); break;
    default: break;
  }
});
$("#help").addEventListener("click", () => $("#help").classList.add("hidden"));

// ---------------------------------------------------------------- boot
initCollapsed();
refreshAll();
pollJobs();
setInterval(() => {
  loadStats();
  if (state.openIndex < 0) loadClips(); // don't reshuffle the grid under the reviewer
}, REFRESH_MS);


// ---------------------------------------------------------------- connections view
const LOGOS = { twitch: "tw", kick: "K", anthropic: "AI" };

function setView(view) {
  if (state.view === view) return;
  state.view = view;
  const tpl = view === "templates";
  const conn = view === "connections" || tpl;
  $("#connections-view").classList.toggle("hidden", view !== "connections");
  $("#templates-view").classList.toggle("hidden", !tpl);
  $("#nav-templates").classList.toggle("on", tpl);
  for (const id of ["#stats", "#grid", "#empty"]) $(id).classList.toggle("hidden", conn);
  if (!conn) $("#empty").classList.toggle("hidden", state.clips.length > 0);
  $(".search").classList.toggle("hidden", conn);
  $("#view-count").classList.toggle("hidden", conn);
  $("#nav-connections").classList.toggle("on", view === "connections");
  $(".crumb").textContent = tpl ? "~/moldes/" : conn ? "~/config/" : "~/cortes/";
  if (tpl) {
    $("#view-title").textContent = "Moldes";
    closeDrawer();
    loadTemplates();
  } else if (conn) {
    $("#view-title").textContent = "Conexões";
    closeDrawer();
    loadConnections();
    loadStreamers();
  } else {
    renderGrid();
  }
  renderSidebar();
}

async function loadConnections() {
  try {
    state.connections = await api("/api/connections");
  } catch (e) { toast(`conexões: ${e.message}`, "bad"); return; }
  renderConnections();
}

function connState(c) {
  if (c.last_test) return c.last_test.ok ? ["ok", "conectado"] : ["err", "erro"];
  if (c.fields.length && !c.configured) return [c.required ? "pending" : "", c.required ? "não configurado" : "opcional"];
  return ["", "não testado"];
}

function renderConnections() {
  const cards = state.connections.map((c) => {
    const [cls, label] = connState(c);
    const inputs = {};
    const fields = c.fields.map((f) => {
      const input = el("input", {
        type: f.secret ? "password" : "text",
        placeholder: f.is_set ? (f.preview || "definido") : "não definido",
        value: f.secret ? "" : (f.preview || ""),
        autocomplete: "off", spellcheck: "false",
      });
      inputs[f.key] = input;
      return el("label", { class: "conn-field" }, f.label, input);
    });
    const result = c.last_test
      ? el("div", { class: `conn-result ${c.last_test.ok ? "ok" : "err"}`, text: `${c.last_test.message}  (${c.last_test.latency_ms}ms)` })
      : null;

    const save = async () => {
      const values = {};
      for (const f of c.fields) {
        const v = inputs[f.key].value.trim();
        if (f.secret ? v : v !== (f.preview || "")) values[f.key] = v;
      }
      if (!Object.keys(values).length) { toast("nada para salvar"); return; }
      try {
        await api(`/api/connections/${c.id}`, { method: "PUT", body: { values } });
        toast(`${c.name}: salvo no .env`, "ok");
        await test();
      } catch (e) { toast(`${c.name}: ${e.message}`, "bad"); }
    };
    const test = async () => {
      btnTest.disabled = true; btnTest.textContent = "testando…";
      try {
        const r = await api(`/api/connections/${c.id}/test`, { method: "POST" });
        toast(`${c.name}: ${r.ok ? "ok" : "falhou"}`, r.ok ? "ok" : "bad");
      } catch (e) { toast(`${c.name}: ${e.message}`, "bad"); }
      await loadConnections();
    };
    const clear = async () => {
      if (!confirm(`Remover as credenciais de ${c.name} do .env?`)) return;
      const values = Object.fromEntries(c.fields.map((f) => [f.key, null]));
      try { await api(`/api/connections/${c.id}`, { method: "PUT", body: { values } }); toast(`${c.name}: removido`); }
      catch (e) { toast(e.message, "bad"); }
      loadConnections();
    };
    const btnTest = el("button", { class: "btn btn-ghost", type: "button", onclick: test }, "Testar");
    return el("div", { class: `conn-card ${cls}` },
      el("div", { class: "conn-head" },
        el("div", { class: `conn-logo ${c.id}`, text: LOGOS[c.id] || "?" }),
        el("div", { class: "conn-title" },
          el("div", { class: "conn-name", text: c.name }),
          el("div", { class: "conn-desc", text: c.description })),
        el("span", { class: "conn-state", text: label })),
      fields.length ? el("div", { class: "conn-fields" }, fields) : null,
      result,
      el("div", { class: "conn-actions" },
        fields.length ? el("button", { class: "btn btn-primary", type: "button", onclick: save }, "Salvar") : null,
        btnTest,
        c.fields.some((f) => f.is_set) ? el("button", { class: "btn btn-ghost", type: "button", onclick: clear }, "Limpar") : null,
        el("span", { class: "spacer" }),
        el("a", { href: c.docs_url, target: "_blank", rel: "noopener", text: "docs ↗" })),
    );
  });
  cards.push(el("div", { class: "conn-card dashed" },
    el("div", { class: "conn-head" },
      el("div", { class: "conn-logo other", text: "+" }),
      el("div", { class: "conn-title" },
        el("div", { class: "conn-name", text: "Outra plataforma" }),
        el("div", { class: "conn-desc" },
          "YouTube, TikTok Live… implemente ", el("code", { text: "ClipSource" }), " em ",
          el("code", { text: "src/clipper/sources/" }), " e registre aqui.")))));
  $("#conn-grid").replaceChildren(...cards);

  const problems = state.connections.filter((c) => (c.last_test && !c.last_test.ok) || (c.required && c.fields.length && !c.configured)).length;
  $("#conn-notif").classList.toggle("hidden", problems === 0);
  $("#conn-count").textContent = `${state.connections.filter((c) => c.last_test?.ok).length}/${state.connections.length}`;
}

async function loadStreamers() {
  try { state.streamers = await api("/api/streamers"); }
  catch (e) { toast(`streamers: ${e.message}`, "bad"); return; }
  renderStreamers();
}

async function streamerCall(method, path, body, okMsg) {
  try {
    state.streamers = await api(path, { method, body });
    if (okMsg) toast(okMsg, "ok");
    renderStreamers();
    loadStats();
    return true;
  } catch (e) { toast(e.message, "bad"); return false; }
}

function renderStreamers() {
  const head = el("div", { class: "st-row head" },
    el("span"), el("span", { text: "nome" }), el("span", { text: "plataforma" }), el("span", { class: "col-login", text: "login" }),
    el("span", { text: "min views" }), el("span", { class: "col-layout", text: "layout" }), el("span", { text: "molde" }),
    el("span", { text: "ativo" }), el("span"), el("span"));
  const rows = state.streamers.map((s) => {
    const base = `/api/streamers/${s.platform}/${encodeURIComponent(s.login)}`;
    const min = el("input", { type: "number", min: "0", placeholder: "global", value: s.min_views ?? "" });
    min.addEventListener("change", () => {
      const v = min.value.trim();
      streamerCall("PATCH", base, v === "" ? { clear_min_views: true } : { min_views: Number(v) }, `${s.name}: min views ${v || "global"}`);
    });
    const toggle = el("label", { class: "toggle" }, el("input", { type: "checkbox", checked: s.enabled }), el("span", { class: "toggle-ui" }));
    toggle.querySelector("input").addEventListener("change", (e) =>
      streamerCall("PATCH", base, { enabled: e.target.checked }, `${s.name} ${e.target.checked ? "ativado" : "pausado"}`));
    return el("div", { class: `st-row ${s.enabled ? "" : "off"}` },
      el("span", { class: `avatar ${s.platform}`, text: initials(s.name) }),
      el("span", { class: "name", text: s.name }),
      el("span", { class: `chip ${s.platform}`, text: s.platform.toUpperCase() }),
      el("span", { class: "muted col-login", text: s.login }),
      min,
      el("span", { class: "layout-tag col-layout" }, el("b", { text: s.layout }),
        s.layout === "stack" ? ` · ${s.facecam_positions} pos` : ""),
      templateSelect(s, base),
      toggle,
      el("button", { class: "st-cal", title: "Calibrar câmera", type: "button", onclick: () => openCalib(s) },
        svg('<circle cx="12" cy="12" r="7"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/><circle cx="12" cy="12" r="1.5"/>')),
      el("button", {
        class: "st-del", title: "Remover", type: "button",
        onclick: () => { if (confirm(`Remover ${s.name} (${s.platform}) do monitoramento?`)) streamerCall("DELETE", base, undefined, `${s.name} removido`); },
      }, svg('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>')),
    );
  });
  $("#streamer-table").replaceChildren(head, ...rows);
}

$("#nav-connections").addEventListener("click", () => { closeMobileMenu(); setView(state.view === "connections" ? "clips" : "connections"); });
$("#add-streamer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    platform: $("#as-platform").value,
    login: $("#as-login").value.trim(),
    name: $("#as-name").value.trim(),
    min_views: $("#as-min").value === "" ? null : Number($("#as-min").value),
  };
  let ok = await streamerCall("POST", "/api/streamers", body, `${body.name} adicionado`);
  if (!ok && confirm("Não deu para confirmar que o canal existe. Adicionar mesmo assim?")) {
    ok = await streamerCall("POST", "/api/streamers", { ...body, verify: false }, `${body.name} adicionado (sem verificação)`);
  }
  if (ok) e.target.reset();
});
loadConnections();


// ---------------------------------------------------------------- calibration editor
const MIN_SIZE = 0.03;
const calib = { s: null, layout: null, sel: null, clips: [], previewCam: 0, dirty: false };

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const round4 = (v) => Math.round(v * 10000) / 10000;
const rectOf = (sel) => (sel === "game" ? calib.layout.gameplay : calib.layout.cams[sel]);

async function openCalib(s) {
  calib.s = s;
  calib.sel = null;
  calib.dirty = false;
  $("#cb-name").textContent = s.name;
  $("#cb-sub").textContent = `${s.platform} · ${s.login}`;
  const av = $("#cb-avatar"); av.className = `avatar ${s.platform}`; av.textContent = initials(s.name);
  $("#cb-detect-out").classList.add("hidden");
  $("#calib").classList.remove("hidden");
  const base = `/api/streamers/${s.platform}/${encodeURIComponent(s.login)}`;
  try {
    const [layout, clips] = await Promise.all([api(`${base}/layout`), api(`${base}/clips`)]);
    calib.layout = {
      mode: layout.mode,
      cams: layout.mode === "stack" ? [layout.facecam, ...layout.facecam_alternates] : [],
      gameplay: layout.mode === "stack" && layout.gameplay && !(layout.gameplay.x === 0 && layout.gameplay.y === 0 && layout.gameplay.w === 1 && layout.gameplay.h === 1) ? layout.gameplay : null,
      ratio: layout.facecam_ratio ?? 0.3,
      zoom: layout.zoom ?? 1,
    };
    calib.clips = clips;
  } catch (e) { toast(`calibrar: ${e.message}`, "bad"); closeCalib(true); return; }
  $("#cb-clip").replaceChildren(...(calib.clips.length
    ? calib.clips.map((c) => el("option", { value: c.key }, `${c.downloaded ? "●" : "○"} ${c.title.slice(0, 60)} · ${fmtDur(c.duration_s)}`))
    : [el("option", { value: "" }, "nenhum clip deste streamer ainda — rode o loop primeiro")]));
  $("#cb-ratio").value = calib.layout.ratio;
  calib.previewCam = 0;
  syncMode();
  loadCalibFrame();
}

function closeCalib(force = false) {
  if (!force && calib.dirty && !confirm("Descartar as alterações deste layout?")) return;
  $("#calib").classList.add("hidden");
  calib.s = null;
}

function loadCalibFrame() {
  const key = $("#cb-clip").value;
  const img = $("#cb-img");
  if (!key) { img.removeAttribute("src"); renderRects(); drawPreview(); return; }
  const clip = calib.clips.find((c) => c.key === key);
  const at = Math.min(parseFloat($("#cb-at").value), Math.max((clip?.duration_s || 60) - 0.5, 0));
  $("#cb-at").max = String(Math.max(Math.floor(clip?.duration_s || 60), 1));
  $("#cb-at-val").textContent = `${at.toFixed(1)}s`;
  $("#cb-loading").classList.remove("hidden");
  const base = `/api/streamers/${calib.s.platform}/${encodeURIComponent(calib.s.login)}`;
  img.onload = () => { $("#cb-loading").classList.add("hidden"); renderRects(); drawPreview(); if (clip) clip.downloaded = true; };
  img.onerror = async () => {
    $("#cb-loading").classList.add("hidden");
    try { await api(`${base}/frame?clip=${encodeURIComponent(key)}&at=${at}`); } catch (e) { toast(`frame: ${e.message}`, "bad"); }
  };
  img.src = `${base}/frame?clip=${encodeURIComponent(key)}&at=${at}`;
}

function syncMode() {
  const stack = calib.layout.mode === "stack";
  document.querySelectorAll("#cb-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === calib.layout.mode));
  for (const id of ["#cb-add-cam", "#cb-toggle-game", "#cb-remove", "#cb-detect", "#cb-ratio-row"]) $(id).classList.toggle("hidden", !stack);
  if (stack && calib.layout.cams.length === 0) calib.layout.cams.push({ x: 0.02, y: 0.02, w: 0.22, h: 0.22 });
  $("#cb-toggle-game").textContent = calib.layout.gameplay ? "− área de gameplay" : "+ área de gameplay";
  renderRects();
  drawPreview();
}

function renderRects() {
  const box = $("#cb-rects");
  if (!calib.layout || calib.layout.mode !== "stack") { box.replaceChildren(); return; }
  const items = calib.layout.cams.map((r, i) => rectEl(r, i, `CAM ${i + 1}${i === 0 ? " · principal" : ""}`, "cam"));
  if (calib.layout.gameplay) items.unshift(rectEl(calib.layout.gameplay, "game", "GAMEPLAY", "game"));
  box.replaceChildren(...items);
}

function rectEl(r, sel, label, kind) {
  const node = el("div", {
    class: `cb-rect ${kind} ${calib.sel === sel ? "sel" : ""}`,
    style: `left:${r.x * 100}%;top:${r.y * 100}%;width:${r.w * 100}%;height:${r.h * 100}%`,
  }, el("span", { class: "cb-tag", text: label }),
     ...["nw", "ne", "sw", "se"].map((h) => el("span", { class: `cb-handle ${h}`, "data-h": h })));
  node.addEventListener("pointerdown", (e) => startDrag(e, sel));
  return node;
}

function startDrag(e, sel) {
  e.preventDefault();
  e.stopPropagation();
  calib.sel = sel;
  if (sel !== "game") calib.previewCam = sel;
  const stage = $("#cb-stage").getBoundingClientRect();
  const r = rectOf(sel);
  const start = { ...r };
  const handle = e.target.dataset?.h || null;
  const x0 = e.clientX, y0 = e.clientY;
  const move = (ev) => {
    const dx = (ev.clientX - x0) / stage.width, dy = (ev.clientY - y0) / stage.height;
    if (!handle) {
      r.x = clamp(start.x + dx, 0, 1 - start.w);
      r.y = clamp(start.y + dy, 0, 1 - start.h);
    } else {
      let { x, y, w, h } = start;
      if (handle.includes("w")) { const nx = clamp(x + dx, 0, x + w - MIN_SIZE); w += x - nx; x = nx; }
      if (handle.includes("e")) { w = clamp(w + dx, MIN_SIZE, 1 - x); }
      if (handle.includes("n")) { const ny = clamp(y + dy, 0, y + h - MIN_SIZE); h += y - ny; y = ny; }
      if (handle.includes("s")) { h = clamp(h + dy, MIN_SIZE, 1 - y); }
      Object.assign(r, { x, y, w, h });
    }
    calib.dirty = true;
    renderRects();
    drawPreview();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    for (const k of ["x", "y", "w", "h"]) r[k] = round4(r[k]);
    renderRects();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  renderRects();
  drawPreview();
}

// Same math as ffmpeg `scale=...:force_original_aspect_ratio=increase,crop=W:H` (center crop).
function drawCover(ctx, img, src, dx, dy, dw, dh) {
  const iw = img.naturalWidth, ih = img.naturalHeight;
  let sx = src.x * iw, sy = src.y * ih, sw = src.w * iw, sh = src.h * ih;
  if (sw / sh > dw / dh) { const cw = sh * dw / dh; sx += (sw - cw) / 2; sw = cw; }
  else { const ch = sw * dh / dw; sy += (sh - ch) / 2; sh = ch; }
  ctx.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh);
}

function drawPreview() {
  const canvas = $("#cb-preview"), ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height, img = $("#cb-img");
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
  if (!calib.layout || !img.complete || !img.naturalWidth) return;
  if (calib.layout.mode === "blur") {
    ctx.filter = "blur(6px) brightness(0.92)";
    drawCover(ctx, img, { x: 0, y: 0, w: 1, h: 1 }, 0, 0, W, H);
    ctx.filter = "none";
    const fgH = W * calib.layout.zoom * img.naturalHeight / img.naturalWidth;
    drawCover(ctx, img, { x: 0, y: 0, w: 1, h: 1 }, 0, (H - fgH) / 2, W, fgH);
    $("#cb-preview-of").textContent = "blur";
    return;
  }
  const cam = calib.layout.cams[Math.min(calib.previewCam, calib.layout.cams.length - 1)];
  const faceH = Math.round(H * calib.layout.ratio);
  drawCover(ctx, img, cam, 0, 0, W, faceH);
  drawCover(ctx, img, calib.layout.gameplay || { x: 0, y: 0, w: 1, h: 1 }, 0, faceH, W, H - faceH);
  ctx.fillStyle = "rgba(34,211,238,.9)"; ctx.fillRect(0, faceH - 1, W, 2);
  $("#cb-preview-of").textContent = `CAM ${Math.min(calib.previewCam, calib.layout.cams.length - 1) + 1}`;
  $("#cb-ratio-val").textContent = `${Math.round(calib.layout.ratio * 100)}%`;
}

function layoutBody() {
  const L = calib.layout;
  if (L.mode === "blur") return { mode: "blur", zoom: L.zoom };
  return { mode: "stack", facecam: L.cams[0], facecam_alternates: L.cams.slice(1), gameplay: L.gameplay, facecam_ratio: L.ratio };
}

async function saveCalib() {
  const base = `/api/streamers/${calib.s.platform}/${encodeURIComponent(calib.s.login)}`;
  try {
    await api(`${base}/layout`, { method: "PUT", body: layoutBody() });
    calib.dirty = false;
    toast(`${calib.s.name}: layout salvo`, "ok");
    loadStreamers();
  } catch (e) { toast(`salvar: ${e.message}`, "bad"); }
}

async function detectCalib() {
  const key = $("#cb-clip").value;
  if (!key) return;
  const out = $("#cb-detect-out");
  out.className = "conn-result"; out.textContent = "rodando detecção de rosto…"; out.classList.remove("hidden");
  const base = `/api/streamers/${calib.s.platform}/${encodeURIComponent(calib.s.login)}`;
  try {
    const r = await api(`${base}/detect`, { method: "POST", body: { clip: key, layout: layoutBody() } });
    out.className = `conn-result ${r.chosen === null ? "err" : "ok"}`;
    out.textContent = `${r.message} · hits=[${r.hits.join(", ")}]/${r.samples}`;
    if (r.chosen !== null) {
      calib.previewCam = r.chosen; calib.sel = r.chosen; renderRects(); drawPreview();
      document.querySelectorAll(".cb-rect.cam")[r.chosen]?.classList.add("win");
    }
  } catch (e) { out.className = "conn-result err"; out.textContent = e.message; }
}

function removeSelected() {
  if (calib.sel === null) { toast("selecione uma área primeiro"); return; }
  if (calib.sel === "game") calib.layout.gameplay = null;
  else if (calib.layout.cams.length > 1) calib.layout.cams.splice(calib.sel, 1);
  else { toast("o modo stack precisa de pelo menos uma câmera", "bad"); return; }
  calib.sel = null; calib.previewCam = 0; calib.dirty = true;
  syncMode();
}

function calibKey(e) {
  if (!$("#tpl-editor").classList.contains("hidden")) { teKey(e); return; }
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (e.key === "Escape") { closeCalib(); return; }
  if (typing) return;
  if ((e.key === "Delete" || e.key === "Backspace") && calib.sel !== null) { e.preventDefault(); removeSelected(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveCalib(); }
}

$("#cb-close").addEventListener("click", () => closeCalib());
$("#cb-clip").addEventListener("change", loadCalibFrame);
let calibAtTimer = null;
$("#cb-at").addEventListener("input", () => {
  $("#cb-at-val").textContent = `${parseFloat($("#cb-at").value).toFixed(1)}s`;
  clearTimeout(calibAtTimer); calibAtTimer = setTimeout(loadCalibFrame, 250);
});
$("#cb-ratio").addEventListener("input", () => { calib.layout.ratio = parseFloat($("#cb-ratio").value); calib.dirty = true; drawPreview(); });
document.querySelectorAll("#cb-mode button").forEach((b) => b.addEventListener("click", () => {
  if (calib.layout.mode === b.dataset.mode) return;
  calib.layout.mode = b.dataset.mode; calib.dirty = true; calib.sel = null; syncMode();
}));
$("#cb-add-cam").addEventListener("click", () => {
  const n = calib.layout.cams.length;
  const last = calib.layout.cams[n - 1] || { x: 0.02, y: 0.02, w: 0.22, h: 0.22 };
  calib.layout.cams.push({ x: round4(clamp(1 - last.x - last.w, 0, 1 - last.w)), y: last.y, w: last.w, h: last.h });
  calib.sel = n; calib.previewCam = n; calib.dirty = true;
  renderRects(); drawPreview();
});
$("#cb-toggle-game").addEventListener("click", () => {
  calib.layout.gameplay = calib.layout.gameplay ? null : { x: 0.25, y: 0, w: 0.5, h: 1 };
  calib.sel = calib.layout.gameplay ? "game" : null; calib.dirty = true;
  syncMode();
});
$("#cb-remove").addEventListener("click", removeSelected);
$("#cb-detect").addEventListener("click", detectCalib);
$("#cb-save").addEventListener("click", saveCalib);
$("#cb-stage").addEventListener("pointerdown", (e) => { if (e.target.id === "cb-img" || e.target.id === "cb-stage") { calib.sel = null; renderRects(); } });
window.addEventListener("resize", () => { if (calib.s) renderRects(); });


// ---------------------------------------------------------------- templates ("moldes")
const OUT_W = 1080, OUT_H = 1920;
const tplState = { list: [], refs: {}, refKey: "" };

async function loadTemplatesList() {
  try { tplState.list = await api("/api/templates"); } catch (e) { toast(`moldes: ${e.message}`, "bad"); }
  $("#tpl-count").textContent = String(tplState.list.length);
  return tplState.list;
}

function fillTemplateSelect(select, value, emptyLabel) {
  const opts = [el("option", { value: "" }, emptyLabel),
    ...tplState.list.map((t) => el("option", { value: t.id }, `${t.name}${t.builtin ? "" : " ·"}`))];
  select.replaceChildren(...opts);
  select.value = tplState.list.some((t) => t.id === value) ? value : "";
}

function templateSelect(s, base) {
  const sel = el("select", { "aria-label": `Molde de ${s.name}` });
  fillTemplateSelect(sel, s.template || "", "clássico (layout)");
  sel.addEventListener("change", () => {
    const v = sel.value;
    streamerCall("PATCH", base, v ? { template: v } : { clear_template: true },
      `${s.name}: molde ${v ? tplState.list.find((t) => t.id === v)?.name : "clássico"}`);
  });
  return sel;
}

// Reference frame + source rects (cam/gameplay) of a streamer, to preview templates with real footage.
async function loadRef(key) {
  if (!key) return null;
  if (tplState.refs[key]) return tplState.refs[key];
  const [platform, login] = key.split(":");
  const base = `/api/streamers/${platform}/${encodeURIComponent(login)}`;
  try {
    const [layout, clips] = await Promise.all([api(`${base}/layout`), api(`${base}/clips`)]);
    const clip = clips.find((c) => c.downloaded) || clips[0];
    if (!clip) return null;
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = `${base}/frame?clip=${encodeURIComponent(clip.key)}&at=4`; });
    let cam = layout.mode === "stack" ? layout.facecam : null;
    if (layout.mode === "stack" && layout.facecam_alternates.length) {
      // the streamer moves the cam around: ask the detector which position this frame uses
      try {
        const r = await api(`${base}/detect`, { method: "POST", body: { clip: clip.key, layout } });
        if (r.chosen !== null) cam = [layout.facecam, ...layout.facecam_alternates][r.chosen];
      } catch { /* keep the primary position */ }
    }
    const ref = {
      img,
      cam,
      game: layout.mode === "stack" && layout.gameplay ? layout.gameplay : { x: 0, y: 0, w: 1, h: 1 },
    };
    tplState.refs[key] = ref;
    return ref;
  } catch { return null; }
}

function fillRefSelect(select) {
  const streamers = (state.stats?.streamers || []).concat();
  const opts = streamers.map((s) => el("option", { value: `${s.platform}:${s.login}` }, `${s.name} · ${s.platform}`));
  select.replaceChildren(el("option", { value: "" }, "esquema (sem frame)"), ...opts);
  select.value = tplState.refKey && opts.some((o) => o.value === tplState.refKey) ? tplState.refKey : "";
}

function slotBox(slot, W, H) {
  let w = Math.max(2, slot.dst.w * W), h = Math.max(2, slot.dst.h * H);
  if (slot.shape === "circle") w = h = Math.min(w, h);
  const x = Math.min(slot.dst.x * W, W - w), y = Math.min(slot.dst.y * H, H - h);
  return { x, y, w, h };
}

function shapePath(ctx, slot, b) {
  ctx.beginPath();
  if (slot.shape === "circle") ctx.ellipse(b.x + b.w / 2, b.y + b.h / 2, b.w / 2, b.h / 2, 0, 0, Math.PI * 2);
  else if (slot.shape === "rounded") ctx.roundRect(b.x, b.y, b.w, b.h, Math.min(b.w, b.h) * 0.12);
  else ctx.rect(b.x, b.y, b.w, b.h);
}

function drawContain(ctx, img, src, b) {
  const iw = img.naturalWidth, ih = img.naturalHeight;
  const sw = src.w * iw, sh = src.h * ih, scale = Math.min(b.w / sw, b.h / sh);
  const dw = sw * scale, dh = sh * scale;
  ctx.drawImage(img, src.x * iw, src.y * ih, sw, sh, b.x + (b.w - dw) / 2, b.y + (b.h - dh) / 2, dw, dh);
}

// Mirrors media/compose.py: background, then slots in order (later on top), masks and borders.
function drawTemplate(canvas, tpl, ref, { guides = false } = {}) {
  const ctx = canvas.getContext("2d"), W = canvas.width, H = canvas.height, k = W / OUT_W;
  ctx.save();
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
  const real = ref && ref.img && ref.img.naturalWidth;
  if (tpl.background === "blur") {
    if (real) { ctx.filter = `blur(${Math.max(4, 12 * k)}px) brightness(0.92)`; drawCover(ctx, ref.img, ref.game, 0, 0, W, H); ctx.filter = "none"; }
    else { const g = ctx.createLinearGradient(0, 0, 0, H); g.addColorStop(0, "#1b2733"); g.addColorStop(1, "#0b1016"); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); }
  }
  for (const slot of tpl.slots) {
    const b = slotBox(slot, W, H);
    ctx.save();
    if (slot.shape !== "rect" && slot.fit === "cover") { shapePath(ctx, slot, b); ctx.clip(); }
    const src = slot.kind === "cam" ? ref?.cam : ref?.game;
    if (real && src) {
      if (slot.fit === "cover") drawCover(ctx, ref.img, src, b.x, b.y, b.w, b.h);
      else drawContain(ctx, ref.img, src, b);
    } else {
      ctx.fillStyle = slot.kind === "cam" ? "rgba(34,211,238,.18)" : "rgba(251,191,36,.12)";
      ctx.fillRect(b.x, b.y, b.w, b.h);
      ctx.fillStyle = slot.kind === "cam" ? "#22d3ee" : "#fbbf24";
      ctx.font = `700 ${Math.max(9, 34 * k)}px "JetBrains Mono", monospace`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(slot.kind === "cam" ? "CAM" : "GAMEPLAY", b.x + b.w / 2, b.y + b.h / 2);
    }
    ctx.restore();
    if (slot.border_px > 0) {
      ctx.save(); ctx.lineWidth = slot.border_px * k; ctx.strokeStyle = slot.border_color;
      const inset = ctx.lineWidth / 2;
      shapePath(ctx, slot, { x: b.x + inset, y: b.y + inset, w: b.w - 2 * inset, h: b.h - 2 * inset }); ctx.stroke(); ctx.restore();
    }
  }
  if (guides) {
    // hook card + caption sample, to judge legibility
    const hy = tpl.hook_y * H, bw = W * 0.72, bh = 96 * k;
    ctx.fillStyle = "rgba(255,255,255,.95)"; ctx.beginPath(); ctx.roundRect((W - bw) / 2, hy - bh / 2, bw, bh, 18 * k); ctx.fill();
    ctx.fillStyle = "#000"; ctx.font = `800 ${40 * k}px Arial, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("GANCHO AQUI?", W / 2, hy);
    ctx.font = `800 ${60 * k}px Arial, sans-serif`; ctx.lineWidth = 6 * k; ctx.strokeStyle = "#000"; ctx.fillStyle = "#fff";
    ctx.textBaseline = "alphabetic";
    ctx.strokeText("legenda", W / 2, tpl.caption_y * H); ctx.fillText("legenda", W / 2, tpl.caption_y * H);
  }
  ctx.restore();
}

async function loadTemplates() {
  await loadTemplatesList();
  fillRefSelect($("#tpl-ref"));
  if (!tplState.refKey) {
    // default to the streamer with the most clips to review: most likely to have footage downloaded
    const best = [...(state.stats?.streamers || [])].sort((a, b) => b.to_review - a.to_review)[0];
    if (best && best.to_review > 0) { tplState.refKey = `${best.platform}:${best.login}`; $("#tpl-ref").value = tplState.refKey; }
  }
  const ref = await loadRef(tplState.refKey);
  const cards = tplState.list.map((t) => {
    const canvas = el("canvas", { width: "270", height: "480" });
    drawTemplate(canvas, t, ref, { guides: true });
    return el("div", { class: "tpl-card" },
      canvas,
      el("div", { class: "tpl-meta" },
        el("div", { class: "tpl-name", text: t.name }),
        el("div", { class: "tpl-tags" },
          el("span", { class: t.builtin ? "builtin" : "", text: t.builtin ? "pronto" : "meu" }),
          el("span", { text: t.background === "blur" ? "fundo blur" : "fundo preto" }),
          t.used_by.length ? el("span", { class: "used", title: t.used_by.join(", "), text: `${t.used_by.length} streamer${t.used_by.length > 1 ? "s" : ""}` }) : null),
        el("div", { class: "tpl-actions" },
          t.builtin ? null : el("button", { class: "link-btn", type: "button", onclick: () => openTemplateEditor(t, false) }, "editar"),
          el("button", { class: "link-btn", type: "button", onclick: () => openTemplateEditor(t, true) }, "duplicar"),
          t.builtin ? null : el("button", { class: "link-btn", type: "button", onclick: () => deleteTemplate(t) }, "excluir"))));
  });
  $("#tpl-grid").replaceChildren(...cards);
}

async function deleteTemplate(t) {
  if (!confirm(`Excluir o molde "${t.name}"?`)) return;
  try { tplState.list = await api(`/api/templates/${t.id}`, { method: "DELETE" }); toast("molde excluído", "ok"); loadTemplates(); }
  catch (e) { toast(e.message, "bad"); }
}

$("#nav-templates").addEventListener("click", () => { closeMobileMenu(); setView(state.view === "templates" ? "clips" : "templates"); });
$("#tpl-ref").addEventListener("change", (e) => { tplState.refKey = e.target.value; loadTemplates(); });
$("#tpl-new").addEventListener("click", () => openTemplateEditor({
  id: "", name: "Novo molde", background: "black", hook_y: 0.3, caption_y: 0.83,
  slots: [{ kind: "cam", dst: { x: 0, y: 0, w: 1, h: 0.3 }, fit: "cover", shape: "rect", border_px: 0, border_color: "#ffffff" },
          { kind: "game", dst: { x: 0, y: 0.3, w: 1, h: 0.7 }, fit: "cover", shape: "rect", border_px: 0, border_color: "#ffffff" }],
}, true));

// ---------------------------------------------------------------- template editor
const te = { tpl: null, isNew: false, sel: null, ref: null, dirty: false };
const slug = (s) => s.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

async function openTemplateEditor(t, asCopy) {
  te.tpl = JSON.parse(JSON.stringify(t));
  delete te.tpl.builtin; delete te.tpl.used_by;
  te.isNew = asCopy;
  if (asCopy) {
    te.tpl.name = t.id ? `${t.name} (cópia)` : t.name;
    te.tpl.id = slug(te.tpl.name);
    while (tplState.list.some((x) => x.id === te.tpl.id)) te.tpl.id = `${te.tpl.id.slice(0, 36)}-${Math.floor(Math.random() * 90 + 10)}`;
  }
  te.sel = null; te.dirty = false;
  $("#te-title").textContent = te.tpl.name;
  $("#te-sub").textContent = asCopy ? "novo molde" : `editando · ${te.tpl.id}`;
  $("#te-name").value = te.tpl.name;
  $("#te-id").value = te.tpl.id;
  $("#te-id").disabled = !asCopy;
  $("#te-bg").value = te.tpl.background;
  fillRefSelect($("#te-ref"));
  $("#te-note").textContent = "salvo em config/templates.yaml · escolha o molde por streamer em Conexões";
  $("#tpl-editor").classList.remove("hidden");
  te.ref = await loadRef($("#te-ref").value);
  teRender();
}

function closeTemplateEditor(force = false) {
  if (!force && te.dirty && !confirm("Descartar as alterações deste molde?")) return;
  $("#tpl-editor").classList.add("hidden");
  te.tpl = null;
}

function teRender() {
  if (!te.tpl) return;
  drawTemplate($("#te-canvas"), te.tpl, te.ref, { guides: true });
  const boxes = te.tpl.slots.map((slot, i) => {
    const b = slotBox(slot, 1, 1);
    const node = el("div", {
      class: `cb-rect ${slot.kind === "cam" ? "cam" : "game"} ${slot.shape} ${te.sel === i ? "sel" : ""}`,
      style: `left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%;${slot.kind === "game" ? "border-style:dashed" : ""}`,
    }, el("span", { class: "cb-tag", text: `${i + 1} · ${slot.kind === "cam" ? "CAM" : "GAMEPLAY"}` }),
       ...["nw", "ne", "sw", "se"].map((h) => el("span", { class: `cb-handle ${h}`, "data-h": h })));
    node.addEventListener("pointerdown", (e) => teDrag(e, i));
    return node;
  });
  $("#te-boxes").replaceChildren(...boxes);
  $("#te-hook").style.top = `${te.tpl.hook_y * 100}%`;
  $("#te-caption").style.top = `${te.tpl.caption_y * 100}%`;
  teRenderSlots();
}

function teRenderSlots() {
  const rows = te.tpl.slots.map((slot, i) => {
    const pick = (label, key, options) => {
      const s = el("select", {}, ...options.map(([v, t]) => el("option", { value: v }, t)));
      s.value = slot[key];
      s.addEventListener("change", () => { slot[key] = s.value; if (key === "shape" && s.value === "circle") squareCircle(slot); te.dirty = true; teRender(); });
      return el("label", {}, label, s);
    };
    const border = el("input", { type: "number", min: "0", max: "40", value: slot.border_px });
    border.addEventListener("input", () => { slot.border_px = Math.max(0, Math.min(40, Number(border.value) || 0)); te.dirty = true; drawTemplate($("#te-canvas"), te.tpl, te.ref, { guides: true }); });
    const color = el("input", { type: "color", value: slot.border_color });
    color.addEventListener("input", () => { slot.border_color = color.value; te.dirty = true; drawTemplate($("#te-canvas"), te.tpl, te.ref, { guides: true }); });
    const row = el("div", { class: `te-slot ${te.sel === i ? "sel" : ""}`, onclick: () => { te.sel = i; teRender(); } },
      el("div", { class: "te-slot-head" },
        el("span", { class: slot.kind === "cam" ? "cam" : "game", text: `${i + 1} · ${slot.kind === "cam" ? "câmera" : "gameplay"}` }),
        el("span", { class: "te-add" },
          el("button", { class: "link-btn", type: "button", title: "Trazer para frente", onclick: (e) => { e.stopPropagation(); moveSlot(i, 1); } }, "↑"),
          el("button", { class: "link-btn", type: "button", title: "Remover", onclick: (e) => { e.stopPropagation(); removeSlot(i); } }, "✕"))),
      pick("encaixe", "fit", [["cover", "preencher"], ["contain", "caber inteiro"]]),
      pick("formato", "shape", [["rect", "retângulo"], ["rounded", "arredondado"], ["circle", "círculo"]]),
      el("label", {}, "borda px", border),
      el("label", {}, "cor da borda", color));
    return row;
  });
  $("#te-slots").replaceChildren(...rows);
}

function squareCircle(slot) {
  // make the slot a pixel square, keeping its height
  slot.dst.w = Math.min(1 - slot.dst.x, round4(slot.dst.h * OUT_H / OUT_W));
}

function moveSlot(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= te.tpl.slots.length) return;
  [te.tpl.slots[i], te.tpl.slots[j]] = [te.tpl.slots[j], te.tpl.slots[i]];
  te.sel = j; te.dirty = true; teRender();
}

function removeSlot(i) {
  if (te.tpl.slots.length <= 1) { toast("o molde precisa de pelo menos uma área", "bad"); return; }
  te.tpl.slots.splice(i, 1); te.sel = null; te.dirty = true; teRender();
}

function addSlot(kind) {
  if (te.tpl.slots.length >= 4) { toast("máximo de 4 áreas", "bad"); return; }
  const dst = kind === "cam" ? { x: 0.05, y: 0.05, w: 0.45, h: 0.15 } : { x: 0, y: 0.3, w: 1, h: 0.7 };
  te.tpl.slots.push({ kind, dst, fit: "cover", shape: "rect", border_px: 0, border_color: "#ffffff" });
  te.sel = te.tpl.slots.length - 1; te.dirty = true; teRender();
}

function teDrag(e, i) {
  e.preventDefault(); e.stopPropagation();
  te.sel = i;
  const slot = te.tpl.slots[i], r = slot.dst, start = { ...r };
  const stage = $("#te-stage").getBoundingClientRect();
  const handle = e.target.dataset?.h || null, x0 = e.clientX, y0 = e.clientY;
  const move = (ev) => {
    const dx = (ev.clientX - x0) / stage.width, dy = (ev.clientY - y0) / stage.height;
    if (!handle) {
      r.x = clamp(start.x + dx, 0, 1 - start.w); r.y = clamp(start.y + dy, 0, 1 - start.h);
    } else {
      let { x, y, w, h } = start;
      if (handle.includes("w")) { const nx = clamp(x + dx, 0, x + w - MIN_SIZE); w += x - nx; x = nx; }
      if (handle.includes("e")) w = clamp(w + dx, MIN_SIZE, 1 - x);
      if (handle.includes("n")) { const ny = clamp(y + dy, 0, y + h - MIN_SIZE); h += y - ny; y = ny; }
      if (handle.includes("s")) h = clamp(h + dy, MIN_SIZE, 1 - y);
      Object.assign(r, { x, y, w, h });
      if (slot.shape === "circle") { r.w = Math.min(1 - r.x, h * OUT_H / OUT_W); r.h = Math.min(1 - r.y, r.w * OUT_W / OUT_H); }
    }
    te.dirty = true; teRender();
  };
  const up = () => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    for (const k of ["x", "y", "w", "h"]) r[k] = round4(r[k]);
    teRender();
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
  teRender();
}

function teGuideDrag(e, key, lo, hi) {
  e.preventDefault();
  const stage = $("#te-stage").getBoundingClientRect();
  const move = (ev) => { te.tpl[key] = round4(clamp((ev.clientY - stage.top) / stage.height, lo, hi)); te.dirty = true; teRender(); };
  const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
}

async function saveTemplate() {
  const t = te.tpl;
  t.name = $("#te-name").value.trim() || "Sem nome";
  if (te.isNew) t.id = slug($("#te-id").value || t.name);
  if (!/^[a-z0-9][a-z0-9-]{1,39}$/.test(t.id)) { toast("id inválido: use letras minúsculas, números e -", "bad"); return; }
  if (te.isNew && tplState.list.some((x) => x.id === t.id)) { toast(`já existe um molde com id "${t.id}"`, "bad"); return; }
  t.background = $("#te-bg").value;
  try {
    tplState.list = await api(`/api/templates/${t.id}`, { method: "PUT", body: t });
    te.dirty = false; te.isNew = false;
    $("#te-id").disabled = true; $("#te-sub").textContent = `editando · ${t.id}`;
    toast(`molde "${t.name}" salvo`, "ok");
    loadTemplates();
  } catch (e) { toast(`salvar: ${e.message}`, "bad"); }
}

function teKey(e) {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (e.key === "Escape") { closeTemplateEditor(); return; }
  if (typing) return;
  if ((e.key === "Delete" || e.key === "Backspace") && te.sel !== null) { e.preventDefault(); removeSlot(te.sel); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveTemplate(); }
}

$("#te-close").addEventListener("click", () => closeTemplateEditor());
$("#te-save").addEventListener("click", saveTemplate);
$("#te-add-cam").addEventListener("click", () => addSlot("cam"));
$("#te-add-game").addEventListener("click", () => addSlot("game"));
$("#te-name").addEventListener("input", () => {
  te.dirty = true; $("#te-title").textContent = $("#te-name").value;
  if (te.isNew) $("#te-id").value = slug($("#te-name").value);
});
$("#te-bg").addEventListener("change", () => { te.tpl.background = $("#te-bg").value; te.dirty = true; teRender(); });
$("#te-ref").addEventListener("change", async () => { tplState.refKey = $("#te-ref").value; te.ref = await loadRef(tplState.refKey); teRender(); });
$("#te-hook").addEventListener("pointerdown", (e) => teGuideDrag(e, "hook_y", 0.05, 0.95));
$("#te-caption").addEventListener("pointerdown", (e) => teGuideDrag(e, "caption_y", 0.3, 0.95));
$("#te-stage").addEventListener("pointerdown", (e) => { if (e.target.tagName === "CANVAS") { te.sel = null; teRender(); } });
loadTemplatesList();
