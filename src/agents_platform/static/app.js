/* No framework or browser-side secrets: every operation uses the scoped API. */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const e = escapeHTML;
const pretty = (value) => e(JSON.stringify(value, null, 2));
const short = (id) => (id ? `TASK-${id.slice(0, 8).toUpperCase()}` : "—");
const number = (n) => new Intl.NumberFormat().format(n || 0);
const date = (d, full = false) =>
  d
    ? new Date(d).toLocaleString(
        undefined,
        full
          ? { dateStyle: "medium", timeStyle: "short" }
          : { month: "short", day: "numeric" },
      )
    : "—";
const statusName = (s) =>
  s
    ? s
        .toLowerCase()
        .replaceAll("_", " ")
        .replace(/^./, (c) => c.toUpperCase())
    : "Unknown";
const badge = (s) =>
  `<span class="badge ${e((s || "").toLowerCase())}">${e(statusName(s))}</span>`;
const closed = (t) => ["COMPLETED", "FAILED", "CANCELLED"].includes(t.status);
const names = {
  supervisor: "Case supervisor",
  medical_records_collector: "Medical records",
  client_check_in: "Client check-in",
};
const agent = (t) =>
  `<span class="agent-cell"><span class="agent-initial" aria-hidden="true">${t.agent_key === "client_check_in" ? "CI" : "MR"}</span>${e(names[t.agent_key] || (t.task_type === "case.coordination" ? "Case supervisor" : t.agent_key))} <small>v${e(t.agent_version || 1)}</small></span>`;
