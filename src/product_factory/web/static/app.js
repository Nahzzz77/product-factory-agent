const state = { config: null, projects: [], current: null, runId: null };
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
}

async function loadProjects(selectPath = null) {
  const payload = await api("/api/projects");
  state.projects = payload.projects;
  const list = $("#project-list"); list.replaceChildren();
  for (const project of state.projects) {
    const button = document.createElement("button"); button.className = "project-link";
    const strong = document.createElement("strong"); strong.textContent = project.name;
    const small = document.createElement("small"); small.textContent = `${project.workflow_state} · r${project.revision}`;
    button.append(strong, small);
    button.addEventListener("click", () => openProject(project.path));
    if (state.current?.path === project.path) button.classList.add("active");
    list.append(button);
  }
  const target = selectPath || state.current?.path;
  if (target) await openProject(target);
  else if (state.projects.length) await openProject(state.projects[0].path);
  else showEmpty();
}

function showEmpty() {
  state.current = null;
  $("#empty-view").classList.remove("hidden");
  $("#project-view").classList.add("hidden");
}

async function openProject(path) {
  const payload = await api(`/api/project?path=${encodeURIComponent(path)}`);
  state.current = payload;
  $("#empty-view").classList.add("hidden");
  $("#project-view").classList.remove("hidden");
  renderProject();
  await renderProjectListOnly();
}

async function renderProjectListOnly() {
  const list = $("#project-list");
  [...list.children].forEach((button, index) => button.classList.toggle("active", state.projects[index]?.path === state.current?.path));
}

function renderProject() {
  const item = state.current;
  text("#project-id", item.project.project_id.toUpperCase());
  text("#project-name", item.project.name);
  text("#project-path", item.path);
  text("#workflow-state", labelState(item.state.workflow_state));
  text("#revision", `r${item.state.revision}`);
  text("#completion", labelCompletion(item.state.current_stage.completion_level));
  text("#lock-status", item.lock ? "使用中" : "空闲");
  text("#step-pill", item.state.current_stage.id);
  text("#next-command", item.recovery.next_command);
  const health = $("#health-badge");
  health.textContent = item.validation.valid ? "协议记录健康" : "需要修复";
  health.classList.toggle("invalid", !item.validation.valid);
  const findings = $("#findings"); findings.replaceChildren();
  for (const finding of item.validation.findings) { const li = document.createElement("li"); li.textContent = finding; findings.append(li); }
  renderAction(item);
  renderAgentAvailability(item);
  renderRun(item.agent_run);
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

function actionButton(card, label, action, payload = () => ({})) {
  const button = document.createElement("button"); button.className = "primary"; button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await performAction(action, payload(card)); showToast("操作完成"); }
    catch (error) { showToast(error.message); }
    finally { button.disabled = false; }
  });
  card.append(button);
}

