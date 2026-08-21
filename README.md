# 产品工厂 Agent

产品工厂 Agent 是一个本地文件驱动的 Web 产品交付系统。它从已确认 PRD 初始化全新项目，以租约锁保护写入，以人工审批和不可变证据推进 `initialized` 到 `next_stage_or_frontend`，并提供可直接操作的本地浏览器工作台。

本版本不接管遗留项目，也不创建原生 App、小程序、桌面端或游戏项目。它不包含 GitHub 远程仓库、云资源、自动部署、公开访问、AI Web 适配器或图像适配器。工作台可以在用户明确点击后启动本机已经安装并登录的 Codex；Codex 仍受工作区沙箱和阶段规则约束。

## 安装

在 Python 3.11 或更高版本的本地环境中运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/product-factory --help
```

所有命令都在本地运行。不要把任何敏感凭据放入命令参数、项目文件、日志、证据或终端输出。

## 推荐用法：本地 Web 工作台

安装后运行：

```bash
.venv/bin/product-factory web \
  --workspace /Users/你的用户名/ProductFactoryProjects
```

系统默认只监听 `127.0.0.1:8765` 并打开浏览器。工作台提供：

- 在指定工作区中创建全新项目；
- 在浏览器里直接选择 PRD 和可选约束文件，无需手填绝对路径；
- 查看当前阶段、修订号、执行锁、恢复建议和协议健康状态；
- 通过临时租约安全执行输入确认、技术审批、系统验证、证据登记和阶段验收；
- 在允许的执行阶段，明确点击后启动本机 Codex 完成当前开发目标；
- 实时查看 Codex 输出、停止运行中的任务，并在服务重启后继续查看历史日志；
- 保存所有业务状态、审批、事件和证据到目标项目，而不是依赖浏览器会话。

创建项目时，页面要求产品负责人明确确认两件事：PRD 是当前业务事实基线，以及 PRD 已覆盖协议要求的七类最低信息。工作台不会把模糊想法自动批准成 PRD。

如果不希望自动打开浏览器：

```bash
.venv/bin/product-factory web --no-open
```

监听非本机地址默认被拒绝。除非已经配置可信网络隔离，否则不要使用 `--allow-network`。

## 最小离线示例

以下命令使用仓库中的 `examples/minimal-project/`。先进入仓库根目录，示例中的 `LOCK_ID` 请替换为上一条 `lock acquire` JSON 输出里的 `details.lock.lock_id`。

```bash
.venv/bin/product-factory --json init \
  --project /tmp/minimal-web \
  --project-id minimal-web \
  --name 离线任务清单 \
  --prd examples/minimal-project/PRD.md \
  --intake examples/minimal-project/intake.yaml \
  --stage stage-01:离线核心

cp examples/minimal-project/technical-adaptation.md /tmp/minimal-web/docs/technical-adaptation.md
cp examples/minimal-project/evidence-manifest.yaml /tmp/minimal-web/evidence-authoring.yaml

.venv/bin/product-factory --json lock acquire \
  --project /tmp/minimal-web --tool operator --session-id check-inputs --lease-seconds 120
.venv/bin/product-factory --json check-inputs \
  --project /tmp/minimal-web --lock-id LOCK_ID --expected-revision 0
.venv/bin/product-factory --json lock release --project /tmp/minimal-web --lock-id LOCK_ID

.venv/bin/product-factory --json lock acquire \
  --project /tmp/minimal-web --tool operator --session-id request-adaptation --lease-seconds 120
.venv/bin/product-factory --json request-approval \
  --project /tmp/minimal-web --gate technical_adaptation \
  --artifact docs/technical-adaptation.md --lock-id LOCK_ID --expected-revision 1
.venv/bin/product-factory --json lock release --project /tmp/minimal-web --lock-id LOCK_ID

.venv/bin/product-factory --json lock acquire \
  --project /tmp/minimal-web --tool operator --session-id approve-adaptation --lease-seconds 120
