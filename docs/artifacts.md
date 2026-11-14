# 文件存储与附件

查阅上传、下载、存储后端、授权与开发环境文件清理边界。

[文档总览](README.md) · [项目首页](../README.md)

## 文件存储

`POST /api/v1/artifacts/uploads` 使用 Bearer 和重复的 `multipart/form-data` 字段 `files`，每批 1 至 5 个文件、单文件上限由 `ARTIFACT_UPLOAD_MAX_BYTES` 配置（默认 20 MiB）。支持 UTF-8 TXT/Markdown、PDF、DOCX、XLSX、PNG 和 JPEG；图片另限 2 MiB、2048×2048 像素，并检查实际编码、完整解码和 PNG 结束块。上传响应返回文件 ID、名称、MIME、大小和 SHA-256，不返回私有存储键。`GET /api/v1/artifacts/{artifact_id}` 查询元数据，`GET /api/v1/artifacts/{artifact_id}/download` 下载，`DELETE /api/v1/artifacts/{artifact_id}` 撤销访问；每次都重新检查所有权、状态和期限，跨用户按不存在处理。

默认文件正文位于 Git 忽略的 `.run-private/artifacts`，可通过 `ARTIFACT_STORAGE_DIR` 指向私有目录；元数据位于 `yp_artifact`。设置 `ARTIFACT_STORAGE_BACKEND=s3`、`ARTIFACT_S3_BUCKET` 及对应的 endpoint、region、凭据后可使用私有 S3 兼容桶；`ARTIFACT_STORAGE_DIR` 此时仍用于临时校验文件。切换存储后端不会自动迁移已有正文。停写并备份后可用 `uv run python -m app.cli.migrate_artifacts_to_s3` 复制和校验现有文件，再让 API 与 worker 同时切换配置。上传配额由 `ARTIFACT_OWNER_MAX_BYTES` 和 `ARTIFACT_OWNER_MAX_FILES` 控制；部署时应配置代理上传上限、恶意软件扫描与备份/孤儿文件清理策略。

### 开发环境文件盘点与手动清理

测试过程中需要按 Agent 或会话核对上传物、生成产物并释放本地磁盘时，可使用以下两个接口。它们仅在 `ENV=development` 且 `ARTIFACT_STORAGE_BACKEND=local` 时开放，要求管理员 Bearer 令牌，并且只处理该管理员自己的文件。普通用户返回 403；跨用户 Agent、会话或文件按不存在处理。API 继续使用当前 `ARTIFACT_STORAGE_DIR`，默认 `.run-private/artifacts`；无需增加配置项或执行数据库迁移。

| 操作 | 请求 | 返回与边界 |
| --- | --- | --- |
| 按 Agent 或会话盘点 | `GET /api/v1/artifacts/dev-files?agent_id=11`，或使用 `session_id=…`；两项必须且只能提供一个。可选 `page=1&page_size=100`，每页最多 200 项。 | `data.items/total/page/page_size`。上传物通过本人的消息附件关联，产物通过本人的 Run 关联；同一文件多次引用只返回一次。包含已逻辑删除、过期、失败 Run 的待发布产物及磁盘已缺失项，也可查询本人已删除 Agent/会话的历史文件。 |
| 按名称清理正文 | `POST /api/v1/artifacts/dev-files/delete`，JSON 为 `{"filenames":["0123456789abcdef0123456789abcdef","唯一原始名称.docx"]}`；每批 1～100 个不重复名称。 | 推荐使用盘点返回的 `storage_name`。原名须在本人全部文件记录中唯一，否则 409，须改用磁盘名；同一文件的原名与磁盘名同时提交时只处理一次。只接受文件名称，不接受路径、额外请求字段或目录。 |

盘点项包含 `artifact_id`、`filename`（原名）、`storage_name`（磁盘名）、`sha256`、`source`、`state`、`is_deleted`、`byte_size`、`exists_on_disk`、`run_id`、`expires_at` 与 `create_time`。**磁盘上的 32 位名称是随机存储键，不是 SHA-256 内容摘要**；两者分别返回。响应不公开绝对路径，缓存策略为 `no-store`。该管理接口不改变普通文件接口隐藏存储键的约定。

例如，先盘点本人 Agent 11 的文件，再将选中的磁盘名提交给删除接口：

```bash
curl 'http://127.0.0.1:5174/api/v1/artifacts/dev-files?agent_id=11&page=1&page_size=100' \
  -H "Authorization: Bearer $TOKEN"

curl 'http://127.0.0.1:5174/api/v1/artifacts/dev-files/delete' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"filenames":["0123456789abcdef0123456789abcdef"]}'
```

其中 `$TOKEN` 使用当前管理员令牌，示例磁盘名应替换为实际返回值。删除会真实移除文件正文。服务端先对整批名称、资源类型、在用关系和文件类型预检；未知或跨用户名称返回 404，同名歧义、Skill 不可变资源、知识库原件、关联会话/运行尚未结束、目录或符号链接返回 409，此阶段不会清理批次中的正常文件。关联运行保护包含排队、审批中、取消等待以及同会话可能续读旧产物的任务。

清理采用**数据库逻辑删除 + 本地文件物理删除**。预检通过后先提交 `state=deleted/is_deleted=1`，保留数据库记录并撤销预览、下载及后续引用，再逐项物理删除正文并记录审计。删除接口的 HTTP 200 表示已返回本批处理结果，**还须检查 `failed_count`**；`data.items[].result` 为 `deleted`、`already_missing` 或 `failed`，并提供 `deleted_count/already_missing_count/failed_count`。磁盘权限等错误不会掩盖为成功；该项记录仍已撤销，使用其 `storage_name` 重试即可。已缺失文件可重复提交。数据库提交与磁盘删除无法组成一个原子事务；若进程中断或删除失败，已撤销记录仍可再次查询和清理。

清理保留文件元数据、历史消息、附件/来源关系和检查点；删除一个被多个会话复用的上传物会使这些会话对该文件的引用全部不可用，不是只解除某一会话的关联。盘点不包含从未关联到该 Agent/会话的上传物、未知归属的孤儿磁盘文件、Skill 包资源或目录；已有文件记录但尚未提交给会话的本人上传物，可通过已知名称清理。未知磁盘孤儿及临时文件继续由既有回收任务处理，本接口不扫描或删除任意路径。S3 及目标数据库并发场景不在本机验证范围内。
