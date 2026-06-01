"use strict";

const POLL_MS = 2000;
const LS_BASE = "sim.apiBase";
const LS_KEY = "sim.apiKey";

const el = (id) => document.getElementById(id);
const state = { base: "", key: "", timer: null, busy: new Set() };

// ---- config / connection -------------------------------------------------

function loadConfig() {
  state.base = localStorage.getItem(LS_BASE) || "";
  state.key = localStorage.getItem(LS_KEY) || "";
  el("api-base").value = state.base;
  el("api-key").value = state.key;
}

function saveConfig() {
  state.base = el("api-base").value.trim().replace(/\/+$/, "");
  state.key = el("api-key").value.trim();
  localStorage.setItem(LS_BASE, state.base);
  localStorage.setItem(LS_KEY, state.key);
}

function setConnStatus(kind, text) {
  const pill = el("conn-status");
  pill.className = "pill pill-" + kind;
  pill.textContent = text;
}

// ---- API helpers ---------------------------------------------------------

async function apiFetch(path, opts = {}) {
  if (!state.base) throw new Error("no API base URL configured");
  const headers = Object.assign({ "X-API-Key": state.key }, opts.headers || {});
  if (opts.body) headers["Content-Type"] = "application/json";
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 5000);
  try {
    const res = await fetch(state.base + path, { ...opts, headers, signal: ctrl.signal });
    return res;
  } finally {
    clearTimeout(t);
  }
}

// ---- rendering -----------------------------------------------------------

function goOffline(detail) {
  setConnStatus("offline", "offline");
  el("offline-banner").classList.remove("hidden");
  el("fleet-meta").classList.add("hidden");
  if (detail) el("offline-detail").textContent = detail;
  // Disable controls
  document.querySelectorAll(".machine button, .machine input").forEach((n) => (n.disabled = true));
  document.querySelectorAll(".machine").forEach((n) => n.classList.add("inactive"));
}

function goOnline(snapshot) {
  setConnStatus("online", "online");
  el("offline-banner").classList.add("hidden");
  el("fleet-meta").classList.remove("hidden");
  el("meta-machines").textContent = `${snapshot.machine_count} machine(s)`;
  el("meta-uptime").textContent = `uptime ${fmtDuration(snapshot.uptime_s)}`;
  el("meta-updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  renderMachines(snapshot.machines || []);
}

function fmtDuration(s) {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function renderMachines(machines) {
  const root = el("machines");
  const seen = new Set();

  for (const m of machines) {
    seen.add(m.machine_id);
    let card = document.getElementById("m-" + m.machine_id);
    if (!card) {
      card = buildCard(m);
      root.appendChild(card);
    }
    updateCard(card, m);
  }
  // remove stale cards
  [...root.children].forEach((c) => {
    if (!seen.has(c.dataset.id)) c.remove();
  });
}

function buildCard(m) {
  const card = document.createElement("div");
  card.className = "machine";
  card.id = "m-" + m.machine_id;
  card.dataset.id = m.machine_id;
  card.innerHTML = `
    <div class="machine-head">
      <span class="id">${m.machine_id}</span>
      <span class="state-badge"></span>
    </div>
    <div class="row">
      <span class="label">Random anomalies</span>
      <label class="switch">
        <input type="checkbox" class="rnd" />
        <span class="slider"></span>
      </label>
    </div>
    <div class="row">
      <span class="label">Inject manually</span>
    </div>
    <div class="inject-btns">
      <button data-kind="spike">Spike</button>
      <button data-kind="drift">Drift</button>
      <button data-kind="stuck">Stuck</button>
    </div>
    <div class="sensors"></div>
  `;
  card.querySelector(".rnd").addEventListener("change", (e) =>
    onToggleRandom(m.machine_id, e.target));
  card.querySelectorAll(".inject-btns button").forEach((b) =>
    b.addEventListener("click", () => onInject(m.machine_id, b.dataset.kind)));
  return card;
}

function updateCard(card, m) {
  card.classList.toggle("inactive", !m.active);

  const badge = card.querySelector(".state-badge");
  badge.textContent = m.active_anomaly ? `${m.state} · ${m.active_anomaly}` : m.state;
  badge.className = "state-badge " +
    (m.active_anomaly ? "state-anom" : m.active ? "state-on" : "state-off");

  const rnd = card.querySelector(".rnd");
  if (!state.busy.has(m.machine_id + ":rnd")) rnd.checked = !!m.random_enabled;
  rnd.disabled = false;
  card.querySelectorAll(".inject-btns button").forEach((b) => (b.disabled = false));

  const sensors = card.querySelector(".sensors");
  const sample = m.last_sample || {};
  const names = m.sensors && m.sensors.length ? m.sensors : Object.keys(sample);
  sensors.innerHTML = names.map((n) => {
    const v = sample[n];
    const txt = (v === undefined || v === null) ? "—" : Number(v).toFixed(2);
    return `<span class="sname">${n}</span><span class="sval">${txt}</span>`;
  }).join("");
}

// ---- actions -------------------------------------------------------------

async function onToggleRandom(id, input) {
  const enabled = input.checked;
  state.busy.add(id + ":rnd");
  try {
    const res = await apiFetch(`/api/machines/${id}/random`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast(`${id}: random anomalies ${enabled ? "ON" : "OFF"}`, "ok");
  } catch (err) {
    input.checked = !enabled; // revert
    toast(`${id}: failed to set random (${err.message})`, "err");
  } finally {
    setTimeout(() => state.busy.delete(id + ":rnd"), 500);
  }
}

async function onInject(id, kind) {
  try {
    const res = await apiFetch(`/api/machines/${id}/inject`, {
      method: "POST",
      body: JSON.stringify({ kind }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    toast(`${id}: injected ${kind}`, "ok");
  } catch (err) {
    toast(`${id}: inject ${kind} failed (${err.message})`, "err");
  }
}

// ---- polling -------------------------------------------------------------

async function poll() {
  try {
    const res = await apiFetch("/api/state");
    if (res.status === 401) {
      goOffline("Unauthorized — check the API key.");
      return;
    }
    if (!res.ok) {
      goOffline(`Control API returned HTTP ${res.status}.`);
      return;
    }
    goOnline(await res.json());
  } catch (err) {
    goOffline("The simulator may be stopped to save cost. " +
      "The panel is inactive until it responds.");
  }
}

function startPolling() {
  if (state.timer) clearInterval(state.timer);
  setConnStatus("unknown", "connecting…");
  poll();
  state.timer = setInterval(poll, POLL_MS);
}

// ---- toasts --------------------------------------------------------------

let toastWrap;
function toast(msg, kind = "ok") {
  if (!toastWrap) {
    toastWrap = document.createElement("div");
    toastWrap.className = "toast-wrap";
    document.body.appendChild(toastWrap);
  }
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  toastWrap.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ---- boot ----------------------------------------------------------------

el("connect-btn").addEventListener("click", () => {
  saveConfig();
  startPolling();
});

loadConfig();
if (state.base) startPolling();
