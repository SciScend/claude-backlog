// claude-backlog board. Plain JavaScript, no build step.
"use strict";

window.addEventListener("error", (e) => {
  const line = document.getElementById("status-line");
  if (line) line.textContent = `The board hit an error: ${e.message}`;
});

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let state = null;
let openTaskId = null;
let openTab = "overview";
let detail = null;
let logState = { run: null, offset: 0, text: "" };
let tailCache = { taskId: null, text: "" };

// --- helpers ----------------------------------------------------------------

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error((data && data.error) || data || `HTTP ${response.status}`);
  }
  return data;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}

async function act(promise, done) {
  try {
    const result = await promise;
    if (done) toast(done);
    await refresh();
    return result;
  } catch (err) {
    toast(err.message);
    return null;
  }
}

function duration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function when(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return time;
  return `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${time}`;
}

function nowSeconds() {
  // The server's clock, so durations agree with what it records.
  return state ? state.now + (Date.now() - state.receivedAt) / 1000 : Date.now() / 1000;
}

// --- rendering --------------------------------------------------------------

function render() {
  renderHeader();
  renderBoard();
  if (openTaskId) renderPanel();
}

function renderHeader() {
  const { usage, settings, runner, presence, warnings } = state;
  const threshold = settings.threshold;
  $("#meter-mark").style.left = `${threshold}%`;
  $("#meter-mark").title = `The queue stops at ${threshold}%. Click to change.`;
  const fill = $("#meter-fill");
  const text = $("#meter-text");
  if (usage.usage) {
    const pct = usage.usage.weekly_pct;
    fill.style.width = `${Math.min(100, pct)}%`;
    fill.classList.toggle("over", pct >= threshold);
    let line = `This week <strong>${Math.round(pct)}%</strong> used, stops at ${threshold}%`;
    if (usage.usage.weekly_resets) line += `, resets ${when(usage.usage.weekly_resets)}`;
    if (usage.usage.five_hour_pct != null) line += `. 5-hour window ${Math.round(usage.usage.five_hour_pct)}%`;
    text.innerHTML = line;
  } else {
    fill.style.width = "0";
    text.textContent = usage.error ? `Usage unknown: ${usage.error}` : "Reading usage...";
  }

  const busy = runner.state !== "idle";
  $("#run-now").textContent = busy && runner.trigger === "away" ? "Keep running when I am back" : "Run now";
  $("#run-now").disabled = busy && runner.trigger !== "away";
  $("#run-menu-btn").disabled = busy && runner.trigger !== "away";
  $("#stop-wrap").hidden = !busy;
  $("#stop").disabled = runner.state === "stopping";
  $("#stop").textContent = runner.stop_mode === "after" ? "Stopping after this task" : "Stop after this task";

  $("#status-line").innerHTML = statusLine();
  $("#away-toggle").checked = settings.away_enabled;
  let away = `Start when I am away (after ${settings.away_minutes} min of screensaver)`;
  if (presence && !presence.detector && presence.read_at) away = "Start when I am away (not available on this desktop)";
  $("#away-label").textContent = away;

  const box = $("#warnings");
  box.hidden = !warnings.length;
  box.innerHTML = warnings.map((w) => `<p>${esc(w)}</p>`).join("");
  document.documentElement.style.setProperty("--header-height", `${$(".top").offsetHeight}px`);
}

function statusLine() {
  const { runner, tasks } = state;
  const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
  if (runner.state !== "idle") {
    const task = byId[runner.task_id];
    const why = runner.trigger === "away" ? "because you are away" : "with Run now";
    let line = task
      ? `Working on <span class="live">${esc(task.title)}</span>, started ${why}.`
      : `Starting the queue ${why}.`;
    if (runner.until) line += ` No new task after ${when(runner.until)}.`;
    if (runner.stop_mode === "after") line += " Stops after this task.";
    if (runner.stop_mode === "now") line += " Stopping now.";
    return line;
  }
  const queued = tasks.filter((t) => t.status === "queued").length;
  let line = queued ? `Idle. ${queued} task${queued === 1 ? "" : "s"} in the queue.` : "Idle. The queue is empty.";
  if (runner.last_stop) line += ` Last run stopped ${when(runner.last_stop.at)}: ${esc(runner.last_stop.reason)}.`;
  return line;
}

