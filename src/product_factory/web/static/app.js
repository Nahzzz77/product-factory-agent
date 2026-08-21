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
  else if (state.projects.length) await openProject(state.projects[0].path);
  else showEmpty();
}

function showEmpty() {
  state.current = null;
  text("#breadcrumb", "工作台");
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
  renderAgentAvailability(item);
  renderRun(item.agent_run);
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
  if (!run) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden"); state.runId = run.run_id;
  text("#agent-run-status", run.status === "running" ? "Codex 正在工作…" : run.status === "completed" ? "Codex 已完成" : "Codex 执行失败");
  text("#agent-output", run.output || "等待输出…");
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

async function copyPath(value, message) {
  if (!navigator.clipboard) { showToast("当前浏览器不支持自动复制"); return; }
  try { await navigator.clipboard.writeText(value); showToast(message); }
  catch { showToast("复制失败，请手动选择路径"); }
}

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
$("#close-agent").addEventListener("click", closeAgent);
$("#create-form").addEventListener("submit", createProject);
$("#refresh").addEventListener("click", () => loadProjects(state.current?.path).catch((error) => showToast(error.message)));
$("#start-agent").addEventListener("click", startAgent);
$("#refresh-run").addEventListener("click", refreshRun);
$("#project-path").addEventListener("click", () => copyPath(state.current?.path || "", "项目路径已复制"));
$("#workspace-button").addEventListener("click", () => copyPath(state.config?.workspace || "", "工作区路径已复制"));

Promise.all([loadConfig(), loadProjects()]).catch((error) => showToast(error.message));
