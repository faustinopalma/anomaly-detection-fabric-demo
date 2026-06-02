"use strict";

const POLL_MS = 2000;
// Client-side live chart window. Built entirely from the existing /api/state
// poll (no extra server stream), so it adds zero backend load and stops on
// its own when polling stops (page close / standby).
const CHART_WINDOW_MS = 5 * 60 * 1000;

const el = (id) => document.getElementById(id);
const state = {
  timer: null,
  busy: new Set(),
  account: null,
  paused: false,      // explicit operator standby
  hiddenPause: false, // auto-pause while the tab is hidden
  chartsOn: false,
  history: new Map(), // machine_id -> { t: number[], s: {sensor: number[]} }
};

// Fail loud (not silent) if the auth library or config didn't load.
if (typeof msal === "undefined" || typeof CONFIG === "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    const s = el("signin");
    if (s) s.classList.remove("hidden");
    const e = el("signin-error");
    if (e) {
      e.textContent =
        "Authentication library failed to load (vendor/msal-browser.min.js or config.js missing). Try a hard refresh.";
      e.classList.remove("hidden");
    }
  });
  throw new Error("MSAL or CONFIG not available");
}

// ---- MSAL auth -----------------------------------------------------------

const msalApp = new msal.PublicClientApplication({
  auth: {
    clientId: CONFIG.clientId,
    authority: `https://login.microsoftonline.com/${CONFIG.tenantId}`,
    redirectUri: window.location.origin,
  },
  cache: { cacheLocation: "localStorage", storeAuthStateInCookie: false },
});

const TOKEN_REQUEST = { scopes: [CONFIG.scope] };

function showSignedIn(account) {
  state.account = account;
  el("signin").classList.add("hidden");
  el("user-info").classList.remove("hidden");
  el("user-info").textContent = account.username || account.name || "signed in";
  el("signout-btn").classList.remove("hidden");
  el("charts-btn").classList.remove("hidden");
  el("pause-btn").classList.remove("hidden");
}

function showSignedOut(errorMsg) {
  state.account = null;
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  state.history.clear();
  el("signin").classList.remove("hidden");
  el("user-info").classList.add("hidden");
  el("signout-btn").classList.add("hidden");
  el("charts-btn").classList.add("hidden");
  el("pause-btn").classList.add("hidden");
  el("fleet-meta").classList.add("hidden");
  el("offline-banner").classList.add("hidden");
  el("machines").innerHTML = "";
  setConnStatus("unknown", "not connected");
  const e = el("signin-error");
  if (errorMsg) { e.textContent = errorMsg; e.classList.remove("hidden"); }
  else e.classList.add("hidden");
}

async function getToken() {
  if (!state.account) throw new Error("not signed in");
  try {
    const res = await msalApp.acquireTokenSilent({ ...TOKEN_REQUEST, account: state.account });
    return res.accessToken;
  } catch (err) {
    // Silent acquisition failed (expired/interaction required) → redirect.
    await msalApp.acquireTokenRedirect(TOKEN_REQUEST);
    throw err; // redirect navigates away
  }
}

async function signIn() {
  try {
    await msalApp.loginRedirect(TOKEN_REQUEST);
  } catch (err) {
    showSignedOut(`Sign-in failed: ${err.message}`);
  }
}

function signOut() {
  msalApp.logoutRedirect({ account: state.account });
}

function setConnStatus(kind, text) {
  const pill = el("conn-status");
  pill.className = "pill pill-" + kind;
  pill.textContent = text;
}

// ---- API helpers ---------------------------------------------------------