function renderAction(item) {
  const area = $("#action-area"); area.replaceChildren();
  const workflow = item.state.workflow_state;
  const completion = item.state.current_stage.completion_level;
  let card;
  if (!item.validation.valid) {
    card = actionCard("先修复协议记录", "项目记录存在异常。请查看下方 findings，不会在损坏状态下猜测下一步写操作。");
  } else if (workflow === "initialized") {
    card = actionCard("确认项目输入", "检查 PRD 基线和七类最低信息，成功后进入技术适配阶段。");
    actionButton(card, "检查并确认输入", "check_inputs");
  } else if (workflow === "inputs_checked") {
    card = actionCard("请求技术适配审批", "先让 Codex 生成 docs/technical-adaptation.md，再把这份方案提交给你审批。");
    card.append(inputField("技术方案文件", "artifact", "docs/technical-adaptation.md"));
    actionButton(card, "提交技术方案", "request_adaptation", (root) => ({ artifact: root.querySelector("input").value }));
  } else if (workflow === "adaptation_pending_approval" || workflow === "human_acceptance_pending") {
    const stageGate = workflow === "human_acceptance_pending";
    card = actionCard(stageGate ? "人工阶段验收" : "人工技术方案审批", "这一步只能由你本人完成。批准语句必须与协议文本完全相同。");
    card.append(inputField("确认人", "actor", "product-owner"));
    card.append(inputField("批准语句", "statement", state.config.approval_statement));
    actionButton(card, "确认并批准", "approve", (root) => ({
      actor: root.querySelector('[name="actor"]').value,
      statement: root.querySelector('[name="statement"]').value,
    }));
  } else if (workflow === "stage_development") {
    card = actionCard("当前阶段开发", "让 Codex 完成当前阶段，确认代码和测试已准备好后进入系统验证。");
    actionButton(card, "开发完成，进入验证", "start_verification");
  } else if (workflow === "system_verification" && completion === "implemented") {
    card = actionCard("登记并验证证据", "先登记 evidence-authoring.yaml，再使用不可变证据 ID 验证当前源码。");
    card.append(inputField("证据清单文件", "manifest", "evidence-authoring.yaml"));
    card.append(inputField("证据 ID", "evidence_id", "evidence-01"));
    actionButton(card, "登记证据", "record_evidence", (root) => ({ manifest: root.querySelector('[name="manifest"]').value }));
    actionButton(card, "验证证据", "verify_stage", (root) => ({ evidence_id: root.querySelector('[name="evidence_id"]').value }));
  } else if (workflow === "system_verification" && completion === "system_verified") {
    card = actionCard("请求阶段验收", "系统证据已经通过。现在可以把实际产品交给你亲自操作验收。");
    actionButton(card, "进入人工验收", "request_acceptance");
  } else if (workflow === "next_stage_or_frontend") {
    card = actionCard("首个里程碑已验收", "核心内核已经记录本阶段完成。后续阶段编排将在下一里程碑继续扩展。");
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
  if (!run) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden"); state.runId = run.run_id;
  text("#agent-run-status", run.status === "running" ? "Codex 正在工作…" : run.status === "completed" ? "Codex 已完成" : "Codex 执行失败");
  text("#agent-output", run.output || "等待输出…");
}

async function startAgent() {
  if (!state.current) return;
  const objective = $("#agent-objective").value.trim();
  if (!objective) { showToast("请先填写本次开发目标"); return; }
  const button = $("#start-agent"); button.disabled = true;
  try {
    const run = await api("/api/agent-runs", { method: "POST", body: JSON.stringify({ project_path: state.current.path, objective }) });
    renderRun(run); showToast("Codex 已启动");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = !agentAllowed(); }
}

async function refreshRun() {
  if (!state.runId) return;
  try { const run = await api(`/api/agent-runs/${encodeURIComponent(state.runId)}`); renderRun(run); }
  catch (error) { showToast(error.message); }
}

function labelState(value) { return ({ initialized: "已初始化", inputs_checked: "输入已确认", adaptation_pending_approval: "等待技术审批", stage_development: "阶段开发", system_verification: "系统验证", human_acceptance_pending: "等待人工验收", next_stage_or_frontend: "里程碑完成" })[value] || value; }
function labelCompletion(value) { return ({ none: "尚未实现", implemented: "已实现", system_verified: "系统已验证", human_accepted: "人工已验收" })[value] || value; }

function openCreate() { $("#create-error").classList.add("hidden"); $("#create-dialog").showModal(); }
function closeCreate() { $("#create-dialog").close(); }

async function createProject(event) {
  event.preventDefault();
  const form = event.currentTarget; const data = new FormData(form);
  const payload = Object.fromEntries(data.entries());
  for (const key of ["prd_confirmed", "requirements_confirmed", "requires_real_model"]) payload[key] = data.has(key);
  const errorBox = $("#create-error"); errorBox.classList.add("hidden");
  const submit = form.querySelector('[type="submit"]'); submit.disabled = true;
  try {
    const created = await api("/api/projects", { method: "POST", body: JSON.stringify(payload) });
    closeCreate(); form.reset(); showToast("项目已创建"); await loadProjects(created.path);
  } catch (error) { errorBox.textContent = error.message; errorBox.classList.remove("hidden"); }
  finally { submit.disabled = false; }
}

$("#new-project").addEventListener("click", openCreate);
$("#empty-create").addEventListener("click", openCreate);
$("#close-dialog").addEventListener("click", closeCreate);
$("#cancel-create").addEventListener("click", closeCreate);
$("#create-form").addEventListener("submit", createProject);
$("#refresh").addEventListener("click", () => loadProjects(state.current?.path).catch((error) => showToast(error.message)));
$("#start-agent").addEventListener("click", startAgent);
$("#refresh-run").addEventListener("click", refreshRun);

Promise.all([loadConfig(), loadProjects()]).catch((error) => showToast(error.message));
