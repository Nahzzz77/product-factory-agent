# 产品工厂 Agent 公共流程内核设计

## 1. 文档状态

- 设计状态：已完成会话内分段确认，等待产品负责人审阅本文件。
- 对应 PRD：`docs/product/产品工厂Agent-PRD-V1.0.md`。
- PRD SHA-256：`704ef803283a76ad1e94ff5c186d172d4f02811f9fac6d2d121c6661df294fee`。
- 设计范围：产品工厂 Agent 第一个可运行里程碑，只覆盖公共流程内核。
- 仓库形态：本地 Git 仓库，不配置远程仓库。

## 2. 目标

交付一个离线可运行、工具无关的公共流程内核。它能够从已确认 PRD 初始化一个新的 Web 产品项目，持久化项目状态，执行确定性的阶段门禁，记录人工审批与验证证据，防止并发写入，并在会话或工具切换后输出可靠的恢复摘要。

本里程碑不能只交付目录和 Schema。验收时必须真实演示以下闭环：

```text
初始化项目
→ 检查输入
→ 请求技术适配审批
→ 人工批准
→ 进入阶段开发
→ 登记实现结果和系统验证证据
→ 请求阶段验收
→ 人工批准
→ 进入下一阶段决策点
→ 新会话恢复
```

## 3. 需求来源与裁决顺序

发生冲突时按以下顺序裁决：

1. 产品负责人在当前项目中明确确认的决定。
2. 已确认 PRD 与验收标准。
3. 本设计文件及后续已确认的变更记录。
4. 三份工程手册中的强制底线。
5. 三份工程手册中的默认方案与示例。

已确认的专项裁决如下：

- V1 只正式支持从零创建 Web 产品，不接管遗留项目，不实现原生 App、小程序或桌面客户端。
- 产品工厂内核采用 Python 3.11 及以上版本的本地 CLI。FastAPI 与 Next.js 是未来生成 AI Web 产品时的默认方案，不是公共内核自身的强制形态。
- 密钥不得进入聊天、仓库、日志、前端或证据。用户在本机环境、系统钥匙串或云平台 Secret 中录入；Agent 只检查存在性和可用性，不回显实际值。
- 部署阶段可以按部署手册为专用子账号申请所列服务的 `FullAccess` 权限组合，但实际授权前必须取得单独批准，明确账号、服务、有效期和费用影响；部署完成后必须提示撤销或收紧权限。
- 默认交付终点为“可部署”。创建付费资源、正式上线、公开访问和生产数据操作仍需单独批准。
- 公网地址能打开时，只能进入 `deployed_pending_acceptance`。完成核心闭环、登录、数据隔离、持久化和监控验收后，才能进入 `production_accepted`。
- veFaaS、邀请码登录、SQLite 临时目录、预留实例和对象存储备份均为部署参考方案，实施时必须按当时平台能力、账号、费用和数据要求重新生成部署适配说明。

## 4. 范围

### 4.1 本里程碑包含

- 语言无关的 JSON Schema 契约。
- Python 参考实现与统一 CLI。
- 项目初始化和 PRD 基线复制。
- 输入最低要求检查。
- 主状态机及当前阶段的三层完成状态。
- 技术适配门禁和普通阶段验收门禁。
- 只追加的审批记录与状态事件记录。
- 证据登记、校验和过期判定。
- 单写入者租约锁、心跳、安全释放和过期接管。
- 原子状态写入、修订冲突检测和中断恢复摘要。
- 技术适配、阶段开发、验收和证据模板的最小集合。
- 一个离线运行的最小示例项目。
- 单元测试、Schema 测试、存储测试、锁测试和 CLI 集成测试。
- README、CHANGELOG 和普通人可执行的验收步骤。

### 4.2 本里程碑不包含

- Codex、Claude Code 和 Pi 的专属薄入口。
- Pi 与 DeepSeek 配置示例。
- AI Web 产品适配器和运行时图像适配器。
- 模型调用、RAG、文件上传、前端页面或浏览器自动化。
- 云资源创建、部署、付费调用或外部数据上传。
- 部署状态之后的实际云操作。
- GitHub 远程仓库、推送、Pull Request 或公开发布。
- 遗留项目接管、原生 App、小程序、桌面客户端和游戏适配器。

## 5. 架构

公共内核分为六个边界：

### 5.1 Contracts

`schemas/` 保存公开协议，包括项目元数据、状态快照、审批记录、事件记录、执行锁和证据清单。Schema 是不同 Agent 和未来其他语言实现共同遵守的正式契约。