function ticket(task) {
  const meta = [`<span>${esc(task.project_name)}</span>`];
  if (task.model) meta.push(`<span>${esc(task.model)}</span>`);
  if (task.effort) meta.push(`<span>effort ${esc(task.effort)}</span>`);
  if (!task.in_git) meta.push("<span>runs in place, no branch</span>");
  if (task.attempts && task.status !== "done") meta.push(`<span>attempt ${task.attempts}</span>`);
  if (task.status === "done") {
    meta.push(`<span>done ${when(task.finished_at)}</span>`);
    if (task.commits) meta.push(`<span>${task.commits} commit${task.commits === 1 ? "" : "s"}</span>`);
    if (task.decisions) meta.push(`<span>${task.decisions} decision${task.decisions === 1 ? "" : "s"} for you</span>`);
  }
  const movable = task.status === "queued" || task.status === "hold";
  let moves = "";
  if (task.status === "queued") {
    moves = `<div class="moves">
      <button class="btn small" data-move="top">Top</button>
      <button class="btn small" data-move="up">Up</button>
      <button class="btn small" data-move="down">Down</button>
      <button class="btn small" data-move="hold">Hold</button></div>`;
  } else if (task.status === "hold") {
    moves = `<div class="moves"><button class="btn small" data-move="queue">Queue it</button></div>`;
  } else if (task.status === "attention" || task.status === "done") {
    moves = `<div class="moves"><button class="btn small" data-move="retry">Run again</button></div>`;
  }
  const isNew = task.new && (task.status === "done" || task.status === "attention");
  return `<article class="ticket ${task.status}" data-id="${esc(task.id)}"
      draggable="${movable || task.status === "done" || task.status === "attention"}" tabindex="0">
    <div class="title">${esc(task.title)}${isNew ? '<span class="new">new</span>' : ""}</div>
    <div class="meta">${meta.join("")}</div>
    ${task.note && task.status !== "done" ? `<div class="note">${esc(task.note)}</div>` : ""}
    ${moves}
  </article>`;
}

function runningTicket(task) {
  const runner = state.runner;
  const started = runner.task_id === task.id && runner.task_started_at ? runner.task_started_at : task.updated_at;
  return `<article class="ticket running" data-id="${esc(task.id)}" tabindex="0">
    <div class="light"></div>
    <div class="inner">
      <div class="title">${esc(task.title)}</div>
      <div class="clock" data-since="${started}">${clock(nowSeconds() - started)}</div>
      <div class="meta"><span>${esc(task.project_name)}</span><span>attempt ${task.attempts + 1}</span>
        ${task.branch ? `<span>${esc(task.branch)}</span>` : ""}</div>
      <div class="tail" id="tail">${esc(tailCache.taskId === task.id ? tailCache.text : "")}</div>
    </div>
  </article>`;
}

function renderBoard() {
  const tasks = state.tasks;
  const counts = { queue: 0, hold: 0, running: 0, done: 0, attention: 0 };
  for (const list of $$(".list[data-status]")) {
    const status = list.dataset.status;
    const priority = Number(list.dataset.priority || 0);
    const items = tasks.filter((t) => t.status === status && (status !== "queued" || t.priority === priority));
    if (status === "done") items.sort((a, b) => (b.finished_at || 0) - (a.finished_at || 0));
    if (status === "attention") items.sort((a, b) => (b.finished_at || b.updated_at) - (a.finished_at || a.updated_at));
    list.innerHTML = items.map(ticket).join("");
  }
  const running = tasks.filter((t) => t.status === "running");
  $("#running-list").innerHTML = running.map(runningTicket).join("");
  for (const t of tasks) {
    if (t.status === "queued") counts.queue++;
    else if (t.status in counts) counts[t.status]++;
  }
  for (const el of $$("[data-count]")) {
    const key = el.dataset.count;
    let n = counts[key];
    if (key === "done" && el.closest(".tabs")) n = counts.done + counts.attention;
    el.textContent = n ? String(n) : "";
  }
  if (running.length) pollTail(running[0].id);
}

async function pollTail(taskId) {
  if (pollTail.busy) return;
  pollTail.busy = true;
  try {
    const data = await api("GET", `/api/tasks/${encodeURIComponent(taskId)}/log`);
    const lines = data.text.trimEnd().split("\n").slice(-8).join("\n");
    tailCache = { taskId, text: lines };
    const el = $("#tail");
    if (el) el.textContent = lines;
  } catch (err) { /* the next refresh tries again */ }
  pollTail.busy = false;
}

// Clocks on the running ticket tick every second without a refresh.
setInterval(() => {
  for (const el of $$(".clock[data-since]")) el.textContent = clock(nowSeconds() - Number(el.dataset.since));
}, 1000);

setInterval(() => {
  const running = state && state.tasks.find((t) => t.status === "running");
  if (running && document.visibilityState === "visible") pollTail(running.id);
}, 5000);

// --- moving tasks -----------------------------------------------------------

function siblings(task) {
  return state.tasks
    .filter((t) => t.status === task.status && t.priority === task.priority)
    .sort((a, b) => a.position - b.position);
}