const state = {
  key: sessionStorage.getItem("operatorKey") || "local-development-only",
  firm: null,
  case: null,
  view: "tasks",
  layout: "list",
  tasks: [],
  next: null,
  activity: [],
  activityNext: null,
  overview: null,
  workspace: null,
  selected: null,
  detail: null,
  tab: "activity",
  events: [],
  eventsNext: null,
  runs: [],
  mentions: new Set(),
  detailHash: "",
  busy: false,
  epoch: 0,
  detailEpoch: 0,
};
let toastTimer;
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 4000);
}
function error(text) {
  $("#error").textContent = text;
  $("#error").hidden = !text;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${state.key}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message =
        typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail);
    } catch {}
    if ([401, 403].includes(response.status) && !$("#connect-modal").open)
      $("#connect-modal").showModal();
    const failure = new Error(message);
    failure.status = response.status;
    throw failure;
  }
  return response.json();
}
const root = () => `/v1/operator/firms/${state.firm}`;
const taskRoot = (id) => `/v1/firms/${state.firm}/tasks/${id}`;
function query(fields) {
  return new URLSearchParams(
    Object.entries(fields).filter(
      ([, v]) => v !== null && v !== undefined && v !== "",
    ),
  ).toString();
}
function mergeRows(old, incoming) {
  return [
    ...new Map([...old, ...incoming].map((x) => [x.id, x])).values(),
  ].sort(
    (a, b) =>
      b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id),
  );
}
async function connect() {
  error("");
  try {
    state.workspace = await api("/v1/operator/workspace");
    $("#firm").innerHTML = state.workspace.firms
      .map((f) => `<option value="${e(f.id)}">${e(f.name)}</option>`)
      .join("");
    const requestedFirm = new URLSearchParams(location.search).get("firm");
    if (state.workspace.firms.some((f) => f.id === requestedFirm))
      state.firm = requestedFirm;
    state.firm = state.workspace.firms.some((f) => f.id === state.firm)
      ? state.firm
      : state.workspace.firms[0]?.id;
    if (!state.firm) {
      $("#content").innerHTML =
        '<div class="empty"><strong>No firms yet</strong>Run agents-seed to create the fictional demo workspace.</div>';
      return;
    }
    $("#firm").value = state.firm;
    $("#mode").textContent =
      state.workspace.mode === "openai" ? "OpenAI model" : "Scripted model";
    await refresh(true);
    const initial = new URLSearchParams(location.search).get("task");
    if (initial) await openTask(initial);
  } catch (err) {
    error(err.message);
    $("#system-label").textContent = "Connection unavailable";
  }
}
async function refresh(reset = false) {
  if (!state.firm) return;
  if (state.busy) {
    if (reset) state.pendingRefresh = true;
    return;
  }
  state.busy = true;
  const epoch = state.epoch;
  try {
    const [overview, page, activity] = await Promise.all([
      api(`${root()}/overview`),
      api(
        `${root()}/tasks?${query({ case_id: state.case, status: $("#status").value })}`,
      ),
      api(`${root()}/activity?${query({ case_id: state.case })}`),
    ]);
    if (epoch !== state.epoch) return;
    state.overview = overview;
    const retainTasks = !reset && state.tasks.length > 100;
    state.tasks = retainTasks ? mergeRows(state.tasks, page.items) : page.items;
    if (!retainTasks) state.next = page.next_cursor;
    const retainActivity = !reset && state.activity.length > 100;
    state.activity = retainActivity
      ? mergeRows(state.activity, activity.items)
      : activity.items;
    if (!retainActivity) state.activityNext = activity.next_cursor;
    render();
    error("");
    $("#last-sync").textContent =
      `Synced ${new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} / live`;
    $("#system-label").textContent = "Workspace connected";
    if (state.selected) await refreshDetail();
  } catch (err) {
    error(err.message);
    $("#system-label").textContent = "Connection unavailable";
  } finally {
    state.busy = false;
    if (state.pendingRefresh) {
      state.pendingRefresh = false;
      refresh(true);
    }
  }
}
function selectedCase() {
  return state.overview?.cases.find((c) => c.id === state.case);
}
function taskCounts() {
  return selectedCase()?.counts || state.overview.counts;
}
function render() {
  const counts = taskCounts();
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const attention = (counts.BLOCKED || 0) + (counts.FAILED || 0);
  $("#nav-count").textContent = number(
    Object.values(state.overview.counts).reduce((a, b) => a + b, 0),
  );
  $("#case-count").textContent = state.overview.cases.length;
  $("#case-nav").innerHTML = state.overview.cases
    .map(
      (c) =>
        `<button class="case-nav ${state.case === c.id ? "active" : ""}" data-case="${e(c.id)}"><div>${e(c.number)}<br><small>${e(c.title)}</small></div></button>`,
    )
    .join("");
  $("#metrics").innerHTML = [
    ["Total tasks", total, "In selected scope", ""],
    [
      "In progress",
      (counts.RUNNING || 0) + (counts.PENDING || 0),
      `${number(counts.WAITING)} waiting for a response`,
      "",
    ],
    [
      "Needs review",
      attention,
      attention ? "Blocked or failed" : "No blockers",
      attention ? "attention" : "",
    ],
    [
      "Completed",
      counts.COMPLETED || 0,
      `${number(state.overview.executions)} executions / ${number(state.overview.tokens)} tokens across firm`,
      "",
    ],
  ]
    .map(
      ([label, value, note, cls]) =>
        `<article class="metric"><div class="metric-top">${label}</div><div class="metric-number ${cls}">${number(value)}</div><div class="metric-note">${note}</div></article>`,
    )
    .join("");
  const viewTitles = {
    tasks: [
      "Tasks",
      "Assign work, review blockers, and follow agent progress.",
      "All tasks",
    ],
    supervisor: [
      "Supervisor",
      "Review case progress and tasks that need a person.",
      "Supervisor",
    ],
    activity: [
      "Activity & audit",
      "Comments, agent decisions, and provider updates.",
      "Activity & audit",
    ],
    agents: [
      "Agents",
      "Published agents and their available workflows.",
      "Agents",
    ],
  };
  const [title, desc, crumb] = viewTitles[state.view];
  $("#page-title").textContent =
    state.case && state.view === "tasks"
      ? selectedCase()?.number || title
      : title;
  $("#page-description").textContent =
    state.case && state.view === "tasks" ? selectedCase()?.title || desc : desc;
  $("#crumb-title").textContent = crumb;
  $$(".nav").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === state.view && !state.case),
  );
  $("#view-tabs").hidden = state.view !== "tasks";
  $("#section-label").hidden = state.view === "tasks";
  $("#section-label").textContent = crumb;
  $(".filters").hidden = !["tasks"].includes(state.view);
  const term = $("#search").value.trim().toLowerCase();
  const tasks = state.tasks.filter((t) =>
    `${t.objective} ${t.id} ${short(t.id)}`.toLowerCase().includes(term),
  );
  if (state.view === "tasks") renderTasks(tasks);
  if (state.view === "supervisor") renderSupervisor();
  if (state.view === "activity")
    $("#content").innerHTML =
      `<div class="audit-feed">${state.activity.length ? state.activity.map(eventHTML).join("") : '<div class="empty">No activity yet. Create a task to get started.</div>'}</div>`;
  if (state.view === "agents")
    $("#content").innerHTML =
      `<div class="agent-grid">${state.workspace.agents.map((a) => `<article class="agent-card"><span class="agent-initial">${a.key === "supervisor" ? "S" : a.key === "client_check_in" ? "CI" : "MR"}</span><h3>${e(a.name)}</h3><p>${e(a.capability)}</p><small>${e(a.key)}</small><p>${a.key === "medical_records_collector" ? "Published v2 / task-scoped tools / bounded executions / durable follow-ups" : a.key === "supervisor" ? "Coordinates approved case objectives through independent durable specialist tasks." : "Published v1 / scheduled simulated outreach / attributed replies / meaningful-update comparison."}</p></article>`).join("")}</div>`;
  $("#load-more").hidden = !(state.view === "tasks"
    ? state.next
    : state.view === "activity"
      ? state.activityNext
      : false);
  $("#result-count").textContent =
    state.view === "tasks"
      ? `${number(tasks.length)} loaded tasks${term ? " matching search" : ""} / ${number(total)} in scope`
      : state.view === "activity"
        ? `${number(state.activity.length)} loaded events`
        : "Computed from persisted platform data";
}
function caseName(t) {
  return state.overview.cases.find((c) => c.id === t.case_id)?.number || "Case";
}
function taskSub(t) {
  return `<span class="task-sub"><code>${short(t.id)}</code><span class="meta-separator" aria-hidden="true">/</span><span>${e(caseName(t))}</span>${t.parent_task_id ? '<span class="meta-separator" aria-hidden="true">/</span><span>Subtask</span>' : ""}</span>`;
}
function renderTasks(tasks) {
  if (!tasks.length) {
    $("#content").innerHTML =
      `<div class="empty"><strong>${state.tasks.length ? "No matching tasks" : "No tasks yet"}</strong>${state.tasks.length ? "Try another search or status filter." : "Create a task to assign work to an agent."}${!state.tasks.length ? '<br><button class="primary" data-create>Create a task</button>' : ""}</div>`;
    return;
  }
  if (state.layout === "board") {
    const cols = [
      ["To do", ["PENDING"]],
      ["In motion", ["RUNNING", "WAITING"]],
      ["Needs attention", ["BLOCKED", "FAILED"]],
      ["Closed", ["COMPLETED", "CANCELLED"]],
    ];
    $("#content").innerHTML = `<div class="board">${cols
      .map(([title, statuses]) => {
        const items = tasks.filter((t) => statuses.includes(t.status));
        return `<section class="column"><div class="column-title">${title}<b>${items.length}</b></div>${items.map((t) => `<button class="task-card" data-task="${e(t.id)}">${badge(t.status)}<span class="task-title">${e(t.objective)}</span><div class="task-card-bottom">${taskSub(t)}<span class="agent-initial" aria-hidden="true">MR</span></div></button>`).join("")}</section>`;
      })
      .join("")}</div>`;
    return;
  }
  $("#content").innerHTML =
    `<table class="task-table"><thead><tr><th>Task</th><th>Status</th><th>Assigned agent</th><th>Created</th><th>Version</th></tr></thead><tbody>${tasks.map((t) => `<tr><td><button class="task-name" data-task="${e(t.id)}"><span class="task-check ${t.status === "COMPLETED" ? "done" : ""}">${t.status === "COMPLETED" ? "✓" : ""}</span><span><span class="task-title">${e(t.objective)}</span>${taskSub(t)}</span></button></td><td>${badge(t.status)}</td><td>${agent(t)}</td><td>${e(date(t.created_at))}</td><td>v${e(t.agent_version)}</td></tr>`).join("")}</tbody></table>`;
}
function renderSupervisor() {
  const cases = state.overview.cases.filter(
    (c) => !state.case || state.case === c.id,
  );
  const needs = state.tasks.filter((t) =>
    ["BLOCKED", "FAILED"].includes(t.status),
  );
  const mentions = state.activity
    .filter((ev) => ev.payload.mentions?.includes("supervisor"))
    .slice(0, 8);
  $("#content").innerHTML =
    `<div class="supervisor-grid"><div class="notice"><strong>Operator overview</strong> / The supervisor delegates approved records scope and reviews material outcomes. Client-report reviews can be decided on their task. Other review questions still need explicit operator guidance or objective revision; comments are not approval.</div><section><div class="section-heading">Case progress <span>${cases.length} cases</span></div>${cases
      .map((c) => {
        const total = Object.values(c.counts).reduce((a, b) => a + b, 0),
          done = c.counts.COMPLETED || 0,
          blocked = (c.counts.BLOCKED || 0) + (c.counts.FAILED || 0);
        return `<article class="case-card"><div class="case-card-top">${e(c.number)} / ${e(c.status)}</div><h3>${e(c.title)}</h3><p>${
          state.overview.groups
            .filter((g) => g.case_id === c.id)
            .map((g) => e(g.objective))
            .join("<br>") || "No parent task"
        }</p>${supervisorPanel(c)}<div class="progress-track"><i style="width:${total ? Math.round((done / total) * 100) : 0}%"></i></div><div class="case-stats"><span>${done} of ${total} tasks complete</span><span>${blocked} need attention</span></div><button class="text-button" data-case="${e(c.id)}" style="margin-top:14px">View case tasks</button></article>`;
      })
      .join(
        "",
      )}</section><section><div class="section-heading">Needs review <span>Loaded tasks</span></div>${needs.length ? needs.map((t) => `<button class="attention-row" data-task="${e(t.id)}">${badge(t.status)}<span class="task-title">${e(t.objective)}</span><p>${e(t.progress.last_reason || t.progress.last_summary || "Review the latest execution and add guidance.")}</p>${taskSub(t)}</button>`).join("") : '<div class="attention-empty">No blockers in the loaded tasks.<br>Case totals above include all persisted tasks.</div>'}<div class="section-heading" style="margin-top:28px">Supervisor mentions <span>Recent activity</span></div>${mentions.length ? mentions.map(eventHTML).join("") : '<div class="attention-empty">Tag @supervisor in a comment to wake the configured case coordinator.</div>'}</section></div>`;
}
const eventTitles = {
  "client.review_decided": "Teammate recorded a client-report decision",
  "case.objective_submitted": "Case objective submitted",
  "supervisor.review_completed": "Supervisor reviewed case",
  "supervisor.failed": "Supervisor needs recovery",
  "task.created": "Task assigned",
  "task.comment_added": "Comment added",
  "provider.response_received": "Provider update received",
  "agent.execution_completed": "Execution completed",
  "task.activation_completed": "Agent reported back",
  "task.failed": "Task failed",
  "task.waiting": "Follow-up scheduled",
  "task.completed": "Task completed",
  "task.blocked": "Human review needed",
  "task.running": "Agent started",
};
function eventHTML(ev) {
  const type = ev.type || ev.event_type,
    payload = ev.payload || {};
  const title =
    eventTitles[type] || type.replaceAll(".", " / ").replaceAll("_", " ");
  const decision = payload.decision;
  const text =
    payload.text ||
    payload.summary ||
    decision?.summary ||
    payload.reason ||
    payload.assignment?.objective ||
    "";
  return `<article class="feed-item"><div class="feed-body"><div class="feed-meta"><span class="feed-title">${e(title)}</span><time>${e(date(ev.created_at, true))}</time></div><div class="task-sub">${e(ev.actor || ev.actor_ref || "platform")}${ev.task_id ? `<span class="meta-separator" aria-hidden="true">/</span><button class="task-link" data-task="${e(ev.task_id)}">${short(ev.task_id)}</button>` : ""}</div>${text ? `<p class="feed-text">${e(text)}</p>` : ""}${payload.mentions?.length ? `<div class="feed-tags">${payload.mentions.map((m) => `<span class="mention">@${e(m)}</span>`).join("")}${payload.woke_agent ? '<span class="mention">Wake requested</span>' : ""}</div>` : ""}<details><summary>Event payload / ${e(ev.id?.slice(0, 8))}</summary><pre>${pretty(payload)}</pre></details></div></article>`;
}
async function openTask(id) {
  if (state.selected !== id) {
    state.detailEpoch++;
    state.detailHash = "";
    state.mentions.clear();
    $("#comment").value = "";
    $("#wake").checked = false;
    state.tab = "activity";
    state.events = [];
    state.eventsNext = null;
  }
  state.selected = id;
  $("#drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
  document.body.style.overflow = "hidden";
  $("#detail-id").textContent = short(id);
  $("#detail-heading").innerHTML =
    '<div class="inline-note">Loading task…</div>';
  history.replaceState(
    null,
    "",
    `?firm=${encodeURIComponent(state.firm)}&task=${encodeURIComponent(id)}`,
  );
  try {
    await refreshDetail(true);
    $("#close-detail").focus();
  } catch (err) {
    toast(err.message);
  }
}
async function refreshDetail(force = false) {
  const id = state.selected,
    epoch = state.detailEpoch,
    firm = state.firm;
  if (!id) return;
  const [task, events, runs, memory] = await Promise.all([
    api(taskRoot(id)),
    api(`${root()}/activity?${query({ task_id: id })}`),
    api(`${taskRoot(id)}/executions`),
    api(`${taskRoot(id)}/memory`),
  ]);
  if (
    state.selected !== id ||
    epoch !== state.detailEpoch ||
    firm !== state.firm
  )
    return;
  const hash = JSON.stringify([task, events, runs, memory]);
  if (!force && hash === state.detailHash) return;
  state.detailHash = hash;
  state.detail = task;
  state.runs = runs;
  state.memory = memory;
  const retaining = state.events.length > 100;
  state.events = retaining
    ? mergeRows(state.events, events.items)
    : events.items;
  if (!retaining) state.eventsNext = events.next_cursor;
  renderDetail();
}
function renderDetail() {
  const t = state.detail,
    latest = state.runs.at(-1),
    outcome = latest?.outcome;
  const group = state.overview.groups.find((g) => g.id === t.parent_task_id);
  const reason =
    outcome?.reason ||
    latest?.decision?.next_action?.reason ||
    latest?.decision?.summary ||
    t.progress_json.last_summary ||
    "";
  $("#detail-id").textContent = short(t.id);
  $("#detail-heading").innerHTML =
    `${badge(t.task_type === "case.coordination" ? state.overview.supervisors?.find((s) => s.group_task_id === t.id)?.status || t.status : t.status)}<h2>${e(t.objective)}</h2><dl class="detail-props"><dt>Task ID</dt><dd><code>${e(t.id)}</code></dd><dt>Assigned to</dt><dd>${agent(t)}</dd><dt>Case</dt><dd>${e(caseName(t))}</dd><dt>Part of</dt><dd>${e(group?.objective || "Standalone task")}</dd><dt>Next step</dt><dd>${e(reason || "Agent will assess the task.")}</dd>${outcome?.wake_at ? `<dt>Scheduled wake</dt><dd>${e(date(outcome.wake_at, true))}</dd>` : ""}<dt>Workflow</dt><dd><code>${e(t.workflow_id || state.overview.supervisors?.find((s) => s.group_task_id === t.id)?.workflow_id)}</code></dd></dl>${t.records_request ? `<div class="requirements">${t.records_request.requirements.map((r) => `<span>${e(statusName(r.record_type))} / ${r.all_dates ? "all dates" : `${e(r.from_date)} ${e(r.through_date)}`}</span>`).join("")}<span>${e(statusName(t.records_request.status))}</span></div>${!closed(t) ? '<button class="text-button" data-provider>Simulate provider response</button>' : ""}` : ""}`;
  $("#detail-heading").insertAdjacentHTML("beforeend", clientReviewHTML(t));
  $$(".detail-tabs button").forEach((b) =>
    b.classList.toggle("selected", b.dataset.tab === state.tab),
  );
  $("#mention-options").innerHTML = state.workspace.agents
    .map(
      (a) =>
        `<button type="button" data-mention="${e(a.key)}" class="${state.mentions.has(a.key) ? "selected" : ""}" aria-pressed="${state.mentions.has(a.key)}">@${e(a.key)}</button>`,
    )
    .join("");
  updateWake();
  renderDetailTab();
}
function updateWake() {
  const t = state.detail;
  const canWake =
    t &&
    !closed(t) &&
    (t.agent_key === "client_check_in" || (t.agent_key === "medical_records_collector" && t.agent_version >= 2)) &&
    state.mentions.has(t.agent_key);
  $("#wake").disabled = !canWake;
  if (!canWake) $("#wake").checked = false;
  $("#wake-help").textContent = canWake
    ? "Wake queues a durable activation with this comment as new context."
    : closed(t)
      ? "Task is closed. You can still add an audit comment."
      : "Tag the assigned medical-records agent to enable wake. @supervisor wakes the case coordinator when configured.";
}
function renderDetailTab() {
  const target = $("#detail-content");
  if (state.tab === "activity")
    target.innerHTML = state.events.length
      ? `${[...state.events].reverse().map(eventHTML).join("")}${state.eventsNext ? '<button class="text-button" data-older-events>Load older events</button>' : ""}`
      : '<div class="empty">No activity yet.</div>';
  if (state.tab === "executions")
    target.innerHTML = `<p class="inline-note">Latest 20 activations. Context, decisions, tool results, and actual token usage are saved per execution. Provider tools are simulated.</p>${state.runs.map((run, i) => `<details class="run" ${i === state.runs.length - 1 ? "open" : ""}><summary><span>Activation ${run.cycle + 1} / ${e(run.mode)}</span>${badge(run.status)}</summary><div class="run-meta">${e(date(run.created_at, true))} / ${e(run.id)} / attempt ${run.attempt} / ${number(run.usage?.total_tokens)} tokens / ${number(run.usage?.requests)} model calls</div><p>${e(run.decision?.summary || run.error_type || "Execution is in progress.")}</p>${run.decision?.next_action ? `<p><strong>${e(run.decision.next_action.type)}</strong> / ${e(run.decision.next_action.reason)}</p>` : ""}${run.outcome?.reason && run.outcome.reason !== run.decision?.next_action?.reason ? `<p class="inline-note">Backend: ${e(run.outcome.reason)}</p>` : ""}<h4>Tool calls (${run.tools.length})</h4>${run.tools.map((tool) => `<details class="run-tools"><summary>${e(tool.name)}<span class="mode">Simulated</span></summary><p>Idempotency key: <code>${e(tool.logical_key)}</code></p><pre>${pretty(tool.result)}</pre></details>`).join("")}<details><summary>Saved decision & committed outcome</summary><pre>${pretty({ decision: run.decision, outcome: run.outcome, usage: run.usage })}</pre></details></details>`).join("") || '<div class="empty">No executions yet. The worker will pick up the task.</div>'}`;
  if (state.tab === "context") {
    const run = state.runs.at(-1),
      ctx = run?.context;
    const snapshot = ctx
      ? `<p class="inline-note">Saved context for activation ${run.cycle + 1}. Includes scoped instructions and source-backed task history. Historical passages and a source-linked digest are saved when available; current restrictions remain separate.</p><div class="mini-label">Instructions</div>${(ctx.instructions || []).map((i) => `<div class="instructions"><strong>${e(statusName(i.scope))} / Revision ${i.revision}</strong><p>${e(i.text)}</p><code>${e(i.id)}</code></div>`).join("")}<div class="mini-label">Context sources / ${(ctx.sources || []).length}</div>${(ctx.sources || []).map((s) => `<details class="run"><summary>Source / ${e(s.id.slice(0, 8))}</summary><pre>${pretty(s)}</pre></details>`).join("") || '<p class="inline-note">No incoming message sources at the start of this activation. Sources returned by tools appear under Executions.</p>'}<details class="run"><summary>Full context snapshot</summary><pre>${pretty(ctx)}</pre></details><details class="run"><summary>Current task progress & completion criteria</summary><pre>${pretty({ progress: state.detail.progress_json, completion: state.detail.completion_json })}</pre></details>`
      : '<div class="empty">Context is assembled when the agent first runs.</div>';
    target.innerHTML = checkInHTML() + memoryHTML() + historyHTML() + snapshot;
  }
}

function memoryHTML() {
  const memory = state.memory;
  if (!memory || state.detail.kind !== "AGENT") return "";
  const editable = !closed(state.detail);
  return `<section class="task-memory"><h4>Operational memory</h4>
    <p class="inline-note">Open items always load, regardless of age. Case holds also cover future tasks. Task resolutions do not clear case holds.</p>
    ${[...(memory.case_restrictions || []), ...(memory.history || [])].map((item) => `<div class="instructions">
      <strong>${e(item.scope === "CASE" ? (item.provider_contact_id ? "Case / provider hold" : "Case-wide hold") : statusName(item.kind))} / ${e(statusName(item.status))}</strong>
      <p>${e(item.text)}</p><code>Source ${e(item.source_id.slice(0, 8))} / revision ${item.revision}</code>
      ${item.due_at ? `<p>Due ${e(date(item.due_at, true))}</p>` : ""}
      ${item.status === "OPEN" && editable ? `<form data-memory-form data-scope="${item.scope === "CASE" ? "CASE" : "TASK"}" data-item="${e(item.id)}">
        <label>Resolution reason<input name="text" required maxlength="2000" placeholder="What changed or was verified?"></label>
        <button type="submit" class="text-button">Resolve & wake agent</button></form>` : ""}
      ${item.resolution_reason ? `<p>Resolved: ${e(item.resolution_reason)}</p>` : ""}
    </div>`).join("") || '<p class="inline-note">No operational memory recorded.</p>'}
    ${editable ? `<details><summary>Add an obligation or contact restriction</summary>
      <form data-memory-form><label>Applies to<select name="scope"><option value="TASK">This task</option><option value="CASE">Entire case / contact hold</option>${state.detail.records_request ? '<option value="PROVIDER">This provider across the case / contact hold</option>' : ""}</select></label><label>Type<select name="kind"><option value="OBLIGATION">Obligation / requires resolution before completion</option><option value="STOP_CONTACT">Stop contact / blocks outreach immediately</option></select></label>
      <label>Details<textarea name="text" required maxlength="2000" rows="2" placeholder="Record a specific instruction and its reason"></textarea></label>
      <button type="submit" class="text-button">Save & wake agent</button></form></details>` : ""}
    <details><summary>Current memory manifest</summary><pre>${pretty(memory.manifest)}</pre></details>
  </section>`;
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-memory-form]");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button[type=submit]");
  const data = new FormData(form);
  const scope = form.dataset.scope || data.get("scope") || "TASK";
  const isCase = scope !== "TASK";
  const target = isCase ? `/v1/firms/${state.firm}/cases/${state.detail.case_id}/restrictions` : taskRoot(state.selected) + "/memory";
  const payload = {expected_version: isCase ? state.memory.case_memory_version : state.memory.task_version, text: data.get("text"),
    ...(form.dataset.item ? {operation: "RESOLVE", item_id: form.dataset.item} :
      isCase ? (scope === "PROVIDER" ? {provider_contact_id: state.detail.records_request.provider_contact_id} : {}) : {kind: data.get("kind")})};
  const intent = JSON.stringify({...payload, expected_version: undefined});
  // Preserve the original body/version on uncertain retries, even after a refresh.
  if (state.memoryWrite?.intent !== intent || state.memoryWrite?.target !== target)
    state.memoryWrite = {intent, body: JSON.stringify(payload), target, key: crypto.randomUUID()};
  button.disabled = true;
  try {
    await api(target, {method: "POST", headers: {"Idempotency-Key": state.memoryWrite.key}, body: state.memoryWrite.body});
    state.memoryWrite = null;
    toast("Memory saved. Agent wake queued.");
    await refreshDetail(true);
    await refresh();
  } catch (err) {
    if (err.status === 409) state.memoryWrite = null;
    toast(err.message);
  } finally {
    button.disabled = false;
  }
});


