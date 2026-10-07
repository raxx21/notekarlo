// NoteKarLo front-end: audio capture, live notes, tools, history. No build step.

const $ = (sel, root = document) => root.querySelector(sel);
const VIEWER = location.pathname.startsWith("/view");

const SECTIONS = [
  ["feedback", "Feedback", "kya accha tha, kya sudharna hai"],
  ["actions", "Action Items", "to-do"],
  ["decisions", "Decisions", "kya decide hua"],
  ["points", "Key Points", "yaad rakhne wali baatein"],
  ["questions", "Open Questions", "follow-up"],
];
const SECTION_LABEL = Object.fromEntries(SECTIONS.map(([k, l]) => [k, l]));
const CHANNEL_CODE = { mic: 0, meeting: 1 };

const S = {
  ws: null,
  profile: {},
  caps: {},
  meeting: null,
  active: false,
  labels: {},
  notes: { items: [], summary: "", topic: "" },
  changed: new Set(),
  transcript: [],
  mom: "",
  momStreaming: false,
  capture: null,
  lastNotesAt: 0,
  loudAt: {},
  pendingRender: false,
  view: "setup",
};

// ───────────────────────── helpers ─────────────────────────
function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
  return node;
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

let toastTimer;
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

async function copyText(text, what = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = el("textarea", { style: "position:fixed;opacity:0" });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(`${what} ✓`);
}

function hhmm(ts) {
  return new Date(ts * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
}
function hhmmss(ts) {
  return new Date(ts * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString("en-IN", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtDuration(secs) {
  secs = Math.max(0, Math.round(secs));
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function show(view) {
  S.view = view;
  for (const id of ["setup", "live", "history"]) $("#" + id).classList.toggle("hidden", id !== view);
  updateTopbar();
}

function updateTopbar() {
  $("#mtTitle").textContent = S.view === "live" && S.meeting ? S.meeting.title : "";
  $("#liveDot").classList.toggle("hidden", !S.active);
  $("#btnStop").classList.toggle("hidden", !S.active || VIEWER);
  $("#btnNew").hidden = VIEWER || S.active || S.view === "setup";
  $("#timer").classList.toggle("hidden", !S.meeting || S.view !== "live");
  $("#btnPin").classList.toggle("hidden", !S.active);
}

// ───────────────────────── banner ─────────────────────────
function banner(text, actions = []) {
  $("#bannerText").textContent = text;
  const box = $("#bannerActions");
  box.innerHTML = "";
  for (const a of actions) box.append(el("button", { onclick: a.onClick }, a.label));
  $("#banner").classList.remove("hidden");
}
function hideBanner() { $("#banner").classList.add("hidden"); }

// ───────────────────────── markdown (tiny, safe) ─────────────────────────
function esc(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_(.+?)_(?=[\s).,!?]|$)/g, "$1<em>$2</em>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
}
function renderMarkdown(src) {
  const lines = src.replace(/\r/g, "").split("\n");
  const out = [];
  const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { const n = Math.min(h[1].length, 3); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const head = cells(rows[0]);
      const body = rows.slice(1).filter((r) => !isSep(r));
      out.push("<table><thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
        body.map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }
    if (/^\s*[-*•]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*•]\s+/, ""));
      out.push("<ul>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ul>");
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+[.)]\s+/, ""));
      out.push("<ol>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ol>");
      continue;
    }
    if (!line.trim() || /^-{3,}$/.test(line.trim())) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*\||\s*[-*•]\s+|\s*\d+[.)]\s+)/.test(lines[i])) para.push(lines[i++]);
    out.push(`<p>${inline(para.join(" "))}</p>`);
  }
  return out.join("\n");
}

// ───────────────────────── notes ─────────────────────────
function notesToText(notes, title, started) {
  const head = `${title || "Meeting"} — Notes (${started ? fmtDate(started) : ""})`;
  const out = [head];
  if (notes.summary) out.push("", `Summary: ${notes.summary}`);
  for (const [key, label] of SECTIONS) {
    const items = (notes.items || []).filter((i) => i.section === key);
    if (!items.length) continue;
    out.push("", `${label}:`);
    for (const i of items) out.push(`• ${itemLine(i)}`);
  }
  return out.join("\n");
}

function itemLine(i) {
  const meta = [];
  if (i.owner) meta.push(`Owner: ${i.owner}`);
  if (i.due) meta.push(`Deadline: ${i.due}`);
  return `${i.text}${meta.length ? ` (${meta.join(", ")})` : ""}${i.important ? " [Important]" : ""}`;
}