async function moveTask(id, how) {
  const task = state.tasks.find((t) => t.id === id);
  if (!task) return;
  if (how === "top") return act(api("POST", `/api/tasks/${id}/action`, { action: "run-next" }), "Moved to the top of the queue.");
  if (how === "hold") return act(api("POST", `/api/tasks/${id}/action`, { action: "hold" }), "Put on hold.");
  if (how === "queue") return act(api("POST", `/api/tasks/${id}/action`, { action: "queue" }), "Added to the queue.");
  if (how === "retry") return act(api("POST", `/api/tasks/${id}/action`, { action: "retry" }), "Back in the queue.");
  const list = siblings(task);
  const index = list.findIndex((t) => t.id === id);
  if (how === "up") {
    if (index > 0) return act(api("POST", `/api/tasks/${id}/move`, { status: "queued", priority: task.priority, index: index - 1 }));
    if (task.priority > 1) return act(api("POST", `/api/tasks/${id}/move`, { status: "queued", priority: task.priority - 1 }));
  }
  if (how === "down") {
    if (index < list.length - 1) return act(api("POST", `/api/tasks/${id}/move`, { status: "queued", priority: task.priority, index: index + 1 }));
    if (task.priority < 3) return act(api("POST", `/api/tasks/${id}/move`, { status: "queued", priority: task.priority + 1, index: 0 }));
  }
  return null;
}

let dragId = null;

function setupDragAndDrop() {
  const board = $("#board");
  board.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".ticket[draggable='true']");
    if (!card) return;
    dragId = card.dataset.id;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragId);
  });
  board.addEventListener("dragend", (e) => {
    const card = e.target.closest(".ticket");
    if (card) card.classList.remove("dragging");
    $$(".list.over").forEach((l) => l.classList.remove("over"));
    dragId = null;
  });
  board.addEventListener("dragover", (e) => {
    const list = e.target.closest(".list.drop");
    if (!list || !dragId) return;
    e.preventDefault();
    $$(".list.over").forEach((l) => l !== list && l.classList.remove("over"));
    list.classList.add("over");
  });
  board.addEventListener("drop", (e) => {
    const list = e.target.closest(".list.drop");
    if (!list || !dragId) return;
    e.preventDefault();
    list.classList.remove("over");
    const cards = $$(".ticket", list).filter((c) => c.dataset.id !== dragId);
    let index = cards.length;
    for (let i = 0; i < cards.length; i++) {
      const box = cards[i].getBoundingClientRect();
      if (e.clientY < box.top + box.height / 2) { index = i; break; }
    }
    const body = { status: list.dataset.status, priority: Number(list.dataset.priority), index };
    act(api("POST", `/api/tasks/${dragId}/move`, body));
  });
}

// --- the detail panel -------------------------------------------------------

async function openTask(id, tab) {
  openTaskId = id;
  openTab = tab || "overview";
  logState = { run: null, offset: 0, text: "" };
  $("#panel").hidden = false;
  await loadDetail();
  const task = state.tasks.find((t) => t.id === id);
  if (task && task.new) api("POST", `/api/tasks/${id}/action`, { action: "seen" }).catch(() => {});
}

function closePanel() {
  openTaskId = null;
  detail = null;
  $("#panel").hidden = true;
}

async function loadDetail() {
  if (!openTaskId) return;
  try {
    detail = await api("GET", `/api/tasks/${encodeURIComponent(openTaskId)}`);
  } catch (err) {
    toast(err.message);
    closePanel();
    return;
  }
  renderPanel();
}

function renderPanel() {
  if (!detail) return;
  const task = detail.task;
  $("#panel-title").textContent = task.title;
  for (const b of $$("#panel-tabs button")) b.classList.toggle("active", b.dataset.tab === openTab);
  const body = $("#panel-body");
  if (openTab === "overview") body.innerHTML = overviewHtml();
  if (openTab === "edit" && !body.querySelector("#edit-form[data-id='" + CSS.escape(task.id) + "']")) body.innerHTML = editHtml();
  if (openTab === "log") renderLog(body);
  if (openTab === "changes") renderChanges(body);
}

const STATUS_NAMES = { hold: "On hold", queued: "In the queue", running: "Running", done: "Done", attention: "Needs attention", archived: "Archived" };