// Case scopes represent contact restrictions; obligations remain task-scoped.
document.addEventListener("change", (event) => {
  if (!event.target.matches("[data-memory-form] select[name=scope]")) return;
  const kind = event.target.form.querySelector("select[name=kind]");
  kind.disabled = event.target.value !== "TASK";
  if (kind.disabled) kind.value = "STOP_CONTACT";
});

function historyHTML() {
  const summary = state.memory?.summary;
  const search = state.memorySearch?.caseId === state.detail.case_id ? state.memorySearch : null;
  return `<section class="task-memory"><h4>Case history</h4>
    <p class="inline-note">Search saved evidence across this case. Historical text and digests do not grant contact permission or prove task completion.</p>
    ${summary ? `<details><summary>Source digest / revision ${summary.revision} / latest ${summary.covered_recent_sources} of ${summary.source_count} sources</summary>
      <p class="inline-note">Extractive excerpts with source links; this is a partial history view.</p>
      ${summary.entries.map((entry) => `<div class="instructions"><small>${e(date(entry.recorded_at, true))} / ${e(statusName(entry.kind))}</small><p>${e(entry.excerpt)}</p><button class="text-button" data-memory-source="${e(entry.source_id)}">Read source ${e(entry.source_id.slice(0, 8))}</button><div data-source-view></div></div>`).join("")}</details>` : '<p class="inline-note">The background worker is preparing the source digest.</p>'}
    <form data-history-search><label>Search case history<input name="q" required maxlength="500" placeholder="Authorization problem, provider promise, or source ID" value="${e(search?.query || "")}"></label><button class="text-button" type="submit">Search history</button></form>
    ${search?.result ? `<p class="inline-note">${search.result.method === "hybrid-rrf" ? "Keyword + semantic retrieval" : "Identifier + keyword retrieval"} / ${search.result.results.length} results</p>${search.result.results.map((hit) => `<div class="instructions"><small>${e(date(hit.recorded_at, true))} / ${e(statusName(hit.kind))} / ${e(hit.lanes.join(" + "))}</small><p>${e(hit.text)}</p><button class="text-button" data-memory-source="${e(hit.source_id)}">Read original source ${e(hit.source_id.slice(0, 8))}</button><div data-source-view></div></div>`).join("") || '<p class="inline-note">No indexed matches. New evidence is indexed in the background.</p>'}` : ""}
  </section>`;
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-history-search]");
  if (!form) return;
  event.preventDefault();
  const caseId = state.detail.case_id, firmId = state.firm;
  const q = new FormData(form).get("q").trim();
  if (!q) return;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const result = await api(`/v1/firms/${firmId}/cases/${caseId}/memory/search?q=${encodeURIComponent(q)}`);
    if (state.detail?.case_id !== caseId || state.firm !== firmId) return;
    state.memorySearch = {caseId, query:q, result};
    renderDetailTab();
  } catch (err) { toast(err.message); }
  finally { button.disabled = false; }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-memory-source]");
  if (!button) return;
  button.disabled = true;
  try {
    const source = await api(`/v1/firms/${state.firm}/cases/${state.detail.case_id}/sources/${button.dataset.memorySource}`);
    button.nextElementSibling.innerHTML = `<pre>${pretty(source)}</pre>`;
  } catch (err) { toast(err.message); }
  finally { button.disabled = false; }
});

