# M6.4 第二阶段补充验收记录

日期：2026-11-13。

结论：PDF 第二阶段的本机功能基线通过本轮补充验收；修复中文分词空格导致的连续词检索遗漏。真实 OCR 已经过正式接口及索引任务验证。用户业务 PDF 的事实准确性、真实供应商加工质量和目标部署环境继续待验收，不将本记录视为 M6/M8 总验收完成。

## 验收环境与隔离范围

- 本机 macOS、Python 3.12、SQLite FTS5、Tesseract 5.5.4，已安装 chi_sim / eng。
- 使用 PDFium 实际渲染、真实 Tesseract TSV 识别；原始中文样例另经 Poppler 渲染作目视对照。
- 服务端测试经 HTTPX ASGITransport 调用正式 FastAPI 路由，使用真实登录、上传、数据库索引任务执行函数、检索、固定引用与 PNG 原页渲染。数据库和文件目录位于 pytest 临时目录，不写入现有业务知识库。
- 页面脚本运行实际前端组件，API 响应受浏览器拦截；真实 PNG 和对应 OCR 证据 JSON 用于原页高亮核对。它与服务端真实 OCR 专项分别提供证据，不等于浏览器直连常驻 API/worker 的端到端验收。
- 新增完整链路测试采用关键词索引和保留原文模式，没有配置或调用真实加工/嵌入供应商。已有模型加工测试仍使用确定性假模型，只验证路由、预算、缓存和证据隔离。

## 发现并修复的问题

`chinese-scan.pdf` 上传及索引成功，但用户检索“五千元”没有命中。修复前真实 OCR 提取为：

```text
住宿 费用 上 限 为 五 千 元 。
```

中文全文检索使用连续字符的二元词项；以上内容只能生成“五”“千”“元”等孤立词项，无法匹配查询中的“五千”“千元”。

修复后 PDF 每个提取行会合并相邻汉字之间的分词空格：

```text
住宿费用上限为五千元 。
```

英文词间距、数字间的分隔、标点和换行保留，不补字、不更改数值、不推断扫描表格关系。片段仍是文档版本规范提取文本的精确切片；原始 PDF 与原页坐标不变。该处理也适用于文字层提取产生的同类中文分词空格。

解析版本从 `pdf-layout-ocr-v1` 升至 `pdf-layout-ocr-v2`，后续重建通过新指纹重新解析。现有活动索引与历史引用不自动改写，需显式重建并成功发布新代后应用修复。本轮无数据库迁移，开发库仍为 `054`。

## 覆盖内容及结果

| 范围 | 已核对内容 | 结果 |
| --- | --- | --- |
| 英文扫描 | 实际 OCR、上传、任务发布、Hotel 检索、固定引用、原页 PNG | 通过 |
| 中文扫描 | 实际 OCR、五千元连续词检索、原文偏移及原页区域 | 修复前失败，修复后通过 |
| 混合 PDF | 文字/扫描/空白三页，OCR/空白摘要，片段不跨页 | 通过；正文相同允许检索去重，扫描页仍保留独立引用 |
| 证据及权限 | content 精确切片、OCR 待核对标志、PNG 尺寸/坐标、其他用户 404 | 通过 |
| 策略切换 | 同一扫描文件 auto 成功 → text 失败 → ocr 成功，分别形成上传版本 | 通过 |
| 失败回退 | text 失败为 ocr_required，旧预览/旧索引/旧原页仍可用；恢复后发布新代 | 通过 |
| 正常删除 | 正常配置下删除后立即拒绝旧引用/原页，检索不再返回该文档 | 通过；不覆盖已配置模型失效时的删除缺口 |
| 文字边界 | 中文期限与金额、英文单词、阿拉伯数字分隔、换行、制表符、标点 | 通过 |
| 既有 PDF 保护 | OCR 不可用/低质量/超时/超限、语言包、取消清理、裁剪/旋转/非零原点、发布摘要快照、缓存及撤权 | 既有专项回归通过 |
| 页面 | 桌面/手机的提取选项、摘要、置信度、原页高亮、撤权清图、失败提示与无横向溢出 | 浏览器契约及目视核对通过 |

自动化结果：

- 新增 `tests/test_knowledge_pdf_acceptance.py`：**12 passed**。
- PDF、加工、知识库、生命周期、集成、回答模式、迁移组合：**111 passed / 3 skipped / 3 warnings，93.59 秒**。
- 跳过项是显式开关未启用的真实嵌入/对话模型、MySQL 迁移和 PostgreSQL 迁移，不计通过。
- PDF 页面：**26 项断言**；加工页面：**50 项断言**；知识库页面：**40 个场景**。
- Ruff check/format、新增函数中文说明、非空字典/多行结构及 Git 差异检查通过。

