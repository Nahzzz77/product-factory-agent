const state = { config: null, projects: [], current: null, runId: null, runTimer: null };
const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const error = payload.error || { message: "操作失败" };
    throw new Error(error.message || error.code || "操作失败");
  }
  return payload.data ?? payload;
}

function text(selector, value) { $(selector).textContent = value ?? "—"; }
function showToast(message) {
  const toast = $("#toast"); toast.textContent = message; toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 2600);
}

async function loadConfig() {
  state.config = await api("/api/config");
  text("#workspace", state.config.workspace);
  const status = $("#codex-status");
  status.textContent = state.config.codex_available ? "已就绪" : "未找到";
  status.style.color = state.config.codex_available ? "#26713c" : "#a73535";
  $("#start-agent").disabled = !state.config.codex_available;
  if (state.current) renderAgentAvailability(state.current);
}

async function loadProjects(selectPath = null) {
  const payload = await api("/api/projects");
  state.projects = payload.projects;
  const list = $("#project-list"); list.replaceChildren();
  for (const project of state.projects) {
    const button = document.createElement("button"); button.className = "project-link";
    const strong = document.createElement("strong"); strong.textContent = project.name;
    const small = document.createElement("small"); small.textContent = `${labelState(project.workflow_state)} · 版本 ${project.revision}`;
    button.append(strong, small);
    button.addEventListener("click", () => openProject(project.path));
    if (state.current?.path === project.path) button.classList.add("active");
    list.append(button);
  }
  const target = selectPath || state.current?.path;
  if (target) await openProject(target);
  else if (state.projects.length) showDashboard();
  else showEmpty();
}

function showEmpty() {
  state.current = null;
  text("#breadcrumb", "工作台");
  $("#home-link").classList.add("active");
  $("#empty-view").classList.remove("hidden");
  $("#dashboard-view").classList.add("hidden");
  $("#project-view").classList.add("hidden");
}

function showDashboard() {
  if (!state.projects.length) { showEmpty(); return; }
  state.current = null;
  text("#breadcrumb", "工作台");
  $("#home-link").classList.add("active");
  $("#empty-view").classList.add("hidden");
  $("#project-view").classList.add("hidden");
  $("#dashboard-view").classList.remove("hidden");
  renderDashboard();
  renderProjectListOnly();
}

function renderDashboard() {
  const pending = state.projects.filter((project) => project.waiting_on || ["adaptation_pending_approval", "human_acceptance_pending"].includes(project.workflow_state));
  const agents = state.projects.filter((project) => project.agent_run?.status === "running");
  const issues = state.projects.filter((project) => !project.valid);
  text("#metric-projects", state.projects.length);
  text("#metric-pending", pending.length);
  text("#metric-agents", agents.length);
  text("#metric-issues", issues.length);
  text("#project-count", `${state.projects.length} 个项目`);
  const cards = $("#project-cards"); cards.replaceChildren();
  for (const project of state.projects) cards.append(projectCard(project));
  const pendingList = $("#pending-list"); pendingList.replaceChildren();
  if (!pending.length) pendingList.append(emptyMessage("目前没有等待你确认的事项"));
  for (const project of pending) {
    const row = document.createElement("button"); row.className = "pending-item";
    const icon = document.createElement("span"); icon.textContent = "!";
    const copy = document.createElement("div");
    const title = document.createElement("b"); title.textContent = project.name;
    const hint = document.createElement("small"); hint.textContent = project.workflow_state === "human_acceptance_pending" ? "等待你完成阶段验收" : "等待你确认技术方案";
    copy.append(title, hint); row.append(icon, copy); row.addEventListener("click", () => openProject(project.path)); pendingList.append(row);
  }
  const recent = $("#recent-projects"); recent.replaceChildren();
  for (const project of [...state.projects].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))).slice(0, 6)) {
    const row = document.createElement("button"); row.className = "recent-item";
    const dot = document.createElement("span"); const copy = document.createElement("div");
    const title = document.createElement("b"); title.textContent = project.name;
    const time = document.createElement("small"); time.textContent = `${labelState(project.workflow_state)} · ${formatTime(project.updated_at)}`;
    copy.append(title, time); row.append(dot, copy); row.addEventListener("click", () => openProject(project.path)); recent.append(row);
  }
}