function closeDetail() {
  state.selected = null;
  state.detail = null;
  state.detailEpoch++;
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
  document.body.style.overflow = "";
  history.replaceState(
    null,
    "",
    `${location.pathname}?firm=${encodeURIComponent(state.firm)}`,
  );
  $("#new-task").focus();
}
function navigate(view, caseId = null) {
  state.view = view;
  state.case = caseId;
  state.epoch++;
  state.tasks = [];
  state.activity = [];
  $("#search").value = "";
  $("#status").value = "";
  refresh(true);
}
async function loadMore() {
  const button = $("#load-more");
  button.disabled = true;
  try {
    const isTask = state.view === "tasks";
    const page = await api(
      `${root()}/${isTask ? "tasks" : "activity"}?${query({ case_id: state.case, status: isTask ? $("#status").value : null, before: isTask ? state.next : state.activityNext })}`,
    );
    if (isTask) {
      state.tasks = mergeRows(state.tasks, page.items);
      state.next = page.next_cursor;
    } else {
      state.activity = mergeRows(state.activity, page.items);
      state.activityNext = page.next_cursor;
    }
    render();
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
  }
}
function updateCreateKind() {
  const checkIn = $("#create-agent").value === "check_in";
  $("#create-provider-field").hidden = checkIn;
  $("#create-provider").required = !checkIn;
  $("#create-records-fields").hidden = checkIn;
  $("#create-checkin-fields").hidden = !checkIn;
  $("#from-date").required = !checkIn && !$("#all-dates").checked;
  $("#through-date").required = !checkIn && !$("#all-dates").checked;
  $("#create-objective").placeholder = checkIn ? "Check in with the client and report meaningful changes…" : "Collect records and imaging from the provider…";
}
$("#create-agent").addEventListener("change", updateCreateKind);