function isEditing() {
  const a = document.activeElement;
  return !!(a && a.closest && a.closest("#sections") && (a.isContentEditable || a.tagName === "INPUT"));
}

function setNotes(notes, changed = []) {
  S.notes = notes || { items: [] };
  for (const id of changed) S.changed.add(id);
  $("#topic").textContent = S.notes.topic || (S.active ? "Sun raha hoon…" : "—");
  $("#summary").textContent = S.notes.summary || "";
  if (isEditing()) { S.pendingRender = true; return; }
  renderNotes($("#sections"), S.notes, VIEWER);
  S.changed.clear();
}

const LEFT_COLUMN = new Set(["feedback", "decisions", "questions"]);

function renderNotes(container, notes, readOnly = false) {
  container.innerHTML = "";
  const left = el("div", { class: "col" });
  const right = el("div", { class: "col" });
  container.append(left, right);
  for (const [key, title, hint] of SECTIONS) {
    const items = (notes.items || []).filter((i) => i.section === key);
    const section = el("section", { class: "section", "data-s": key },
      el("div", { class: "section-head" },
        el("h3", {}, title, el("span", { class: "hi" }, hint)),
        items.length ? el("span", { class: "count" }, String(items.length)) : null,
        items.length ? el("button", {
          class: "ghost icon", title: `Copy ${title}`,
          onclick: () => copyText(`${title}:\n` + items.map((i) => `• ${itemLine(i)}`).join("\n"), `${title} copied`),
        }, "⧉") : null,
      ),
    );
    if (items.length) {
      section.append(el("ul", { class: "items" }, items.map((i) => itemEl(i, readOnly))));
    } else {
      section.append(el("p", { class: "empty-s" }, "Abhi kuch nahi."));
    }
    if (!readOnly) section.append(addRow(key));
    (LEFT_COLUMN.has(key) ? left : right).append(section);
  }
}

function itemEl(item, readOnly) {
  const text = el("div", { class: "item-text" }, item.text);
  const meta = el("div", { class: "item-meta" },
    item.owner ? el("span", { class: "tag" + (item.owner === S.profile.my_name ? " mine" : "") }, "👤 " + item.owner) : null,
    item.due ? el("span", { class: "tag due" }, "⏰ " + item.due) : null,
    item.important ? el("span", { class: "tag star" }, "★ Important") : null,
    item.by_user ? el("span", { class: "tag", title: "Written or edited by you — Claude won't change it" }, "✍ yours") : null,
  );
  const li = el("li", { class: "item" + (item.important ? " important" : "") + (S.changed.has(item.id) ? " fresh" : ""), "data-id": item.id },
    el("div", {}, text, meta.childElementCount ? meta : null),
  );
  if (!readOnly) {
    li.append(el("div", { class: "item-actions" },
      el("button", { title: "Mark important", class: item.important ? "on" : "", onclick: () => patchItem(item.id, { important: !item.important }) }, "★"),
      el("button", { title: "Copy", onclick: () => copyText(itemLine(item)) }, "⧉"),
      el("button", { title: "Edit", onclick: () => startEdit(text, item) }, "✎"),
      el("button", { title: "Delete", onclick: () => deleteItem(item.id) }, "✕"),
    ));
    text.addEventListener("dblclick", () => startEdit(text, item));
  } else {
    li.append(el("div", { class: "item-actions" },
      el("button", { title: "Copy", onclick: () => copyText(itemLine(item)) }, "⧉")));
  }
  return li;
}

function startEdit(node, item) {
  if (node.isContentEditable) return;
  node.contentEditable = "true";
  node.focus();
  document.getSelection().selectAllChildren(node);
  const finish = async (save) => {
    node.removeEventListener("keydown", onKey);
    node.contentEditable = "false";
    const value = node.textContent.trim();
    if (save && value && value !== item.text) await patchItem(item.id, { text: value });
    else node.textContent = item.text;
    if (S.pendingRender) { S.pendingRender = false; setNotes(S.notes); }
  };
  const onKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); node.blur(); }
    if (e.key === "Escape") { node.dataset.cancel = "1"; node.blur(); }
  };
  node.addEventListener("keydown", onKey);
  node.addEventListener("blur", () => { finish(node.dataset.cancel !== "1"); delete node.dataset.cancel; }, { once: true });
}