function projectCard(project) {
  const card = document.createElement("article"); card.className = "project-card"; card.tabIndex = 0;
  const head = document.createElement("div"); head.className = "project-card-head";
  const icon = document.createElement("span"); icon.className = "project-card-icon"; icon.textContent = project.name.slice(0, 1).toUpperCase();
  const badge = document.createElement("span"); badge.className = "project-card-state"; badge.textContent = labelState(project.workflow_state); head.append(icon, badge);
  const title = document.createElement("h3"); title.textContent = project.name;
  const stage = document.createElement("p"); stage.textContent = `${project.current_stage.id} · 版本 ${project.revision}`;
  const bar = document.createElement("div"); bar.className = "mini-progress"; const fill = document.createElement("span"); fill.style.width = `${progressPercent(project.workflow_state)}%`; bar.append(fill);
  const footer = document.createElement("div"); footer.className = "project-card-footer";
  const next = document.createElement("span"); next.textContent = project.waiting_on ? "等待人工确认" : "可继续工作";
  const time = document.createElement("span"); time.textContent = formatTime(project.updated_at); footer.append(next, time);
  card.append(head, title, stage, bar, footer);
  card.addEventListener("click", () => openProject(project.path));
  card.addEventListener("keydown", (event) => { if (event.key === "Enter") openProject(project.path); });
  return card;
}

function emptyMessage(message) { const item = document.createElement("div"); item.className = "empty-list"; item.textContent = message; return item; }

async function openProject(path) {
  const payload = await api(`/api/project?path=${encodeURIComponent(path)}`);
  state.current = payload;
  $("#home-link").classList.remove("active");
  $("#empty-view").classList.add("hidden");
  $("#dashboard-view").classList.add("hidden");
  $("#project-view").classList.remove("hidden");
  switchTab("overview");
  renderProject();
  await renderProjectListOnly();
  await loadRunHistory();
}

async function renderProjectListOnly() {
  const list = $("#project-list");
  [...list.children].forEach((button, index) => button.classList.toggle("active", state.projects[index]?.path === state.current?.path));
}

function renderProject() {
  const item = state.current;
  const stage = item.project.stage_plan.find((entry) => entry.id === item.state.current_stage.id);
  text("#breadcrumb", `项目 / ${item.project.name}`);
  text("#project-id", item.project.project_id);
  text("#project-stage", stage?.name || item.state.current_stage.id);
  text("#project-name", item.project.name);
  text("#project-path", item.path);
  text("#workflow-state", labelState(item.state.workflow_state));
  text("#revision", `r${item.state.revision}`);
  text("#completion", labelCompletion(item.state.current_stage.completion_level));
  text("#lock-status", item.lock ? "使用中" : "空闲");
  text("#step-pill", item.state.current_stage.id);
  text("#next-command", item.recovery.next_command);
  const health = $("#health-badge");
  const healthDot = document.createElement("span");
  health.replaceChildren(healthDot, document.createTextNode(item.validation.valid ? "项目记录正常" : "项目需要修复"));
  health.classList.toggle("invalid", !item.validation.valid);
  const findings = $("#findings"); findings.replaceChildren();
  for (const finding of item.validation.findings) { const li = document.createElement("li"); li.textContent = finding; findings.append(li); }
  renderAction(item);
  renderProgress(item);
  renderProjectContent(item);
  renderAgentAvailability(item);
  renderRun(item.agent_run);
}