### 5.2 Domain

`src/product_factory/domain/` 保存不访问文件系统的纯领域逻辑：状态枚举、允许的转换、门禁规则、证据要求、审批匹配和过期判定。领域层输入结构化值并返回决定或固定错误，不直接输出终端文本。

### 5.3 Storage

`src/product_factory/storage/` 负责 JSON、YAML 和 JSONL 的安全读写、原子替换、只追加记录、目录边界检查、执行锁和损坏检测。领域层不依赖具体存储实现。

### 5.4 Services

`src/product_factory/services/` 编排初始化、输入检查、审批请求、审批消费、证据登记、阶段验证、状态转换、锁接管和恢复。服务层负责在一次操作中按安全顺序调用存储与领域逻辑。

### 5.5 CLI

`src/product_factory/cli/` 提供人和不同编程 Agent 共用的命令。默认输出简明中文说明，`--json` 输出稳定的机器可读结果。CLI 不复制领域规则。

### 5.6 Templates and Examples

`templates/` 保存项目状态文件和阶段文档模板。`examples/minimal-project/` 保存一个固定样例及预期结果，用于文档演示和集成测试。

## 6. 工厂仓库结构

计划结构如下：

```text
product-factory-agent/
├── pyproject.toml
├── src/product_factory/
│   ├── cli/
│   ├── contracts/
│   ├── domain/
│   ├── services/
│   └── storage/
├── schemas/
├── templates/
├── references/handbooks/
├── examples/minimal-project/
├── tests/
├── docs/product/
├── docs/superpowers/specs/
├── docs/superpowers/plans/
├── README.md
└── CHANGELOG.md
```

一个由工厂管理的产品项目至少包含：

```text
new-product/
├── inputs/
│   ├── PRD.md
│   ├── constraints.md
│   └── assets/
├── .product-factory/
│   ├── project.yaml
│   ├── intake.yaml
│   ├── state.json
│   ├── approvals.jsonl
│   ├── events.jsonl
│   ├── execution-lock.json
│   └── evidence/
├── docs/
├── backend/
├── frontend/
└── README.md
```

## 7. 数据契约

### 7.1 `project.yaml`

保存相对稳定的项目元数据：

- `schema_version`
- `project_id`
- `name`
- `created_at`
- `factory_version`
- `prd.path`
- `prd.sha256`
- `constraints.path`
- `handbooks` 的版本、路径与 SHA-256
- `stage_plan` 中有序且唯一的阶段 ID、名称和类型
- 源码摘要的排除路径

`project_id` 初始化后不可改变。PRD 内容发生变化时必须走变更流程并更新版本与摘要，不能静默覆盖。

### 7.2 `state.json`

保存当前流程事实：

- `schema_version`
- `project_id`
- `revision`
- `workflow_state`
- `current_stage.id`
- `current_stage.sequence`
- `current_stage.completion_level`
- `waiting_on.type`
- `waiting_on.request_id`
- `last_valid_evidence_id`
- `last_event_id`
- `updated_at`

`revision` 每次成功修改加一。写操作必须带入读取时的预期修订号；修订不一致时返回冲突，不覆盖较新的状态。

### 7.2.1 `intake.yaml`

保存输入完整性声明，避免脚本猜测 Markdown 的业务语义：

- `schema_version`
- `project_id`
- `prd_confirmed`
- `confirmed_by`
- `confirmed_at`
- `requirements` 中七类最低 PRD 信息的 `status` 与 `source`

七类信息对应目标用户与核心任务、输入过程与产物、用户主流程与人工确认点、范围与优先级、效果验收标准、模型费用平台约束、数据隐私性能部署要求。`status` 只能为 `present`、`missing` 或 `not_applicable`；`not_applicable` 必须提供原因。`check-inputs` 同时校验实际文件、PRD 摘要和本声明，不能只相信布尔值，也不能用关键词匹配代替产品负责人确认。

### 7.3 `approvals.jsonl`

每行是一条不可修改的审批记录，至少包含：

- `schema_version`
- `approval_id`
- `request_id`
- `gate_type`
- `scope`
- `state_revision`
- `statement`
- `actor`
- `source`
- `created_at`
- `consumed_by_revision`

`approve` 只接受当前等待中的请求。普通阶段统一要求产品负责人交互式输入：`验收通过，批准进入下一阶段。`

本地 CLI 无法从密码学上证明输入者一定是产品经理。因此 V1 审批门禁是可审计的流程约束，不是身份认证系统。CLI 必须降低误操作概率并保留完整记录，但不得宣称能够抵抗拥有本机写权限的恶意操作者。