function addRow(section) {
  const row = el("div", { class: "add-row" });
  const btn = el("button", { class: "ghost", onclick: () => {
    row.innerHTML = "";
    const input = el("input", { placeholder: "Apna note likho, Enter dabao (main spelling theek kar dunga)" });
    input.addEventListener("keydown", async (e) => {
      if (e.key === "Escape") { row.replaceWith(addRow(section)); flushPending(); }
      if (e.key !== "Enter" || !input.value.trim()) return;
      const raw = input.value.trim();
      input.disabled = true;
      let text = raw;
      try { text = (await api("POST", "/api/fix", { text: raw })).text || raw; } catch {}
      try { await api("POST", "/api/notes/item", { section, text }); } catch (err) { toast(err.message); }
      row.replaceWith(addRow(section));
      flushPending();
    });
    input.addEventListener("blur", () => { if (!input.value.trim()) { row.replaceWith(addRow(section)); flushPending(); } });
    row.append(input);
    input.focus();
  } }, "+ Add note");
  row.append(btn);
  return row;
}

function flushPending() {
  if (S.pendingRender && !isEditing()) { S.pendingRender = false; setNotes(S.notes); }
}

async function patchItem(id, fields) {
  try { await api("PATCH", `/api/notes/item/${id}`, fields); } catch (e) { toast(e.message); }
}
async function deleteItem(id) {
  try { await api("DELETE", `/api/notes/item/${id}`); } catch (e) { toast(e.message); }
}

// ───────────────────────── transcript ─────────────────────────
function lineEl(l) {
  return el("div", { class: "line" + (l.trigger ? " trigger" : ""), "data-id": l.id },
    el("span", { class: "t" }, hhmmss(l.t)),
    el("span", { class: `who ${l.who}` }, l.who === "Me" ? (S.profile.my_name || "Me") : l.who),
    document.createTextNode(l.text),
  );
}

function renderTranscript() {
  const box = $("#transcript");
  box.innerHTML = "";
  if (!S.transcript.length) {
    box.append(el("p", { class: "empty" }, "Jaise hi koi bolega, yahan dikhega…"));
    return;
  }
  for (const l of [...S.transcript].sort((a, b) => a.t - b.t).slice(-500)) box.append(lineEl(l));
  box.scrollTop = box.scrollHeight;
}