function checkInHTML() {
  const checkIn=state.detail?.client_check_in;
  if (!checkIn) return "";
  const report=checkIn.report;
  return `<section class="task-memory"><h4>Client check-in / ${e(statusName(checkIn.status))}</h4><p>Scheduled ${e(date(checkIn.scheduled_at,true))}${checkIn.sent_at ? ` / Sent ${e(date(checkIn.sent_at,true))}` : ""}</p><p>${e(checkIn.message)}</p>
    ${report ? `<div class="instructions"><strong>Client self-report / ${report.needs_review ? "Needs teammate review" : report.meaningful ? "Meaningful update" : "No material change"}</strong><p>${e(report.report.message)}</p><p>Pain: ${e(report.report.pain_score ?? "not reported")} / Daily function: ${e(statusName(report.report.daily_function))}</p><p>${e(report.report.treatment_update)} ${e(report.report.concern)}</p><p>${e(report.changes.reasons.join("; ") || "No configured material-change rule triggered.")}</p><code>Source ${e(report.source_id)}</code><details><summary>Comparison with previous client report</summary><pre>${pretty(report)}</pre></details></div>` : checkIn.sent_at && !closed(state.detail) ? '<button class="text-button" data-client-reply>Simulate client reply</button>' : '<p class="inline-note">The durable worker waits until the scheduled time before sending.</p>'}</section>`;
}

document.addEventListener("click", async (event) => {
  if (!event.target.closest("[data-client-reply]")) return;
  $("#client-reply-form").reset(); $("#client-reply-error").hidden=true;
  state.replyKey=null; $("#client-reply-modal").showModal();
});
$("#client-reply-form").addEventListener("input", () => state.replyKey=null);
$("#client-reply-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button=event.submitter; button.disabled=true;
  const data=new FormData(event.target);
  try {
    await api(`${taskRoot(state.selected)}/client-replies`, {method:"POST", headers:{"Idempotency-Key":(state.replyKey ||= crypto.randomUUID())},
      body:JSON.stringify({message:data.get("message"), pain_score:data.get("pain_score") === "" ? null : Number(data.get("pain_score")),
        daily_function:data.get("daily_function"), treatment_update:data.get("treatment_update"), treatment_changed:data.has("treatment_changed"),
        concern:data.get("concern"),requests_help:data.has("requests_help")})});
    $("#client-reply-modal").close(); state.replyKey=null; toast("Attributed reply saved and compared.");
    await refreshDetail(true); await refresh();
  } catch(err) {$("#client-reply-error").textContent=err.message; $("#client-reply-error").hidden=false;}
  finally {button.disabled=false;}
});