.venv/bin/product-factory --json approve \
  --project /tmp/minimal-web --actor product-owner --lock-id LOCK_ID --expected-revision 2
```

`approve` 会交互式提示输入。两次审批都必须在提示处手工键入完全相同的语句：`验收通过，批准进入下一阶段。`

```bash
.venv/bin/product-factory --json lock release --project /tmp/minimal-web --lock-id LOCK_ID
.venv/bin/product-factory --json lock acquire \
  --project /tmp/minimal-web --tool operator --session-id system-verification --lease-seconds 120
.venv/bin/product-factory --json transition \
  --project /tmp/minimal-web --to system_verification --lock-id LOCK_ID --expected-revision 3
.venv/bin/product-factory --json lock release --project /tmp/minimal-web --lock-id LOCK_ID

.venv/bin/product-factory --json lock acquire \
  --project /tmp/minimal-web --tool operator --session-id evidence --lease-seconds 120
.venv/bin/product-factory --json record-evidence \
  --project /tmp/minimal-web --manifest evidence-authoring.yaml --lock-id LOCK_ID --expected-revision 4
.venv/bin/product-factory --json verify-stage \
  --project /tmp/minimal-web --evidence-id evidence-01 --lock-id LOCK_ID --expected-revision 4
.venv/bin/product-factory --json lock release --project /tmp/minimal-web --lock-id LOCK_ID

.venv/bin/product-factory --json lock acquire \
  --project /tmp/minimal-web --tool operator --session-id request-stage-acceptance --lease-seconds 120
.venv/bin/product-factory --json request-approval \
  --project /tmp/minimal-web --gate stage_acceptance --lock-id LOCK_ID --expected-revision 5
.venv/bin/product-factory --json lock release --project /tmp/minimal-web --lock-id LOCK_ID

.venv/bin/product-factory --json lock acquire \
  --project /tmp/minimal-web --tool operator --session-id approve-stage-acceptance --lease-seconds 120
.venv/bin/product-factory --json approve \
  --project /tmp/minimal-web --actor product-owner --lock-id LOCK_ID --expected-revision 6
.venv/bin/product-factory --json lock release --project /tmp/minimal-web --lock-id LOCK_ID

.venv/bin/product-factory --json status --project /tmp/minimal-web
.venv/bin/product-factory --json validate --project /tmp/minimal-web
.venv/bin/product-factory --json resume --project /tmp/minimal-web
```

## 项目中的协议文件

- `inputs/PRD.md` 与 `.product-factory/intake.yaml`：确认过的产品输入和七类最低信息声明。
- `.product-factory/state.json`：当前流程事实与递增修订号。
- `.product-factory/approvals.jsonl` 与 `.product-factory/events.jsonl`：只追加的审批和审计记录。
- `.product-factory/execution-lock.json`：单写入者的租约记录；写操作需要它和匹配的修订号。
- `.product-factory/evidence/<stage>/<evidence>/manifest.json`：一经登记即不可覆盖的阶段证据。
- `.product-factory/agent-runs/<run-id>/`：Web 工作台启动的 Codex 任务元数据与输出日志。

命令行入口仍然完整保留，适合自动化或其他 Agent 集成。默认输出适合人阅读。把全局 `--json` 放在命令前会得到稳定的机器可读结果；审批提示仍显示在终端，但批准语句不进入 JSON 结果。

`status`、`resume` 和 `validate` 都是只读操作。`resume` 只给出下一条安全建议；到达 `next_stage_or_frontend` 后会建议等待后续里程碑。`repair-audit` 是唯一允许补写缺失引用事件的修复命令，且不会改变业务状态。

## 验证范围

本版本已在 macOS 上用本地 Python 3.12 环境完成自动化和真实 Chromium 验证。Windows 与 Linux 按文件协议和路径规则设计为兼容，但未进行实机验证。后续协议状态可以被校验，但不是本版本可执行的转换。Codex 任务需要本机已经安装、登录且能够访问相应模型服务；其网络与用量由用户自己的 Codex 配置决定。