### 7.4 `events.jsonl`

记录状态转换、审批请求、审批消费、证据验证、锁接管、异常和恢复。事件拥有唯一 ID，并记录操作前后修订号。`state.json` 是当前事实来源，事件日志用于审计和恢复核对。

### 7.5 `execution-lock.json`

保存：

- `schema_version`
- `lock_id`
- `owner.tool`
- `owner.session_id`
- `owner.pid`
- `owner.host`
- `acquired_at`
- `heartbeat_at`
- `lease_expires_at`
- `state_revision`

锁通过独占创建获得。有效租约存在时，其他写命令失败，但只读命令仍可运行。只有租约过期后才能显式接管；接管必须提供原因并写入事件记录。

### 7.6 证据 `manifest.json`

至少保存：

- `schema_version`
- `evidence_id`
- `stage_id`
- `state_revision`
- `factory_version`
- `prd_sha256`
- `source_digest`
- 执行命令、开始时间、结束时间、退出状态和摘要
- 运行环境与访问入口
- Mock 或真实能力标记
- 检查项及产物相对路径
- 已知问题及严重程度
- `ready_for_human_acceptance`

源码摘要使用排序后的相对路径和文件内容计算 SHA-256，排除 `.git/`、整个 `.product-factory/` 流程目录、缓存、构建产物、密钥文件及项目配置中的额外排除项。流程状态、审批和证据通过各自的修订号与引用关系校验，不能参与源码摘要，否则正常状态更新会让证据立即过期。摘要只保存哈希值，不复制源码或敏感内容。

证据的阶段 ID、PRD 摘要、工厂版本或源码摘要与当前项目不一致时，证据自动过期。过期证据不能满足阶段门禁。

证据保存到 `.product-factory/evidence/<stage-id>/<evidence-id>/manifest.json`。新证据使用新的 `evidence_id` 和目录，不覆盖旧证据；`state.last_valid_evidence_id` 指向当前有效证据。

## 8. 状态机

协议识别 PRD V1 的完整主状态集合：

```text
initialized
inputs_checked
adaptation_pending_approval
stage_development
system_verification
human_acceptance_pending
next_stage_or_frontend
release_ready
deployment_pending_approval
deployed_pending_acceptance
production_accepted
observing
```

第一个里程碑实现从 `initialized` 到 `next_stage_or_frontend` 的全部行为。后续状态在 Schema 中保留，但尝试进入尚未实现的部署流程时返回 `unsupported_transition`，不能伪装成已经具备部署能力。

当前阶段的完成级别为：

```text
none
implemented
system_verified
human_accepted
```

规则如下：

- 输入未通过不能进入技术适配审批。
- 技术适配审批不存在或不匹配当前请求时不能进入开发。
- 没有登记 `implemented` 不能进入系统验证。
- 没有当前且有效的系统验证证据不能请求人工验收。
- 没有匹配当前阶段、当前修订和当前请求的人工审批不能进入下一阶段决策点。
- 相关源码、PRD 或状态发生变化后，受影响证据失效并回到相应验证步骤。
- 安全、密钥、数据丢失、权限隔离和核心链路仍使用 Mock 等强制底线不能通过带问题验收绕过。

## 9. CLI

首批命令为：

```text
product-factory init
product-factory check-inputs
product-factory status
product-factory request-approval
product-factory approve
product-factory record-evidence
product-factory verify-stage
product-factory transition
product-factory lock acquire
product-factory lock status
product-factory lock heartbeat
product-factory lock release
product-factory lock takeover
product-factory resume
product-factory validate
product-factory repair-audit
```

所有写命令必须验证项目身份、Schema、修订号和执行锁。`status`、`resume` 和 `validate` 为只读命令，不得隐式修改项目。`repair-audit` 是显式写命令，只能补写由当前 `state.last_event_id` 明确引用但缺失的恢复事件，不能改变业务状态。

CLI 默认不接受密钥参数。任何未来需要外部凭证的命令只能读取已批准的秘密载体，并且不得在输出中显示实际值。

## 10. 写入与恢复

`state.json` 和其他可替换文件采用同目录临时文件、刷新缓冲、同步落盘和原子替换。临时文件名包含随机事务 ID，不使用可预测的共享临时文件。

一次审批驱动的转换按以下顺序执行：