function overviewHtml() {
  const { task, runs, commits } = detail;
  const actions = [];
  if (task.status === "queued") actions.push(["run-next", "Run next"], ["hold", "Put on hold"]);
  if (task.status === "hold") actions.push(["queue", "Add to the queue"], ["run-next", "Run next"]);
  if (task.status === "done" || task.status === "attention") actions.push(["retry", "Run again"]);
  if (task.status !== "running" && task.status !== "archived") actions.push(["archive", "Archive"]);
  if (task.status !== "running") actions.push(["delete", "Delete"]);
  const parts = [];
  parts.push(`<section><p>${STATUS_NAMES[task.status]}, ${esc(task.priority_name)} priority. Project <span class="mono">${esc(task.project_dir)}</span>.</p>
    ${task.note ? `<p class="note-box">${esc(task.note)}</p>` : ""}
    <div class="actions-bar">${actions.map(([a, label]) => `<button class="btn small${a === "delete" ? " danger" : ""}" data-action="${a}">${label}</button>`).join("")}</div></section>`);
  if (detail.summary_html) parts.push(`<section><h3>What Claude reports (SUMMARY.md)</h3><div class="summary">${detail.summary_html}</div></section>`);
  if (task.branch) {
    parts.push(`<section><h3>Branch</h3>
      <p><span class="mono">${esc(task.branch)}</span>, from ${esc(task.base_branch || "")} at <span class="mono">${esc((task.base_commit || "").slice(0, 10))}</span>. ${commits.length} commit${commits.length === 1 ? "" : "s"}.</p>
      ${task.worktree ? `<p>Worktree: <span class="mono">${esc(task.worktree)}</span></p>` : ""}
      ${detail.merge_command ? `<div class="copy"><code>${esc(detail.merge_command)}</code><button class="btn small" data-copy="${esc(detail.merge_command)}">Copy</button></div>` : ""}
      ${task.status !== "running" && task.status !== "queued" ? '<p><button class="btn small danger" data-action="delete-branch">Delete the branch</button></p>' : ""}
    </section>`);
  } else if (!task.in_git) {
    parts.push("<section><p class=\"hint\">This folder is not a git repository, so the task runs in the folder itself, without a branch.</p></section>");
  }
  if (runs.length) {
    parts.push(`<section><h3>Attempts</h3><table><tr><th>Started</th><th>Took</th><th>Result</th><th>Exit</th><th></th></tr>
      ${runs.map((r) => `<tr><td>${when(r.started_at)}</td><td>${r.ended_at ? duration(r.ended_at - r.started_at) : "running"}</td>
        <td>${esc(r.result || "")}</td><td>${r.exit_code ?? ""}</td>
        <td><button class="btn small" data-log="${r.id}">Log</button></td></tr>`).join("")}</table></section>`);
  }
  if (detail.state_md) parts.push(`<section><h3>Progress notes (STATE.md)</h3><pre>${esc(detail.state_md)}</pre></section>`);
  parts.push(`<section><p class="hint">Task files: <span class="mono">${esc(detail.task_dir)}</span></p></section>`);
  return parts.join("");
}

function editHtml() {
  const t = detail.task;
  const locked = t.status === "running";
  const fixed = t.attempts > 0;
  const opt = (value, label, current) => `<option value="${value}"${String(current ?? "") === value ? " selected" : ""}>${label}</option>`;
  return `<form id="edit-form" data-id="${esc(t.id)}" class="edit">
    ${locked ? '<p class="note-box">The task is running. Stop it to edit.</p>' : ""}
    <label>Title <input name="title" value="${esc(t.title)}" ${locked ? "disabled" : ""}></label>
    <label>Prompt <textarea name="prompt" rows="16" ${locked ? "disabled" : ""}>${esc(t.prompt)}</textarea></label>
    <label>Project folder <input name="project_dir" value="${esc(t.project_dir)}" ${locked || fixed ? "disabled" : ""}>
      ${fixed ? '<small class="hint">Fixed once the task has run, because its branch depends on it.</small>' : ""}</label>
    <div class="row">
      <label>Priority <select name="priority" ${locked ? "disabled" : ""}>${opt("1", "High", t.priority)}${opt("2", "Normal", t.priority)}${opt("3", "Low", t.priority)}</select></label>
      <label>Model <input name="model" value="${esc(t.model || "")}" placeholder="Default" ${locked ? "disabled" : ""}></label>
      <label>Effort <select name="effort" ${locked ? "disabled" : ""}>${opt("", "Default", t.effort)}${["low", "medium", "high", "xhigh", "max"].map((e) => opt(e, e, t.effort)).join("")}</select></label>
      <label>5-hour windows per attempt <input name="max_windows" value="${esc(t.max_windows || "")}" placeholder="Default" ${locked ? "disabled" : ""}></label>
      <label>Attempts <input name="max_attempts" type="number" min="1" value="${esc(t.max_attempts || "")}" placeholder="Default" ${locked ? "disabled" : ""}></label>
      <label>Stop at weekly % <input name="threshold" type="number" min="1" max="100" value="${esc(t.threshold || "")}" placeholder="Default" ${locked ? "disabled" : ""}></label>
    </div>
    <p class="form-error" id="edit-error" hidden></p>
    <div class="actions"><button class="btn primary" type="submit" ${locked ? "disabled" : ""}>Save changes</button></div>
  </form>`;
}

async function renderLog(body) {
  if (!body.querySelector(".log")) {
    body.innerHTML = '<p class="hint" id="log-hint"></p><pre class="log" id="log"></pre>';
  }
  await pollLog();
}

