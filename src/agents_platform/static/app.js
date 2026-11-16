/* No framework or browser-side secrets: every operation uses the scoped API. */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const escapeHTML = (value) =>
  String(value ?? "")
    .replace(/\blocal[- ]operator\b/gi, "User")
    .replace(/\bdemo-seed\b/gi, "Workspace setup")
    .replace(/\bDEMO-/g, "CASE-")
    .replace(/\bdemo\b:?\s*/gi, "")
    .replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const e = escapeHTML;
const taskTitle = (task) => task?.progress?.display_title || task?.progress_json?.display_title || task?.objective || "Task";
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
const agent = (t) => t.task_type === "client.monthly_schedule"
  ? '<span class="agent-cell"><span class="agent-initial">S</span>Supervisor / monthly calendar</span>'
  : `<span class="agent-cell"><span class="agent-initial" aria-hidden="true">${t.task_type === "case.coordination" ? "S" : t.agent_key === "client_check_in" ? "CI" : "MR"}</span>${e(names[t.agent_key] || (t.task_type === "case.coordination" ? "Case supervisor" : t.agent_key))} <small>v${e(t.agent_version || (t.task_type === "case.coordination" ? state.overview?.supervisors?.find(s => s.group_task_id === t.id)?.agent_version : null) || 1)}</small></span>`;
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
        '<div class="empty"><strong>No firms yet</strong>Run agents-seed to create the sample workspace.</div>';
      return;
    }
    $("#firm").value = state.firm;
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
    const [overview, page, activity, humanPage, incidentPage, operations] = await Promise.all([
      api(`${root()}/overview`),
      api(
        `${root()}/tasks?${query({ case_id: state.case, status: $("#status").value, include_parents: true })}`,
      ),
      api(`${root()}/activity?${query({ case_id: state.case })}`),
      api(`/v1/firms/${state.firm}/human-requests`),
      api(`/v1/firms/${state.firm}/incidents`),
      api(`${root()}/operations`).catch(err => ({error: err.message})),
    ]);
    if (epoch !== state.epoch) return;
    state.operations = operations;
    state.overview = overview;
    state.humanQueue = humanPage.items;
    state.incidents = incidentPage.items;
    state.incidentsNext = incidentPage.next_offset;
    state.humanNext = humanPage.next_offset;
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
function render() {
  $("#nav-count").textContent = number(
    Object.values(state.overview.counts).reduce((a, b) => a + b, 0) + (state.overview.parent_count || 0),
  );
  $("#case-count").textContent = state.overview.cases.length;
  $("#case-nav").innerHTML = state.overview.cases
    .map(
      (c) =>
        `<button class="case-nav ${state.case === c.id ? "active" : ""}" data-case="${e(c.id)}"><div>${e(c.number)}<br><small>${e(c.title)}</small></div></button>`,
    )
    .join("");
  const title = {tasks: "All tasks", supervisor: "Supervisor", activity: "Activity", agents: "Agents", human: "Needs your input", incidents: "Execution incidents", operations: "Operations"}[state.view];
  $("#page-title").textContent = state.case && state.view === "tasks" ? selectedCase()?.number || title : title;
  $$(".nav").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === state.view && !state.case),
  );
  $("#view-tabs").hidden = state.view !== "tasks";
  $("#section-label").hidden = state.view === "tasks";
  $("#section-label").textContent = title;
  $(".filters").hidden = !["tasks"].includes(state.view);
  const term = $("#search").value.trim().toLowerCase();
  const tasks = state.tasks.filter((t) =>
    `${taskTitle(t)} ${t.objective} ${t.id} ${short(t.id)}`.toLowerCase().includes(term),
  );
  if (state.view === "tasks") renderTasks(tasks);
  if (state.view === "supervisor") renderSupervisor();
  if (state.view === "human") renderHumanQueue();
  if (state.view === "incidents") renderIncidentQueue();
  if (state.view === "operations") renderOperations();
  $("#ops-count").textContent = state.operations?.alert_count || "";
  $("#ops-count").hidden = !state.operations?.alert_count;
  if (state.view === "activity")
    $("#content").innerHTML =
      `<div class="audit-feed">${state.activity.length ? state.activity.map(eventHTML).join("") : '<div class="empty">No activity yet. Create a task to get started.</div>'}</div>`;
  if (state.view === "agents")
    $("#content").innerHTML =
      `<div class="agent-grid">${state.workspace.agents.map((a) => `<article class="agent-card"><span class="agent-initial">${e(a.name.split(/\s+/).slice(0, 2).map(word => word[0]).join("").toUpperCase())}</span><h3>${e(a.name)}</h3><p>${e(a.description || a.capability)}</p></article>`).join("")}</div>`;
  $("#load-more").hidden = !(state.view === "tasks"
    ? state.next
    : state.view === "activity"
      ? state.activityNext
      : state.view === "human" ? state.humanNext !== null : state.view === "incidents" ? state.incidentsNext !== null : false);
  $("#result-count").textContent =
    state.view === "tasks"
      ? `${number(tasks.length)} tasks${term ? " found" : ""}`
      : state.view === "activity"
        ? `${number(state.activity.length)} loaded events`
        : state.view === "human" ? `${state.humanQueue?.length || 0} open requests loaded` : "";
}
function caseName(t) {
  return state.overview.cases.find((c) => c.id === t.case_id)?.number || "Case";
}
function taskSub(t) {
  return `<span class="task-sub"><code>${short(t.id)}</code><span class="meta-separator" aria-hidden="true">/</span><span>${e(caseName(t))}</span>${t.parent_task_id ? '<span class="meta-separator" aria-hidden="true">/</span><span>Subtask</span>' : ""}</span>`;
}
function pendingAssignmentsHTML() {
  const supervisors = (state.overview?.supervisors || []).filter(s =>
    (!state.case || s.case_id === state.case) && s.status !== "SATISFIED");
  if (!supervisors.length || $("#search").value.trim() || $("#status").value) return "";
  return supervisors.map(s=>`<div class="empty"><strong>${s.status === "NEEDS_REVIEW" ? "Assignment needs review" : "Assignment saved"}</strong><p>${s.status === "NEEDS_REVIEW" ? "The supervisor stopped before creating a specialist task. Open the assignment to review what happened." : "The supervisor is preparing specialist work. You can inspect the saved assignment while it runs."}</p><button class="primary" data-task="${e(s.group_task_id)}">Open supervisor assignment</button></div>`).join("");
}
function subtasksHTML(task) {
  return (task.subtasks || []).length
    ? task.subtasks.map(child => `<button class="subtask-link" data-task="${e(child.id)}" aria-label="Open subtask: ${e(taskTitle(child))}">↳ ${e(taskTitle(child))}</button>`).join("")
    : '<span class="no-subtasks">—</span>';
}
function renderTasks(tasks) {
  if (!tasks.length) {
    $("#content").innerHTML = (!state.tasks.length && pendingAssignmentsHTML()) ||
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
        return `<section class="column"><div class="column-title">${title}<b>${items.length}</b></div>${items.map((t) => `<button class="task-card" data-task="${e(t.id)}">${badge(t.status)}<span class="task-title">${e(taskTitle(t))}</span><div class="task-card-bottom">${taskSub(t)}${agent(t)}</div></button>`).join("")}</section>`;
      })
      .join("")}</div>`;
    return;
  }
  const statuses = ["RUNNING", "WAITING", "BLOCKED", "PENDING", "FAILED", "COMPLETED", "CANCELLED"];
  $("#content").innerHTML = `<div class="task-groups">${statuses.map((status) => {
    const items = tasks.filter((t) => t.status === status).sort((a, b) => Number(b.kind === "GROUP") - Number(a.kind === "GROUP"));
    if (!items.length) return "";
    return `<section class="task-group" aria-label="${e(statusName(status))} tasks">
      <div class="group-heading">${badge(status)}<span class="group-count">${items.length}</span></div>
      <table class="task-table"><thead><tr><th scope="col">Name</th><th scope="col">Assignee</th><th scope="col">Case</th><th scope="col">Created</th><th scope="col">Subtasks</th></tr></thead>
      <tbody>${items.map((t) => `<tr><td><button class="task-name" data-task="${e(t.id)}"><span class="task-check ${t.status === "COMPLETED" ? "done" : ""}" aria-hidden="true">${t.status === "COMPLETED" ? "✓" : ""}</span><span class="task-title">${e(taskTitle(t))}</span>${t.kind === "GROUP" ? '<span class="parent-mark">Parent</span>' : ""}${t.parent_task_id ? '<span class="subtask-mark" title="Subtask" aria-label="Subtask">↳</span>' : ""}</button></td><td>${agent(t)}</td><td><span class="case-tag">${e(caseName(t))}</span></td><td>${e(date(t.created_at))}</td><td class="subtasks-cell">${subtasksHTML(t)}</td></tr>`).join("")}</tbody></table>
    </section>`;
  }).join("")}<button class="add-task-row" data-create><span aria-hidden="true">＋</span> Add task</button></div>`;
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
    `<div class="supervisor-grid"><section><div class="section-heading">Case progress <span>${cases.length} cases</span></div>${cases
      .map((c) => {
        const total = Object.values(c.counts).reduce((a, b) => a + b, 0),
          done = c.counts.COMPLETED || 0,
          blocked = (c.counts.BLOCKED || 0) + (c.counts.FAILED || 0);
        return `<article class="case-card"><div class="case-card-top">${e(c.number)} / ${e(c.status)}</div><h3>${e(c.title)}</h3><p>${
          state.overview.groups
            .filter((g) => g.case_id === c.id)
            .map((g) => `<button class="task-link" data-task="${e(g.id)}">${e(taskTitle(g))}</button>`)
            .join("<br>") || "No parent task"
        }</p>${supervisorPanel(c)}${monthlyPanel(c.id)}<div class="progress-track"><i style="width:${total ? Math.round((done / total) * 100) : 0}%"></i></div><div class="case-stats"><span>${done} of ${total} tasks complete</span><span>${blocked} need attention</span></div><button class="text-button" data-case="${e(c.id)}" style="margin-top:14px">View case tasks</button></article>`;
      })
      .join(
        "",
      )}</section><section><div class="section-heading">Needs review <span>Loaded tasks</span></div>${needs.length ? needs.map((t) => `<button class="attention-row" data-task="${e(t.id)}">${badge(t.status)}<span class="task-title">${e(taskTitle(t))}</span><p>${e(t.progress.last_reason || t.progress.last_summary || "Review the latest execution and add guidance.")}</p>${taskSub(t)}</button>`).join("") : '<div class="attention-empty">No blockers in the loaded tasks.<br>Case totals above include all persisted tasks.</div>'}<div class="section-heading" style="margin-top:28px">Supervisor mentions <span>Recent activity</span></div>${mentions.length ? mentions.map(eventHTML).join("") : '<div class="attention-empty">Tag @supervisor in a comment to wake the configured case coordinator.</div>'}</section></div>`;
}
const eventTitles = {
  "client.review_decided": "Teammate recorded a client-report decision",
  "records.reminder_sent": "Simulated provider reminder sent",
  "series.occurrence_created": "Recurring check-in created",
  "series.controlled": "Check-in series control recorded",
  "series.ended": "Check-in series ended",
  "case.objective_submitted": "Case objective submitted",
  "supervisor.review_completed": "Supervisor reviewed case",
  "supervisor.failed": "Supervisor needs recovery",
  "task.created": "Task assigned",
  "task.plan_rejected": "Agent plan needs correction",
  "task.comment_added": "Comment added",
  "communication.received": "Simulated communication received",
  "provider.response_received": "Provider update received",
  "provider.response_ingested": "Provider response saved",
  "provider.response_extracted": "Provider response reviewed",
  "human.request_created": "Review requested",
  "human.request_decided": "Review decision recorded",
  "human.decision_recorded": "Review decision recorded",
  "incident.resolved": "Execution recovered",
  "agent.execution_completed": "Execution completed",
  "task.activation_completed": "Agent reported back",
  "task.failed": "Task failed",
  "task.waiting": "Next check scheduled",
  "task.completed": "Task completed",
  "task.blocked": "Human review needed",
  "task.running": "Agent started",
};
function eventHTML(ev) {
  const type = ev.type || ev.event_type,
    payload = ev.payload || {};
  const title =
    (type === "task.waiting" && payload.outcome?.wait_for_event ? "Waiting for update" : eventTitles[type]) || type.replaceAll(".", " / ").replaceAll("_", " ");
  const decision = payload.decision;
  const text =
    payload.text ||
    payload.summary ||
    (type === "provider.response_extracted" ? `${payload.received_requirement_ids?.length || 0} requirements received; ${payload.outstanding_requirement_ids?.length || 0} still outstanding.${payload.authorization_required ? " Updated authorization required." : payload.needs_review ? " Human review required." : ""}` : "") ||
    (type === "provider.response_ingested" ? "Original response saved. The agent will review the message and any attached documents." : "") ||
    decision?.summary ||
    payload.reason ||
    payload.assignment?.objective ||
    "";
  const attribution = ev.source_actor
    ? `${e(ev.source_actor.name)}${ev.simulated ? " · Simulated update" : ""}${ev.recorded_by ? ` · Recorded by ${e(ev.recorded_by)}` : ""}`
    : e(ev.actor || ev.actor_ref || "platform");
  return `<article class="feed-item"><div class="feed-body"><div class="feed-meta"><span class="feed-title">${e(title)}</span><time>${e(date(ev.created_at, true))}</time></div><div class="task-sub">${attribution}${ev.task_id ? `<span class="meta-separator" aria-hidden="true">/</span><button class="task-link" data-task="${e(ev.task_id)}">${short(ev.task_id)}</button>` : ""}</div>${text ? `<p class="feed-text">${e(text)}</p>` : ""}${payload.message ? `<p class="feed-text">${e(payload.message)}</p>` : ""}${payload.communication_source_id ? `<details><summary>Original communication</summary><button class="text-button" data-memory-source="${e(payload.communication_source_id)}">Read transcript</button><div></div></details>` : ""}${payload.mentions?.length ? `<div class="feed-tags">${payload.mentions.map((m) => `<span class="mention">@${e(m)}</span>`).join("")}${payload.woke_agent ? '<span class="mention">Wake requested</span>' : ""}</div>` : ""}<details><summary>Event payload / ${e(ev.id?.slice(0, 8))}</summary><pre>${pretty(payload)}</pre></details></div></article>`;
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
  $("#open-temporal").hidden = true;
  $("#open-temporal").removeAttribute("href");
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
  const [task, events, runs, memory, humanPage, incidentPage, contactPage] = await Promise.all([
    api(taskRoot(id)),
    api(`${root()}/activity?${query({ task_id: id })}`),
    api(`${taskRoot(id)}/executions`),
    api(`${taskRoot(id)}/memory`),
    api(`/v1/firms/${state.firm}/human-requests?${query({task_id:id,status:"ALL",limit:100})}`),
    api(`/v1/firms/${state.firm}/incidents?${query({task_id:id,status:"ALL",limit:100})}`),
    api(`${taskRoot(id)}/manual-contacts`),
  ]);
  if (
    state.selected !== id ||
    epoch !== state.detailEpoch ||
    firm !== state.firm
  )
    return;
  const hash = JSON.stringify([task, events, runs, memory, humanPage, incidentPage, contactPage]);
  if (!force && hash === state.detailHash) return;
  // A background refresh must not discard a document the reviewer just selected.
  if (!force && (document.querySelector('[name="authorization_file"]')?.files?.length || document.activeElement?.closest("[data-demo-call], [data-human-response], [data-human-assignment]"))) return;
  state.detailHash = hash;
  state.detail = task;
  state.runs = runs;
  state.memory = memory;
  state.humanRequests = humanPage.items;
  state.taskIncidents = incidentPage.items;
  state.manualContacts = contactPage.items;
  const retaining = state.events.length > 100;
  state.events = retaining
    ? mergeRows(state.events, events.items)
    : events.items;
  if (!retaining) state.eventsNext = events.next_cursor;
  renderDetail();
}
function pendingUpdateHTML() {
  const events = state.events || [];
  const interpreted = new Set(events.flatMap(ev=>[
    ev.payload?.raw_source_id, ev.payload?.communication_source_id
  ]).filter(Boolean));
  const pending = events.some(ev=>["provider.response_ingested", "communication.received"].includes(ev.type || ev.event_type)
    && ev.payload?.source_id && !interpreted.has(ev.payload.source_id));
  return pending ? '<p class="pending-update" role="status"><strong>Update received</strong>Saved and queued for agent review. Document status and the next step will update after review.</p>' : "";
}
function temporalTaskURL(task) {
  const config = state.workspace?.temporal;
  const coordinator = state.overview?.supervisors?.find(s => s.group_task_id === task.id);
  const workflow = coordinator?.workflow_id || task.workflow_id;
  if (!config?.ui_url || !workflow) return null;
  try {
    const url = new URL(config.ui_url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    url.search = url.hash = "";
    url.pathname = url.pathname.replace(/\/$/, "") + `/namespaces/${encodeURIComponent(config.namespace)}/workflows/${encodeURIComponent(workflow)}`;
    return url.href;
  } catch { return null; }
}
function renderDetail() {
  const t = state.detail,
    latest = state.runs.at(-1),
    outcome = latest?.outcome;
  const continuationWake = (state.humanRequests || []).flatMap(r => r.continuations || []).find(c => c.status === "PENDING")?.ready_at;
  const scheduledWake = t.status === "WAITING" && !t.records_request?.callback_at
    ? continuationWake || t.records_request?.next_reminder_at || outcome?.wake_at : null;
  const group = state.overview.groups.find((g) => g.id === t.parent_task_id);
  const reason =
    (t.client_check_in && !t.client_check_in.sent_at && new Date(t.client_check_in.scheduled_at) > new Date()
      ? `Scheduled for ${date(t.client_check_in.scheduled_at, true)}. No check-in will be sent before then.` : "") ||
    (t.records_request?.callback_at ? "Waiting until the provider’s agreed callback time." : "") ||
    t.progress_json.last_reason ||
    outcome?.reason ||
    latest?.decision?.next_action?.reason ||
    latest?.decision?.summary ||
    t.progress_json.last_summary ||
    "";
  $("#detail-id").textContent = short(t.id);
  const temporalURL = temporalTaskURL(t);
  $("#open-temporal").hidden = !temporalURL;
  if (temporalURL) $("#open-temporal").href = temporalURL;
  else $("#open-temporal").removeAttribute("href");
  $("#detail-heading").innerHTML =
    `${badge(t.task_type === "case.coordination" ? state.overview.supervisors?.find((s) => s.group_task_id === t.id)?.status || t.status : t.status)}<h2>${e(taskTitle(t))}</h2><dl class="detail-props"><dt>Assigned to</dt><dd>${agent(t)}</dd><dt>Case</dt><dd>${e(caseName(t))}</dd><dt>Part of</dt><dd>${group ? `<button class="task-link" data-task="${e(group.id)}">${e(taskTitle(group))}</button>` : "Standalone task"}</dd><dt>Next step</dt><dd>${e(reason || "Agent will assess the task.")}</dd>${scheduledWake ? `<dt>Scheduled wake</dt><dd>${e(date(scheduledWake, true))}</dd>` : ""}</dl>${t.records_request ? `<div class="requirements">${t.records_request.requirements.map((r) => `<span>${e(statusName(r.record_type))} / ${e(statusName(r.status))} / ${r.all_dates ? "all dates" : `${e(r.from_date)} ${e(r.through_date)}`}</span>`).join("")}<span>${e(statusName(t.records_request.status))}</span></div>${!closed(t) ? '<button class="text-button" data-provider>Simulate provider response</button>' : ""}` : ""}`;
  $("#detail-heading").insertAdjacentHTML("beforeend", pendingUpdateHTML() + checkInHTML() + humanRequestsHTML(t) + recoveryHTML(t) + communicationHTML(t) + manualContactsHTML(t) + seriesHTML(t) + monthlyHTML(t));
  if (t.records_request?.callback_at) {
    const r = t.records_request;
    const when = new Date(r.callback_at).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short", timeZone: r.callback_timezone});
    $("#detail-heading").insertAdjacentHTML("beforeend", `<p class="inline-note"><strong>Provider callback:</strong> ${e(when)} (${e(r.callback_timezone)}). Follow-ups pause until then.</p>`);
  }
  if (t.records_request?.reminders_enabled) {
    const r = t.records_request;
    $("#detail-heading").insertAdjacentHTML("beforeend", `<p class="inline-note">Simulated reminders: ${r.reminder_count} / ${r.max_reminders}${r.next_reminder_at && !r.callback_at ? ` / Next due ${e(date(r.next_reminder_at,true))}` : ""}. Existing contact restrictions remain enforced.</p>`);
  }
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
      : "Tag the assigned agent to enable wake. @supervisor wakes the case coordinator when configured.";
}
function executionTraceLinks(run) {
  const traces = run.observability || run.context?.observability || {};
  return [["jaeger_url", "Open execution trace"], ["langfuse_url", "Open LLM trace"]]
    .filter(([key]) => /^https?:\/\//.test(traces[key] || ""))
    .map(([key, label]) => `<a href="${e(traces[key])}" target="_blank" rel="noopener noreferrer">${label}</a>`)
    .join(" · ");
}

function renderDetailTab() {
  const target = $("#detail-content");
  if (state.tab === "activity")
    target.innerHTML = state.events.length
      ? `${state.events.map(eventHTML).join("")}${state.eventsNext ? '<button class="text-button" data-older-events>Load older events</button>' : ""}`
      : '<div class="empty">No activity yet.</div>';
  if (state.tab === "executions")
    target.innerHTML = `<p class="inline-note">Latest 20 activations. Context, decisions, tool results, and actual token usage are saved per execution. Provider tools are simulated.</p>${state.runs.map((run, i) => `<details class="run" ${i === state.runs.length - 1 ? "open" : ""}><summary><span>Activation ${run.cycle + 1} / ${e(run.mode)}</span>${badge(run.status)}</summary><div class="run-meta">${e(date(run.created_at, true))} / ${e(run.id)} / attempt ${run.attempt} / ${number(run.usage?.total_tokens)} tokens / ${number(run.usage?.requests)} model calls</div><p>${executionTraceLinks(run)}</p><p>${e(run.decision?.summary || run.error_type || "Execution is in progress.")}</p>${run.decision?.next_action ? `<p><strong>${e(run.decision.next_action.type)}</strong> / ${e(run.decision.next_action.reason)}</p>` : ""}${run.outcome?.reason && run.outcome.reason !== run.decision?.next_action?.reason ? `<p class="inline-note">Backend: ${e(run.outcome.reason)}</p>` : ""}${(run.usage?.tool_rejections || []).map(r => `<p class="inline-note">Action postponed: ${e(statusName(r.tool))}. Due ${e(date(r.ready_at, true))}. No action was taken.</p>`).join("")}<h4>Tool calls (${run.tools.length})</h4>${run.tools.map((tool) => `<details class="run-tools"><summary>${e(tool.name)}<span class="mode">Simulated</span></summary><p>Idempotency key: <code>${e(tool.logical_key)}</code></p><pre>${pretty(tool.result)}</pre></details>`).join("")}<details><summary>Saved decision & committed outcome</summary><pre>${pretty({ decision: run.decision, outcome: run.outcome, usage: run.usage })}</pre></details></details>`).join("") || '<div class="empty">No executions yet. The worker will pick up the task.</div>'}`;
  if (state.tab === "context") {
    const run = state.runs.at(-1),
      ctx = run?.context;
    const snapshot = ctx
      ? `<p class="inline-note">Saved context for activation ${run.cycle + 1}. Includes scoped instructions and source-backed task history. Historical passages and a source-linked digest are saved when available; current restrictions remain separate.</p><div class="mini-label">Instructions</div>${(ctx.instructions || []).map((i) => `<div class="instructions"><strong>${e(statusName(i.scope))} / Revision ${i.revision}</strong><p>${e(i.text)}</p><code>${e(i.id)}</code></div>`).join("")}<div class="mini-label">Context sources / ${(ctx.sources || []).length}</div>${(ctx.sources || []).map((s) => `<details class="run"><summary>Source / ${e(s.id.slice(0, 8))}</summary><pre>${pretty(s)}</pre></details>`).join("") || '<p class="inline-note">No incoming message sources at the start of this activation. Sources returned by tools appear under Executions.</p>'}<details class="run"><summary>Full context snapshot</summary><pre>${pretty(ctx)}</pre></details><details class="run"><summary>Current task progress & completion criteria</summary><pre>${pretty({ progress: state.detail.progress_json, completion: state.detail.completion_json })}</pre></details>`
      : '<div class="empty">Context is assembled when the agent first runs.</div>';
    target.innerHTML = memoryHTML() + historyHTML() + snapshot;
  }
}

function memoryHTML() {
  const memory = state.memory;
  if (!memory || state.detail.kind !== "AGENT") return "";
  const editable = !closed(state.detail);
  const items = [...new Map([
    ...(memory.operational_items || []), ...(memory.optional_notes || []),
    ...(memory.case_restrictions || []), ...(memory.history || []),
  ].map((item) => [item.id, item])).values()];
  const itemHTML = (item) => {
    const expired = item.expires_at && new Date(item.expires_at) <= new Date();
    const open = item.status === "OPEN" && !expired;
    const label = expired && item.status === "OPEN" ? "Expired" : item.status === "OPEN" && item.active === false ? "Scheduled" : statusName(item.status);
    return `<div class="instructions">
      <strong>${e(item.scope === "CASE" ? (item.provider_contact_id ? "Case / provider hold" : "Case-wide hold") : statusName(item.kind))} / ${e(label)}</strong>
      <p>${e(item.text)}</p><code>Source ${e(item.source_id.slice(0, 8))} / revision ${item.revision}</code>
      ${item.effective_from ? `<p>Effective ${e(date(item.effective_from, true))}</p>` : ""}
      ${item.due_at ? `<p>Due ${e(date(item.due_at, true))}</p>` : ""}
      ${item.review_at ? `<p>${item.review_due ? "Review overdue" : "Review scheduled"}: ${e(date(item.review_at, true))}. A review date does not lift this item.</p>` : ""}
      ${item.expires_at ? `<p>Note expires ${e(date(item.expires_at, true))}</p>` : ""}
      ${open && editable ? `<details><summary>Review, replace or resolve</summary><form data-memory-form data-scope="${item.scope === "CASE" ? "CASE" : "TASK"}" data-item="${e(item.id)}" data-kind="${e(item.kind)}">
        <label>Action<select name="operation"><option value="${item.review_due ? "REVIEW" : "RESOLVE"}">${item.review_due ? "Reviewed / keep active" : "Resolve"}</option><option value="${item.review_due ? "RESOLVE" : "REVIEW"}">${item.review_due ? "Resolve" : "Reviewed / keep active"}</option><option value="SUPERSEDE">Replace with a revised item</option></select></label>
        <label data-replacement hidden>Replacement details<textarea name="replacement" maxlength="2000" rows="2"></textarea></label>
        ${item.kind === "NOTE" ? '<label data-note-expiry hidden>Replacement note expires (optional, local time)<input name="expires_at" type="datetime-local" disabled></label>' : ""}
        <label>Reason<input name="text" required maxlength="2000" placeholder="What changed or was verified?"></label>
        <label data-review-date ${item.review_due ? "" : "hidden"}>Next review (optional, local time)<input name="review_at" type="datetime-local"></label>
        <button type="submit" class="text-button">Save decision & wake agent</button></form></details>` : ""}
      ${item.resolution_reason ? `<p>Decision: ${e(item.resolution_reason)}</p>` : ""}
      ${item.superseded_by_id ? `<p>Replaced by ${e(item.superseded_by_id.slice(0, 8))}</p>` : ""}
    </div>`;
  };
  const current = items.filter((i) => i.status === "OPEN" && !(i.expires_at && new Date(i.expires_at) <= new Date()));
  const archived = items.filter((i) => !current.includes(i));
  return `<section class="task-memory"><h4>Operational memory</h4>
    <p class="inline-note">Effective holds and obligations always load. Resolved and replaced items stay in history. Review dates never lift contact holds.</p>
    ${current.map(itemHTML).join("") || '<p class="inline-note">No open memory items.</p>'}
    ${archived.length ? `<details><summary>Archived memory / ${archived.length} loaded</summary>${archived.map(itemHTML).join("")}</details>` : ""}
    ${memory.history_next_offset != null ? `<button class="text-button" data-memory-older="${memory.history_next_offset}">Load older task memory</button>` : ""}
    ${editable ? `<details><summary>Add an obligation, restriction or note</summary>
      <form data-memory-form><label>Applies to<select name="scope"><option value="TASK">This task</option><option value="CASE">Entire case / contact hold</option>${state.detail.records_request ? '<option value="PROVIDER">This provider across the case / contact hold</option>' : ""}</select></label><label>Type<select name="kind"><option value="OBLIGATION">Obligation / requires explicit resolution</option><option value="STOP_CONTACT">Stop contact / blocks outreach</option><option value="NOTE">Supporting note / optional context</option></select></label>
      <label>Details<textarea name="text" required maxlength="2000" rows="2" placeholder="Record a specific item and its reason"></textarea></label>
      <label>Effective from (optional, local time)<input name="effective_from" type="datetime-local"></label>
      <label>Review on (optional, local time)<input name="review_at" type="datetime-local"></label>
      <label data-note-expiry hidden>Note expires (optional, local time)<input name="expires_at" type="datetime-local" disabled></label>
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
  const operation = form.dataset.item ? data.get("operation") : "ADD";
  const payload = {
    expected_version: isCase ? state.memory.case_memory_version : state.memory.task_version,
    operation,
    text: operation === "SUPERSEDE" ? data.get("replacement") : data.get("text"),
  };
  if (form.dataset.item) payload.item_id = form.dataset.item;
  if (operation === "SUPERSEDE") payload.reason = data.get("text");
  if (!isCase && ["ADD", "SUPERSEDE"].includes(operation)) payload.kind = form.dataset.kind || data.get("kind");
  if (isCase && operation === "ADD" && scope === "PROVIDER") payload.provider_contact_id = state.detail.records_request.provider_contact_id;
  for (const field of operation === "ADD" ? ["effective_from", "review_at", "expires_at"] : operation === "SUPERSEDE" ? ["review_at", "expires_at"] : operation === "REVIEW" ? ["review_at"] : []) {
    if (data.get(field)) payload[field] = new Date(data.get(field)).toISOString();
  }
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


// Lifecycle controls keep hold resolution separate from review acknowledgement.
function updateMemoryForm(form) {
  const scope = form.dataset.scope || form.querySelector('[name="scope"]')?.value || "TASK";
  const kind = form.querySelector('[name="kind"]');
  if (kind) {
    kind.disabled = scope !== "TASK";
    if (kind.disabled) kind.value = "STOP_CONTACT";
  }
  const operation = form.querySelector('[name="operation"]')?.value || "ADD";
  const replacement = form.querySelector('[data-replacement]');
  if (replacement) {
    replacement.hidden = operation !== "SUPERSEDE";
    replacement.querySelector('textarea').required = operation === "SUPERSEDE";
  }
  const review = form.querySelector('[data-review-date]');
  if (review) review.hidden = operation === "RESOLVE";
  const expiry = form.querySelector('[data-note-expiry]');
  if (expiry) {
    expiry.hidden = scope !== "TASK" || (form.dataset.kind || kind?.value) !== "NOTE" || !["ADD", "SUPERSEDE"].includes(operation);
    expiry.querySelector('input').disabled = expiry.hidden;
  }
}
document.addEventListener("change", (event) => {
  const form = event.target.closest('[data-memory-form]');
  if (form) updateMemoryForm(form);
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest('[data-memory-older]');
  if (!button) return;
  const taskId = state.selected;
  button.disabled = true;
  try {
    const page = await api(`${taskRoot(taskId)}/memory?history_offset=${button.dataset.memoryOlder}`);
    if (state.selected !== taskId) return;
    state.memory.history = [...new Map([...state.memory.history, ...page.history].map((i) => [i.id, i])).values()];
    state.memory.history_next_offset = page.history_next_offset;
    renderDetailTab();
  } catch (err) { toast(err.message); }
  finally { button.disabled = false; }
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
    if (state.view === "incidents") {
      const page = await api(`/v1/firms/${state.firm}/incidents?offset=${state.incidentsNext}`);
      state.incidents = [...state.incidents, ...page.items];
      state.incidentsNext = page.next_offset; render(); return;
    }
    if (state.view === "human") {
      const page = await api(`/v1/firms/${state.firm}/human-requests?offset=${state.humanNext}`);
      state.humanQueue = [...state.humanQueue, ...page.items];
      state.humanNext = page.next_offset;
      render(); return;
    }
    const isTask = state.view === "tasks";
    const page = await api(
      `${root()}/${isTask ? "tasks" : "activity"}?${query({ case_id: state.case, include_parents: isTask ? true : null, status: isTask ? $("#status").value : null, before: isTask ? state.next : state.activityNext })}`,
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
document.querySelector("#create-series").addEventListener("change", updateCreateKind);

function isMonthlyCreate() {
  return $("#create-agent").value === "supervisor" && $("#create-supervisor-mode").value === "monthly";
}
function automaticCreate() {
  return !state.humanObjective && !$("#create-manual-settings").open;
}
function invalidateAssignmentPlan() {
  state.assignmentPlan = null;
  $("#create-plan").hidden = true;
  $("#create-plan").innerHTML = "";
}
$("#create-manual-settings").addEventListener("toggle", () => {
  invalidateAssignmentPlan();
  updateCreateKind();
  if (!automaticCreate() && $("#create-agent").value === "supervisor") loadCreateSupervisor();
});

function assignmentPlanHTML(draft) {
  const plan = draft.plan;
  const names = new Map(draft.contacts.map(c => [c.id, c.name]));
  const name = id => e(names.get(id) || "Unknown contact");
  const steps = plan.steps.map(step => {
    if (step.kind === "records") return `<section class="instructions"><strong>Collect hospital records</strong>${step.targets.map(t => `<p>${name(t.patient_contact_id)} → ${name(t.provider_contact_id)}</p><ul>${t.requirements.map(r => `<li>${e(statusName(r.record_type))}: ${r.all_dates ? "all dates" : `${e(r.from_date)} through ${e(r.through_date)}`}</li>`).join("")}</ul>`).join("")}</section>`;
    if (step.kind === "agent") {
      const capability = (draft.capabilities || []).find(c => c.agent_key === step.agent_key);
      let assignment = {};
      try { assignment = JSON.parse(step.assignment_json); } catch (_) { /* Backend validates before approval. */ }
      return `<section class="instructions"><strong>${e(capability?.name || step.objective)}</strong><p>${e(step.objective)}</p><dl>${Object.entries(assignment).map(([key, value]) => `<dt>${e(statusName(key))}</dt><dd>${e(typeof value === "object" ? JSON.stringify(value) : String(value))}</dd>`).join("")}</dl></section>`;
    }
    const a = step.assignment;
    const timing = step.kind === "monthly" ? `Monthly on day ${a.day_of_month} at ${a.local_time} (${a.timezone}), ${a.max_occurrences} check-ins. Minimum gap: ${a.minimum_gap_days} days.${a.starts_on ? ` Start: ${a.starts_on}.` : ""}${a.ends_at ? ` End: ${date(a.ends_at,true)}.` : ""}`
      : step.kind === "series" ? `Every ${a.interval_seconds / 86400} days after completion; ${a.max_occurrences || "end-date bounded"} check-ins. First: ${date(a.first_due_at,true)}.${a.ends_at ? ` End: ${date(a.ends_at,true)}.` : ""}`
      : `Send: ${date(a.scheduled_at,true)}`;
    return `<section class="instructions"><strong>${e(step.kind === "monthly" ? "Monthly client check-ins" : step.kind === "series" ? "Recurring client check-ins" : "Client check-in")}</strong><p>${name(a.client_contact_id)} · ${e(timing)}</p><p>${e(a.message)}</p></section>`;
  }).join("");
  return `<h3>${plan.status === "ready" ? "Supervisor's proposed plan" : "The supervisor needs more information"}</h3><p>${e(plan.summary)}</p>${steps}${plan.assumptions.length ? `<strong>Please review these assumptions</strong><ul>${plan.assumptions.map(a => `<li>${e(a)}</li>`).join("")}</ul>` : ""}${plan.questions.length ? `<ul>${plan.questions.map(q => `<li>${e(q)}</li>`).join("")}</ul><p>Update your description above, then review the plan again.</p>` : ""}${draft.preserves_existing_records_scope && plan.steps.some(s => s.kind === "records") ? '<p>Existing approved hospital scopes will also be preserved.</p>' : ""}${plan.status === "ready" ? '<button type="button" class="primary" id="approve-assignment-plan">Approve & start</button>' : ""}`;
}
$("#create-plan").addEventListener("click", async event => {
  if (!event.target.closest("#approve-assignment-plan") || !state.assignmentPlan) return;
  const draft = state.assignmentPlan;
  const caseId = $("#create-case").value;
  const firm = state.firm;
  // Freeze the reviewed description while its atomic approval is in flight.
  $("#create-form").inert = true;
  $("#create-error").hidden = true;
  try {
    const result = await api(`/v1/firms/${firm}/cases/${caseId}/assignments/${draft.plan_id}/approve`, {method:"POST"});
    $("#create-modal").close();
    invalidateAssignmentPlan();
    toast("Plan approved. Assignments queued.");
    await refresh(true);
    await openTask(result.tasks[0].task_id);
  } catch (err) {
    $("#create-error").textContent = err.message;
    $("#create-error").hidden = false;
  } finally { $("#create-form").inert = false; }
});
$("#create-workflow").addEventListener("change", () => {
  if (state.humanObjective) { updateCreateKind(); return; }
  const choice = $("#create-workflow").value;
  $("#create-agent").value = ["records", "monthly"].includes(choice) ? "supervisor" : choice;
  $("#create-supervisor-mode").value = choice === "monthly" ? "monthly" : "records";
  $("#create-objective").value = "";
  $("#create-agent").dispatchEvent(new Event("change"));
});
function updateCreateKind() {
  $$("#create-manual-settings input, #create-manual-settings select, #create-manual-settings textarea").forEach(el => {
    if (el.dataset.assignmentDisabled !== undefined) {
      el.disabled = el.dataset.assignmentDisabled === "true";
      delete el.dataset.assignmentDisabled;
    }
  });
  const supervisor = $("#create-agent").value === "supervisor";
  $("#create-agent").disabled = Boolean(state.humanObjective);
  const monthly = isMonthlyCreate();
  const checkIn = $("#create-agent").value === "check_in";
  $("#create-workflow").value = monthly ? "monthly" : supervisor ? "records" : $("#create-agent").value;
  $("#create-workflow").disabled = Boolean(state.humanObjective);
  const preserved = supervisor && !monthly && Boolean(state.createSavedTargets);
  const scope = !monthly && (!supervisor || $("#create-scope").checked);
  $("#create-supervisor-fields").hidden = !supervisor;
  $("#create-records-supervision").hidden = monthly;
  $("#create-monthly-fields").hidden = !monthly;
  $$('#create-monthly-fields input, #create-monthly-fields textarea').forEach((el) => el.disabled = !monthly);
  $("#create-parent-field").hidden = supervisor;
  $("#create-parent").disabled = supervisor || (checkIn && $("#create-series").checked);
  $("#series-fields").hidden = !checkIn || !$("#create-series").checked;
  $("#create-provider-field").hidden = checkIn || !scope;
  $("#create-records-fields").hidden = checkIn || !scope;
  $("#create-checkin-fields").hidden = !checkIn;
  $("#create-patient").disabled = supervisor && !monthly && (!scope || preserved);
  $("#create-provider").required = !checkIn && scope;
  $("#create-provider").disabled = preserved || !scope;
  $("#create-scope").disabled = preserved;
  $$('#create-records-fields input').forEach((el) => el.disabled = preserved || !scope);
  $("#from-date").required = $("#through-date").required = !checkIn && scope && !preserved && !$("#all-dates").checked;
  $("#date-range").hidden = $("#all-dates").checked;
  $("#create-replace").required = supervisor && !monthly && Boolean(state.createSupervisor?.revision);
  $("#create-submit").disabled = supervisor && !monthly && (state.createSupervisorLoading || !state.createSupervisor);
  $("#create-submit").textContent = monthly ? "Create monthly schedule" : supervisor && state.createSupervisor?.revision ? "Update supervisor task" : "Create & start task";
  $("#create-help").textContent = monthly ? "Schedule monthly client updates for the firm."
    : supervisor ? "The supervisor assigns a records specialist and monitors progress."
    : checkIn ? "Send a check-in and report the client's reply to the firm."
    : "Start one records specialist without supervisor planning.";
  $("#create-objective").placeholder = monthly ? "Check in with the client monthly and report meaningful changes…" : checkIn ? "Check in with the client and report meaningful changes…" : "Collect records and imaging from the provider…";
  // Manual-only fields must not validate or block a description-first proposal.
  const automatic = automaticCreate();
  $$("#create-manual-settings input, #create-manual-settings select, #create-manual-settings textarea").forEach(el => {
    if (automatic) {
      el.dataset.assignmentDisabled = String(el.disabled);
      el.disabled = true;
    }
  });
  $("#create-case").required = true;
  if (automatic) {
    $("#create-workflow").disabled = true;
    $("#create-help").textContent = "Describe the work. The supervisor will propose assignments for you to review.";
    $("#create-objective").placeholder = "Collect bills and records from City Hospital, and check in with the client monthly…";
    $("#create-submit").textContent = "Review supervisor plan";
    $("#create-submit").disabled = false;
  }
}
$("#create-supervisor-mode").addEventListener("change", () => {
  state.createKey = null;
  state.createSupervisorEpoch = (state.createSupervisorEpoch || 0) + 1;
  $("#create-objective").value = "";
  if (isMonthlyCreate()) updateCreateKind();
  else loadCreateSupervisor();
});
$("#create-agent").addEventListener("change", () => {
  state.createKey = null;
  if ($("#create-agent").value === "supervisor") loadCreateSupervisor();
  else {
    state.createSupervisorEpoch = (state.createSupervisorEpoch || 0) + 1;
    updateCreateKind();
  }
});
$("#create-scope").addEventListener("change", updateCreateKind);

async function loadCreateSupervisor() {
  if (isMonthlyCreate()) { updateCreateKind(); return; }
  const caseId = $("#create-case").value;
  const epoch = state.createSupervisorEpoch = (state.createSupervisorEpoch || 0) + 1;
  state.createSupervisor = null;
  state.createSavedTargets = null;
  state.createSupervisorLoading = true;
  state.createKey = null;
  $("#create-replace").checked = false;
  $("#create-replace-field").hidden = true;
  $("#create-supervisor-note").textContent = "Loading case objective…";
  updateCreateKind();
  try {
    const current = await api(`/v1/firms/${state.firm}/cases/${caseId}/supervisor`);
    if (epoch !== state.createSupervisorEpoch || caseId !== $("#create-case").value || isMonthlyCreate()) return;
    state.createSupervisor = current;
    const targets = current.objective?.targets || [];
    // Keep complex scopes intact when updating through the single-provider form.
    state.createSavedTargets = targets.length > 1 ? targets : null;
    $("#create-replace-field").hidden = !current.revision;
    $("#create-supervisor-note").textContent = current.revision
      ? "This case already has a supervisor task. Saving updates it; existing specialist tasks continue."
      : "A supervisor task will be created for this case, with specialist subtasks as needed.";
    if (current.objective) {
      if (!$("#create-objective").value) $("#create-objective").value = current.objective.objective;
      $("#create-scope").checked = Boolean(targets.length);
      const target = targets[0];
      if (target) {
        $("#create-patient").value = target.patient_contact_id;
        $("#create-provider").value = target.provider_contact_id;
        const requirements = target.requirements;
        // Mixed date ranges cannot be represented faithfully by the shared dates fields.
        const ranges = new Set(requirements.map((r) => JSON.stringify([r.all_dates, r.from_date, r.through_date])));
        if (ranges.size > 1) state.createSavedTargets = targets;
        $$('input[name="record"]').forEach((el) => el.checked = requirements.some((r) => r.record_type === el.value));
        $("#all-dates").checked = requirements[0].all_dates;
        $("#from-date").value = requirements[0].from_date || "";
        $("#through-date").value = requirements[0].through_date || "";
        if (state.createSavedTargets) $("#create-supervisor-note").textContent += " Existing approved scopes are preserved.";
      }
    }
  } catch (err) {
    if (epoch !== state.createSupervisorEpoch) return;
    $("#create-error").textContent = err.message;
    $("#create-error").hidden = false;
    $("#create-supervisor-note").textContent = "Could not load the case objective. Reopen this form to retry.";
  } finally {
    if (epoch === state.createSupervisorEpoch) {
      state.createSupervisorLoading = false;
      updateCreateKind();
    }
  }
}

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

function openCreate(caseId = null) {
  if (!state.overview) return;
  state.createKey = null;
  state.humanObjective = null;
  invalidateAssignmentPlan();
  $("#create-agent").disabled = $("#create-case").disabled = false;
  state.createAt = null;
  $("#create-form").reset();
  $$("#create-form details").forEach((el) => el.open = false);
  $("#monthly-zone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
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
  if (caseId || state.case) $("#create-case").value = caseId || state.case;
  updateCreateContacts();
  $("#create-modal").showModal();
}
function updateCreateContacts() {
  invalidateAssignmentPlan();
  state.createSupervisor = null;
  state.createKey = null;
  $("#create-objective").value = "";
  $("#create-scope").checked = true;
  $("#all-dates").checked = true;
  $("#from-date").value = $("#through-date").value = "";
  $$('input[name="record"]').forEach((el) => el.checked = el.defaultChecked);
  const caseId = $("#create-case").value,
    contacts = state.overview.contacts.filter((c) => c.case_id === caseId);
  $("#create-contacts").textContent = `People and providers in this case: ${contacts.map(c => c.name).join(", ") || "None linked yet"}.`;
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
      .map((g) => `<option value="${e(g.id)}">${e(taskTitle(g))}</option>`)
      .join("");
  if ($("#create-parent").options.length > 1)
    $("#create-parent").selectedIndex = 1;
  if (!automaticCreate() && $("#create-agent").value === "supervisor") loadCreateSupervisor();
  else updateCreateKind();
}
$("#create-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.submitter;
  button.disabled = true;
  $("#create-error").hidden = true;
  try {
    if (automaticCreate()) {
      const description = $("#create-objective").value;
      const caseId = $("#create-case").value;
      const firm = state.firm;
      const draft = await api(`/v1/firms/${firm}/cases/${caseId}/assignments/plan`, {
        method:"POST", body:JSON.stringify({description, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"}),
      });
      if (firm !== state.firm || caseId !== $("#create-case").value || description !== $("#create-objective").value || !automaticCreate()) return;
      state.assignmentPlan = draft;
      $("#create-plan").innerHTML = assignmentPlanHTML(draft);
      $("#create-plan").hidden = false;
      return;
    }
    if (isMonthlyCreate()) {
      const payload = {
        client_contact_id: $("#create-patient").value,
        timezone: $("#monthly-zone").value.trim(), day_of_month: Number($("#monthly-day").value),
        local_time: $("#monthly-time").value, minimum_gap_days: Number($("#monthly-gap").value),
        max_occurrences: Number($("#monthly-count").value), objective: $("#create-objective").value,
        message: $("#monthly-message").value,
        ...($("#monthly-start").value ? {starts_on: $("#monthly-start").value} : {}),
        ...($("#monthly-end").value ? {ends_at:new Date($("#monthly-end").value).toISOString()} : {}),
      };
      const row = await api(`/v1/firms/${state.firm}/cases/${$("#create-case").value}/monthly-check-ins`, {
        method:"POST", headers:{"Idempotency-Key":(state.createKey ||= crypto.randomUUID())}, body:JSON.stringify(payload),
      });
      state.createKey = null; $("#create-modal").close(); toast("Monthly schedule created. The supervisor will review each due check-in.");
      await refresh(true); await openTask(row.group_task_id); return;
    }
    if ($("#create-agent").value === "check_in") {
      const scheduled = $("#checkin-at").value ? new Date($("#checkin-at").value).toISOString() : (state.createAt ||= new Date().toISOString());
      if ($("#create-series").checked) {
        const series = await api(`/v1/firms/${state.firm}/cases/${$("#create-case").value}/check-in-series`, {
          method:"POST",headers:{"Idempotency-Key":(state.createKey ||= crypto.randomUUID())},
          body:JSON.stringify({client_contact_id:$("#create-patient").value,first_due_at:scheduled,
            interval_seconds:Number($("#series-interval").value)*60,
            max_occurrences:$("#series-max").value ? Number($("#series-max").value) : null,
            ends_at:$("#series-end").value ? new Date($("#series-end").value).toISOString() : null,
            objective:$("#create-objective").value,message:$("#checkin-message").value})});
        state.createKey=null; $("#create-modal").close();toast("Recurring series scheduled.");
        await refresh(true);await openTask(series.group_task_id);return;
      }
      const task = await api(`/v1/firms/${state.firm}/tasks/client-check-in`, {
        method:"POST", headers:{"Idempotency-Key": (state.createKey ||= crypto.randomUUID())},
        body: JSON.stringify({case_id:$("#create-case").value, client_contact_id:$("#create-patient").value,
          parent_task_id:$("#create-parent").value || null, scheduled_at:scheduled,
          objective:$("#create-objective").value, message:$("#checkin-message").value})});
      state.createKey=null; $("#create-modal").close(); toast("Check-in scheduled.");
      await refresh(true); await openTask(task.id); state.tab="context"; renderDetail(); return;
    }
    const supervisor = $("#create-agent").value === "supervisor";
    const scoped = !supervisor || $("#create-scope").checked;
    const types = $$("input[name=record]:checked").map((x) => x.value);
    if (scoped && !types.length) throw new Error("Select at least one document type.");
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
    if (supervisor) {
      if (state.createSupervisorLoading || !state.createSupervisor) throw new Error("Wait for the case objective to load.");
      if (state.createSupervisor.revision && !$("#create-replace").checked) throw new Error("Confirm the update to the existing supervisor task.");
      const objective = {
        objective: $("#create-objective").value,
        expected_revision: state.createSupervisor.revision,
        targets: state.createSavedTargets || (scoped ? [{
          patient_contact_id: $("#create-patient").value,
          provider_contact_id: $("#create-provider").value, requirements,
        }] : []),
      };
      const intervention = state.humanObjective;
      if (intervention && $("#create-case").value !== intervention.request.case_id)
        throw new Error("Keep the clarification in its original case.");
      const result = intervention
        ? await api(`/v1/firms/${state.firm}/human-requests/${intervention.request.id}/decisions`, {
          method:"POST", headers:{"Idempotency-Key":(state.createKey ||= crypto.randomUUID())},
          body:JSON.stringify({expected_task_version:intervention.request.task_version,
            expected_request_version:intervention.request.version, action:"REVISE_OBJECTIVE",
            payload:objective, reason:intervention.reason}),
        })
        : await api(`/v1/firms/${state.firm}/cases/${$("#create-case").value}/supervisor`, {
          method:"POST", headers:{"Idempotency-Key":(state.createKey ||= crypto.randomUUID())},
          body:JSON.stringify(objective),
        });
      state.createKey = null;
      $("#create-modal").close();
      toast("Task assigned to supervisor. Delegation queued.");
      await refresh(true);
      await openTask(intervention ? intervention.request.task_id : result.group_task_id);
      return;
    }
    const task = await api(`/v1/firms/${state.firm}/tasks/medical-records`, {
      method: "POST",
      headers: { "Idempotency-Key": (state.createKey ||= crypto.randomUUID()) },
      body: JSON.stringify({
        case_id: $("#create-case").value,
        parent_task_id: $("#create-parent").value || null,
        patient_contact_id: $("#create-patient").value,
        provider_contact_id: $("#create-provider").value,
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
  invalidateAssignmentPlan();
  state.createKey = null;
  // Editing the answer must preserve the review identity and its case binding.
  // openCreate resets these when starting a different assignment.
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
providerDialog.innerHTML = `
  <form id="provider-form">
    <div class="modal-heading"><h2>Simulate provider response</h2><button type="button" data-close="provider-modal" class="icon-button" aria-label="Close">✕</button></div>
    <label>Intake method<select id="provider-intake"><option value="document">Read response and attached documents</option><option value="manual">Manually record a simulated receipt</option></select></label>
    <label data-manual-receipt>Response status<select id="provider-status"><option value="PENDING">Still processing</option><option value="PARTIAL">Receive selected items</option><option value="RECEIVED">Receive all outstanding items</option></select></label>
    <fieldset id="provider-receipt-fields" hidden><legend>Items included in this delivery</legend><div id="provider-receipt-items"></div><p class="muted">Select only items actually received. Other requirements remain outstanding.</p></fieldset>
    <label>Message<textarea id="provider-message" rows="3" required maxlength="2000" placeholder="Please call back Friday at 2pm."></textarea></label>
    <label id="provider-documents">Documents (PDF or text, up to three files, 2 MB each)<input id="provider-files" type="file" accept="application/pdf,text/plain,.pdf,.txt" multiple /></label>
    <p id="provider-file-summary" class="inline-note" role="status">No documents selected. A message alone does not prove receipt.</p>
    <p id="provider-document-note" class="muted">The worker reads the response and files. A page count alone does not prove an item arrived. Scanned PDFs need human review.</p>
    <details id="provider-callback-fields"><summary>Callback schedule (optional)</summary>
      <label>Hospital timezone<input id="provider-callback-zone" placeholder="America/New_York" maxlength="100" /></label>
      <div class="form-row"><label>Exact date (optional)<input id="provider-callback-date" type="date" /></label><label>Time if not in message<input id="provider-callback-time" type="time" value="09:00" /></label></div>
      <p class="muted">Use a date, or write “call back Friday” in the message. Confirm the hospital timezone; an omitted time uses the time above.</p>
      <label id="provider-clear-field" class="wake-label" hidden><input id="provider-clear-callback" type="checkbox" /> Cancel the current callback and resume normal follow-ups</label>
    </details>
    <div id="provider-error" class="error" role="alert" hidden></div>
    <div class="modal-footer"><span>Simulated response</span><button class="primary" type="submit">Save response</button></div>
  </form>`;
document.body.append(providerDialog);
$("#provider-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.submitter;
  button.disabled = true;
  $("#provider-error").hidden = true;
  try {
    if ($("#provider-intake").value === "document") {
      const target = `${taskRoot(state.selected)}/provider-responses`;
      const files = Array.from($("#provider-files").files);
      if (files.length > 3) throw new Error("Attach at most three files.");
      const message = $("#provider-message").value;
      const attachments = await Promise.all(files.map(providerAttachment));
      await api(target, {method:"POST", headers:{"Idempotency-Key":(state.providerKey ||= crypto.randomUUID())},
        body:JSON.stringify({message,attachments})});
      state.providerKey = null;
      providerDialog.close();
      toast("Original response saved. Document extraction and agent wake queued.");
      await refreshDetail(true); await refresh();
      return;
    }
    const pending = $("#provider-status").value !== "RECEIVED";
    const receipt = $("#provider-status").value !== "PENDING";
    const received = $$('#provider-receipt-items input:checked').map((el) => el.value);
    if (receipt && !received.length) throw new Error("Select at least one received item.");
    const schedule = pending && !$("#provider-clear-callback").checked && ($("#provider-callback-date").value || /\b(call\s+back|callback|follow[ -]?up)\b/i.test($("#provider-message").value));
    await api(`${taskRoot(state.selected)}/provider-updates`, {
      method: "POST",
      headers: {
        "Idempotency-Key": (state.providerKey ||= crypto.randomUUID()),
      },
      body: JSON.stringify({
        status: $("#provider-status").value,
        message: $("#provider-message").value,
        ...(receipt ? {received_requirement_ids:received} : {}),
        ...(schedule ? {
          callback_date: $("#provider-callback-date").value || null,
          callback_local_time: $("#provider-callback-time").value || "09:00",
          callback_timezone: $("#provider-callback-zone").value.trim() || null,
        } : {}),
        ...(pending && $("#provider-clear-callback").checked ? {clear_callback: true} : {}),
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
$("#provider-files").addEventListener("change", () => {
  const files = Array.from($("#provider-files").files);
  $("#provider-file-summary").textContent = files.length
    ? files.map(file=>`${file.name} (${Math.ceil(file.size/1024)} KB)`).join(" · ")
    : "No documents selected. A message alone does not prove receipt.";
  state.providerKey = null;
});
async function providerAttachment(file) {
  if (!file.size || file.size > 2000000) throw new Error("Each document must be between 1 byte and 2 MB.");
  const content_base64 = await new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("Could not read document."));
    reader.readAsDataURL(file);
  });
  return {filename:file.name, content_type:file.name.toLowerCase().endsWith(".pdf") ? "application/pdf" : "text/plain",content_base64};
}
$("#provider-intake").onchange = updateProviderReceipt;
function updateProviderReceipt() {
  const status = $("#provider-status").value;
  const documentMode = $("#provider-intake").value === "document";
  $("[data-manual-receipt]").hidden = documentMode;
  $("#provider-documents").hidden = $("#provider-document-note").hidden = !documentMode;
  $("#provider-callback-fields").hidden = documentMode || status === "RECEIVED";
  $("#provider-receipt-fields").hidden = documentMode || status === "PENDING";
  $$('#provider-receipt-items input').forEach((el) => {
    el.disabled = status !== "PARTIAL";
    el.checked = status === "RECEIVED";
  });
}
$("#provider-status").onchange = updateProviderReceipt;
$("#provider-message").oninput = () => {
  if (/\b(call\s+back|callback|follow[ -]?up)\b/i.test($("#provider-message").value))
    $("#provider-callback-fields").open = true;
};
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
    $("#provider-callback-fields").hidden = false;
    $("#provider-receipt-items").innerHTML = state.detail.records_request.requirements
      .filter((r) => r.status !== "RECEIVED")
      .map((r) => `<label class="wake-label"><input type="checkbox" value="${e(r.id)}" /> ${e(statusName(r.record_type))} / ${r.all_dates ? "all dates" : `${e(r.from_date)} through ${e(r.through_date)}`}</label>`).join("");
    updateProviderReceipt();
    $("#provider-callback-fields").open = !!state.detail.records_request?.callback_at;
    $("#provider-callback-zone").value = state.detail.records_request?.callback_timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
    $("#provider-clear-field").hidden = !state.detail.records_request?.callback_at;
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
$("#new-task").onclick = () => openCreate();
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
$("#all-dates").onchange = updateCreateKind;
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
    return `<div class="case-objective"><p>No case objective assigned.</p><button class="primary" data-objective="${e(c.id)}">Assign task</button></div>`;
  const plan = supervisor.plan || {};
  return `<div class="case-objective">${badge(supervisor.status)}<h4>${e(supervisor.objective.objective)}</h4><p>${e(plan.summary || "Supervisor activation queued.")}</p>${(plan.actions || []).map((a) => `<p><button class="task-link" data-task="${e(a.task_id)}">${short(a.task_id)}</button> ${e(statusName(a.type))}</p>`).join("")}${(plan.client_updates || []).map((u) => `<div class="instructions"><strong>Client-reported update</strong><p>${e(u.message)}</p><p>${e(u.changes.reasons.join("; "))}</p><button class="task-link" data-task="${e(u.task_id)}">${short(u.task_id)} / inspect report</button></div>`).join("")}${(plan.client_review_outcomes || []).filter((r) => r.decision).map((r) => `<div class="instructions"><strong>Client review / ${e(statusName(r.decision))}</strong><p>${e(r.reason)}</p><p>${e(r.decided_by)} / Task ${e(statusName(r.task_status || "pending"))}</p><button class="task-link" data-task="${e(r.task_id)}">Open reviewed task</button></div>`).join("")}${(plan.reviews || []).map((r) => `<div class="review-question"><strong>Unresolved review</strong><p>${e(r.question)}</p>${r.task_id ? `<button class="task-link" data-task="${e(r.task_id)}">Review task</button>` : ""}</div>`).join("")}<div class="objective-actions"><button class="text-button" data-task="${e(supervisor.group_task_id)}">Decisions, context & guidance</button><button class="text-button" data-objective="${e(c.id)}">Edit supervisor task</button></div></div>`;
}
// Supervisor assignment and editing use the same task form as direct specialists.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-objective]");
  if (button) openCreate(button.dataset.objective);
});


function renderHumanQueue() {
  const rows = state.humanQueue || [];
  $("#content").innerHTML = rows.length ? `<div class="audit-feed">${rows.map((r) => `
    <article class="task-memory">${badge(r.kind)} ${r.overdue ? '<strong>Overdue</strong>' : ""}
    <h3>${e(r.question)}</h3><p>${e(r.context_summary)}</p>
    <p>Owner: ${e(r.assignee || "Unassigned")}${r.due_at ? ` / Due ${e(date(r.due_at,true))}` : ""}</p>
    <button class="primary" data-task="${e(r.task_id)}">Review and choose next step</button></article>`).join("")}</div>`
    : '<div class="empty">No open human requests. New requests appear here when work needs your input.</div>';
  $("#result-count").textContent = `${rows.length} open requests loaded`;
}
function humanRequestsHTML(task) {
  const renderRequest = (r) => {
    const draft = state.humanDrafts?.[r.id] || {};
    const selected = draft.action || r.allowed_actions[0]?.action;
    const choice = r.allowed_actions.find((a) => a.action === selected);
    const authorization = selected === "PROVIDE_UPDATED_AUTHORIZATION" || (selected === "RESOLVE_COMMUNICATION" && choice?.payload_schema?.required?.includes("authorization_source_id"));
    const scheduled = selected === "SCHEDULE_FOLLOW_UP";
    return `<section class="task-memory" aria-label="Human request">
      <h3>${e(statusName(r.kind))} / ${e(statusName(r.status))}</h3>
      <p><strong>${e(r.question)}</strong></p><details class="review-assessment"><summary>Agent assessment</summary><p>${e(r.context_summary)}</p></details>
      <p>Owner: ${e(r.assignee || "Unassigned")}${r.due_at ? ` / Due ${e(date(r.due_at,true))}` : ""}${r.overdue ? " / Overdue" : ""}</p>
      ${(r.evidence_source_ids || []).slice(-20).map((id, index) => `<details><summary>${index === 0 && ["provider_response.v1", "communication_review.v1"].includes(r.handler) ? "Original response" : "Supporting evidence"}</summary><button class="text-button" data-memory-source="${e(id)}">Read evidence ${e(id.slice(0,8))}</button><div></div></details>`).join("")}
      ${r.decisions.map((d) => `<div class="instructions"><strong>${e(statusName(d.action))}</strong><p>${e(d.reason)}</p><small>${e(d.actor)} / ${e(date(d.created_at,true))}</small></div>`).join("")}
      ${r.continuations.map((c) => `<p class="inline-note">Authorized follow-up: ${e(statusName(c.status))} / Scheduled ${e(date(c.ready_at,true))} / Response deadline ${e(date(c.response_due_at,true))}. Contact remains simulated.</p>`).join("")}
      ${r.actionable && !closed(task) ? `<form data-human-response="${e(r.id)}" data-request-version="${r.version}" data-task-version="${task.version}">
        <label>Next step<select name="action">${r.allowed_actions.map((a) => `<option value="${e(a.action)}" ${selected === a.action ? "selected" : ""}>${e(a.label)}</option>`).join("")}</select></label>
        <p data-human-consequence class="inline-note">${e(choice?.consequence)}</p>
        <div data-human-schedule ${scheduled ? "" : "hidden"}>
          <label>Follow-up time (${e(Intl.DateTimeFormat().resolvedOptions().timeZone)})<input type="datetime-local" name="follow_up_at" min="${e(localInput(new Date(Date.now()+60000).toISOString()))}" value="${e(draft.follow_up_at || "")}" ${scheduled ? "required" : ""}></label>
          <label>Wait for provider response until<input type="datetime-local" name="response_due_at" value="${e(draft.response_due_at || "")}" ${scheduled ? "required" : ""}></label>
        </div>
        <div data-human-authorization ${authorization ? "" : "hidden"}>
          <label>Updated authorization document<input name="authorization_file" type="file" accept="application/pdf,text/plain,.pdf,.txt" ${authorization ? "required" : ""}></label>
          <label ${selected === "RESOLVE_COMMUNICATION" ? "hidden" : ""}>Wait for provider response until<input name="authorization_response_due_at" type="datetime-local" value="${e(draft.authorization_response_due_at || "")}" min="${e(localInput(new Date(Date.now()+60000).toISOString()))}" ${selected === "PROVIDE_UPDATED_AUTHORIZATION" ? "required" : ""}></label>
          <label><input name="confirmed_valid" type="checkbox" ${draft.confirmed_valid ? "checked" : ""} ${authorization ? "required" : ""}> I reviewed this document and confirm it is valid for this request.</label>
        </div>
        ${selected === "RESOLVE_COMMUNICATION" ? '<label><input name="confirmed_reviewed" type="checkbox" required> I reviewed the original communication and its interpretation.</label><label><input name="restrictions_resolved" type="checkbox" required> I confirm the reported restrictions and prerequisites have been resolved for this task. This does not change firm policy.</label>' : ""}
        <label>Reason / guidance<textarea name="reason" rows="3" required maxlength="2000">${e(draft.reason || "")}</textarea></label>
        <p class="inline-note">Decisions are attributed to the User. Existing restrictions remain enforced.</p>
        <p class="error" role="alert" data-human-error ${state.humanErrors?.[r.id] ? "" : "hidden"}>${e(state.humanErrors?.[r.id] || "")}</p>
        <button type="submit" class="primary">${selected === "REVISE_OBJECTIVE" ? "Continue to objective form" : "Record decision"}</button>
      </form>` : r.status === "OPEN" ? '<p class="inline-note">Wait for the agent to stop for review, then refresh.</p>' : '<p class="inline-note">Decision recorded. Check the task timeline for the continuation outcome.</p>'}
      ${r.status === "OPEN" && !closed(task) ? `<details><summary>Assign owner or deadline</summary><form data-human-assignment="${e(r.id)}" data-request-version="${r.version}">
        <label>Responsible person<input name="assignee" maxlength="100" value="${e(draft.assignee ?? r.assignee ?? "")}"></label>
        <label>Due time<input type="datetime-local" name="due_at" value="${e(draft.due_at ?? localInput(r.due_at))}"></label>
        <p class="inline-note">Owner labels organize work in this local prototype; they do not grant account permissions.</p>
        <button type="submit" class="text-button">Save assignment</button></form></details>` : ""}
    </section>`;
  };
  const rows = state.humanRequests || [];
  const active = rows.filter(r => r.status === "OPEN");
  const past = rows.filter(r => r.status !== "OPEN");
  return active.map(renderRequest).join("") + (past.length
    ? `<details class="task-memory review-history"><summary>Previous reviews (${past.length})</summary>${past.map(renderRequest).join("")}</details>` : "");
}
function localInput(value) {
  if (!value) return "";
  const d = new Date(value);
  return new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,16);
}
document.addEventListener("input", (event) => {
  const form = event.target.closest("[data-human-response], [data-human-assignment]");
  if (!form) return;
  const id = form.dataset.humanResponse || form.dataset.humanAssignment;
  state.humanDrafts ||= {};
  state.humanDrafts[id] = {...state.humanDrafts[id], ...Object.fromEntries(new FormData(form))};
});
document.addEventListener("change", (event) => {
  const form = event.target.closest("[data-human-response]");
  if (!form || event.target.name !== "action") return;
  const request = state.humanRequests.find((r) => r.id === form.dataset.humanResponse);
  const selected = form.elements.action.value;
  form.querySelector("[data-human-consequence]").textContent = request.allowed_actions.find((a) => a.action === selected).consequence;
  form.querySelector("[data-human-schedule]").hidden = selected !== "SCHEDULE_FOLLOW_UP";
  form.elements.follow_up_at.required = form.elements.response_due_at.required = selected === "SCHEDULE_FOLLOW_UP";
  const authorization = selected === "PROVIDE_UPDATED_AUTHORIZATION";
  form.querySelector("[data-human-authorization]").hidden = !authorization;
  form.elements.authorization_file.required = form.elements.authorization_response_due_at.required = form.elements.confirmed_valid.required = authorization;
  form.querySelector("button[type=submit]").textContent = selected === "REVISE_OBJECTIVE" ? "Continue to objective form" : "Record decision";
});
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-human-response], [data-human-assignment]");
  if (!form) return;
  event.preventDefault();
  const id = form.dataset.humanResponse || form.dataset.humanAssignment;
  const request = state.humanRequests.find((r) => r.id === id);
  const data = Object.fromEntries(new FormData(form));
  if (form.dataset.humanResponse && data.action === "REVISE_OBJECTIVE") {
    const reason = data.reason.trim();
    if (!reason) { toast("Record a reason before continuing."); return; }
    $("#create-agent").value = "supervisor";
    openCreate(request.case_id);
    state.humanObjective = {request, reason};
    $("#create-manual-settings").open = true;
    $("#create-agent").disabled = $("#create-case").disabled = true;
    updateCreateKind();
    return;
  }
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    let body, operation;
    if (form.dataset.humanAssignment) {
      operation = "assignment";
      body = {expected_request_version:Number(form.dataset.requestVersion),
        assignee:data.assignee.trim() || null, due_at:data.due_at ? new Date(data.due_at).toISOString() : null};
    } else {
      operation = "decisions";
      body = {expected_task_version:Number(form.dataset.taskVersion), expected_request_version:Number(form.dataset.requestVersion),
        action:data.action, reason:data.reason.trim(), payload:data.action === "SCHEDULE_FOLLOW_UP"
          ? {follow_up_at:new Date(data.follow_up_at).toISOString(),response_due_at:new Date(data.response_due_at).toISOString()} : {}};
    }
    if (form.dataset.humanResponse && data.action === "RESOLVE_COMMUNICATION") {
      if (!form.elements.confirmed_reviewed.checked || !form.elements.restrictions_resolved.checked) throw new Error("Review the communication and resolve its blockers first.");
      body.payload = {confirmed_reviewed:true, restrictions_resolved:true};
    }
    if (form.dataset.humanResponse && (data.action === "PROVIDE_UPDATED_AUTHORIZATION" || (data.action === "RESOLVE_COMMUNICATION" && request.allowed_actions.find(a=>a.action===data.action)?.payload_schema?.required?.includes("authorization_source_id")))) {
      const file = form.elements.authorization_file.files[0];
      if (!file || !form.elements.confirmed_valid.checked) throw new Error("Provide and review the updated authorization document.");
      const attachment = await providerAttachment(file);
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",await file.arrayBuffer()))).map(n=>n.toString(16).padStart(2,"0")).join("");
      const uploaded = await api(`/v1/firms/${state.firm}/tasks/${request.task_id}/records-authorizations`, {
        method:"POST",headers:{"Idempotency-Key":`auth-${id}-${digest}`},body:JSON.stringify(attachment)});
      body.payload = {...body.payload, authorization_source_id:uploaded.source_id,confirmed_valid:true};
      if (data.action === "PROVIDE_UPDATED_AUTHORIZATION") body.payload.response_due_at = new Date(data.authorization_response_due_at).toISOString();
    }
    const target = `/v1/firms/${state.firm}/human-requests/${id}/${operation}`;
    const intent = JSON.stringify(body);
    state.humanWrites ||= {};
    if (state.humanWrites[target]?.intent !== intent) state.humanWrites[target] = {intent,key:crypto.randomUUID()};
    await api(target, {method:"POST",headers:{"Idempotency-Key":state.humanWrites[target].key},body:intent});
    delete state.humanWrites[target];
    if (state.humanErrors) delete state.humanErrors[id];
    if (state.humanDrafts) delete state.humanDrafts[id];
    toast(operation === "assignment" ? "Assignment saved." : "Decision saved. Continuation queued; other blockers still apply.");
    await refreshDetail(true); await refresh();
  } catch (err) {
    (state.humanErrors ||= {})[id] = err.message;
    if (err.status === 409) { state.humanWrites = {}; await refreshDetail(true); }
    const error = document.querySelector(`[data-human-response="${CSS.escape(id)}"] [data-human-error]`);
    if (error) { error.hidden = false; error.textContent = err.message; }
    toast(err.message);
  } finally { button.disabled = false; }
});

function seriesHTML(task) {
  const s=task.check_in_series;
  if (!s) return "";
  return `<section class="task-memory"><h3>Recurring check-in series / ${e(statusName(s.status))}</h3>
    <p>${s.occurrence_count} / ${s.max_occurrences || "100 (safety cap)"} occurrences · Interval ${e(s.interval_seconds/60)} minutes after completion</p>
    <p>Next due: ${e(s.next_due_at ? date(s.next_due_at,true) : ["STOPPED","FINISHED"].includes(s.status) ? "No future occurrence" : "After the current occurrence completes")}${s.ends_at ? ` / Ends ${e(date(s.ends_at,true))}` : ""}</p>
    ${s.blocked_reason ? `<p>${e(s.blocked_reason)}</p>` : ""}
    ${s.active_task_id ? `<button class="task-link" data-task="${e(s.active_task_id)}">Inspect current occurrence ${short(s.active_task_id)}</button>` : ""}
    ${!["STOPPED","FINISHED"].includes(s.status) ? `<form data-series-control="${e(s.id)}" data-version="${s.version}"><label>Action<select name="action">${s.status === "ACTIVE" ? '<option value="PAUSE">Pause unsent outreach</option>' : '<option value="RESUME">Resume series</option>'}<option value="STOP">Stop series / cancel an unsent occurrence</option></select></label><label>Reason<input name="reason" required maxlength="2000" value="${e(state.seriesDraft?.id === s.id ? state.seriesDraft.reason : "")}" /></label><p class="inline-note">Sent replies and reviews can still be handled. No overlapping occurrence or catch-up burst will be created.</p><button class="text-button" type="submit">Apply series control</button></form>` : ""}
    <details><summary>Occurrences / ${s.occurrence_count}</summary>${(s.occurrences || []).map(o=>`<p><button class="task-link" data-task="${e(o.task_id)}">Check-in ${o.occurrence} / ${e(statusName(o.status))}</button> / ${e(date(o.scheduled_at,true))}</p>`).join("")}</details>
    <code>${e(s.workflow_id)}</code></section>`;
}

document.addEventListener("input",event=>{
  const form=event.target.closest("[data-series-control]");
  if(form)state.seriesDraft={id:form.dataset.seriesControl,reason:form.elements.reason.value};
});
document.addEventListener("submit",async event=>{
  const form=event.target.closest("[data-series-control]");if(!form)return;
  event.preventDefault();const button=event.submitter;
  const target=`/v1/firms/${state.firm}/cases/${state.detail.case_id}/check-in-series/${form.dataset.seriesControl}/control`;
  const payload={expected_version:Number(form.dataset.version),action:form.elements.action.value,reason:form.elements.reason.value.trim()};
  const intent=JSON.stringify({action:payload.action,reason:payload.reason});
  if(state.seriesWrite?.intent!==intent||state.seriesWrite?.target!==target)state.seriesWrite={intent,target,key:crypto.randomUUID(),body:JSON.stringify(payload)};
  button.disabled=true;
  try{
    await api(target,{method:"POST",headers:{"Idempotency-Key":state.seriesWrite.key},body:state.seriesWrite.body});
    state.seriesWrite=null;state.seriesDraft=null;toast("Series control recorded.");await refreshDetail(true);await refresh();
  }catch(err){if(err.status===409){state.seriesWrite=null;await refreshDetail(true);}toast(err.message);}
  finally{button.disabled=false;}
});


function monthlyPanel(caseId) {
  return (state.overview.monthly_schedules || []).filter((s) => s.case_id === caseId).map((s) =>
    `<div class="case-objective"><strong>Monthly client check-ins / ${e(statusName(s.status))}</strong><p>Day ${s.day_of_month} at ${e(s.local_time)} (${e(s.timezone)}) / minimum ${s.minimum_gap_days} days between contacts</p><button class="task-link" data-task="${e(s.group_task_id)}">View schedule and decisions</button></div>`).join("");
}
function monthlyHTML(task) {
  const s = task.monthly_check_in_schedule;
  if (!s) return "";
  const draft = state.monthlyDraft?.id === s.id ? state.monthlyDraft : {};
  return `<section class="task-memory"><h3>Monthly client check-ins / ${e(statusName(s.status))}</h3>
    <p>Day ${s.day_of_month} at ${e(s.local_time)} (${e(s.timezone)}). A missing day uses the last day of the month.</p>
    <p>${s.next_due_at ? `Next calendar date: ${e(new Date(s.next_due_at).toLocaleString(undefined, {dateStyle:"medium", timeStyle:"short", timeZone:s.timezone}))}` : "No further calendar dates queued."} / ${s.occurrence_count} of ${s.max_occurrences} monthly slots</p>
    <p class="inline-note">Minimum ${s.minimum_gap_days} local calendar days after the latest sent check-in or client reply, including manual work. This spacing policy remains after stopping the calendar. Pending replies/reviews prevent overlap. The supervisor handles due occurrences within 24 hours; missed windows are skipped.</p>
    ${(s.occurrences || []).map((o) => `<div class="instructions"><strong>${e(o.month)} / ${e(statusName(o.status))}</strong><p>${e(o.reason || "Waiting for supervisor review.")}</p>${o.task_id ? `<button class="task-link" data-task="${e(o.task_id)}">View check-in task</button>` : ""}${o.covering_task_id ? `<button class="task-link" data-task="${e(o.covering_task_id)}">View the check-in that covered this occurrence</button>` : ""}</div>`).join("") || '<p class="inline-note">No monthly occurrences are due yet.</p>'}
    ${!["STOPPED","FINISHED"].includes(s.status) ? `<form data-monthly-control="${e(s.id)}" data-version="${s.version}"><label>Action<select name="action">${s.status === "ACTIVE" ? '<option value="PAUSE">Pause automatic check-ins</option>' : '<option value="RESUME">Resume monthly schedule</option>'}<option value="STOP">Stop calendar / cancel unsent monthly work</option></select></label><label>Reason<input name="reason" required maxlength="2000" value="${e(draft.reason || "")}"></label><button type="submit" class="text-button">Apply schedule control</button></form>` : ""}
  </section>`;
}
document.addEventListener("input", (event) => {
  const form = event.target.closest('[data-monthly-control]');
  if (form) state.monthlyDraft = {id:form.dataset.monthlyControl, reason:form.querySelector('[name="reason"]').value};
});
document.addEventListener("submit", async (event) => {
  const form = event.target.closest('[data-monthly-control]');
  if (!form) return;
  event.preventDefault();
  const button = event.submitter; button.disabled = true;
  const data = new FormData(form), id = form.dataset.monthlyControl;
  const payload = {expected_version:Number(form.dataset.version), action:data.get('action'), reason:data.get('reason')};
  const intent = JSON.stringify({id, action:payload.action, reason:payload.reason});
  if (state.monthlyWrite?.intent !== intent) state.monthlyWrite = {intent, key:crypto.randomUUID(), body:JSON.stringify(payload)};
  try {
    await api(`/v1/firms/${state.firm}/cases/${state.detail.case_id}/monthly-check-ins/${id}/control`, {
      method:"POST", headers:{"Idempotency-Key":state.monthlyWrite.key}, body:state.monthlyWrite.body,
    });
    state.monthlyWrite = null; state.monthlyDraft = null; toast("Monthly schedule updated.");
    await refreshDetail(true); await refresh();
  } catch (err) { if (err.status === 409) state.monthlyWrite = null; toast(err.message); }
  finally { button.disabled = false; }
});


function renderIncidentQueue() {
  $("#content").innerHTML = (state.incidents || []).length ? `<div class="audit-feed">${state.incidents.map((i) => `<article class="task-memory"><h3>${e(i.objective)}</h3><p>${e(statusName(i.category))} / ${e(i.error_type)}</p><p>Execution ${i.failed_cycle}; recovery attempts used ${i.generation} / 3</p><button class="primary" data-task="${e(i.task_id)}">Inspect and recover</button></article>`).join("")}</div>` : '<div class="empty">No open execution incidents.</div>';
}
function contactFields(task, draft = {}) {
  const contacts = state.overview.contacts.filter((c) => c.case_id === task.case_id);
  return `<label>Contact<select name="contact_id">${contacts.map((c) => `<option value="${e(c.id)}" ${draft.contact_id === c.id ? "selected" : ""}>${e(c.name)}</option>`).join("")}</select></label>
    <label>Actual contact time<input name="occurred_at" type="datetime-local" required value="${e(draft.occurred_at || localInput(new Date().toISOString()))}"></label>
    <label>Channel<select name="channel">${["PHONE","EMAIL","IN_PERSON","OTHER"].map((v) => `<option ${draft.channel === v ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <label>Outcome<select name="outcome">${["REACHED","VOICEMAIL","NO_ANSWER","WRONG_CONTACT"].map((v) => `<option ${draft.outcome === v ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <label>What happened<textarea name="notes" required maxlength="4000" rows="3">${e(draft.notes || "")}</textarea></label>
    <label>Supporting source IDs (optional, comma-separated)<input name="evidence" value="${e(draft.evidence || "")}"></label>
    ${task.records_request ? `<details><summary>Explicitly authorize one provider follow-up (optional)</summary><p>Choose the task's provider and REACHED outcome. This authorizes one simulated attempt for missing items; holds remain enforced.</p><label>Follow-up time<input type="datetime-local" name="follow_up_at" value="${e(draft.follow_up_at || "")}"></label><label>Response deadline<input type="datetime-local" name="response_due_at" value="${e(draft.response_due_at || "")}"></label></details>` : ""}
    <p class="inline-note">Attributed to User. Notes remain unverified statements, not document receipt or a completed client check-in.</p>`;
}
function recoveryHTML(task) {
  const renderIncident = (i) => {
    const draft = state.recoveryDrafts?.[i.id] || {};
    return `<section class="task-memory"><h3>Execution incident / ${e(statusName(i.status))}</h3><p>${e(i.error_type)} / ${e(statusName(i.category))}</p><p>Failed cycle ${i.failed_cycle}. Recovery attempts used: ${i.generation} / 3.</p><p>${e(i.delivery_note)}</p><details><summary>Saved actions (${i.saved_actions.length}${i.saved_actions_truncated ? "+" : ""})</summary><pre>${pretty(i.saved_actions)}</pre></details>
    ${i.resolution ? `<p>${e(i.resolution.action)}: ${e(i.resolution.reason)}</p><small>${e(i.resolution.actor)}</small>` : ""}
    ${i.status === "OPEN" && task.status === "FAILED" ? `<form data-recover="${e(i.id)}" data-task-version="${task.version}" data-incident-version="${i.version}">
      <label>Recovery action<select name="action">${i.allowed_actions.map((a) => `<option ${draft.action === a ? "selected" : ""}>${a}</option>`).join("")}</select></label>
      <label>What was fixed / reason<textarea name="reason" rows="3" required maxlength="2000">${e(draft.reason || "")}</textarea></label>
      <label class="wake-label"><input type="checkbox" name="include_contact" ${draft.include_contact ? "checked" : ""}> Record manual contact with this decision</label>
      <fieldset data-recovery-contact ${draft.include_contact ? "" : "hidden disabled"}><legend>Manual contact</legend>${contactFields(task,draft)}</fieldset>
      <p class="inline-note">Retry starts a new execution on the same assignment with current facts and policy. Saved actions remain deduplicated. Cancel stops future work.</p><button type="submit" class="primary">Save recovery decision</button></form>` : ""}</section>`;
  };
  const rows = state.taskIncidents || [];
  const open = rows.filter(i=>i.status === "OPEN");
  const resolved = rows.filter(i=>i.status !== "OPEN");
  return open.map(renderIncident).join("") + (resolved.length
    ? `<details class="task-memory review-history"><summary>Resolved execution incidents (${resolved.length})</summary>${resolved.map(renderIncident).join("")}</details>` : "");
}
function manualContactsHTML(task) {
  if (task.kind !== "AGENT") return "";
  const draft = state.contactDrafts?.[task.id] || {};
  return `<details class="task-memory"><summary>Manual contact (optional)</summary>${(state.manualContacts || []).map((c) => `<div class="instructions"><strong>${e(statusName(c.outcome))} / ${e(statusName(c.channel))}</strong><p>${e(c.notes)}</p><small>Contact ${e(date(c.occurred_at,true))}; recorded ${e(date(c.recorded_at,true))} by ${e(c.actor)}</small></div>`).join("")}
    ${!["COMPLETED","CANCELLED"].includes(task.status) ? `<details><summary>Record a call or other contact</summary><form data-record-contact="${e(task.id)}" data-task-version="${task.version}">${contactFields(task,draft)}<button type="submit" class="text-button">Save contact</button></form></details>` : ""}</details>`;
}
function contactPayload(form) {
  const f = form.elements;
  return {contact_id:f.contact_id.value,occurred_at:new Date(f.occurred_at.value).toISOString(),
    channel:f.channel.value,outcome:f.outcome.value,notes:f.notes.value.trim(),
    evidence_source_ids:f.evidence.value.split(",").map((v) => v.trim()).filter(Boolean),
    ...(f.follow_up_at?.value ? {follow_up_at:new Date(f.follow_up_at.value).toISOString()} : {}),
    ...(f.response_due_at?.value ? {response_due_at:new Date(f.response_due_at.value).toISOString()} : {})};
}
document.addEventListener("input", (event) => {
  const form = event.target.closest("[data-recover], [data-record-contact]");
  if (!form) return;
  const recovering = !!form.dataset.recover;
  const store = recovering ? (state.recoveryDrafts ||= {}) : (state.contactDrafts ||= {});
  const id = form.dataset.recover || form.dataset.recordContact;
  store[id] = Object.fromEntries(new FormData(form));
});
document.addEventListener("change", (event) => {
  const form = event.target.closest("[data-recover]");
  if (form && event.target.name === "include_contact") {
    const fields = form.querySelector("[data-recovery-contact]");
    fields.hidden = fields.disabled = !event.target.checked;
  }
});
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-recover], [data-record-contact]");
  if (!form) return;
  event.preventDefault(); const button = form.querySelector("button[type=submit]"); button.disabled = true;
  try {
    const recovering = !!form.dataset.recover;
    const id = form.dataset.recover || form.dataset.recordContact;
    const target = recovering ? `/v1/firms/${state.firm}/incidents/${id}/resolve` : `${taskRoot(id)}/manual-contacts`;
    const body = {expected_task_version:Number(form.dataset.taskVersion)};
    if (recovering) {
      Object.assign(body,{expected_incident_version:Number(form.dataset.incidentVersion),action:form.elements.action.value,reason:form.elements.reason.value.trim()});
      if (form.elements.include_contact.checked) body.contact = contactPayload(form);
    } else body.contact = contactPayload(form);
    const intent = JSON.stringify(body); state.recoveryWrites ||= {};
    if (state.recoveryWrites[target]?.intent !== intent) state.recoveryWrites[target] = {intent,key:crypto.randomUUID()};
    await api(target,{method:"POST",headers:{"Idempotency-Key":state.recoveryWrites[target].key},body:intent});
    delete state.recoveryWrites[target];
    if (recovering && state.recoveryDrafts) delete state.recoveryDrafts[id];
    else if (state.contactDrafts) delete state.contactDrafts[id];
    toast(recovering ? "Recovery saved. Inspect the task for the continuation outcome." : "Manual contact recorded.");
    await refreshDetail(true); await refresh(true);
  } catch (err) { toast(err.message); if (err.status === 409) {state.recoveryWrites={};await refreshDetail(true);} }
  finally {button.disabled=false;}
});

function duration(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
}
function renderOperations() {
  const data = state.operations;
  if (!data || data.error) {
    $("#content").innerHTML = `<div class="empty"><strong>Operations unavailable</strong>${e(data?.error || "Refresh to try again.")}</div>`;
    return;
  }
  const expanded = new Set($$("details[data-ops-key][open]").map(el => el.dataset.opsKey));
  const descriptions = {
    outbox: "Oldest undelivered message, including retries.",
    worker: "Oldest runnable unread event or pending task. Scheduled and human-blocked waits excluded.",
    failed: "Tasks currently failed; inspect their execution incident.",
    callbacks: "Callback deadline passed beyond the grace period. Holds still apply.",
    human: "Open human requests. Alerts when overdue or older than the review threshold.",
  };
  $("#content").innerHTML = `<div class="operations"><div class="ops-summary"><span>${data.alert_count ? `${data.alert_count} active alert conditions` : "No active alerts"}</span><small>Checked ${e(date(data.as_of, true))} · Firm-wide</small></div>${data.signals.map(s => `<section class="ops-signal ${s.alert ? "ops-alert" : ""}"><div class="ops-heading"><h3>${e(s.label)}</h3><span class="ops-state">${s.alert ? "Needs attention" : "Within threshold"}</span></div><div class="ops-value">${number(s.count)}${s.age_seconds !== undefined ? `<small>Oldest ${duration(s.age_seconds)}</small>` : ""}${s.overdue_count ? `<small>${s.overdue_count} past due</small>` : ""}</div><p>${e(descriptions[s.key])}${s.threshold_seconds !== undefined ? ` Threshold: ${duration(s.threshold_seconds)}.` : ""}</p>${s.items.length ? `<details data-ops-key="${e(s.key)}" ${expanded.has(s.key) ? "open" : ""}><summary>Inspect oldest ${s.items.length} items</summary><div class="ops-items">${s.items.map(i => `<div><button class="task-link" data-task="${e(i.task_id)}">${e(short(i.task_id))}</button><span>${i.age_seconds !== undefined ? duration(i.age_seconds) : e(statusName(i.status || "OPEN"))}${i.attempts !== undefined ? ` · ${i.attempts} delivery retries` : ""}</span><code>${e(i.workflow_id || i.request_id || "")}</code></div>`).join("")}</div></details>` : ""}</section>`).join("")}</div>`;
}


function communicationHTML(task) {
  if (closed(task) || task.kind !== "AGENT" || task.task_type === "case.coordination") return "";
  const contacts = state.overview.contacts.filter(c=>c.case_id === task.case_id);
  const draft = state.communicationDraft?.task === task.id ? state.communicationDraft : {};
  return `<details class="task-memory"><summary>Simulate call</summary><p>No phone call is placed. Paste reported speech to test classification, memory and human review.</p><form data-demo-call="${e(task.id)}"><label>Speaker<select name="contact_id" required><option value="" disabled ${draft.contact_id ? "" : "selected"}>Select who spoke</option>${contacts.map(c=>`<option value="${e(c.id)}" ${draft.contact_id === c.id ? "selected" : ""}>${e(c.name)}</option>`).join("")}</select></label><label>Transcript<textarea name="transcript" rows="5" maxlength="10000" required placeholder="We found the records. Please do not call again until we receive an updated authorization.">${e(draft.transcript || "")}</textarea></label><p class="inline-note">Use a full date with a year, or a clear delay such as “call back in 3 days”. A date-only commitment schedules follow-up at 09:00 in ${e(Intl.DateTimeFormat().resolvedOptions().timeZone)}. A promise does not count as received records.</p><button class="primary" type="submit">Save simulated call</button></form></details>`;
}
document.addEventListener("input",event=>{
  const form=event.target.closest("[data-demo-call]");
  if(form)state.communicationDraft={task:form.dataset.demoCall,...Object.fromEntries(new FormData(form))};
});
document.addEventListener("submit",async event=>{
  const form=event.target.closest("[data-demo-call]");if(!form)return;
  event.preventDefault();const button=form.querySelector("button");button.disabled=true;
  const body={...Object.fromEntries(new FormData(form)),timezone:Intl.DateTimeFormat().resolvedOptions().timeZone};
  const intent=JSON.stringify(body),target=`${taskRoot(form.dataset.demoCall)}/demo-call`;
  if(state.communicationWrite?.intent!==intent || state.communicationWrite?.target!==target)state.communicationWrite={intent,target,key:crypto.randomUUID()};
  try{
    await api(target,{method:"POST",headers:{"Idempotency-Key":state.communicationWrite.key},body:intent});
    state.communicationWrite=null;state.communicationDraft=null;
    toast("Simulated call saved. Interpretation queued; watch Activity and memory.");
    await refreshDetail(true);await refresh();
  }catch(err){toast(err.message);}finally{button.disabled=false;}
});