function renderProjectContent(item) {
  const documents = new Map(item.documents.map((document) => [document.id, document]));
  const prd = documents.get("prd"); const plan = documents.get("technical-adaptation");
  text("#prd-path", prd?.path); text("#prd-content", prd?.content || "产品需求文档不可读取。");
  text("#plan-path", plan?.path); text("#plan-content", plan?.exists ? plan.content : "技术方案还没有生成。\n\n返回“总览”，让 Codex 阅读 PRD 并准备技术适配方案。");
  text("#plan-status", plan?.exists ? "已生成" : "尚未生成");
  text("#development-status", labelState(item.state.workflow_state));
  text("#development-stage", item.state.current_stage.id);
  text("#development-completion", labelCompletion(item.state.current_stage.completion_level));
  text("#development-revision", `r${item.state.revision}`);
  const run = item.agent_run;
  text("#development-run", run ? `最近任务：${run.objective} · ${runSummaryLabel(run.status)}` : "还没有 Codex 运行记录。完成需求确认和技术方案后，可以从这里启动开发。" );
  text("#evidence-count", item.stats.evidence); text("#event-count", item.stats.events); text("#approval-count", item.stats.approvals);
  const verified = item.state.current_stage.completion_level === "system_verified" || item.state.current_stage.completion_level === "human_accepted";
  text("#evidence-status", verified ? "验证通过" : `${item.stats.evidence} 份证据`);
  text("#verification-title", verified ? "当前阶段已经通过系统验证" : item.state.workflow_state === "system_verification" ? "正在准备系统验证" : "等待进入系统验证");
  text("#verification-description", verified ? "证据已经绑定当前代码，可以进入人工验收。" : "完成开发后，登记测试、构建和真实运行证据。" );
  const accepted = item.state.current_stage.completion_level === "human_accepted";
  $("#delivery-development").classList.toggle("complete", verified || accepted);
  $("#delivery-acceptance").classList.toggle("complete", accepted);
  text("#delivery-title", accepted ? "当前阶段已经完成交付" : "项目仍在开发流程中");
  text("#delivery-description", accepted ? "需求、开发、验证和人工验收记录都已保存，可以规划下一阶段。" : "完成当前阶段验证并由你亲自验收后，才会进入下一项交付工作。" );
  renderDocuments(item.documents); renderActivity(item.activity);
}

function renderDocuments(documents) {
  const root = $("#document-list"); root.replaceChildren();
  for (const document of documents) {
    const row = documentRow(document); root.append(row);
  }
}

function documentRow(document) {
  const row = window.document.createElement("button"); row.className = `document-row${document.exists ? "" : " missing"}`;
  const icon = window.document.createElement("span"); icon.textContent = "MD";
  const copy = window.document.createElement("div"); const title = window.document.createElement("b"); title.textContent = document.title;
  const path = window.document.createElement("small"); path.textContent = document.path; copy.append(title, path);
  const status = window.document.createElement("em"); status.textContent = document.exists ? "已就绪" : "未生成";
  row.append(icon, copy, status); row.addEventListener("click", () => switchTab(document.id === "prd" ? "requirements" : document.id === "technical-adaptation" ? "plan" : "requirements")); return row;
}

function renderActivity(activity) {
  const root = $("#activity-list"); root.replaceChildren();
  if (!activity.length) { root.append(emptyMessage("项目刚刚创建，后续操作会记录在这里")); return; }
  for (const item of activity.slice(0, 6)) {
    const row = document.createElement("div"); row.className = "activity-row";
    const dot = document.createElement("span"); dot.className = "activity-dot";
    const copy = document.createElement("div"); const title = document.createElement("b"); title.textContent = activityLabel(item.type);
    const meta = document.createElement("small"); meta.textContent = `${formatTime(item.created_at)} · 版本 ${item.revision}`; copy.append(title, meta); row.append(dot, copy); root.append(row);
  }
}

