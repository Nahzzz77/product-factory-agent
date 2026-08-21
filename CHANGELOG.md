# Changelog

## 0.1.0 — 2026-08-20

- 新增离线 `product-factory` CLI、严格 JSON Schema 契约和可提交的确定性 Schema 导出。
- 新增全新 Web 产品项目初始化、七类 PRD 输入确认、原子状态快照和只追加审批/事件审计。
- 新增单写入者租约、心跳、安全释放、过期接管、技术适配审批、阶段验收审批、不可变证据和源摘要验证。
- 新增只读 `status`、`resume`、`validate` 与受限的 `repair-audit` 恢复能力。
- 新增模板、最小离线项目和安装入口端到端验证；全部验证均不需要网络、云资源、模型或付费调用。

限制：`next_stage_or_frontend` 之后的状态仅作为可校验协议值，尚不实现进入或执行这些阶段的转换。本版本已在 macOS 本地环境验证；Windows 和 Linux 为设计兼容，尚未实机验证。
