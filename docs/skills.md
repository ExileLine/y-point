# Skill 文件包与报告模板

编写和发布 Skill，配置受管脚本、DOCX 模板和字段契约，并关联 Agent。

[文档总览](README.md) · [项目首页](../README.md)

## Skill 文件包与运行时授权

Skill 以 `SKILL.md` 为入口，组织指令、参考资料、脚本和产物模板。前端 `/skills` 为资源列表，打开条目进入 `/skills/{id}` 独立工作区；桌面使用文件树与编辑/预览区，手机使用栏目导航。工作区包含文件、概览与设置、执行配置、脚本试运行、版本历史。用户在此维护 Skill，日常对话直接提出任务，由模型根据正文选择资料、工具和模板。

```text
my-skill/
├── SKILL.md                       # 必需，YAML frontmatter 含 name 和 description
├── references/                    # 按需读取的参考材料
├── scripts/                       # Python/Bash 脚本及辅助源码
├── assets/
│   ├── templates/                 # DOCX 产物模板
│   ├── schemas/                   # 每个模板的 JSON Schema
│   └── examples/                  # 示例输入或静态资源
├── y-point.yaml                   # 需要模板或可执行脚本时提供
└── README.md                      # 可选，使用说明
```

**编辑与发布**：新建和 ZIP 导入先生成私有草稿，未发布不能关联 Agent。文本编辑、批量文件/目录上传、重命名、删除只影响草稿；`SKILL.md` 必须保留。每次写入使用 `expected_revision` 比较并交换，过期编辑返回 HTTP 409，界面保留未保存文字以便处理冲突。校验检查入口元信息、完整路径清单、模板字段和脚本验证状态；发布在同一事务中固定全部资源、模板及脚本，生成不可变版本。设置的可见性、分组和启用状态随发布生效。历史版本可查看、导出、与草稿比较正文，或恢复到草稿再发布。

**资源与存储**：包内名称是相对逻辑路径，不是服务器目录。正文仍使用 `.run-private/artifacts` 或私有 S3，文件元数据与摘要在 `yp_artifact`（`source=skill_resource`）；`yp_skill_draft` 保存草稿清单/修订号/基准版本，`yp_skill_version_file` 保存发布版本到文件的关联，`yp_skill_version.manifest` 固定完整文件摘要，`files` 保存运行所需的文本视图。本地正文以随机 `storage_key` 保存，实际路径为 `ARTIFACT_STORAGE_DIR/<storage_key>`；不会按页面文件名在磁盘直接创建同名目录树。草稿或发布清单把包内路径映射到 `artifact_id`，再由 `yp_artifact.storage_key` 定位文件；对象存储后端沿用相同关系。`yp_skill_script` 另外记录包摘要、脚本路径与内部 Tool/版本的关系。资源无普通会话附件的默认有效期。被替换且从未发布的草稿文件进入既有过期清理；已发布资源继续保留，支持历史 Agent/Run。下载和预览逐次核对 Skill 授权、文件状态、长度与 SHA-256，不暴露存储键；文本和代码可以编辑/复制，Markdown、DOCX/PDF/XLSX 内容及 PNG/JPEG 可预览，复杂版式以原件下载为准。

**ZIP 与限制**：完整导入/导出携带二进制模板和字段契约，可在根目录直接放 `SKILL.md`，或包含一个 Skill 根文件夹。完整包最多 128 个文件、32 MiB，单个资源最多 10 MiB、UTF-8 文本最多 128 KiB；实际 DOCX 模板仍最多 2 MiB、每版本最多 10 个。导入拒绝加密、链接、目录逃逸、隐藏路径、重复或大小写冲突以及文件/目录冲突。工作区批量上传和 ZIP 导入在 multipart 解析前限制总请求 33 MiB。下载标准模板可得到目录示例与配置说明。

接口均需 Bearer；草稿写入和历史管理仅限所有者，其他用户只能查看、下载已启用公开 Skill 的当前发布版本。