function openCreate() {
  if (!state.overview) return;
  state.createKey = null;
  state.createAt = null;
  $("#create-form").reset();
  $("#date-range").hidden = true;
  updateCreateKind();
  $("#create-error").hidden = true;
  $("#create-case").innerHTML = state.overview.cases
    .filter((c) => c.status === "OPEN")
    .map(
      (c) =>
        `<option value="${e(c.id)}">${e(c.number)}: ${e(c.title)}</option>`,
    )
    .join("");
  if (state.case) $("#create-case").value = state.case;
  updateCreateContacts();
  $("#create-modal").showModal();
}
function updateCreateContacts() {
  const caseId = $("#create-case").value,
    contacts = state.overview.contacts.filter((c) => c.case_id === caseId);
  $("#create-patient").innerHTML = contacts
    .filter((c) => c.roles.some((r) => ["CLIENT", "PATIENT"].includes(r)))
    .map((c) => `<option value="${e(c.id)}">${e(c.name)}</option>`)
    .join("");
  $("#create-provider").innerHTML = contacts
    .filter((c) => c.roles.includes("MEDICAL_PROVIDER"))
    .map((c) => `<option value="${e(c.id)}">${e(c.name)}</option>`)
    .join("");
  $("#create-parent").innerHTML =
    '<option value="">No parent task</option>' +
    state.overview.groups
      .filter((g) => g.case_id === caseId)
      .map((g) => `<option value="${e(g.id)}">${e(g.objective)}</option>`)
      .join("");
  if ($("#create-parent").options.length > 1)
    $("#create-parent").selectedIndex = 1;
}
$("#create-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.submitter;
  button.disabled = true;
  $("#create-error").hidden = true;
  try {
    if ($("#create-agent").value === "check_in") {
      const scheduled = $("#checkin-at").value ? new Date($("#checkin-at").value).toISOString() : (state.createAt ||= new Date().toISOString());
      const task = await api(`/v1/firms/${state.firm}/tasks/client-check-in`, {
        method:"POST", headers:{"Idempotency-Key": (state.createKey ||= crypto.randomUUID())},
        body: JSON.stringify({case_id:$("#create-case").value, client_contact_id:$("#create-patient").value,
          parent_task_id:$("#create-parent").value || null, scheduled_at:scheduled,
          objective:$("#create-objective").value, message:$("#checkin-message").value})});
      state.createKey=null; $("#create-modal").close(); toast("Check-in scheduled.");
      await refresh(true); await openTask(task.id); state.tab="context"; renderDetail(); return;
    }
    const types = $$("input[name=record]:checked").map((x) => x.value);
    if (!types.length) throw new Error("Select at least one document type.");
    const allDates = $("#all-dates").checked;
    const requirements = types.map((record_type) => ({
      record_type,
      all_dates: allDates,
      ...(!allDates
        ? {
            from_date: $("#from-date").value,
            through_date: $("#through-date").value,
          }
        : {}),
    }));
    const task = await api(`/v1/firms/${state.firm}/tasks/medical-records`, {
      method: "POST",
      headers: { "Idempotency-Key": (state.createKey ||= crypto.randomUUID()) },
      body: JSON.stringify({
        case_id: $("#create-case").value,
        parent_task_id: $("#create-parent").value || null,
        patient_contact_id: $("#create-patient").value,
        provider_contact_id: $("#create-provider").value,
        agent_version: 2,
        objective: $("#create-objective").value,
        requirements,
      }),
    });
    state.createKey = null;
    $("#create-modal").close();
    toast("Task created. Agent queued for execution.");
    await refresh(true);
    await openTask(task.id);
  } catch (err) {
    $("#create-error").textContent = err.message;
    $("#create-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});
$("#create-form").addEventListener("input", () => {
  state.createKey = null;
  state.createAt = null;
});
$("#comment-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!state.selected) return;
  const button = ev.submitter;
  button.disabled = true;
  const text = $("#comment").value,
    mentions = new Set(state.mentions);
  for (const a of state.workspace.agents)
    if (new RegExp(`@${a.key}(?![a-zA-Z0-9_])`).test(text)) mentions.add(a.key);
  const body = {
    text,
    mentions: [...mentions],
    wake_agent: $("#wake").checked,
  };
  const serialized = JSON.stringify(body);
  if (state.commentBody !== serialized) {
    state.commentKey = crypto.randomUUID();
    state.commentBody = serialized;
  }
  try {
    const result = await api(`${root()}/tasks/${state.selected}/comments`, {
      method: "POST",
      headers: { "Idempotency-Key": state.commentKey },
      body: serialized,
    });
    $("#comment").value = "";
    state.mentions.clear();
    $("#wake").checked = false;
    state.commentKey = null;
    toast(
      result.woke_agent
        ? "Comment saved. Agent wake queued."
        : "Comment saved to audit trail.",
    );
    await refreshDetail(true);
    await refresh();
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
  }
});
// A deliberately explicit demo intake; it never sends a message to a real provider.
const providerDialog = document.createElement("dialog");
providerDialog.id = "provider-modal";
providerDialog.innerHTML =
  '<form id="provider-form"><div class="modal-heading"><h2>Simulate provider response</h2><button type="button" data-close="provider-modal" class="icon-button" aria-label="Close">✕</button></div><p>This stores a fictional response, creates synthetic evidence when received, and wakes the assigned agent.</p><label>Response status<select id="provider-status"><option value="PENDING">Still processing</option><option value="RECEIVED">All requested records received</option></select></label><label>Message<textarea id="provider-message" rows="4" required maxlength="2000" placeholder="The requested records are ready…"></textarea></label><div id="provider-error" class="error" role="alert" hidden></div><div class="modal-footer"><span>Simulation only / saved in audit history</span><button class="primary" type="submit">Save response & wake</button></div></form>';
document.body.append(providerDialog);
$("#provider-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.submitter;
  button.disabled = true;
  $("#provider-error").hidden = true;
  try {
    await api(`${taskRoot(state.selected)}/provider-updates`, {
      method: "POST",
      headers: {
        "Idempotency-Key": (state.providerKey ||= crypto.randomUUID()),
      },
      body: JSON.stringify({
        status: $("#provider-status").value,
        message: $("#provider-message").value,
      }),
    });
    state.providerKey = null;
    providerDialog.close();
    toast("Simulated response saved. Agent wake queued.");
    await refreshDetail(true);
    await refresh();
  } catch (err) {
    $("#provider-error").textContent = err.message;
    $("#provider-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});
$("#provider-form").addEventListener("input", () => (state.providerKey = null));
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  if (b.dataset.task) await openTask(b.dataset.task);
  if (b.dataset.case) navigate("tasks", b.dataset.case);
  if (b.dataset.view) navigate(b.dataset.view);
  if (b.dataset.layout) {
    state.layout = b.dataset.layout;
    $$("[data-layout]").forEach((x) => x.classList.toggle("selected", x === b));
    render();
  }
  if (b.dataset.tab) {
    state.tab = b.dataset.tab;
    $$("[data-tab]").forEach((x) => x.classList.toggle("selected", x === b));
    renderDetailTab();
  }
  if (b.dataset.mention) {
    const key = b.dataset.mention;
    state.mentions.has(key)
      ? state.mentions.delete(key)
      : state.mentions.add(key);
    b.classList.toggle("selected", state.mentions.has(key));
    b.setAttribute("aria-pressed", state.mentions.has(key));
    updateWake();
  }
  if (b.dataset.close) $("#" + b.dataset.close).close();
  if (b.hasAttribute("data-create")) openCreate();
  if (b.hasAttribute("data-provider")) {
    $("#provider-form").reset();
    $("#provider-error").hidden = true;
    state.providerKey = null;
    providerDialog.showModal();
  }
  if (b.hasAttribute("data-older-events")) {
    try {
      const page = await api(
        `${root()}/activity?${query({ task_id: state.selected, before: state.eventsNext })}`,
      );
      state.events = mergeRows(state.events, page.items);
      state.eventsNext = page.next_cursor;
      renderDetailTab();
    } catch (err) {
      toast(err.message);
    }
  }
});
$("#search").addEventListener("input", render);
$("#status").addEventListener("change", () => {
  state.epoch++;
  refresh(true);
});
$("#refresh").onclick = () => refresh(true);
$("#load-more").onclick = loadMore;
$("#new-task").onclick = openCreate;
$("#close-detail").onclick = closeDetail;
$("#drawer-backdrop").onclick = closeDetail;
$("#copy-id").onclick = async () => {
  try {
    await navigator.clipboard.writeText(state.selected);
    toast("Full task ID copied.");
  } catch {
    toast(state.selected);
  }
};
$("#connection").onclick = () => $("#connect-modal").showModal();
$("#connect-form").onsubmit = async (ev) => {
  ev.preventDefault();
  state.key = $("#credential").value;
  sessionStorage.setItem("operatorKey", state.key);
  $("#credential").value = "";
  $("#connect-modal").close();
  await connect();
};
$("#firm").onchange = () => {
  closeDetail();
  state.firm = $("#firm").value;
  history.replaceState(null, "", `?firm=${encodeURIComponent(state.firm)}`);
  state.case = null;
  state.epoch++;
  state.tasks = [];
  state.activity = [];
  refresh(true);
};
$("#create-case").onchange = updateCreateContacts;
$("#all-dates").onchange = () => {
  $("#date-range").hidden = $("#all-dates").checked;
  $("#from-date").required = $("#through-date").required =
    !$("#all-dates").checked;
};
$("#comment").addEventListener("input", () => {
  for (const a of state.workspace.agents)
    if (new RegExp(`@${a.key}(?![a-zA-Z0-9_])`).test($("#comment").value))
      state.mentions.add(a.key);
  $$("[data-mention]").forEach((b) => {
    b.classList.toggle("selected", state.mentions.has(b.dataset.mention));
    b.setAttribute("aria-pressed", state.mentions.has(b.dataset.mention));
  });
  updateWake();
});
document.addEventListener("keydown", (ev) => {
  if (
    ev.key === "Escape" &&
    state.selected &&
    !$$("dialog").some((d) => d.open)
  )
    closeDetail();
  if (ev.key === "Tab" && state.selected && !$$("dialog").some((d) => d.open)) {
    const focusable = $$(
      "#drawer button:not(:disabled), #drawer textarea, #drawer input:not(:disabled), #drawer summary",
    ).filter((el) => el.getClientRects().length);
    const first = focusable[0],
      last = focusable.at(-1);
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }
});
setInterval(() => {
  if (!document.hidden && !$$("dialog").some((d) => d.open)) refresh();
}, 5000);
connect();

// Case objectives are operator-approved scopes. Models propose assignments;
// the backend commits them and owns versions, evidence checks, and deduplication.
function supervisorPanel(c) {
  const supervisor = state.overview.supervisors?.find(
    (s) => s.case_id === c.id,
  );
  if (!supervisor)
    return `<div class="case-objective"><p>No case objective assigned.</p><button class="primary" data-objective="${e(c.id)}">Assign case objective</button></div>`;
  const plan = supervisor.plan || {};
  return `<div class="case-objective">${badge(supervisor.status)}<h4>${e(supervisor.objective.objective)}</h4><p>${e(plan.summary || "Supervisor activation queued.")}</p>${(plan.actions || []).map((a) => `<p><button class="task-link" data-task="${e(a.task_id)}">${short(a.task_id)}</button> ${e(statusName(a.type))}</p>`).join("")}${(plan.client_updates || []).map((u) => `<div class="instructions"><strong>Client-reported update</strong><p>${e(u.message)}</p><p>${e(u.changes.reasons.join("; "))}</p><button class="task-link" data-task="${e(u.task_id)}">${short(u.task_id)} / inspect report</button></div>`).join("")}${(plan.client_review_outcomes || []).filter((r) => r.decision).map((r) => `<div class="instructions"><strong>Client review / ${e(statusName(r.decision))}</strong><p>${e(r.reason)}</p><p>${e(r.decided_by)} / Task ${e(statusName(r.task_status || "pending"))}</p><button class="task-link" data-task="${e(r.task_id)}">Open reviewed task</button></div>`).join("")}${(plan.reviews || []).map((r) => `<div class="review-question"><strong>Unresolved review</strong><p>${e(r.question)}</p>${r.task_id ? `<button class="task-link" data-task="${e(r.task_id)}">Review task</button>` : ""}</div>`).join("")}<div class="objective-actions"><button class="text-button" data-task="${e(supervisor.group_task_id)}">Decisions, context & guidance</button><button class="text-button" data-objective="${e(c.id)}">Replace objective</button></div></div>`;
}
const objectiveDialog = document.createElement("dialog");
objectiveDialog.id = "objective-modal";
objectiveDialog.innerHTML = `<form id="objective-form"><div class="modal-heading"><h2>Case objective</h2><button type="button" data-close="objective-modal" class="icon-button" aria-label="Close">×</button></div><p id="objective-case"></p><label>Objective<textarea id="objective-text" rows="3" required maxlength="2000" placeholder="Collect records and imaging from the hospital…"></textarea></label><label class="wake-label"><input type="checkbox" id="objective-records" checked> Approve records scope below (all dates)</label><p id="objective-scope-note" hidden></p><div class="form-row"><label>Client / patient<select id="objective-patient"></select></label><label>Provider<select id="objective-provider"></select></label></div><div class="checks"><label><input type="checkbox" name="supervisor-record" value="RECORDS" checked> Records</label><label><input type="checkbox" name="supervisor-record" value="IMAGING" checked> Imaging</label><label><input type="checkbox" name="supervisor-record" value="BILLS"> Bills</label></div><p>Routine assignments start automatically. Unsupported or unspecified scope is sent for human review. Provider operations are simulated.</p><div class="error" id="objective-error" role="alert" hidden></div><div class="modal-footer"><span id="objective-revision"></span><button class="primary" type="submit">Save objective & start</button></div></form>`;
document.body.append(objectiveDialog);
let objectiveCase, objectiveRevision, objectiveKey, objectiveSavedTargets;
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-objective]");
  if (!button) return;
  objectiveCase = button.dataset.objective;
  objectiveKey = crypto.randomUUID();
  const c = state.overview.cases.find((c) => c.id === objectiveCase);
  $("#objective-form").reset();
  $("#objective-error").hidden = true;
  $("#objective-case").textContent = `${c.number}: ${c.title}`;
  const contacts = state.overview.contacts.filter(
    (c) => c.case_id === objectiveCase,
  );
  const options = (role) =>
    contacts
      .filter((c) => c.roles.some((r) => role.includes(r)))
      .map((c) => `<option value="${e(c.id)}">${e(c.name)}</option>`)
      .join("");
  $("#objective-patient").innerHTML = options(["PATIENT", "CLIENT"]);
  $("#objective-provider").innerHTML = options(["MEDICAL_PROVIDER"]);
  try {
    const current = await api(
      `/v1/firms/${state.firm}/cases/${objectiveCase}/supervisor`,
    );
    objectiveRevision = current.revision;
    const existingTargets = current.objective?.targets || [];
    objectiveSavedTargets =
      existingTargets.length > 1 ||
      existingTargets.some((t) => t.requirements.some((r) => !r.all_dates))
        ? existingTargets
        : null;
    const note = $("#objective-scope-note");
    note.hidden = !objectiveSavedTargets;
    note.textContent =
      "The existing provider scopes and date ranges will be preserved. This form edits one all-dates scope only.";
    [
      $("#objective-records"),
      $("#objective-patient"),
      $("#objective-provider"),
      ...$$('input[name="supervisor-record"]'),
    ].forEach((el) => (el.disabled = Boolean(objectiveSavedTargets)));
    const target = existingTargets[0];
    if (target) {
      $("#objective-patient").value = target.patient_contact_id;
      $("#objective-provider").value = target.provider_contact_id;
      $$('input[name="supervisor-record"]').forEach(
        (el) =>
          (el.checked = target.requirements.some(
            (r) => r.record_type === el.value,
          )),
      );
    }
    $("#objective-records").checked = !current.objective || Boolean(target);

    $("#objective-text").value = current.objective?.objective || "";
    $("#objective-revision").textContent = current.revision
      ? `Explicit replacement of revision ${current.revision}`
      : "New case objective";
    objectiveDialog.showModal();
  } catch (error) {
    toast(error.message);
  }
});
$("#objective-form").addEventListener(
  "input",
  () => (objectiveKey = crypto.randomUUID()),
);
$("#objective-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("#objective-error").hidden = true;
  try {
    const targets = objectiveSavedTargets ? [...objectiveSavedTargets] : [];
    if (!objectiveSavedTargets && $("#objective-records").checked) {
      const requirements = $$('input[name="supervisor-record"]:checked').map(
        (x) => ({ record_type: x.value, all_dates: true }),
      );
      if (!requirements.length)
        throw new Error("Select at least one required record type.");
      targets.push({
        patient_contact_id: $("#objective-patient").value,
        provider_contact_id: $("#objective-provider").value,
        requirements,
      });
    }
    await api(`/v1/firms/${state.firm}/cases/${objectiveCase}/supervisor`, {
      method: "POST",
      headers: { "Idempotency-Key": objectiveKey },
      body: JSON.stringify({
        objective: $("#objective-text").value,
        targets,
        expected_revision: objectiveRevision,
      }),
    });
    objectiveDialog.close();
    await refresh(true);
  } catch (error) {
    $("#objective-error").textContent = error.message;
    $("#objective-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});


function clientReviewHTML(task) {
  const report = task.client_check_in?.report;
  const review = report?.human_review;
  if (!review) return "";
  const draft = state.reviewDraft?.id === review.id ? state.reviewDraft : {reason:"", decision:"ALLOW_COMPLETION"};
  const actionable = review.status === "OPEN" && task.status === "BLOCKED";
  return `<section class="task-memory" aria-label="Client report review"><h3>Teammate review / ${e(statusName(review.status))}</h3>
    <p><strong>${e(review.question)}</strong></p><blockquote>${e(report.report.message)}</blockquote>
    <p class="inline-note">This decision concerns this client report only. Contact holds and open obligations remain enforced. No message will be sent by this form.</p>
    ${report.review_stale ? `<p role="alert">The report changed after acknowledgement. This decision no longer permits completion; the report needs a new correction/review process.</p>` : ""}
    ${review.reason ? `<p>Last decision: <strong>${e(statusName(review.decision))}</strong> / ${e(review.reason)}</p><small>${e(review.decided_by)} / ${e(date(review.decided_at,true))}</small>` : ""}
    ${actionable ? `<form data-client-review="${e(review.id)}" data-task="${e(task.id)}" data-version="${task.version}" data-review-version="${review.version}">
      <label>Decision<select name="decision"><option value="ALLOW_COMPLETION" ${draft.decision === "ALLOW_COMPLETION" ? "selected" : ""}>Allow completion / I have handled this report</option><option value="REQUEST_CHANGES" ${draft.decision === "REQUEST_CHANGES" ? "selected" : ""}>Request changes / give guidance and keep review open</option></select></label>
      <label>Reason or instructions<textarea name="reason" required maxlength="2000" rows="3" placeholder="Record what you reviewed, handled, or need the agent to change">${e(draft.reason)}</textarea></label>
      <p class="inline-note">Recorded under the authenticated local operator credential. Requesting changes wakes the agent once; completion still needs an explicit acknowledgement.</p>
      <button type="submit" class="primary">Record decision & wake same task</button></form>` : `<p>${review.status === "RESOLVED" ? "Acknowledgement saved. The agent resumes on this same task; other blockers still apply." : "Wait for the agent to block for review, then decide here."}</p>`}
  </section>`;
}

document.addEventListener("input", (event) => {
  const form = event.target.closest("[data-client-review]");
  if (!form) return;
  state.reviewDraft = {id:form.dataset.clientReview, reason:form.elements.reason.value, decision:form.elements.decision.value};
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-client-review]");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button[type=submit]");
  const target = `${taskRoot(form.dataset.task)}/client-reviews/${form.dataset.clientReview}/decisions`;
  const payload = {expected_task_version:Number(form.dataset.version), expected_review_version:Number(form.dataset.reviewVersion), decision:form.elements.decision.value, reason:form.elements.reason.value.trim()};
  if (!payload.reason) { toast("A review reason is required."); return; }
  const intent = JSON.stringify({decision:payload.decision,reason:payload.reason});
  if (state.reviewWrite?.target !== target || state.reviewWrite?.intent !== intent)
    state.reviewWrite = {target,intent,key:crypto.randomUUID(),body:JSON.stringify(payload)};
  button.disabled = true;
  try {
    await api(target, {method:"POST",headers:{"Idempotency-Key":state.reviewWrite.key},body:state.reviewWrite.body});
    state.reviewWrite = null; state.reviewDraft = null;
    toast("Decision recorded. Same task and supervisor wake queued.");
    await refreshDetail(true); await refresh();
  } catch (err) {
    if (err.status === 409) { state.reviewWrite = null; await refreshDetail(true); }
    toast(err.message);
  } finally { button.disabled = false; }
});
