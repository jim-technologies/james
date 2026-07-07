"use strict";
// james web dashboard — list sessions and drive any thread from the browser.
// Talks Connect-JSON to the SAME DispatchService the chat channels use, so a
// web prompt goes through the exact dispatch path + secret-stripping. HTTP Basic
// auth is handled by the browser (the page itself is behind it), so fetch()
// carries credentials automatically — no token handling here.
//
// Phase 1: SEND + LIST only. There is no server-side transcript, so a thread's
// log shows only the round-trips made in THIS page session.

const $ = (id) => document.getElementById(id);
const state = {
  current: null, // {backend, conversationId}
  logs: new Map(), // conversationId -> [{role, backend, text, error, artifacts}]
  known: new Map(), // conversationId -> backend (for the sidebar)
};

async function rpc(method, body) {
  const r = await fetch(`/james.v1.DispatchService/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const e = await r.json();
      if (e && e.message) msg = e.message;
    } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function convoLabel(id) {
  return id.length > 28 ? id.slice(0, 27) + "…" : id;
}

function renderSessions() {
  const ul = $("sessions");
  ul.innerHTML = "";
  const ids = [...state.known.keys()].sort();
  for (const id of ids) {
    const li = document.createElement("li");
    li.className = "session" + (state.current && state.current.conversationId === id ? " active" : "");
    const b = document.createElement("span");
    b.className = "badge";
    b.textContent = state.known.get(id);
    const t = document.createElement("span");
    t.className = "convo";
    t.textContent = convoLabel(id);
    t.title = id;
    li.append(b, t);
    li.onclick = () => select(state.known.get(id), id);
    ul.append(li);
  }
  $("side-foot").textContent = ids.length ? `${ids.length} session(s)` : "no sessions yet";
}

function select(backend, conversationId) {
  state.current = { backend, conversationId };
  if (!state.logs.has(conversationId)) state.logs.set(conversationId, []);
  $("thread-title").textContent = `${backend} · ${conversationId}`;
  $("prompt").disabled = false;
  $("send").disabled = false;
  $("prompt").focus();
  renderSessions();
  renderLog();
}

function renderLog() {
  const log = $("log");
  log.innerHTML = "";
  const msgs = (state.current && state.logs.get(state.current.conversationId)) || [];
  if (!msgs.length) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "No history shown — Phase 1 renders only this session's round-trips. Send a message to begin.";
    log.append(hint);
  }
  for (const m of msgs) log.append(renderMsg(m));
  log.scrollTop = log.scrollHeight;
}

function renderMsg(m) {
  const div = document.createElement("div");
  div.className = `msg ${m.role}`;
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = m.role === "user" ? "you" : `[${m.backend || "?"}]`;
  const body = document.createElement("div");
  body.className = "body" + (m.error ? " err" : "");
  body.textContent = m.error ? `⚠ ${m.error}` : m.text || "(no output)";
  div.append(who, body);
  for (const a of m.artifacts || []) div.append(renderArtifact(a));
  return div;
}

function renderArtifact(a) {
  // Proto bytes arrive base64-encoded in Connect-JSON.
  const mime = a.mime || "application/octet-stream";
  const name = a.filename || "file";
  if (mime.startsWith("image/")) {
    const img = document.createElement("img");
    img.className = "artifact";
    img.alt = name;
    img.src = `data:${mime};base64,${a.content}`;
    return img;
  }
  const link = document.createElement("a");
  link.className = "artifact-link";
  link.download = name;
  link.href = `data:${mime};base64,${a.content}`;
  link.textContent = `⬇ ${name}`;
  return link;
}

async function send(text) {
  const { backend, conversationId } = state.current;
  const msgs = state.logs.get(conversationId);
  msgs.push({ role: "user", text });
  renderLog();
  const pending = { role: "assistant", backend, text: "…running…" };
  msgs.push(pending);
  renderLog();
  try {
    const res = await rpc("Dispatch", {
      backend,
      prompt: text,
      channel: "web",
      conversation_id: conversationId,
    });
    pending.backend = res.backend || backend;
    pending.text = res.text || "";
    pending.error = res.ok ? "" : res.error || "failed";
    pending.artifacts = res.artifacts || [];
  } catch (e) {
    pending.error = String(e.message || e);
    pending.text = "";
  }
  renderLog();
}

async function refresh() {
  try {
    const { sessions } = await rpc("ListSessions", {});
    for (const s of sessions || []) {
      const id = s.conversationId || s.conversation_id;
      if (id) state.known.set(id, s.backend || "?");
    }
    renderSessions();
  } catch (e) {
    $("side-foot").textContent = `list failed: ${e.message || e}`;
  }
}

async function loadBackends() {
  // Populate the new-conversation dropdown from the registry, so the UI never
  // drifts from biz/backends.py. The hardcoded options in index.html stay as
  // the fallback if this call fails.
  try {
    const res = await rpc("ListBackends", {});
    const names = (res.backends || []).map((b) => b.name).filter(Boolean);
    if (!names.length) return;
    const def = res.defaultBackend || res.default_backend || names[0];
    const sel = $("new-backend");
    sel.innerHTML = "";
    for (const name of names) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.append(opt);
    }
    sel.value = names.includes(def) ? def : names[0];
  } catch (_) {} // keep the static fallback list
}

function wire() {
  $("new-btn").onclick = () => {
    const row = $("new-row");
    row.hidden = !row.hidden;
  };
  $("new-go").onclick = () => {
    const backend = $("new-backend").value;
    const rnd = Math.random().toString(36).slice(2, 8);
    const id = `web:${Date.now()}-${rnd}`;
    state.known.set(id, backend);
    $("new-row").hidden = true;
    select(backend, id);
  };
  const form = $("composer");
  form.onsubmit = (e) => {
    e.preventDefault();
    const ta = $("prompt");
    const text = ta.value.trim();
    if (!text || !state.current) return;
    ta.value = "";
    send(text);
  };
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
}

wire();
refresh();
loadBackends();