async function pollLog() {
  if (!openTaskId || openTab !== "log") return;
  const params = new URLSearchParams();
  if (logState.run) params.set("run", logState.run);
  if (logState.offset) params.set("offset", logState.offset);
  try {
    const data = await api("GET", `/api/tasks/${encodeURIComponent(openTaskId)}/log?${params}`);
    if (data.run !== logState.run) logState = { run: data.run, offset: 0, text: "" };
    logState.text += data.text;
    logState.offset = data.offset;
    logState.running = data.running;
    const el = $("#log");
    if (!el) return;
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = logState.text || "No log yet.";
    if (atBottom) el.scrollTop = el.scrollHeight;
    $("#log-hint").textContent = data.run ? `Attempt log ${data.run}${data.running ? ", live" : ""}.` : "The task has not run yet.";
  } catch (err) { /* try again on the next tick */ }
}

setInterval(() => { if (logState.running) pollLog(); }, 2000);

async function renderChanges(body) {
  const { task, files } = detail;
  if (!task.branch) {
    body.innerHTML = `<p class="hint">${task.in_git ? "No branch yet: the task has not run." : "Not a git repository, so there is no branch to compare."}</p>`;
    return;
  }
  const rows = files.map((f) => `<tr><td class="mono">${esc(f.file)}</td><td>${f.added ?? "bin"}</td><td>${f.removed ?? ""}</td></tr>`).join("");
  body.innerHTML = `<section><h3>${files.length} file${files.length === 1 ? "" : "s"} changed</h3>
    ${files.length ? `<table><tr><th>File</th><th>Added</th><th>Removed</th></tr>${rows}</table>` : ""}</section>
    <section><h3>Diff</h3><pre class="diff" id="diff">Loading...</pre></section>`;
  try {
    const text = await api("GET", `/api/tasks/${encodeURIComponent(task.id)}/diff`);
    $("#diff").innerHTML = text ? text.split("\n").map((line) => {
      let cls = "";
      if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
      else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
      else if (line.startsWith("@@")) cls = "hunk";
      return cls ? `<span class="${cls}">${esc(line)}</span>` : esc(line);
    }).join("\n") : "No changes yet.";
  } catch (err) {
    $("#diff").textContent = err.message;
  }
}

function setupPanel() {
  $("#panel-close").addEventListener("click", closePanel);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#panel").hidden && !document.querySelector("dialog[open]")) closePanel();
  });
  $("#panel-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    openTab = b.dataset.tab;
    $("#panel-body").innerHTML = "";
    renderPanel();
  });
  $("#panel-body").addEventListener("click", async (e) => {
    const action = e.target.closest("[data-action]");
    const copy = e.target.closest("[data-copy]");
    const log = e.target.closest("[data-log]");
    if (copy) {
      try { await navigator.clipboard.writeText(copy.dataset.copy); toast("Copied."); }
      catch (err) { toast("Copy failed; select the text instead."); }
    }
    if (log) {
      logState = { run: Number(log.dataset.log), offset: 0, text: "" };
      openTab = "log";
      $("#panel-body").innerHTML = "";
      renderPanel();
    }
    if (!action) return;
    const name = action.dataset.action;
    if (name === "delete" && !confirm("Delete this task? Its branch is kept; the task and its history are removed.")) return;
    if (name === "delete-branch" && !confirm("Delete the branch and its worktree? Commits that are not merged are lost.")) return;
    const messages = { "run-next": "Runs next.", hold: "Put on hold.", queue: "Added to the queue.", retry: "Back in the queue.", archive: "Archived.", delete: "Deleted.", "delete-branch": "Branch deleted." };
    const result = await act(api("POST", `/api/tasks/${encodeURIComponent(openTaskId)}/action`, { action: name }), messages[name]);
    if (!result) return;
    if (name === "delete" || name === "archive") closePanel();
    else loadDetail();
  });
  $("#panel-body").addEventListener("submit", async (e) => {
    if (e.target.id !== "edit-form") return;
    e.preventDefault();
    const form = new FormData(e.target);
    const changes = {};
    for (const [k, v] of form.entries()) {
      const before = detail.task[k];
      if (String(before ?? "") !== String(v)) changes[k] = v;
    }
    if (!Object.keys(changes).length) return toast("Nothing changed.");
    try {
      await api("PATCH", `/api/tasks/${encodeURIComponent(openTaskId)}`, changes);
      toast("Saved.");
      $("#panel-body").innerHTML = "";
      await refresh();
      await loadDetail();
    } catch (err) {
      const box = $("#edit-error");
      box.textContent = err.message;
      box.hidden = false;
    }
  });
}

// --- new task ---------------------------------------------------------------