| 接口 | 用途与关键参数 |
| --- | --- |
| `POST /api/v1/skills/packages` | 用 `name`、`description` 创建未发布草稿。 |
| `POST /api/v1/skills/packages/import` | `file` 上传完整 ZIP，创建新草稿；同名返回冲突。 |
| `GET /api/v1/skills/{id}/workspace` | 当前元数据、本人草稿和有权查看的发布历史。 |
| `POST /api/v1/skills/{id}/draft` | 打开草稿；`from_version` 加当前 `expected_revision` 可恢复历史。 |
| `PUT /api/v1/skills/{id}/draft` | 携带 `expected_revision`，批量更新 `files`、`renames`、`delete_paths` 和待发布设置。 |
| `POST /api/v1/skills/{id}/draft/files` | 重复 `files`、按上传顺序的路径 JSON 数组 `paths`、`expected_revision`。 |
| `POST /api/v1/skills/{id}/draft/import` | `file`、`expected_revision`，原子替换完整草稿。 |
| `POST /api/v1/skills/{id}/draft/validate`、`/publish` | 携带 `expected_revision` 校验或原子发布；未通过校验时禁止发布。 |
| `GET /api/v1/skills/{id}/files`、`/files/preview` | `path` 指定资源，`version` 不填读本人草稿，填写则读获授权发布版本。`files` 支持 `download=true`。 |
| `POST /api/v1/skills/{id}/draft/scripts/prepare`、`/test` | `expected_revision`、脚本 `path`、JSON 对象 `arguments`；准备依赖或试运行精确包。 |
| `GET /api/v1/skills/{id}/versions/{version}/export` | 完整版本 ZIP；`GET /skills/{id}/export` 同样支持当前文件包完整导出。 |

旧 `POST /skills`、`POST /skills/import` 和文字更新/独立模板接口继续兼容旧文字包，原有文字 ZIP 的限制保持不变。旧 Skill 打开工作区时只在草稿中转换资源，历史发布版和 Agent 关联不改写。切换为完整文件包后，文字更新和独立模板发布接口返回 409，提示在工作区编辑，避免覆盖丢失二进制资源；新版 ZIP 请使用完整包接口。

### 使用模式与产物选择

每个 Skill 只要求根目录的 `SKILL.md`；`references/`、`scripts/`、`assets/` 和 `y-point.yaml` 按任务选择，不要求创建空目录或给纯说明型 Skill 配置报告模板。`SKILL.md` 的 `description` 描述适用场景，正文写清资料来源、执行步骤、输出要求和完成标准。

| 使用模式 | 所需资源与配置 | 调用与交付方式 |
| --- | --- | --- |
| 纯说明型，如会议纪要整理 | 只有 `SKILL.md` | 模型按步骤直接在对话回复，无须脚本或文件产物。 |
| 参考资料型，如服务台 FAQ | `SKILL.md` 与 `references/*.md` | 按需读取资料后回答；未提供的事实明确说明，引用文件不等于建立知识库索引。 |
| 仅脚本型，如精确文本统计 | `scripts/*.py` 或 `.sh`，以及 `y-point.yaml.scripts` | 模型从正文的运行时映射选择受管脚本，传 JSON 输入，取得实际结果再回答。 |
| 脚本读取资源，如固定库存汇总 | 脚本、`assets/` 内 CSV/JSON 等及脚本配置 | 脚本读取沙箱中只读挂载的固定包；数据可全部来自包内，无须用户上传附件。 |
| 正文直接生成文档，如固定章节周报 | `SKILL.md`，可选章节说明 | 模型把完整正文交给 `create_document_artifact` 生成 DOCX/PDF，或交给 `create_text_artifact` 生成 Markdown 等文本文件；无须报告字段 Schema。 |
| 固定 DOCX 或多模板报告 | `assets/templates/*.docx`、匹配的字段 Schema、`y-point.yaml.templates` | 模型按正文选择模板，并组织 `report_data` 调用 `render_skill_document`；用户无须在对话页面选择模板或手填字段。 |
| 用户附件审查 | 固定审查指令/清单，用户在对话上传每次变化的材料 | 读取获授权附件后审查，按要求直接回复或生成文件；用户附件不需要并入 Skill 包。 |
| 外部工具或知识库协作 | 正文中的调用步骤，以及 Agent 显式关联的 Tool/知识库 | 外部 Tool 依赖可在 `required_tools` 声明；实际授权、验证和索引可用性分别检查，不由 Skill 自动授予。 |

