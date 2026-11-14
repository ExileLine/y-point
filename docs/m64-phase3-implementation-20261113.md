# M6.4 第三阶段实现与验证记录

日期：2026-11-13。功能基线完成，用户业务文件与目标环境验收待开展；不将 M6/M8 总体验收标记为完成。

## 交付范围

| 文件 | 解析与证据边界 | 来源定位 |
| --- | --- | --- |
| HTML/HTM | 静态主文优先，去除脚本/导航/全局页眉页脚/显式隐藏区域，保留段落、代码和完整表格行 | 标题路径、元素 ID、DOM 路径、源行号、表格行 |
| JSON | 按父对象聚合标量字段，子对象和数组元素独立，混合数组保留原下标 | JSON Pointer，字段路径/类型/值 |
| YAML/YML | 同 JSON，并保留值定义行号；有限非循环别名展开，日期与 on 等保留字符串 | 展开后的逻辑路径与原始值定义行号分别保存 |
| PPTX | 按幻灯片关系清单读取实际页序，排除隐藏页/对象/分组，正文/表格/备注分离 | 原页号、页标题、对象 ID、角色和表格行 |

知识库上传新增六种扩展名，不扩展普通对话附件。没有数据库字段或迁移变化，head 仍为 `054`。生产解析使用现有标准库/PyYAML；新增开发依赖 python-pptx 仅用于样例与测试，生产无需 PowerPoint/LibreOffice。

原始字节进入私有 Blob；新模块统一生成规范文本、精确偏移和来源块。配置记录及表格行不跨记录分块，正文按来源和配置长度分块。关键词/嵌入检索与固定引用复用既有管线，模型解释单独保存；原始证据始终等于 `extracted_text[start_offset:end_offset]`。

新增格式 `auto`/`original` 不调用加工模型；`enhanced` 显式选择已验证文本加工模型后生成独立解释。模型解释不把配置值当运行状态、不把备注当正文、不推测图片/图表内容。解析与提示版本进入缓存身份，已有索引不自动重建。

## 失败与格式边界

重复配置键、循环/过量别名、合并键、自定义 YAML 标签、多文档 YAML、非字符串对象键、非法基础类型及非有限数值不发布；变量占位符保持原样。HTML 不运行 JS/读取 URL/计算外部 CSS，嵌套表格拒绝混成父表记录。PPTX 仅解析已有包内 XML，不执行宏或外链；缺失/越界部件、重复关系、DTD/实体声明拒绝。

HTML/PPTX 明确表头随数据行保存；合并单元格不猜测归属。PPTX 不识别图片、图表、SmartArt 或复杂版面语义，没有原页 PNG 预览，不支持 PPT/PPTM。全部新格式保留既有大小、时间、节点、来源块与最终片段限额，详见 README。

无效更新通过真实解析失败后，旧活动索引、来源摘要及固定引用继续可读；预览另显示当前上传版本已变化。共享撤权或成功删除文档后旧引用阻断。已配置加工/嵌入模型失效导致删除请求 409 回滚的既有问题未在本阶段修复。

## 自动化验证

最终服务端组合：**184 passed, 5 skipped, 4 warnings**。跳过项为真实嵌入/对话模型 1 项、隔离 MySQL 配额 2 项、MySQL/PostgreSQL 迁移各 1 项，未将跳过项记为验收通过。

```bash
.venv/bin/python -m pytest \
  tests/test_knowledge_formats.py tests/test_knowledge_pdf.py \
  tests/test_knowledge_pdf_acceptance.py tests/test_knowledge_processing.py \
  tests/test_knowledge.py tests/test_knowledge_lifecycle.py \
  tests/test_knowledge_integration.py tests/test_knowledge_answer_policy.py \
  tests/test_artifacts.py tests/test_artifact_preview.py tests/test_db_schema.py \
  -q -rs --disable-warnings --tb=short --maxfail=1 \
  --junitxml=/tmp/y-point-m64-phase3-pytest.xml
```

第三阶段专项 **60 passed**，覆盖：

- 标准 Office 库生成的真实 PPTX 字节；实际页序、隐藏页/形状/分组、备注、表格及不读取外链。
- HTML 静态主文、文章/role=main 标题、行内中文与英文空格、代码缩进、表头和嵌套表格。
- 配置路径转义、类型、日期/on、数组次序、别名和各类结构/资源错误。
- 正式 HTTP 上传、Worker 索引、SQLite FTS、固定检索样本、来源预览、共享/撤权、缓存重建。
- 四格式 × auto/original/enhanced，以可控假模型验证触发条件、独立解释和用量记录，不验证真实供应商语义质量。
- 四格式无效新版本失败不切换活动代，保留旧预览/引用，正常删除后旧来源失效。

固定检索样本：`tests/fixtures/m64_format_retrieval.json`，8 条正向检索与导航/脚本/隐藏内容负样本，属于合成结构样本，不能代替企业文档检索收益对照。

前端验证：

| 检查 | 结果 |
| --- | --- |
| `verify:knowledge-formats` | 98 断言，1280/390px，六扩展名上传、来源/解释、长路径、备注、脚本文本不执行、权限错误清理、无横向溢出 |
| `verify:knowledge-processing` | 50 断言 |
| `verify:knowledge-pdf` | 26 断言 |
| `verify:knowledge` | 40 场景，1280/375px；更新新格式提示断言，继续拒绝 EXE |
| `npm --prefix web run build` | TypeScript/生产构建通过，保留既有大 chunk 提示 |
| Ruff 与差异检查 | 相关 Python 检查/格式通过，Git 空白差异检查通过 |

浏览器使用拦截 API 验证页面契约；服务端使用隔离 SQLite 的正式链路，两者不等于常驻服务加真实模型的完整页面验收。第三阶段截图 `/tmp/y-point-knowledge-formats/`，已查看桌面与手机截图；JUnit 在 `/tmp/y-point-m64-phase3-pytest.xml`。既有知识库脚本默认写 `docs`，本轮刷新了对应知识库页面截图；后续复跑可用 `KNOWLEDGE_SCREENSHOT_DIR` 指定输出目录。

## 本机生效

确认本 checkout worker 身份及 cwd，并检查当前 268 条 Run 和 16 条知识任务均已终结后，正常停止并重启 worker，PID `7376 → 31559`；新增启动心跳及 `/api/v1/health/worker` 返回可用。API 7769、前端 5174 继续使用本机开发服务。

未自动上传业务文档、重建业务知识库、重放真实对话或调用收费模型；未暂存、提交或推送 Git。

## 人工测试

样例已生成到 `docs/knowledge-phase3-manual/`：四份正常文件，以及重复键、循环别名、合并键、自定义标签和只有噪声的异常文件，共十份；操作步骤在该目录 `README.md`。

```bash
.venv/bin/python -m scripts.generate_knowledge_phase3_fixtures
```

建议先新建关键词知识库，使用未配置加工模型的 auto 或 original，逐份检查加工结果及检索来源；再配置文本加工模型使用 enhanced，对照独立解释、用量与实际检索结果。真实业务文件的事实、来源和收益、供应商鉴权/限流/费用、PostgreSQL/MySQL/S3/Docker 与完整无障碍继续待验收。