async function openNewTask(status) {
  const dialog = $("#new-dialog");
  const form = $("#new-form");
  form.reset();
  draftState.token += 1;  // a draft still being written belongs to the old form
  draftState.prompt = "";
  draftState.title = "";
  form.description.rows = 3;
  setDrafting(false);
  $("#draft-status").textContent = DRAFT_HINT;
  $("#draft-status").className = "hint";
  form.prompt.value = state.settings.prompt_template ? state.settings.prompt_template + "\n" : "";
  form.status.value = status || "queued";
  $("#new-error").hidden = true;
  $("#project-info").textContent = "";
  $("#base-branch-wrap").hidden = true;
  let last = "";
  try { last = localStorage.getItem("backlog.lastProject") || ""; } catch (err) { /* private mode */ }
  form.project_dir.value = last;
  dialog.showModal();
  if (last) checkProject();
  try {
    const projects = await api("GET", "/api/projects");
    const all = [...new Set([...projects.recent, ...projects.folders])];
    $("#project-list").innerHTML = all.map((p) => `<option value="${esc(p)}">`).join("");
  } catch (err) { /* suggestions are optional */ }
  selectHint(form.prompt);
}

// Focus the prompt with its first hint selected, so typing replaces it.
function selectHint(field) {
  const hint = field.value.match(/^<.*>$/m);
  if (!hint) return field.focus();
  // Selecting before focusing makes the browser scroll the hint into view.
  field.blur();
  field.setSelectionRange(hint.index, hint.index + hint[0].length);
  field.focus();
}

// --- writing the prompt with Claude ---

const DRAFT_HINT = "Claude reads the project and fills in the prompt below. Or write the prompt yourself.";
const draftState = { token: 0, prompt: "", title: "", timer: null };

function setDrafting(on) {
  const button = $("#draft-button");
  button.disabled = on;
  button.textContent = on ? "Claude is writing..." : "Write the prompt with Claude";
  clearInterval(draftState.timer);
  if (!on) return;
  const started = Date.now();
  const status = $("#draft-status");
  status.className = "hint";
  const tick = () => {
    const s = Math.round((Date.now() - started) / 1000);
    status.textContent = `Claude is reading the project (${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}). This usually takes a minute or two.`;
  };
  tick();
  draftState.timer = setInterval(tick, 1000);
}

async function writeDraft() {
  const form = $("#new-form");
  const status = $("#draft-status");
  const description = form.description.value.trim();
  const project = form.project_dir.value.trim();
  const fail = (message) => { status.textContent = message; status.className = "hint warn"; };
  if (!project) return fail("Choose the project folder first.");
  if (!description) return fail("Describe the task first, in a few sentences.");
  const current = form.prompt.value.trim();
  const untouched = [(state.settings.prompt_template || "").trim(), draftState.prompt.trim(), ""];
  if (!untouched.includes(current) && !confirm("Replace the prompt you wrote with Claude's draft?")) return;
  const token = ++draftState.token;
  setDrafting(true);
  try {
    const result = await api("POST", "/api/draft", { description, project_dir: project });
    if (token !== draftState.token) return;
    form.prompt.value = result.prompt;
    draftState.prompt = result.prompt;
    const title = form.title.value.trim();
    if (result.title && (!title || title === draftState.title)) {
      form.title.value = result.title;
      draftState.title = result.title;
    }
    $("#new-error").hidden = true;
    const n = result.questions.length;
    if (n) {
      form.description.value = `${description}\n\nClaude asks (answer here, then write the prompt again):\n`
        + result.questions.map((q) => `- ${q}\n  Answer: `).join("\n");
      form.description.rows = Math.min(14, form.description.value.split("\n").length + 1);
      status.textContent = `Draft ready. Claude has ${n} ${n === 1 ? "question" : "questions"} in the description above: answer and write the prompt again, or add the task as it is.`;
    } else {
      status.textContent = "Draft ready. Read it, change what you like, and add the task.";
    }
  } catch (err) {
    if (token === draftState.token) fail(err.message);
  } finally {
    if (token === draftState.token) setDrafting(false);
  }
}

// Why the prompt cannot be added yet, or "" when it can.
function templateProblem(prompt) {
  const template = state.settings.prompt_template || "";
  const lines = template.split("\n").map((l) => l.trim());
  const hints = new Set(lines.filter((l) => /^<.*>$/.test(l)));
  const headings = new Set(lines.filter((l) => l.startsWith("#")));
  const left = prompt.split("\n").map((l) => l.trim());
  const unchanged = left.filter((l) => hints.has(l));
  if (unchanged.length) {
    const n = unchanged.length;
    return `${n} ${n === 1 ? "hint" : "hints"} from the template ${n === 1 ? "is" : "are"} still there. Replace or delete each line in <angle brackets>.`;
  }
  if (!left.some((l) => l && !headings.has(l))) return "Write the task in the prompt.";
  return "";
}