脚本的 `input_schema` 与报告字段 Schema 是两类契约。前者约束工具输入，后者约束固定 DOCX 模板的占位字段；`review.schema.json` 只是命名示例，并非每个 Skill 的必需文件。无业务输入的脚本仍使用对象型输入契约，例如：

```yaml
scripts:
  scripts/summary.py:
    description: 读取包内固定资料并汇总，不接收业务参数
    input_schema:
      type: object
      properties: {}
      additionalProperties: false
    requirements: []
    requires_approval: false
```

该脚本的试运行和正式调用参数都是 `{}`。脚本通过标准输入读取 JSON，通过标准输出返回单个有界 JSON 值；统计和取数以实际返回为准。脚本试运行验证绑定整个包，即使只修改参考资料或 CSV，也需重新验证后发布。

用户脚本、Skill 受管脚本与 MCP 的执行前参数纠正规则见“Tool 管理与沙箱”。脚本字段由模型依据 Skill 组织，用户无需把内部字段添加到会话 `/runs` 请求中。

“不需要用户传参”不表示内部工具没有参数：正文生成文档仍需模型传入 `filename` 与 `content`，模板生成仍需模型组织字段。通用文档生成适合固定章节及基础标题、段落、列表；严格复用既有 Word 版式时使用模板流程，当前不能把无占位符的静态 DOCX 放入 `assets/templates/` 后跳过字段契约自动写入任意正文。模板也可以只约定一个正文字符串字段，但仍需匹配的 Schema。脚本在临时目录写出的文件不会自动成为会话产物，需由内置文件生成或模板工具交付。

创建时先保存正文与实际资源，仅登记存在的脚本和模板；没有执行能力的 Skill 不用填写执行配置。有脚本则先准备依赖、试运行，通过后校验发布；“准备完成”不等于“已验证”。Agent 关联已发布启用版本，用户新建会话后直接描述任务，需要附件时再上传。以真实调用记录和可下载产物判断完成，不以模型声明“已执行/已生成”作为成功证据。

### 执行配置与脚本

`SKILL.md` 负责描述何时使用、取数步骤、参考路径、模板选择及缺失事实处理。`y-point.yaml` 声明机器可验证的资源关系，正文中提及路径本身不会授予执行权限。配置只支持以下三类；JSON 对象也是合法的 YAML。

```yaml
templates:
  assets/templates/review.docx: assets/schemas/review.schema.json
  assets/templates/summary.docx: assets/schemas/summary.schema.json
scripts:
  scripts/validate.py:
    description: 检查结构化审查结果
    input_schema:
      type: object
      properties:
        title:
          type: string
      required: [title]
    requirements: []
    requires_approval: false
required_tools:
  - func1
```

模板与 Schema 也可在执行配置页逐个关联。配置中的脚本必须位于 `scripts/` 且为 Python/Bash 文本，每个包最多 8 个，输入契约为对象型 JSON Schema；Python pip 依赖复用既有固定版本/锁定与离线 wheel 机制。先准备依赖，worker 完成后试运行，验证通过才可发布。试运行的验证绑定整个文件包摘要；任何正文、参考材料、模板或配置文件变化后都须重新验证，单纯设置变更可复用同一包验证。依赖失败可以在工作区重新准备。