function addLine(line) {
  S.transcript.push(line);
  const box = $("#transcript");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.querySelector(".empty")?.remove();
  box.append(lineEl(line));
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function addPinMarker(t) {
  const box = $("#transcript");
  box.querySelector(".empty")?.remove();
  box.append(el("div", { class: "line pinline" }, `📌 Note-this pressed at ${hhmmss(t)} — Claude will capture it`));
  box.scrollTop = box.scrollHeight;
}

// ───────────────────────── meters ─────────────────────────
function channelName(ch) {
  if (ch === "mic") return S.labels.mic === "Room" ? "Room mic" : "My mic";
  if (ch === "meeting") return S.meeting?.sources?.meeting === "system" ? "System audio" : "Meeting tab";
  return "Recording";
}

function renderLevels(levels, queue) {
  const box = $("#meters");
  const now = Date.now();
  for (const [ch, level] of Object.entries(levels || {})) {
    let m = box.querySelector(`[data-ch="${ch}"]`);
    if (!m) {
      m = el("div", { class: "meter", "data-ch": ch },
        el("span", { class: "label2" }, channelName(ch)),
        el("span", { class: "bar" }, el("span", { class: "fill" })));
      box.append(m);
      S.loudAt[ch] = now;
    }
    if (level > 0.006) S.loudAt[ch] = now;
    m.querySelector(".fill").style.width = `${Math.min(100, Math.sqrt(level) * 260)}%`;
    const silent = now - (S.loudAt[ch] || now) > 15000;
    m.classList.toggle("silent", silent);
    m.querySelector(".label2").textContent = channelName(ch) + (silent ? " · silent" : "");
  }
  $("#queueInfo").textContent = queue > 1 ? `${queue} clips waiting` : "";
}

// ───────────────────────── status ─────────────────────────
function setChip(id, text, cls = "", title = "") {
  const c = $("#" + id);
  c.textContent = text;
  c.className = "chip " + cls;
  if (title) c.title = title;
}

function handleStatus(ev) {
  if (ev.stt === "loading") setChip("chipStt", "Speech model loading…", "busy", "First run downloads ~0.9 GB once");
  if (ev.stt === "ready") setChip("chipStt", "Speech ready", "ok", ev.stt_model || "");
  if (ev.stt === "error") setChip("chipStt", "Speech model error", "err", ev.stt_error || "");
  if (ev.claude === false) setChip("chipNotes", "Claude CLI missing", "err", "Install Claude Code and run `claude` once to log in");
  if (ev.claude === true && $("#chipNotes").className.trim() === "chip") setChip("chipNotes", "Claude ready", "ok", "Notes are written by your Claude CLI login");
  if (ev.notes === "updating") setChip("chipNotes", "Claude writing…", "busy");
  if (ev.notes === "idle") setChip("chipNotes", "Notes up to date", "ok");
  if (ev.notes === "error") setChip("chipNotes", "Claude error", "err", ev.error || "");
  if (ev.error) banner(ev.error);
  if (ev.warning) {
    const actions = [];
    if (ev.action === "privacy") actions.push({ label: "Open Privacy settings", onClick: () => api("POST", "/api/open-privacy-settings") });
    if (ev.action === "fast_model") actions.push({ label: "Switch to fast model", onClick: async () => {
      hideBanner();
      try { S.profile = await api("PUT", "/api/profile", { stt_model: "fast" }); $("#fSttModel").value = "fast"; toast("Loading the fast speech model…"); } catch (e) { toast(e.message); }
    } });
    banner(ev.warning, actions);
  }
}

// ───────────────────────── meeting state ─────────────────────────
function loadSnapshot(st) {
  if (!st) return;
  S.meeting = st.meta;
  S.active = !!st.active;
  S.labels = st.labels || {};
  S.transcript = st.transcript || [];
  renderTranscript();
  setNotes(st.notes || { items: [] });
  S.mom = st.mom || "";
  renderMom();
  updateTopbar();
}

function renderMom() {
  const box = $("#momBox");
  if (!S.mom && !S.momStreaming) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const art = $("#momText");
  art.innerHTML = renderMarkdown(S.mom || "") || "<p class='muted'>Final notes likh raha hoon…</p>";
  art.classList.toggle("cursor", S.momStreaming);
}

let momRaf = 0;
function handleMom(ev) {
  if (S.meeting && ev.id && ev.id !== S.meeting.id) return;
  if (ev.reset) { S.mom = ""; S.momStreaming = true; renderMom(); $("#momBox").scrollIntoView({ behavior: "smooth", block: "start" }); }
  if (ev.delta) {
    S.mom += ev.delta;
    if (!momRaf) momRaf = requestAnimationFrame(() => { momRaf = 0; renderMom(); });
  }
  if (ev.done) { S.mom = ev.text || S.mom; S.momStreaming = false; renderMom(); toast("Final MoM ready ✓"); }
  if (ev.error) { S.momStreaming = false; renderMom(); banner("MoM failed: " + ev.error, [{ label: "Retry", onClick: () => api("POST", "/api/mom") }]); }
}

function onEvent(ev) {
  switch (ev.type) {
    case "status": handleStatus(ev); break;
    case "meeting":
      $("#meters").innerHTML = "";
      loadSnapshot(ev.state);
      hideBanner();
      show("live");
      break;
    case "line":
      if (!S.meeting || ev.meeting === S.meeting.id) addLine(ev.line);
      break;
    case "notes":
      setNotes(ev.notes, ev.changed || []);
      S.lastNotesAt = Date.now();
      if (ev.took) $("#updatedAt").dataset.took = ev.took;
      break;
    case "line_drop":
      S.transcript = S.transcript.filter((l) => l.id !== ev.id);
      $(`#transcript .line[data-id="${ev.id}"]`)?.remove();
      break;
    case "levels": renderLevels(ev.levels, ev.queue); break;
    case "pin": addPinMarker(ev.t); break;
    case "meeting_stopping":
      S.active = false;
      stopCapture();
      $("#meters").innerHTML = "";
      updateTopbar();
      setChip("chipNotes", "Finishing notes…", "busy");
      break;
    case "meeting_ended":
      stopCapture();
      loadSnapshot(ev.state);
      setChip("chipNotes", "Meeting saved", "ok");
      break;
    case "mom": handleMom(ev); break;
    case "import":
      banner(`Recording process ho rahi hai… ${fmtDuration(ev.seconds)} audio read, ${ev.queue} clips left to transcribe.`);
      if (!ev.queue) setTimeout(hideBanner, 3000);
      break;
  }
}

// ───────────────────────── websocket ─────────────────────────
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = async () => {
    S.ws = ws;
    if (S.wasDisconnected) {
      S.wasDisconnected = false;
      hideBanner();
      try { const st = await api("GET", "/api/state"); if (st.meeting) loadSnapshot(st.meeting); handleStatus(st.status); } catch {}
    }
  };
  ws.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    S.ws = null;
    S.wasDisconnected = true;
    setChip("chipNotes", "Reconnecting…", "err", "NoteKarLo server is not reachable");
    setTimeout(connectWS, 1500);
  };
}