async function checkProject() {
  const form = $("#new-form");
  const path = form.project_dir.value.trim();
  const info = $("#project-info");
  info.className = "hint";
  if (!path) { info.textContent = ""; return; }
  try {
    const data = await api("GET", `/api/project-info?path=${encodeURIComponent(path)}`);
    const wrap = $("#base-branch-wrap");
    if (!data.exists) {
      info.textContent = "This folder does not exist.";
      info.className = "hint warn";
      wrap.hidden = true;
      return;
    }
    if (!data.git) {
      info.textContent = "Not a git repository: the task will run in this folder itself, without a branch.";
      info.className = "hint warn";
      wrap.hidden = true;
      return;
    }
    const select = $("#base-branch");
    select.innerHTML = data.branches.map((b) => `<option${b === data.branch ? " selected" : ""}>${esc(b)}</option>`).join("");
    wrap.hidden = !data.branches.length;
    info.textContent = `Git repository. The task gets its own branch, starting from ${data.branch || "the current commit"}.`;
    if (data.dirty) {
      info.textContent += " It has uncommitted changes; they are not part of the task's starting point.";
      info.className = "hint warn";
    }
  } catch (err) { info.textContent = err.message; }
}

function setupNewTask() {
  for (const b of $$("[data-new]")) b.addEventListener("click", () => openNewTask(b.dataset.new));
  const form = $("#new-form");
  form.project_dir.addEventListener("change", checkProject);
  $("#draft-button").addEventListener("click", writeDraft);
  $("#new-submit").addEventListener("click", async () => {
    if (!form.reportValidity()) return;
    const data = Object.fromEntries(new FormData(form).entries());
    for (const k of Object.keys(data)) if (data[k] === "") delete data[k];
    if (data.max_attempts) data.max_attempts = Number(data.max_attempts);
    if ($("#base-branch-wrap").hidden) delete data.base_branch;
    delete data.description;
    const problem = templateProblem(data.prompt || "");
    if (problem) {
      const box = $("#new-error");
      box.textContent = problem;
      box.hidden = false;
      selectHint(form.prompt);
      return;
    }
    try {
      const result = await api("POST", "/api/tasks", data);
      try { localStorage.setItem("backlog.lastProject", data.project_dir); } catch (err) { /* private mode */ }
      $("#new-dialog").close();
      toast(`Added "${result.task.title}".`);
      await refresh();
    } catch (err) {
      const box = $("#new-error");
      box.textContent = err.message;
      box.hidden = false;
    }
  });
}

// --- settings ---------------------------------------------------------------

async function openSettings() {
  const form = $("#settings-form");
  const s = state.settings;
  form.threshold.value = s.threshold;
  form.away_enabled.checked = s.away_enabled;
  form.away_minutes.value = s.away_minutes;
  form.on_return.value = s.on_return;
  form.model.value = s.model;
  form.effort.value = s.effort;
  form.max_windows.value = s.max_windows;
  form.max_attempts.value = s.max_attempts;
  form.project_roots.value = s.project_roots.join("\n");
  form.tailscale_login.value = s.tailscale_login;
  form.prompt_template.value = s.prompt_template;
  $("#settings-error").hidden = true;
  const p = state.presence;
  $("#presence-info").textContent = !p ? "" : p.detector
    ? `Detected: ${p.detector}. ${p.away_seconds ? `The screensaver has been on for ${duration(p.away_seconds)}.` : "The screensaver is off."}`
    : "No screensaver could be detected on this desktop, so starting when away does not work here.";
  $("#settings-dialog").showModal();
  const phone = $("#phone-info");
  phone.textContent = "Checking Tailscale...";
  try {
    const info = await api("GET", "/api/phone");
    if (!info.backend) {
      phone.innerHTML = '<p class="hint">Tailscale is not installed or not running. Install it on this computer and your phone, log in to the same account, then run <span class="mono">claude-backlog install --phone</span>.</p>';
    } else if (info.backend !== "Running") {
      phone.innerHTML = `<p class="hint">Tailscale is ${esc(info.backend.toLowerCase())}. Start it with <span class="mono">tailscale up</span>, then run <span class="mono">claude-backlog install --phone</span>.</p>`;
    } else {
      if (!form.tailscale_login.value && info.login) form.tailscale_login.placeholder = info.login;
      phone.innerHTML = `<p>On your phone, open <a href="${esc(info.url)}">${esc(info.url)}</a> (after <span class="mono">claude-backlog install --phone</span>) and add it to the home screen.</p>
        <img src="/api/phone/qr.svg" alt="QR code of the phone address" width="168" height="168">`;
    }
  } catch (err) { phone.textContent = err.message; }
}

