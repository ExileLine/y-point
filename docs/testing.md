# 数据库迁移与测试

执行数据库结构检查、定向回归和静态检查。

[文档总览](README.md) · [项目首页](../README.md)

## 数据库与测试

SQLAlchemy 模型在 `app/models` 注册，Alembic 以其元数据管理业务表。修改模型后新增迁移；升级目标库前先备份并核对当前版本，再运行 `uv run alembic upgrade head` 和 `uv run alembic check`。应用启动时会检查业务模型需要的表和列；缺少迁移会在启动阶段报错。业务数据可选择 SQLite、MySQL 或 PostgreSQL；Checkpointer 独立配置。跨数据库场景需要对应的独立测试环境，历史验证范围见[文档总览](README.md#历史验证与研究)。

常用验证命令：

```bash
uv run pytest -q
uv run alembic check
uvx ruff check app migrations tests scripts sandbox local_run.py
uvx ruff format --check app migrations tests scripts sandbox local_run.py
```

代码与提交约定见[贡献指南](../CONTRIBUTING.md)。

## 按变更范围验证

先运行覆盖当前变更的测试文件，例如部署入口修改可运行：

```bash
uv run pytest -q tests/test_deploy_all.py tests/test_deployment.py
```

前端类型与生产构建：

```bash
npm --prefix web ci
npm --prefix web run build
```

`web/package.json` 中的 `verify:*` 是按功能划分的浏览器脚本，需要可访问的前端地址与本机 Chrome/Playwright 环境。按脚本说明设置 `WEB_BASE_URL`，只运行与变更相关的项目。多数页面验证使用拦截 API，不代表真实模型或后端全流程联调通过。

数据库方言、真实模型、Docker、OCR 或对象存储相关测试需要对应依赖或显式启用；跳过项应与通过项分别记录。优先使用临时数据库和文件目录，不直接用个人业务数据执行破坏性场景。

纯文档变更核对相对路径、锚点、代码块和实际命令入口即可，无需为排版变更运行整个业务测试集。
