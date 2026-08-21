# 产品工厂 Agent

产品工厂 Agent 是一个离线、本地文件驱动的 Web 产品交付协议内核。它从已确认 PRD 初始化全新项目，以租约锁保护写入，以人工审批和不可变证据推进 `initialized` 到 `next_stage_or_frontend`，并提供只读验证与恢复摘要。

本里程碑不接管遗留项目，也不创建原生 App、小程序、桌面端或游戏项目。它不包含 GitHub 远程仓库、云资源、部署、公开访问、模型调用、AI Web 适配器或图像适配器。

## 安装

在 Python 3.11 或更高版本的本地环境中运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/product-factory --help
```

所有命令都在本地运行。不要把任何敏感凭据放入命令参数、项目文件、日志、证据或终端输出。

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

默认输出适合人阅读。把全局 `--json` 放在命令前会得到稳定的机器可读结果；审批提示仍显示在终端，但批准语句不进入 JSON 结果。

`status`、`resume` 和 `validate` 都是只读操作。`resume` 只给出下一条安全建议；到达 `next_stage_or_frontend` 后会建议等待后续里程碑。`repair-audit` 是唯一允许补写缺失引用事件的修复命令，且不会改变业务状态。

## 验证范围

本版本已在 macOS 上用本地 Python 3.12 环境完成离线自动化验证。Windows 与 Linux 按文件协议和路径规则设计为兼容，但未进行实机验证。后续协议状态可以被校验，但不是本里程碑可执行的转换。