function sendPcm(code, buffer) {
  const ws = S.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 4_000_000) return;
  const out = new Uint8Array(1 + buffer.byteLength);
  out[0] = code;
  out.set(new Uint8Array(buffer), 1);
  ws.send(out.buffer);
}

// ───────────────────────── audio capture ─────────────────────────
async function startCapture({ src, useMic, deviceId }) {
  let display = null;
  let mic = null;
  if (src === "tab") {
    display = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: 1, width: 320, height: 180 },
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, suppressLocalAudioPlayback: false },
      systemAudio: "include",
      selfBrowserSurface: "exclude",
      surfaceSwitching: "include",
      preferCurrentTab: false,
    });
    if (!display.getAudioTracks().length) {
      display.getTracks().forEach((t) => t.stop());
      throw new Error("Audio share nahi hua. Dobara try karein aur meeting wala tab chunte waqt “Also share tab audio” ON karein.");
    }
  }
  if (useMic) {
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: deviceId ? { exact: deviceId } : undefined, echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (e) {
      display?.getTracks().forEach((t) => t.stop());
      throw new Error("Microphone permission nahi mili. Browser address bar mein mic allow karein.");
    }
  }
  const ctx = new AudioContext({ latencyHint: "interactive" });
  await ctx.audioWorklet.addModule("/static/worklet.js");
  await ctx.resume();
  const sink = ctx.createGain();
  sink.gain.value = 0;
  sink.connect(ctx.destination);
  const nodes = [];
  const attach = (stream, code) => {
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "pcm-tap");
    node.port.onmessage = (e) => sendPcm(code, e.data);
    source.connect(node);
    node.connect(sink);
    nodes.push(node);
  };
  if (mic) attach(mic, CHANNEL_CODE.mic);
  if (display) {
    attach(new MediaStream(display.getAudioTracks()), CHANNEL_CODE.meeting);
    for (const track of display.getTracks()) {
      track.addEventListener("ended", () => {
        if (!S.active) return;
        banner("Tab sharing band ho gaya — meeting audio ab nahi aa raha.", [{ label: "Share tab again", onClick: reconnectMeetingTab }]);
      });
    }
  }
  mic?.getAudioTracks()[0]?.addEventListener("ended", () => {
    if (S.active) banner("Microphone disconnect ho gaya.", [{ label: "Reconnect audio", onClick: reconnectCapture }]);
  });
  return { ctx, streams: [display, mic].filter(Boolean), nodes };
}

function stopCapture(cap = S.capture) {
  if (!cap) return;
  cap.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
  cap.nodes.forEach((n) => { n.port.onmessage = null; n.disconnect(); });
  cap.ctx.close().catch(() => {});
  if (cap === S.capture) S.capture = null;
}

async function reconnectCapture() {
  const src = S.meeting?.sources?.meeting || "none";
  const useMic = !!S.meeting?.sources?.mic;
  stopCapture();
  try {
    S.capture = await startCapture({ src: src === "tab" ? "tab" : "none", useMic, deviceId: $("#fMicDevice").value });
    hideBanner();
    toast("Audio reconnected ✓");
  } catch (e) { toast(e.message, 5000); }
}

async function reconnectMeetingTab() {
  await reconnectCapture();
}

// ───────────────────────── setup form ─────────────────────────
function selectedSource() {
  return document.querySelector('input[name="msrc"]:checked')?.value || "tab";
}

function updateSourceHints() {
  const src = selectedSource();
  $("#micLabelHint").textContent = src === "none" ? "— hears everyone in the room (labelled “Room”)" : "— your voice, labelled “Me”";
  $("#fMicDevice").classList.toggle("hidden", !$("#fMic").checked);
}

async function listMics() {
  try {
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
    const sel = $("#fMicDevice");
    const current = sel.value;
    sel.innerHTML = "";
    sel.append(el("option", { value: "" }, "Default microphone"));
    devices.forEach((d, i) => { if (d.deviceId && d.deviceId !== "default") sel.append(el("option", { value: d.deviceId }, d.label || `Microphone ${i + 1}`)); });
    sel.value = current;
  } catch {}
}