包内脚本经 `yp_skill_script` 关联内部托管 Tool/版本，隐藏于独立 Tool 目录，不能手工关联或修改。运行时复用 Tool 的审批、并发、超时、调用预算与 `ToolInvocation` 审计；脚本只读挂载 Run 固定的整个包，可导入同目录辅助源码并以 `/app` 为工作目录读取相对路径。沙箱无网络、只读根文件系统、非特权运行，只有有界临时目录可写；输入/输出均为最多 64 KiB 的 JSON，不自动把临时文件发布为产物。需要文档时可将实际结果交给通用文档工具，或组织字段交给固定模板工具；普通文本/表格产物继续使用已有产物工具。

`required_tools` 最多声明 16 个外部 Tool 名称；它们必须在 Agent 配置中显式关联、当前可访问且名称唯一，在 Agent 保存和 Run 提交时检查。配置不自动授予业务工具、MCP 或宿主机命令权限；`allowed-tools`、`Bash(...)` 和 `!` 命令替换不会被自动执行。未登记脚本仅作为只读资源。

### Agent 关联与对话调用

Agent 最多关联 16 个已发布、可见、已启用且名称不冲突的 Skill，`yp_agent_skill` 固定版本；重新保存关联才使用新发布版本。提交对话后 `yp_run_skill` 再固定本轮版本，包内脚本同步生成 `yp_run_tool`；现有 Run 不随编辑或发布漂移。worker 和每次资源/脚本调用仍复核当前授权，禁用、删除或转私有会撤销不再满足条件的访问。

DeepAgents 在本轮受控虚拟空间提供 `/skills/{name}/{path}`，先通过 Skill 元信息匹配任务，再按需 `read_file` 读取正文和参考文件；二进制资源由受控模板工具使用。服务端在虚拟 `SKILL.md` 末尾附加固定资源目录、模板与字段契约以及包内脚本工具名映射，不修改原文件和版本摘要。脚本调用摘要显示 `Skill 名称@版本 / scripts/路径` 及真实成功/失败状态，产物保留模板来源快照。

外部 Tool 仍可不依赖 Skill 独立使用。例如显式关联的工具名称是 `get_time`，可在正文写“需要时间时调用 get_time，参数为 {}”。模型读取正文不保证每次都调用全部工具。平台仅对本轮唯一且不与保留名称冲突的自然名称生成调用别名；重名需要明确 Tool ID/所有者，未授权 Tool 不因正文提及而可用。

### 对话附件与 Skill 固定模板产物

Skill 正文定义何时使用、取数步骤和产物要求；模板是该 Skill 不可变版本中的资源。用户在对话中提出任务，模型根据任务读取对应 Skill，再调用正文指定的业务工具获取数据或读取用户材料，最后按模板生成文件。对话页面无需选择报告 Skill，关联但未使用的 Skill 不强制生成产物。

使用流程：