function setupSettings() {
  $("#open-settings").addEventListener("click", openSettings);
  $("#settings-save").addEventListener("click", async () => {
    const form = $("#settings-form");
    if (!form.reportValidity()) return;
    const changes = {
      threshold: Number(form.threshold.value),
      away_enabled: form.away_enabled.checked,
      away_minutes: Number(form.away_minutes.value),
      on_return: form.on_return.value,
      model: form.model.value,
      effort: form.effort.value,
      max_windows: form.max_windows.value,
      max_attempts: Number(form.max_attempts.value),
      project_roots: form.project_roots.value.split("\n").map((s) => s.trim()).filter(Boolean),
      tailscale_login: form.tailscale_login.value,
      prompt_template: form.prompt_template.value,
    };
    try {
      await api("PUT", "/api/settings", changes);
      $("#settings-dialog").close();
      toast("Settings saved.");
      await refresh();
    } catch (err) {
      const box = $("#settings-error");
      box.textContent = err.message;
      box.hidden = false;
    }
  });
  $("#template-reset").addEventListener("click", () => {
    $("#settings-form").prompt_template.value = state.default_prompt_template;
  });
  $("#meter-mark").addEventListener("click", async () => {
    const value = prompt("Stop the queue when weekly usage reaches (%):", state.settings.threshold);
    if (value === null) return;
    const n = Number(value);
    if (!(n >= 1 && n <= 100)) return toast("Enter a number from 1 to 100.");
    act(api("PUT", "/api/settings", { threshold: n }), `The queue now stops at ${n}%.`);
  });
  $("#away-toggle").addEventListener("change", (e) => {
    const on = e.target.checked;
    act(api("PUT", "/api/settings", { away_enabled: on }), on ? "Starts by itself when you are away." : "Starts only when you press Run now.");
  });
}

// --- run controls -----------------------------------------------------------

function setupRunControls() {
  $("#run-now").addEventListener("click", () => {
    const keep = state.runner.state !== "idle";
    act(api("POST", "/api/run", {}), keep ? "It keeps running when you come back." : "Started.");
  });
  $("#run-menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#run-menu").hidden = !$("#run-menu").hidden;
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".split")) $("#run-menu").hidden = true;
  });
  $("#run-until-go").addEventListener("click", () => {
    const until = $("#run-until").value;
    if (!until) return toast("Pick a time first.");
    $("#run-menu").hidden = true;
    act(api("POST", "/api/run", { until }), `Started. No new task after ${until}.`);
  });
  $("#stop").addEventListener("click", () => act(api("POST", "/api/stop", { now: false }), "Stops after this task."));
  $("#stop-now").addEventListener("click", () => {
    if (!confirm("Stop the running task now? It goes back to the queue and continues from its notes next time.")) return;
    act(api("POST", "/api/stop", { now: true }), "Stopping now.");
  });
}

// --- board events, tabs, live updates ---------------------------------------

function setupBoard() {
  $("#board").addEventListener("click", (e) => {
    const move = e.target.closest("[data-move]");
    const card = e.target.closest(".ticket");
    if (move && card) {
      e.stopPropagation();
      moveTask(card.dataset.id, move.dataset.move);
      return;
    }
    if (card) openTask(card.dataset.id);
  });
  $("#board").addEventListener("keydown", (e) => {
    const card = e.target.closest(".ticket");
    if (card && (e.key === "Enter" || e.key === " ") && e.target === card) {
      e.preventDefault();
      openTask(card.dataset.id);
    }
  });
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-col]");
    if (!b) return;
    for (const t of $$("#tabs button")) t.classList.toggle("active", t === b);
    for (const c of $$(".col")) c.classList.toggle("active", c.dataset.col === b.dataset.col);
    try { localStorage.setItem("backlog.tab", b.dataset.col); } catch (err) { /* private mode */ }
  });
  try {
    const tab = localStorage.getItem("backlog.tab");
    const button = tab && $(`#tabs button[data-col="${tab}"]`);
    if (button) button.click();
  } catch (err) { /* private mode */ }
}

async function refresh() {
  try {
    const data = await api("GET", "/api/state");
    data.receivedAt = Date.now();
    state = data;
    render();
    if (openTaskId && openTab !== "edit" && openTab !== "log") loadDetail();
  } catch (err) {
    $("#status-line").textContent = `The board cannot reach the service: ${err.message}`;
  }
}

function listen() {
  const source = new EventSource("/api/events");
  let timer = null;
  source.addEventListener("change", () => {
    clearTimeout(timer);
    timer = setTimeout(refresh, 150);
  });
  source.onerror = () => {
    $("#status-line").textContent = "Connection to the service lost; reconnecting...";
  };
}

setupDragAndDrop();
setupPanel();
setupNewTask();
setupSettings();
setupRunControls();
setupBoard();
refresh();
listen();
setInterval(refresh, 60000);