function fillSetup(st) {
  const p = st.profile;
  $("#fName").value = p.my_name || "";
  $("#fPeople").value = p.people || "";
  $("#fGlossary").value = p.glossary || "";
  const sel = $("#fModel");
  sel.innerHTML = "";
  for (const [k, label] of Object.entries(st.models || {})) sel.append(el("option", { value: k }, label));
  sel.value = p.model || "sonnet";
  $("#fSpeed").value = p.speed || "balanced";
  $("#fStt").value = p.stt_mode || "hinglish";
  const choices = st.capabilities.stt_choices || {};
  if (Object.keys(choices).length) {
    const sel2 = $("#fSttModel");
    sel2.innerHTML = "";
    for (const [k, label] of Object.entries(choices)) sel2.append(el("option", { value: k }, label));
    sel2.value = p.stt_model || "auto";
    $("#sttModelField").hidden = false;
  }
  if (st.capabilities.platform === "linux") {
    $("#srcSystemSub").textContent = "Zoom / Teams / Slack desktop app — records what this laptop plays (PulseAudio / PipeWire)";
  } else if (st.capabilities.platform === "mac") {
    $("#srcSystemSub").textContent = "Zoom / Teams / Slack desktop app — captures everything this Mac plays";
  }
  if (!st.capabilities.system_audio) {
    const card = $("#srcSystemCard");
    card.classList.add("disabled");
    card.querySelector("input").disabled = true;
    card.title = st.capabilities.system_audio_reason || "System audio capture is not available";
  }
  try {
    const saved = JSON.parse(localStorage.getItem("nkl.setup") || "{}");
    if (saved.src) { const r = document.querySelector(`input[name="msrc"][value="${saved.src}"]`); if (r && !r.disabled) r.checked = true; }
    if (typeof saved.mic === "boolean") $("#fMic").checked = saved.mic;
  } catch {}
  if (!p.people && !p.glossary) $("#ctxDetails").open = true;
  updateSourceHints();
}

async function saveProfile() {
  S.profile = await api("PUT", "/api/profile", {
    my_name: $("#fName").value.trim(),
    people: $("#fPeople").value.trim(),
    glossary: $("#fGlossary").value.trim(),
    model: $("#fModel").value,
    speed: $("#fSpeed").value,
    stt_mode: $("#fStt").value,
    stt_model: $("#sttModelField").hidden ? undefined : $("#fSttModel").value,
  });
}

async function onStart() {
  const btn = $("#btnStart");
  const src = selectedSource();
  const useMic = $("#fMic").checked;
  if (!useMic && src === "none") { toast("Kam se kam ek audio source chuniye."); return; }
  btn.disabled = true;
  try { localStorage.setItem("nkl.setup", JSON.stringify({ src, mic: useMic })); } catch {}
  let cap = null;
  try {
    cap = await startCapture({ src, useMic, deviceId: $("#fMicDevice").value });
  } catch (e) {
    btn.disabled = false;
    const msg = e.name === "NotAllowedError" ? "Share cancel ho gaya. Dobara Start dabaiye aur meeting tab chuniye." : e.message;
    toast(msg, 6000);
    return;
  }
  try {
    await saveProfile();
    await api("POST", "/api/meeting/start", {
      title: $("#fTitle").value.trim() || "Meeting",
      agenda: $("#fAgenda").value.trim(),
      sources: { mic: useMic, meeting: src },
    });
    S.capture = cap;
    S.active = true;
    listMics();
    show("live");
    toast("Listening… notes yahan aate rahenge.");
  } catch (e) {
    stopCapture(cap);
    toast("Start nahi hua: " + e.message, 6000);
  } finally {
    btn.disabled = false;
  }
}

async function onStop() {
  if (!confirm("Meeting stop karein? Final notes aur MoM ban jayenge.")) return;
  stopCapture();
  setChip("chipNotes", "Finishing notes…", "busy");
  try { await api("POST", "/api/meeting/stop"); } catch (e) { toast(e.message); }
}

async function onPin() {
  if (!S.active) return;
  const b = $("#btnPin");
  b.classList.remove("flash");
  void b.offsetWidth;
  b.classList.add("flash");
  try { await api("POST", "/api/pin"); toast("📌 Pinned — yeh point zaroor notes mein aayega"); } catch (e) { toast(e.message); }
}

// ───────────────────────── tools ─────────────────────────
async function onFix() {
  const input = $("#fixIn");
  const raw = input.value.trim();
  if (!raw) return;
  const out = $("#fixOut");
  const btn = $("#btnFix");
  btn.disabled = true;
  btn.textContent = "Fixing…";
  out.classList.remove("hidden");
  out.textContent = "…";
  try {
    const { text } = await api("POST", "/api/fix", { text: raw });
    out.innerHTML = "";
    out.append(document.createTextNode(text));
    const actions = el("div", { class: "out-actions" },
      el("button", { onclick: () => copyText(text) }, "Copy"),
      ...SECTIONS.map(([key, label]) => el("button", { class: "ghost", onclick: async () => {
        try { await api("POST", "/api/notes/item", { section: key, text }); toast(`Added to ${label}`); input.value = ""; out.classList.add("hidden"); } catch (e) { toast(e.message); }
      } }, `+ ${label}`)),
    );
    out.append(actions);
  } catch (e) {
    out.textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Fix spelling & grammar";
  }
}