1. 在 Skill 页面编写适用场景、取数步骤、模板文件名、字段含义及缺失信息处理规则。例如正文写明“调用 func1 工具获取数据，按照 xxx_template.docx 模板生成报告”。
2. 在文件工作区批量上传实际 DOCX 到 `assets/templates/`，把各自的报告字段 JSON Schema 放入 `assets/schemas/`，在执行配置中逐个关联。每个版本最多 10 个模板、单个最多 2 MiB。Word 模板使用 `{{ title }}`、`{{ summary }}` 等占位符；正文、模板和字段契约一起校验并原子发布。仅在正文中写文件名不会读取服务器本地文件或外部 URL。
3. 在 Agent 中重新保存该 Skill 关联，固定包含模板的版本，并关联正文需要的业务工具；选择已验证 `tools` 能力的模型。之后更新正文或模板，已有 Agent 绑定和已提交 Run 继续使用原版本。
4. 用户正常发送业务任务。若任务需要本地材料，可在输入区添加或拖入文件，勾选“供模型读取”；支持 TXT、Markdown、PDF、DOCX、XLSX、PNG/JPEG，每轮最多 5 个、单文件上限默认 20 MiB、由部署配置控制。若数据来自业务工具，则无需上传附件。
5. worker 固定本轮 Skill 版本，模型按任务读取 `SKILL.md`。正文附带该版本所有模板文件名、Skill ID、各自字段契约及受控生成方式；模型按正文的适用条件选取模板，调用实际授权的业务工具或读取材料，组织所选模板的报告字段。上传多个模板不代表每次任务都要生成全部模板。
6. 模型调用 `render_skill_document(skill_id, report_data, filename, template_name="assets/templates/xxx_template.docx")`；有多个模板时必须提供 `template_name`，单模板可省略。服务端核对本轮绑定版本及当前授权，校验结构化字段，使用私有模板填充 DOCX，保留正文、表格、页眉页脚及样式。
7. 成功读取模板 Skill 正文后，运行会登记该 Skill 的模板交付要求。每个已执行模板 Skill 至少须交付一个来源匹配的 DOCX；具体需生成哪些报告由正文和任务要求决定。文字、通用 DOCX 和其他版本模板不能替代。缺少产物时在预算内进行一次纠正尝试，仍未交付则明确失败。完成后发布文件，助手消息提供来源、预览和下载。

Skill 正文示例（`func1` 应是已关联且可用的业务工具名称；运行时会提供实际调用名称）：

```markdown
## 适用场景
用户要求生成合同审查报告时使用。

## 执行步骤
1. 调用 func1 获取合同数据；用户已上传合同时，结合其提供的材料核对事实。
2. 合同审查使用 xxx_template.docx，title 为报告标题，summary 为审查结论，findings 为逐条发现。若另有上传的其他模板，写明各自用途和选择条件；生成时通过 template_name 指定对应文件名。
3. 缺失数据明确标注“待补充”，不得编造事实。报告完成后返回可下载文件。
```

模板字段契约示例：

```json
{
  "type": "object",
  "properties": {
    "title": { "type": "string", "maxLength": 100 },
    "summary": { "type": "string", "maxLength": 2000 },
    "findings": {
      "type": "array",
      "maxItems": 20,
      "items": {
        "type": "object",
        "properties": {
          "note": { "type": "string", "maxLength": 500 }
        },
        "required": ["note"]
      }
    }
  },
  "required": ["title", "summary", "findings"]
}
```

模板支持字段替换、条件和单层数组循环，可保留模板原有的正文、表格、页眉页脚和样式。表格行可使用 docxtpl 的 `{%tr for item in findings %}` / `{%tr endfor %}` 控制行，内容行填入 `{{ item.note }}`；控制标记分别放在独立行。模板最多 2 MiB，拒绝宏、活动/嵌入对象和外部资源；正文、页眉、页脚执行表达式白名单、单层循环和 XML 输出预算检查。字段契约不接受远程引用和任意复杂规则；数组最多 100 项、单个字符串最多 8000 字符，结构化报告数据最多 64 KiB。用户须按实际报告设置更小的字段上限；复杂 Word 排版仍需用自己的真实模板验收。

文档属性、脚注、尾注及其他不受上述 AST/预算检查的部件不支持模板语法，静态内容可以保留；发布时拒绝这些位置的动态标记。独立模板接口在 multipart 解析前限制请求总量 21 MiB，完整包接口为 33 MiB；配额不足返回明确业务 400，并回滚资源和版本。