function switchTab(tab) {
  document.querySelectorAll("#project-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.panel !== tab));
}

function renderProgress(item) {
  const order = ["input", "plan", "build", "verify", "accept"];
  const current = ({
    initialized: "input",
    inputs_checked: "plan",
    adaptation_pending_approval: "plan",
    stage_development: "build",
    system_verification: "verify",
    human_acceptance_pending: "accept",
    next_stage_or_frontend: "done",
  })[item.state.workflow_state] || "input";
  const currentIndex = current === "done" ? order.length : order.indexOf(current);
  document.querySelectorAll("#progress li").forEach((node, index) => {
    node.classList.toggle("done", index < currentIndex || current === "done");
    node.classList.toggle("current", index === currentIndex && current !== "done");
  });
}

function agentAllowed(item = state.current) {
  return Boolean(
    item && state.config?.codex_available && !item.state.waiting_on &&
    ["inputs_checked", "stage_development", "system_verification"].includes(item.state.workflow_state)
  );
}

function renderAgentAvailability(item) {
  const allowed = agentAllowed(item);
  const status = $("#codex-status");
  if (!state.config?.codex_available) status.textContent = "未找到";
  else status.textContent = allowed ? "已就绪" : "等待可执行阶段";
  status.style.color = allowed ? "#26713c" : "#a06b19";
  $("#start-agent").disabled = !allowed;
  document.querySelectorAll("[data-open-agent]").forEach((button) => { button.disabled = !allowed; });
  $("#plan-agent").disabled = !allowed;
  $("#development-agent").disabled = !allowed;
}

function actionCard(title, description) {
  const card = document.createElement("div"); card.className = "action-card";
  const heading = document.createElement("h3"); heading.textContent = title;
  const paragraph = document.createElement("p"); paragraph.textContent = description;
  card.append(heading, paragraph); return card;
}

function inputField(label, name, value = "") {
  const wrapper = document.createElement("label"); wrapper.textContent = label;
  const input = document.createElement("input"); input.name = name; input.value = value;
  wrapper.append(input); return wrapper;
}

function actionButton(card, label, action, payload = () => ({}), style = "primary") {
  const button = document.createElement("button"); button.className = style; button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await performAction(action, payload(card)); showToast("操作完成"); }
    catch (error) { showToast(error.message); }
    finally { button.disabled = false; }
  });
  card.append(button);
}

function agentSuggestion(card, title, description, objective) {
  const box = document.createElement("div"); box.className = "agent-suggestion";
  const copy = document.createElement("div");
  const spark = document.createElement("span"); spark.className = "spark"; spark.textContent = "✦";
  const words = document.createElement("span");
  const strong = document.createElement("b"); strong.textContent = title;
  const small = document.createElement("small"); small.textContent = description;
  words.append(strong, small); copy.append(spark, words);
  const button = document.createElement("button"); button.textContent = "交给 Codex"; button.dataset.openAgent = "true";
  button.addEventListener("click", () => openAgent(objective));
  box.append(copy, button); card.append(box);
}

