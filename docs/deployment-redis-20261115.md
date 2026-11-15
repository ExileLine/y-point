# Redis 部署补充验证（2026-11-15）

[文档总览](README.md) · [部署手册](deployment-guide.md) · [Redis 配置参考](deployment.md#redis-连接持久化与容量)

## 变更范围

默认 Compose 增加带认证的 Redis、独立持久化卷、健康检查、资源上限与日志轮转；部署初始化启用应用 Redis，并为旧三文件配置补齐凭据及连接参数。Run 队列、租约、worker 心跳和检查点存储未变更，不涉及业务数据库迁移。

Redis 使用 `redis:7.4-alpine`，不发布宿主端口，普通用户运行、只读根目录、tmpfs 临时配置；口令通过独立私有 env 注入，不放入进程命令参数。local/staging 默认容器 512 MiB、数据阈值 256 MiB；production 分别为 1 GiB、512 MiB。

## 自动化检查

在本机 Python 3.12 环境执行：

```bash
.venv/bin/python -m pytest tests/test_deployment.py tests/test_deploy_all.py -q -p no:cacheprovider
ruff check scripts/deploy.py scripts/deploy_all.py scripts/verify_deployment.py tests/test_deployment.py
sh -n deploy/redis-entrypoint.sh
git diff --check
```

结果：35 项测试通过，Ruff、Shell 语法及差异空白检查通过。pytest 另有临时目录清理警告，不影响断言；未删除与本次无关的历史测试目录。

覆盖三种环境、HTTP/HTTPS 与沙箱配置合并、Redis 密码独立和重复初始化稳定性、旧配置升级、外部连接保留，以及缺失/空密码/不匹配/孤立配置的拒绝处理。Compose 检查确认 Redis 不发布端口、不接收应用或 PostgreSQL 密钥，API/worker 等待其健康，生产覆盖为持久化预留内存。

此前组合执行 `tests/test_deployment_proxy.py` 时两个显式开关控制的 HTTP/HTTPS 代理用例跳过；本次没有把跳过记作通过，也没有重新声明完整 SSE 代理验收。

## 隔离 Docker 验证

环境：Docker Engine 29.8.0、Compose 5.5.1。使用随机项目名、临时私有目录、独立网络和命名卷，未更新正在运行的 `y-point-local` 部署配置或容器。

第一组实际启动 Redis，验证：

- 初始化生成的密码能认证并返回 `PONG`；无密码和错误密码均不能通过认证探针。
- 服务以非 root 用户运行，根文件系统只读，不发布宿主端口。
- 应用镜像读取生成的 `application.env` 后，通过现有 Redis 客户端成功连接。
- 写入仅属于本隔离实例的测试键，强制重建 Redis 容器后仍能读取，验证 AOF 与命名卷的保留路径。

第二组复用本机已有 API/Web 镜像，以独立数据库、Redis 卷和临时 HTTP 端口启动完整拓扑：PostgreSQL、Redis、一次性迁移、API、worker、Web 均通过对应健康检查；随后执行更新后的 `scripts/verify_deployment.py`，验证 HTTP 页面、SPA 路由、API、worker 心跳、Redis 认证和应用连接、Alembic 结构、PDF/OCR 依赖、日志轮转及请求 ID 追踪，均通过。

两组临时容器、网络和测试卷已清理。验证没有创建管理员、提交模型任务或访问真实业务库。

## 验证边界

本记录证明上述本机部署与升级路径，不代表 Redis 高可用、故障切换、断电持久性、在线备份恢复、生产并发容量、真实模型或完整业务会话已验收。AOF 使用按秒同步，不能替代备份；正式环境仍需按实际数据量验证内存、磁盘、监控与恢复方案。