async function apiFetch(path, opts = {}) {
  const token = await getToken();
  const headers = Object.assign({ Authorization: "Bearer " + token }, opts.headers || {});
  if (opts.body) headers["Content-Type"] = "application/json";
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 5000);
  try {
    const res = await fetch(CONFIG.backendUrl + path, { ...opts, headers, signal: ctrl.signal });
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
    <div class="row force-row${(m.valid_states && m.valid_states.length) ? "" : " hidden"}">
      <span class="label">Force state</span>
      <select class="force-state">
        <option value="">Auto (FSM)</option>
        ${(m.valid_states || []).map((s) => `<option value="${s}">${s}</option>`).join("")}
      </select>
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
    <div class="chart-wrap hidden">
      <canvas class="chart" height="96"></canvas>
      <div class="chart-legend"></div>
    </div>
  `;
  card.querySelector(".rnd").addEventListener("change", (e) =>
    onToggleRandom(m.machine_id, e.target));
  const sel = card.querySelector(".force-state");
  if (sel) sel.addEventListener("change", (e) =>
    onForceState(m.machine_id, e.target));
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

  const sel = card.querySelector(".force-state");
  if (sel && !state.busy.has(m.machine_id + ":state")) {
    sel.value = m.forced_state || "";
  }
  if (sel) sel.disabled = false;

  const sensors = card.querySelector(".sensors");
  const sample = m.last_sample || {};
  const names = m.sensors && m.sensors.length ? m.sensors : Object.keys(sample);
  sensors.innerHTML = names.map((n) => {
    const v = sample[n];
    const txt = (v === undefined || v === null) ? "\u2014" : Number(v).toFixed(2);
    return `<span class="sname">${n}</span><span class="sval">${txt}</span>`;
  }).join("");

  // Client-side rolling chart, fed from the same poll snapshot.
  const wrap = card.querySelector(".chart-wrap");
  if (state.chartsOn) {
    accumulateHistory(m, names, sample);
    wrap.classList.remove("hidden");
    drawChart(card, m.machine_id, names);
  } else {
    wrap.classList.add("hidden");
  }
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

async function onForceState(id, sel) {
  const value = sel.value || null; // "" -> null -> back to auto FSM
  state.busy.add(id + ":state");
  try {
    const res = await apiFetch(`/api/machines/${id}/state`, {
      method: "POST",
      body: JSON.stringify({ state: value }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    toast(`${id}: state ${value ? "forced to " + value : "back to auto"}`, "ok");
  } catch (err) {
    toast(`${id}: failed to set state (${err.message})`, "err");
  } finally {
    setTimeout(() => state.busy.delete(id + ":state"), 800);
  }
}

// ---- client-side live chart ---------------------------------------------

const CHART_COLORS = [
  "#4f9cf9", "#2ea043", "#d29922", "#f85149",
  "#a371f7", "#3fb6b2", "#db61a2", "#e3b341",
];

function accumulateHistory(m, names, sample) {
  let h = state.history.get(m.machine_id);
  if (!h) { h = { t: [], s: {} }; state.history.set(m.machine_id, h); }
  const now = Date.now();
  h.t.push(now);
  for (const n of names) {
    if (!h.s[n]) h.s[n] = [];
    const v = sample[n];
    h.s[n].push((v === undefined || v === null) ? null : Number(v));
  }
  // Drop samples older than the window.
  const cutoff = now - CHART_WINDOW_MS;
  let drop = 0;
  while (drop < h.t.length && h.t[drop] < cutoff) drop++;
  if (drop > 0) {
    h.t.splice(0, drop);
    for (const n of Object.keys(h.s)) h.s[n].splice(0, drop);
  }
}

function drawChart(card, machineId, names) {
  const h = state.history.get(machineId);
  const canvas = card.querySelector(".chart");
  if (!h || !canvas || h.t.length < 2) return;

  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || card.clientWidth || 280;
  const cssH = 96;
  if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const pad = 4;
  const t0 = h.t[0];
  const t1 = h.t[h.t.length - 1];
  const span = Math.max(1, t1 - t0);
  const xOf = (t) => pad + (t - t0) / span * (cssW - 2 * pad);

  names.forEach((n, i) => {
    const arr = h.s[n];
    if (!arr) return;
    // Per-sensor min/max so all sensors share the vertical space.
    let lo = Infinity, hi = -Infinity;
    for (const v of arr) { if (v == null) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
    if (!isFinite(lo) || !isFinite(hi)) return;
    const rng = (hi - lo) || 1;
    const yOf = (v) => (cssH - pad) - (v - lo) / rng * (cssH - 2 * pad);
    ctx.beginPath();
    ctx.lineWidth = 1.25;
    ctx.strokeStyle = CHART_COLORS[i % CHART_COLORS.length];
    let started = false;
    for (let k = 0; k < arr.length; k++) {
      const v = arr[k];
      if (v == null) { started = false; continue; }
      const x = xOf(h.t[k]), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  });

  const legend = card.querySelector(".chart-legend");
  if (legend && legend.childElementCount !== names.length) {
    legend.innerHTML = names.map((n, i) =>
      `<span class="lg"><i style="background:${CHART_COLORS[i % CHART_COLORS.length]}"></i>${n}</span>`
    ).join("");
  }
}

// ---- charts + standby controls ------------------------------------------

function setChartsOn(on) {
  state.chartsOn = on;
  el("charts-btn").textContent = `Charts: ${on ? "on" : "off"}`;
  el("charts-btn").classList.toggle("active", on);
  if (!on) {
    state.history.clear();
    document.querySelectorAll(".chart-wrap").forEach((w) => w.classList.add("hidden"));
  }
}

function setPaused(paused) {
  state.paused = paused;
  el("pause-btn").textContent = paused ? "Resume" : "Pause";
  el("pause-btn").classList.toggle("active", paused);
  if (paused) {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    setConnStatus("unknown", "standby");
  } else if (state.account && !state.hiddenPause) {
    startPolling();
  }
}

// ---- polling -------------------------------------------------------------

async function poll() {
  try {
    const res = await apiFetch("/api/state");
    if (res.status === 401 || res.status === 403) {
      goOffline("Access denied — your account is not authorized for this app.");
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

function startPolling() {  if (state.paused || state.hiddenPause) return;  if (state.timer) clearInterval(state.timer);
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

el("signin-btn").addEventListener("click", signIn);
el("signout-btn").addEventListener("click", signOut);
el("charts-btn").addEventListener("click", () => setChartsOn(!state.chartsOn));
el("pause-btn").addEventListener("click", () => setPaused(!state.paused));

// Auto-standby while the tab is hidden: stop polling (zero requests) and
// resume automatically when the tab is visible again, unless the operator
// explicitly paused. Closing the page stops everything on its own.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    state.hiddenPause = true;
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  } else {
    state.hiddenPause = false;
    if (state.account && !state.paused) startPolling();
  }
});

async function boot() {
  try {
    const resp = await msalApp.handleRedirectPromise();
    if (resp && resp.account) {
      msalApp.setActiveAccount(resp.account);
    }
  } catch (err) {
    showSignedOut(`Sign-in failed: ${err.message}`);
    return;
  }
  const accounts = msalApp.getAllAccounts();
  if (accounts.length > 0) {
    const account = msalApp.getActiveAccount() || accounts[0];
    msalApp.setActiveAccount(account);
    showSignedIn(account);
    startPolling();
  } else {
    showSignedOut();
  }
}

boot();