## 可重复执行的命令

在项目根目录执行，使用隔离数据，不要求先重建业务知识库：

```bash
.venv/bin/python -m pytest tests/test_knowledge_pdf_acceptance.py -q --disable-warnings --tb=short
```

完整组合：

```bash
.venv/bin/python -m pytest \
  tests/test_knowledge_pdf.py tests/test_knowledge_pdf_acceptance.py \
  tests/test_knowledge_processing.py tests/test_knowledge.py \
  tests/test_knowledge_lifecycle.py tests/test_knowledge_integration.py \
  tests/test_knowledge_answer_policy.py tests/test_db_schema.py \
  -q --disable-warnings --tb=short
```

页面验收需启动本机前端。以下命令仍使用拦截 API：

```bash
WEB_BASE_URL=http://127.0.0.1:5174 node web/scripts/verify-knowledge-pdf.mjs
WEB_BASE_URL=http://127.0.0.1:5174 node web/scripts/verify-knowledge-processing.mjs
WEB_BASE_URL=http://127.0.0.1:5174 \
  KNOWLEDGE_SCREENSHOT_DIR=/tmp/y-point-m64-phase2-acceptance/knowledge \
  node web/scripts/verify-knowledge.mjs
```

本轮额外生成了匹配的真实中文 OCR 证据和原页，可在临时文件仍存在时重复页面视觉核对：

```bash
WEB_BASE_URL=http://127.0.0.1:5174 \
  KNOWLEDGE_PDF_PAGE_FIXTURE=/tmp/y-point-m64-phase2-acceptance/chinese-page.png \
  KNOWLEDGE_PDF_EVIDENCE_FIXTURE=/tmp/y-point-m64-phase2-acceptance/chinese-evidence.json \
  node web/scripts/verify-knowledge-pdf.mjs
```

证据 JSON 只覆盖 content、sha256、start_offset、end_offset、source_locator；测试 API 的对象 ID 仍由脚本固定。提供 JSON 时须同时提供对应 PNG，不用不同样本的图片冒充坐标核对结果。截图在 `/tmp/y-point-knowledge-pdf-{1280,390}.png`，原始 PDF/Poppler 图片在 `/tmp/y-point-m64-phase2-acceptance/`。这些临时文件可清理，不作为长期业务存储。

## 页面人工复测

1. 新建测试知识库，选关键词检索、保留原文；使用 `docs/knowledge-phase2-manual/` 的九份样例。
2. 上传 `chinese-scan.pdf`，等待索引成功；分别检索“五千元”“十五个工作日”“财务部”，核对原文和原页。已经上传过的旧 PDF 可显式重建，不必重新上传。
3. 上传 `mixed.pdf`，核对总页数 3、OCR 1、空白 1，空白页没有伪证据。相同正文检索可能去重，不要求重复列出两页。
4. 同一 `scan.pdf` 自动提取成功后，更新为仅文字层；任务应失败，旧索引和原页继续可用。再更新为强制 OCR，等待新版本成功发布。
5. 用 `table.pdf` 核对 Alpha/3800/50 的同一行关系，用 `columns.pdf` 核对先左列再右列的阅读顺序。
6. 用 `blank.pdf`、`unreadable.pdf`、`encrypted.pdf` 核对失败原因和旧索引保留。
7. 在手机宽度打开原页；共享给测试用户后撤权，确认重新读取被拒绝并清除旧图。正常配置下删除文档后，旧引用与原页不得继续读取。
8. 在独立测试库配置文本加工模型，验证 auto 对文字页不加工，对 OCR/表格加工；enhanced 生成的解释必须与原始证据分开。真实费用、解释质量及业务准确性记录为人工验收结果。

本机已在确认 Run/知识任务均终结后重载 worker，API 与前端代理的 worker 健康正常；未自动重建已有知识库。

## 尚待验收及能力边界

- 用户真实业务 PDF 的金额、期限、字段关系和业务检索效果；OCR 置信度不代表事实准确率。
- 真实供应商加工解释的质量、失败与费用口径。
- 复杂无边框/扫描/合并表格、复杂多栏、图表语义不保证自动恢复，需核对原页。
- 目标 PostgreSQL/MySQL、私有 S3、Docker/多部署组合及完整无障碍专项。
- 已配置加工/嵌入模型失效时删除可能 409 回滚，仍按 README 的已知限制跟踪；本轮正常配置删除通过不等于该问题已解决。
- 第三阶段 HTML/JSON/YAML/PPTX 不在本轮范围，仍未开放。