async function onAsk() {
  const q = $("#askIn").value.trim();
  if (!q) return;
  const out = $("#askOut");
  out.classList.remove("hidden");
  out.textContent = "Soch raha hoon…";
  try {
    const { answer } = await api("POST", "/api/ask", { question: q });
    out.innerHTML = "";
    out.append(document.createTextNode(answer), el("div", { class: "out-actions" }, el("button", { onclick: () => copyText(answer) }, "Copy")));
  } catch (e) {
    out.textContent = "Error: " + e.message;
  }
}

async function onImport(file) {
  if (!file) return;
  const title = $("#fTitle").value.trim() || file.name.replace(/\.[^.]+$/, "");
  try {
    await saveProfile();
    toast("Uploading recording…");
    const res = await fetch(`/api/import?title=${encodeURIComponent(title)}&name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    show("live");
  } catch (e) {
    toast("Import failed: " + e.message, 5000);
  }
}

// ───────────────────────── history ─────────────────────────
async function openHistory() {
  show("history");
  $("#historyDetail").classList.add("hidden");
  const list = $("#historyList");
  list.classList.remove("hidden");
  list.innerHTML = "<p class='muted'>Loading…</p>";
  try {
    const meetings = await api("GET", "/api/meetings");
    list.innerHTML = "";
    if (!meetings.length) list.append(el("p", { class: "muted" }, "Abhi tak koi meeting save nahi hui."));
    for (const m of meetings) {
      const dur = m.ended ? fmtDuration(m.ended - m.started) : "in progress / unfinished";
      list.append(el("button", { class: "h-card", onclick: () => openMeeting(m.id) },
        el("span", { class: "h-title" }, m.title),
        el("span", { class: "h-meta" }, `${fmtDate(m.started)} · ${dur} · ${m.lines || 0} lines${m.has_mom ? " · MoM ✓" : ""}`)));
    }
  } catch (e) {
    list.innerHTML = "";
    list.append(el("p", {}, "Could not load: " + e.message));
  }
}

async function openMeeting(id) {
  const d = await api("GET", `/api/meetings/${encodeURIComponent(id)}`);
  $("#historyList").classList.add("hidden");
  const box = $("#historyDetail");
  box.classList.remove("hidden");
  box.innerHTML = "";
  const notes = d.notes && d.notes.items ? d.notes : { items: [] };
  const sections = el("div", { class: "sections" });
  renderNotes(sections, notes, true);
  const transcript = el("div", { class: "detail-transcript hidden" },
    d.transcript.map((l) => lineEl(l)));
  box.append(
    el("h2", {}, d.meta.title || id),
    el("p", { class: "muted" }, `${fmtDate(d.meta.started)}${d.meta.ended ? " · " + fmtDuration(d.meta.ended - d.meta.started) : ""}`),
    el("div", { class: "detail-actions" },
      el("button", { class: "ghost", onclick: openHistory }, "← All meetings"),
      el("button", { onclick: () => copyText(notesToText(notes, d.meta.title, d.meta.started), "Notes copied") }, "Copy notes"),
      d.mom ? el("button", { onclick: () => copyText(d.mom, "MoM copied") }, "Copy MoM") : null,
      el("button", { class: "ghost", onclick: () => transcript.classList.toggle("hidden") }, "Show transcript"),
      el("button", { class: "ghost", onclick: () => api("POST", `/api/meetings/${encodeURIComponent(id)}/reveal`) }, "Open folder"),
    ),
    transcript,
    notes.summary ? el("p", { class: "summary" }, notes.summary) : null,
    sections,
  );
  if (d.mom) {
    const art = el("article", { class: "md mom" });
    art.innerHTML = renderMarkdown(d.mom);
    box.append(art);
  }
}

// ───────────────────────── theme, keys, timer ─────────────────────────
function applyTheme() {
  let t = null;
  try { t = localStorage.getItem("nkl.theme"); } catch {}
  if (t) document.documentElement.dataset.theme = t;
}
function toggleTheme() {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("nkl.theme", next); } catch {}
}

function toggleBig() {
  document.body.classList.toggle("big");
  try { localStorage.setItem("nkl.big", document.body.classList.contains("big") ? "1" : ""); } catch {}
}

function tickTimer() {
  const m = S.meeting;
  if (m) {
    const end = S.active ? Date.now() / 1000 : (m.ended || Date.now() / 1000);
    $("#timer").textContent = fmtDuration(end - m.started);
  }
  if (S.lastNotesAt) {
    const ago = Math.round((Date.now() - S.lastNotesAt) / 1000);
    const took = $("#updatedAt").dataset.took;
    $("#updatedAt").textContent = `Notes updated ${ago < 5 ? "just now" : ago + "s ago"}${took ? ` · ${took}s` : ""}`;
  }
}

function bindUI() {
  $("#btnStart").addEventListener("click", onStart);
  $("#btnStop").addEventListener("click", onStop);
  $("#btnPin").addEventListener("click", onPin);
  $("#btnCopyAll").addEventListener("click", () => copyText(notesToText(S.notes, S.meeting?.title, S.meeting?.started), "All notes copied"));
  $("#btnRefresh").addEventListener("click", async () => { try { await api("POST", "/api/notes/refresh"); } catch (e) { toast(e.message); } });
  $("#btnBig").addEventListener("click", toggleBig);
  $("#btnTheme").addEventListener("click", toggleTheme);
  $("#btnHistory").addEventListener("click", openHistory);
  $("#btnBack").addEventListener("click", () => show(S.meeting ? "live" : "setup"));
  $("#btnNew").addEventListener("click", () => { $("#fTitle").value = ""; $("#fAgenda").value = ""; show("setup"); });
  $("#bannerClose").addEventListener("click", hideBanner);
  $("#btnFix").addEventListener("click", onFix);
  $("#fixIn").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onFix(); });
  $("#btnAsk").addEventListener("click", onAsk);
  $("#askIn").addEventListener("keydown", (e) => { if (e.key === "Enter") onAsk(); });
  $("#btnCopyMom").addEventListener("click", () => copyText(S.mom, "MoM copied"));
  $("#btnRegenMom").addEventListener("click", async () => { try { await api("POST", "/api/mom"); } catch (e) { toast(e.message); } });
  $("#btnOpenFolder").addEventListener("click", () => S.meeting && api("POST", `/api/meetings/${encodeURIComponent(S.meeting.id)}/reveal`));
  $("#btnImport").addEventListener("click", () => $("#fileImport").click());
  $("#fileImport").addEventListener("change", (e) => onImport(e.target.files[0]));
  $("#fMic").addEventListener("change", updateSourceHints);
  document.querySelectorAll('input[name="msrc"]').forEach((r) => r.addEventListener("change", updateSourceHints));
  navigator.mediaDevices?.addEventListener?.("devicechange", listMics);
  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest("input, textarea, select, [contenteditable='true']");
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "n" || e.key === "N") { if (S.view === "live" && !VIEWER) { e.preventDefault(); onPin(); } }
    if (e.key === "b" || e.key === "B") { if (S.view === "live") toggleBig(); }
    if (e.key === "Escape" && document.body.classList.contains("big")) toggleBig();
  });
  setInterval(tickTimer, 1000);
}

async function init() {
  applyTheme();
  if (VIEWER) document.body.classList.add("viewer", "big");
  try { if (!VIEWER && localStorage.getItem("nkl.big")) document.body.classList.add("big"); } catch {}
  bindUI();
  let st;
  try {
    st = await api("GET", "/api/state");
  } catch (e) {
    banner("NoteKarLo server se connect nahi ho paya. Terminal mein ./start.sh chal raha hai?");
    return;
  }
  S.profile = st.profile;
  S.caps = st.capabilities;
  if (!VIEWER) fillSetup(st);
  handleStatus(st.status);
  if (st.meeting) loadSnapshot(st.meeting);
  if (VIEWER) {
    show("live");
  } else if (st.meeting && st.meeting.active) {
    show("live");
    const needsBrowserAudio = st.meeting.meta.sources?.mic || st.meeting.meta.sources?.meeting === "tab";
    if (needsBrowserAudio && st.meeting.meta.live) {
      banner("Meeting chal rahi hai, lekin yeh page abhi audio nahi bhej raha (page reload hua?).", [{ label: "Reconnect audio", onClick: reconnectCapture }]);
    }
  } else {
    show("setup");
    if (st.meeting) {
      const m = st.meeting.meta;
      banner(`Last meeting: “${m.title}”`, [{ label: "Open its notes", onClick: () => { hideBanner(); show("live"); } }]);
    }
  }
  listMics();
  connectWS();
}

init();
