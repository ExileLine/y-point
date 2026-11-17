# AG-UI 1.0 协议契约固定件

`1.0.schema.json` 取自 AG-UI 官方仓库的冻结 1.0 规范，下载日期为 2026-11-17。测试直接使用独立 JSON Schema 验证实际线事件，避免只验证项目自行定义的事件模型。

- 来源：<https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/spec/1.0/schema.json>
- SHA-256：`4b5c93226838a0e72d88e6c5df20633c686c49fcb75d9be2815c6cbf9e48e71a`
- 配套 Python SDK：`ag-ui-protocol==1.0.0`。
- 配套 TypeScript SDK：`@ag-ui/core@1.0.4`。

这里固定文件内容，不在测试中访问网络。修改适配器或升级 SDK 后，仍须通过此契约和生命周期、恢复、授权测试；需要升级协议时应新增版本，不能用远端主分支的新内容静默替换契约。