function renderAction(item) {
  const area = $("#action-area"); area.replaceChildren();
  const workflow = item.state.workflow_state;
  const completion = item.state.current_stage.completion_level;
  let card;
  if (!item.validation.valid) {
    card = actionCard("这个项目需要先检查一下", "记录中发现了不一致。为避免误改项目，产品工厂已经暂停写入。请展开右侧技术详情查看原因。");
  } else if (workflow === "initialized") {
    card = actionCard("先确认这份需求可以开工", "产品工厂会检查 PRD 是否与创建时确认的版本一致，以及必要信息是否齐全。这个操作不会修改你的 PRD。");
    actionButton(card, "确认需求，进入下一步", "check_inputs");
  } else if (workflow === "inputs_checked") {
    card = actionCard("准备一份你能看懂的技术方案", "先让 Codex 阅读 PRD，写清楚准备怎么做、有什么风险、怎样验收。方案完成后，再提交给你确认。");
    agentSuggestion(card, "建议交给 Codex 准备", "它会停在审批前，不会直接开始开发", "阅读 PRD 和项目资料，生成 docs/technical-adaptation.md。方案要用产品负责人能理解的语言说明技术路线、前后端边界、数据存储、主要风险、费用与安全边界、测试和验收方法。不要推进状态或替我审批。");
    card.append(inputField("已经准备好的方案文件", "artifact", "docs/technical-adaptation.md"));
    actionButton(card, "提交给我确认", "request_adaptation", (root) => ({ artifact: root.querySelector("input").value }), "secondary");
  } else if (workflow === "adaptation_pending_approval" || workflow === "human_acceptance_pending") {
    const stageGate = workflow === "human_acceptance_pending";
    card = actionCard(stageGate ? "请亲自体验并验收这个阶段" : "请确认这份技术方案", stageGate ? "打开实际产品，按照验收清单走完核心流程。确认体验和结果符合 PRD 后，再在这里批准。" : "阅读技术适配文档，重点检查范围、费用、平台和暂缓项。只有你能批准进入开发。");
    card.append(inputField("确认人", "actor", "product-owner"));
    card.append(inputField("批准语句", "statement", state.config?.approval_statement || "验收通过，批准进入下一阶段。"));
    actionButton(card, stageGate ? "验收通过，进入下一阶段" : "批准方案，开始开发", "approve", (root) => ({
      actor: root.querySelector('[name="actor"]').value,
      statement: root.querySelector('[name="statement"]').value,
    }));
  } else if (workflow === "stage_development") {
    card = actionCard("开始完成这个阶段的产品", "Codex 可以在当前项目中编码、测试和修复。它完成后会把改动和验证结果交给你，不会自行跨过验收。");
    agentSuggestion(card, "让 Codex 开始开发", "严格按照当前 PRD 和已经批准的技术方案执行", "完成当前阶段的开发任务。严格依据 inputs/PRD.md 和已批准的 docs/technical-adaptation.md，先检查现有项目，再实现、测试并进行浏览器验证。不要推进产品工厂状态，不要替我验收或部署。");
    actionButton(card, "代码已经完成，进入系统验证", "start_verification", () => ({}), "secondary");
  } else if (workflow === "system_verification" && completion === "implemented") {
    card = actionCard("用测试结果证明它真的能运行", "登记本阶段的测试、构建和真实运行结果。证据会绑定当前代码，之后修改代码就需要重新验证。");
    agentSuggestion(card, "需要补测试或修复？", "Codex 可以继续在当前阶段处理问题", "检查当前阶段实现和测试结果，修复未通过的问题，完成真实运行与浏览器检查，并准备 evidence-authoring.yaml。不要登记证据、推进状态或替我验收。");
    card.append(inputField("证据清单文件", "manifest", "evidence-authoring.yaml"));
    card.append(inputField("证据 ID", "evidence_id", "evidence-01"));
    actionButton(card, "登记证据", "record_evidence", (root) => ({ manifest: root.querySelector('[name="manifest"]').value }));
    actionButton(card, "验证当前代码", "verify_stage", (root) => ({ evidence_id: root.querySelector('[name="evidence_id"]').value }), "secondary");
  } else if (workflow === "system_verification" && completion === "system_verified") {
    card = actionCard("系统检查通过，轮到你体验了", "自动测试和运行证据已经通过。下一步不会继续写代码，而是请你亲自判断产品是否真的好用。");
    actionButton(card, "开始人工验收", "request_acceptance");
  } else if (workflow === "next_stage_or_frontend") {
    card = actionCard("这个阶段已经完成", "你的验收结果、测试证据和项目状态都已经保存。当前版本将在这里安全停下，等待后续阶段能力。");
  } else {
    card = actionCard("查看恢复建议", "当前状态没有可由本版控制台安全执行的动作。");
  }
  area.append(card);
}

async function performAction(action, extra = {}) {
  state.current = await api("/api/action", {
    method: "POST",
    body: JSON.stringify({ project_path: state.current.path, action, ...extra }),
  });
  renderProject();
  await loadProjects(state.current.path);
}

function renderRun(run) {
  const box = $("#agent-run");
  if (!run) { box.classList.add("hidden"); clearRunTimer(); return; }
  box.classList.remove("hidden"); state.runId = run.run_id;
  text("#agent-run-status", runStatusLabel(run.status));
  text("#agent-output", run.output || "等待输出…");
  $("#cancel-run").classList.toggle("hidden", !["running", "cancelling"].includes(run.status));
  if (["running", "cancelling"].includes(run.status)) scheduleRunPoll();
  else clearRunTimer();
}

function openAgent(objective = "") {
  if (objective) $("#agent-objective").value = objective;
  renderAgentAvailability(state.current);
  $("#agent-dialog").showModal();
}

function closeAgent() { $("#agent-dialog").close(); }

async function startAgent() {
  if (!state.current) return;
  const objective = $("#agent-objective").value.trim();
  if (!objective) { showToast("请先填写本次开发目标"); return; }
  const button = $("#start-agent"); button.disabled = true;
  try {
    const run = await api("/api/agent-runs", { method: "POST", body: JSON.stringify({ project_path: state.current.path, objective }) });
    renderRun(run); await loadRunHistory(); showToast("Codex 已启动");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = !agentAllowed(); }
}