1. 校验执行锁、当前修订和待审批请求。
2. 交互式读取并校验批准语句。
3. 追加审批记录并同步落盘。
4. 预先分配状态转换事件 ID，原子写入引用审批 ID 和该事件 ID 的新状态。
5. 追加预先分配的状态转换事件。

如果步骤 3 后中断，系统只会留下尚未消费的有效审批，状态不会跨阶段；恢复时可以重新执行转换。如果状态写入后事件追加失败，`resume` 根据 `state.last_event_id` 检测审计缺口并提示运行 `repair-audit`。`repair-audit` 在取得执行锁并确认状态修订未变化后补写恢复事件，不能回退或改变已经原子生效的业务状态。

`resume` 必须检查：

- 项目和 Schema 是否有效。
- 当前工作流状态和阶段。
- Git 状态与源码摘要变化。
- 当前执行锁是否有效。
- 最近证据是否仍有效。
- 是否正在等待人工批准。
- 上一次事件是否完整。

它只输出恢复摘要、异常和下一条安全命令，不自动执行状态转换或重复初始化。

## 11. 错误模型

错误使用 PRD 已定义的分类：

- `input_required`
- `approval_required`
- `implementation_failed`
- `external_service_failed`
- `environment_blocked`
- `policy_blocked`
- `interrupted`

CLI 机器输出统一包含：

- `ok`
- `code`
- `category`
- `message`
- `step`
- `retryable`
- `action`
- `details`

`details` 只包含安全的结构化元数据。异常堆栈只能在显式调试模式写入本地受控日志，不进入普通终端输出、证据或审批记录。

稳定退出码为：

- `0`：成功。
- `2`：输入或 Schema 无效。
- `3`：等待人工审批。
- `4`：执行锁或修订冲突。
- `5`：证据缺失、失败或过期。
- `6`：策略阻止。
- `10`：未分类内部错误。

## 12. 测试策略

所有首个里程碑测试必须离线运行，不调用模型、云平台或付费服务。

### 12.1 单元测试

- 合法和非法状态转换。
- 未审批越级与错误审批复用。
- 审批请求与状态修订绑定。
- 三层完成状态推进。
- 证据摘要匹配与过期判定。
- 异常分类和安全错误输出。

### 12.2 Schema 测试

- 每份公开 Schema 的最小合法样例和完整合法样例。
- 缺字段、未知枚举、错误类型和跨项目引用。
- 损坏 JSON、YAML 和 JSONL 的明确错误。
- 示例项目中的结构化文件全部通过校验。

### 12.3 存储和锁测试

- 原子写入成功和替换前中断。
- 预期修订号冲突。
- 审批追加成功但状态尚未消费的恢复。
- 重复加锁、有效租约、心跳、正常释放、过期接管和错误所有者释放。
- 路径遍历和写出项目边界被拒绝。

### 12.4 CLI 集成测试

在临时目录中执行完整可运行闭环，并验证：

- 未审批不能进入开发。
- 无有效证据不能请求阶段验收。
- 相关文件改变后证据过期。
- 有效审批只消费一次。
- 新进程执行 `resume` 能准确恢复。
- `--json` 输出能被稳定解析。
- 日志和生成文件不包含测试密钥标记。

### 12.5 平台声明

实现使用 `pathlib`、原子文件替换和租约文件，不依赖仅限 POSIX 的文件锁。首轮必须在当前 macOS 环境真实验证。Windows 和 Linux 保持兼容设计，但在对应平台实际运行前不得宣称已经验证。

## 13. 验收标准

本里程碑只有同时满足以下条件才可提交人工验收：

- 本地可安装 `product-factory` CLI。
- 最小示例能从初始化走到下一阶段决策点。
- 所有需要审批的转换均能被未审批测试阻止。
- 状态、审批、证据和事件符合公开 Schema。
- 并发写入、过期锁和修订冲突行为有自动测试。
- 证据过期和中断恢复有自动测试。
- 全部离线测试通过。
- README 提供非技术人员可执行的安装、启动、验收和恢复步骤。
- CHANGELOG 记录首个里程碑范围。
- 未实现能力、已知问题和平台验证范围如实披露。

## 14. 后续里程碑边界

公共内核通过人工验收后，再分别设计和实现：

1. Codex、Claude Code 和 Pi 薄入口。
2. AI Web 产品适配器。
3. 运行时图像适配器。
4. 发布准备与经授权的部署模块。
5. 根据真实项目增加其他终端或游戏适配器。

后续模块只能调用公共 CLI 或契约，不复制或修改公共审批语义。任何扩大 V1 产品范围的决定必须形成 PRD 变更记录。