| 操作 | 接口与契约 |
| --- | --- |
| 批量发布模板 | `POST /api/v1/skills/{skill_id}/templates`，multipart 字段为 `expected_version`、重复的 DOCX `files` 与 JSON 字符串 `fields_schema`。后者以原始文件名为键、独立 JSON Schema 为值，例如 `{"A_template.docx": {...}, "B_template.docx": {...}}`。先校验整批再原子发布；任一文件或字段无效则整批失败。仅所有者可发布，版本冲突 409。 |
| 兼容单模板接口 | `POST /api/v1/skills/{skill_id}/template` 继续接受 `expected_version`、单个 JSON Schema `fields_schema` 和 DOCX `file`，保留旧替换语义。版本已有多个模板时返回 409，须改用批量接口，避免误删其他模板。 |
| 获取模板信息 | 所有者读取 `GET /api/v1/skills/{skill_id}` 的 `report_templates` 列表，每项包含 `template_key`、文件 ID、名称、SHA-256 和独立字段契约；仅有一个模板时仍提供兼容字段 `report_template`。原始模板通过现有文件下载接口授权下载。 |
| 上传与发送前检查 | 先 `POST /api/v1/artifacts/uploads`，再 `POST /api/v1/agents/{agent_id}/sessions/{session_id}/attachments/check`。检查请求仅接受 `artifact_ids` / `context_artifact_ids` 两个列表，上下文 ID 必须属于消息附件且不重复；核对所有权、有效期、当前会话模型能力、完整文本可读性及总字节预算。成功返回 `valid`、`context_bytes`、`max_context_bytes`，不创建 Run 或调用模型。 |
| 提交任务 | 使用原会话 `/runs` 提交消息、模型预期值及可选附件，服务端固定 Skill 版本。模型按需读取 Skill 后自动登记模板交付要求；重试仍比较原始消息、模型、附件和产物设置，不将运行中新增的交付要求作为用户提交参数。 |
| 报告预览与下载 | 完成后从消息 `artifacts` 取 ID，使用现有 `/artifacts/{id}/preview` 和 `/download`；预览是正文展示，下载保留生成文件原件。 |

附件预检查只说明当前状态允许提交；实际 `/runs` 仍接收完整消息、模型、幂等键和附件参数，并再次强制校验，worker 执行前继续检查内容与授权。不能将预检查响应作为绕过正式提交校验的凭据。图片上下文最多两张，要求当前模型已验证视觉能力。

数据链路为：`SkillVersion` → `yp_skill_template(skill_id, skill_version, template_key, artifact_id, fields_schema)` → 本轮 `yp_run_skill` 版本快照 → 模型读取正文并登记 `required_report` → 业务工具取数/可选用户材料 → 字段校验/沙箱渲染 → `yp_artifact` 暂存 → Run 完成发布 → 助手消息。模板正文沿用私有文件存储，`yp_artifact.source=skill_resource`，不随普通上传保留期过期，受所有者文件配额约束；历史版本资源不能通过通用文件删除接口单独移除。文字版本更新沿用全部模板关联，替换模板创建新资源。文件名按 Unicode NFC 和不区分大小写规范化，每个版本内唯一，禁止使用目录路径；多个模板未指定名称或名称不存在时明确报错，不自动猜测。模板报告在 `yp_artifact.template_source` 保存 Skill 名称/ID/版本、模板文件名/键、模板文件 ID/摘要和结构化数据摘要；消息中的 `context_artifact_ids` 继续记录本轮提供的材料。这些是生成来源记录，不代表模型已正确审查全部事实或实际引用每一段材料。

文本上下文提取后的正文连同文件标识合计受 `ATTACHMENT_CONTEXT_MAX_BYTES` 控制，默认 **20 MiB**；发送前与 worker 使用同一提取预算，超限明确拒绝，不静默截断。长材料需拆分或使用知识库；知识库检索只召回相关片段，也不能等同于逐页全文审查。扫描 PDF 的 OCR、长文档分段读取与覆盖证明、完整 Word 分页版式预览尚未提供。完整 Skill ZIP 可携带模板及字段契约跨环境迁移；脚本执行环境及其试运行验证不会随 ZIP 迁移，需在目标环境重新准备并验证。 20 MiB 是 UTF-8 字节预算，不代表模型一定容纳同等大小的输入；实际仍受所选模型上下文窗口及解析格式约束（最多 5000 行，PDF 最多 50 页，XLSX 最多 5 表、每表 1000 行/100 列）。