async function refreshRun() {
  if (!state.runId) return;
  if (!state.current) return;
  try {
    const run = await api(`/api/agent-runs/${encodeURIComponent(state.runId)}?project_path=${encodeURIComponent(state.current.path)}`);
    renderRun(run);
    if (!["running", "cancelling"].includes(run.status)) {
      const activeTab = document.querySelector("#project-tabs button.active")?.dataset.tab || "overview";
      state.current = await api(`/api/project?path=${encodeURIComponent(state.current.path)}`);
      renderProject(); switchTab(activeTab);
      await loadRunHistory();
    }
  }
  catch (error) { showToast(error.message); }
}

function clearRunTimer() {
  if (state.runTimer) window.clearTimeout(state.runTimer);
  state.runTimer = null;
}

function scheduleRunPoll() {
  clearRunTimer();
  state.runTimer = window.setTimeout(() => refreshRun(), 900);
}

async function cancelRun() {
  if (!state.current || !state.runId) return;
  const button = $("#cancel-run"); button.disabled = true;
  try {
    const run = await api(`/api/agent-runs/${encodeURIComponent(state.runId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ project_path: state.current.path }),
    });
    renderRun(run); showToast("正在停止 Codex 任务");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
}

async function loadRunHistory() {
  const root = $("#run-history");
  if (!state.current) { root.replaceChildren(); return; }
  try {
    const payload = await api(`/api/agent-runs?project_path=${encodeURIComponent(state.current.path)}`);
    root.replaceChildren();
    if (!payload.runs.length) { root.append(emptyMessage("还没有 Codex 运行记录")); return; }
    for (const run of payload.runs.slice(0, 10)) {
      const row = document.createElement("button"); row.className = "run-history-row";
      const status = document.createElement("span"); status.className = `run-status ${run.status}`; status.textContent = runStatusLabel(run.status).replace("Codex ", "");
      const copy = document.createElement("span"); const title = document.createElement("strong"); title.textContent = run.objective;
      const meta = document.createElement("small"); meta.textContent = run.exit_code === null ? "点击查看输出" : `退出码 ${run.exit_code} · 点击查看输出`; copy.append(title, meta);
      const time = document.createElement("time"); time.textContent = formatTime(run.started_at);
      row.append(status, copy, time); row.addEventListener("click", () => { renderRun(run); $("#agent-dialog").showModal(); }); root.append(row);
    }
  } catch (error) { root.replaceChildren(emptyMessage(error.message)); }
}

function runStatusLabel(status) {
  return ({ running: "Codex 正在工作…", cancelling: "正在停止…", completed: "Codex 已完成", failed: "Codex 执行失败", cancelled: "任务已停止", interrupted: "任务已中断" })[status] || status;
}

function runSummaryLabel(status) {
  return ({ running: "正在运行", cancelling: "正在停止", completed: "已完成", failed: "执行失败", cancelled: "已停止", interrupted: "已中断" })[status] || status;
}

function labelState(value) { return ({ initialized: "已初始化", inputs_checked: "输入已确认", adaptation_pending_approval: "等待技术审批", stage_development: "阶段开发", system_verification: "系统验证", human_acceptance_pending: "等待人工验收", next_stage_or_frontend: "里程碑完成" })[value] || value; }
function labelCompletion(value) { return ({ none: "尚未实现", implemented: "已实现", system_verified: "系统已验证", human_accepted: "人工已验收" })[value] || value; }
function progressPercent(value) { return ({ initialized: 8, inputs_checked: 22, adaptation_pending_approval: 34, stage_development: 52, system_verification: 72, human_acceptance_pending: 88, next_stage_or_frontend: 100 })[value] || 5; }
function activityLabel(value) { return ({ inputs_checked: "项目输入已确认", approval_requested: "发起人工确认", approval_consumed: "人工确认已记录", implementation_recorded: "开发实现已完成", evidence_recorded: "验证证据已登记", system_verified: "系统验证已通过", technical_adaptation: "技术方案已批准", stage_acceptance: "阶段验收已批准" })[value] || value.replaceAll("_", " "); }
function formatTime(value) { if (!value) return "刚刚"; const date = new Date(value); return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date); }

function openCreate() { $("#create-error").classList.add("hidden"); $("#create-dialog").showModal(); }
function closeCreate() { $("#create-dialog").close(); }

async function copyPath(value, message) {
  if (!navigator.clipboard) { showToast("当前浏览器不支持自动复制"); return; }
  try { await navigator.clipboard.writeText(value); showToast(message); }
  catch { showToast("复制失败，请手动选择路径"); }
}

async function createProject(event) {
  event.preventDefault();
  const form = event.currentTarget; const data = new FormData(form);
  const payload = Object.fromEntries(data.entries());
  delete payload.prd_file; delete payload.constraints_file;
  for (const key of ["prd_confirmed", "requirements_confirmed", "requires_real_model"]) payload[key] = data.has(key);
  const errorBox = $("#create-error"); errorBox.classList.add("hidden");
  const submit = form.querySelector('[type="submit"]'); submit.disabled = true;
  try {
    const prd = $("#prd-file").files[0];
    if (!prd) throw new Error("请选择 PRD 需求文档");
    if (prd.size > 1_000_000) throw new Error("PRD 文件不能超过 1 MB");
    payload.prd_content = await prd.text(); payload.prd_filename = prd.name;
    const constraints = $("#constraints-file").files[0];
    if (constraints) {
      if (constraints.size > 1_000_000) throw new Error("约束文件不能超过 1 MB");
      payload.constraints_content = await constraints.text(); payload.constraints_filename = constraints.name;
    }
    const created = await api("/api/projects", { method: "POST", body: JSON.stringify(payload) });
    closeCreate(); form.reset(); updateFileName("prd"); updateFileName("constraints"); showToast("项目已创建"); await loadProjects(created.path);
  } catch (error) { errorBox.textContent = error.message; errorBox.classList.remove("hidden"); }
  finally { submit.disabled = false; }
}

function updateFileName(kind) {
  const file = $(`#${kind}-file`).files[0];
  text(`#${kind}-file-name`, file ? `${file.name} · ${Math.max(1, Math.round(file.size / 1024))} KB` : kind === "prd" ? "选择 Markdown 或文本文件" : "选择约束文件");
}

$("#new-project").addEventListener("click", openCreate);
$("#empty-create").addEventListener("click", openCreate);
$("#dashboard-create").addEventListener("click", openCreate);
$("#home-link").addEventListener("click", showDashboard);
$("#close-dialog").addEventListener("click", closeCreate);
$("#cancel-create").addEventListener("click", closeCreate);
$("#close-agent").addEventListener("click", closeAgent);
$("#create-form").addEventListener("submit", createProject);
$("#refresh").addEventListener("click", () => loadProjects(state.current?.path).catch((error) => showToast(error.message)));
$("#start-agent").addEventListener("click", startAgent);
$("#refresh-run").addEventListener("click", refreshRun);
$("#cancel-run").addEventListener("click", cancelRun);
$("#refresh-history").addEventListener("click", loadRunHistory);
$("#prd-file").addEventListener("change", () => updateFileName("prd"));
$("#constraints-file").addEventListener("change", () => updateFileName("constraints"));
$("#project-path").addEventListener("click", () => copyPath(state.current?.path || "", "项目路径已复制"));
$("#workspace-button").addEventListener("click", () => copyPath(state.config?.workspace || "", "工作区路径已复制"));
document.querySelectorAll("#project-tabs button").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
document.querySelectorAll("[data-go-tab]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.goTab)));
$("#plan-agent").addEventListener("click", () => openAgent("阅读 PRD 和项目资料，生成 docs/technical-adaptation.md。用产品负责人能理解的语言说明技术路线、前后端边界、数据存储、主要风险、费用与安全边界、测试和验收方法。不要推进状态或替我审批。"));
$("#development-agent").addEventListener("click", () => openAgent("完成当前阶段的开发任务。严格依据 inputs/PRD.md 和已批准的 docs/technical-adaptation.md，实现、测试并进行浏览器验证。不要推进产品工厂状态，不要替我验收或部署。"));
$("#copy-prd").addEventListener("click", () => copyPath($("#prd-content").textContent, "PRD 内容已复制"));

if (window.location.protocol === "file:") {
  document.body.classList.add("direct-file-open");
  $("#direct-open-notice").classList.remove("hidden");
} else {
  Promise.all([loadConfig(), loadProjects()]).catch((error) => showToast(error.message));
}
