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
    headers: { "Content-Type": "application/json" },
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
      class: `rail-item ${state.status === s.id ? "on" : ""}`,
      style: `--item-color:${s.color}`,
      "data-tip": `${s.label} · ${fmtNum(n)}`,
      "aria-label": s.label,
      onclick: () => { state.status = s.id; closeDrawer(); closeMobileMenu(); loadClips({ keepOpen: false }); renderSidebar(); },
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
  $("#pulse").className = `pulse ${ageS < 600 ? "live" : "idle"}`;
  const activity = last ? `Loop ativo ${ago(last)}` : "Sem atividade ainda";
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
  try { await navigator.clipboard.writeText(path); toast("📁 Caminho da pasta copiado", "ok"); }
  catch { toast(path); }
});

function setStreamer(login) {
  state.streamer = state.streamer === login ? "" : login;
  closeDrawer();
  closeMobileMenu();
  renderSidebar();
  loadClips({ keepOpen: false });
}

function renderStats() {
  const c = state.stats?.by_status || {};
  const cards = [
    { label: "Para revisar", value: c.ready || 0, color: "var(--warn)" },
    { label: "Aprovados", value: c.approved || 0, color: "var(--ok)" },
    { label: "Postados", value: c.posted || 0, color: "var(--info)" },
    { label: "Na fila do loop", value: c.discovered || 0, color: "var(--accent)" },
  ];
  $("#stats").replaceChildren(...cards.map((s) => el("div", { class: "stat", style: `--c:${s.color}` },
    el("div", { class: "stat-label" }, el("span", { class: "dot" }), s.label),
    el("div", { class: "stat-value", text: fmtNum(s.value) }),
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
        el("span", { text: `👁 ${fmtNum(clip.views)}` }),
        el("span", { text: `⏱ ${fmtDur(clip.duration_s)}` }),
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
    const msg = { approved: "✓ Aprovado", rejected: "✕ Rejeitado", posted: "🚀 Marcado como postado", ready: "↺ De volta para revisão" }[status];
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
    metric(fmtNum(clip.views), "views"),
    metric(fmtDur(clip.duration_s), "duração"),
    metric(clip.score.toFixed(1), "score"),
    clip.cluster_size > 1 ? metric(`×${clip.cluster_size}`, "clips do lance") : null,
    metric(ago(clip.updated_at), "atualizado"),
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
  $("#rerender").textContent = busy ? "Renderizando…" : "Re-renderizar";
}

function metric(value, label) {
  return el("span", { class: "metric" }, el("b", { text: value }), label);
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
    toast("Re-render na fila 🎬", "ok");
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
        toast(j.state === "done" ? "✓ Re-render pronto" : `✕ Re-render falhou: ${j.error || ""}`, j.state === "done" ? "ok" : "bad");
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
    toast("📋 Legenda copiada", "ok");
  } catch {
    $("#d-caption").select();
    document.execCommand("copy");
    toast("📋 Legenda copiada", "ok");
  }
}

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); loadClips({ keepOpen: false }); }, 250);
});

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
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
