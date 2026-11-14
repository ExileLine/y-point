# Run、会话与实时恢复

了解运行提交、会话模型、消息与产物、SSE 恢复和失败后的继续对话。

[文档总览](README.md) · [项目首页](../README.md)

## Run 与 Checkpoint

直接文本运行入口为 `POST /api/v1/runs`；私有 Agent 对话使用下文的会话 Run 入口。一个 Run 有自己的 `run_id`、状态和从 1 开始的事件序号。同一检查点线程不能同时有两个未结束的 Run；重复提交由同一用户的 `request_key` 保护。正常状态为 `queued → running → completed`，取消、超时或故障分别进入 `cancelled`、`timed_out`、`failed`。队列等待、模型执行、工具执行、模型/工具调用次数及 Token/估算成本均可设置上限；Run 创建时固定有效配置，超限写入脱敏 `failure_code`。Token 和成本上限依赖上游用量，不能阻止单次调用在返回用量前超额。

Worker 从业务库领取排队 Run，并在有效租约内调用 DeepAgents。输入和 `queued` 事件与 Run 同事务提交；模型可见文本先写入 `yp_agent_run_event.text_delta`，然后才能经 SSE 发送。LangGraph 在独立 Checkpointer 写状态；完成时最终 `assistant` 消息、Run 的 `completed` 状态、`checkpoint_id` 和终态事件在一个业务事务中提交。前端不能把 `text_delta` 当作最终回答。模型/工具调用失败、取消或租约过期时不自动重做可能有外部副作用的工作；只有检查点能证明无业务工具的 Run 已完成时，worker 才会补写遗漏的最终结果。

`CHECKPOINT_BACKEND` 默认是 `sqlite`，不要求与 `DB_BACKEND` 相同。共享 PostgreSQL 检查点需设置 `CHECKPOINT_BACKEND=postgres` 和 `CHECKPOINT_POSTGRES_DSN`，使用独立且仅 worker 可访问的库；此时不使用 `--checkpoint-db` 文件。切换 Checkpointer 不迁移旧线程，已有会话仍依赖原存储，切换前必须处理未结束 Run 和旧线程。

长会话使用模型目录的 `context_window` 和 Agent 的 `max_output_tokens` 计算输入预算，并预留系统指令及工具描述空间；接近阈值时由 DeepAgents 将较早历史压缩为摘要，最近消息继续保留。摘要模型调用同样计入 Run 的调用次数、Token 和估算成本，受模型超时与预算限制。摘要只改变后续模型看到的检查点上下文，业务库中的用户和助手消息保持原文，可继续供前端按游标读取；压缩后的历史可能丢失细节，文件仍可按需重新读取。图片按每张额外预留约 2048 个输入 Token，摘要提示仅保留图片旁的文字，不重新发送内嵌图片。窗口不足以容纳输出上限及最低输入空间时，Agent 配置返回 409；旧配置、直接 Run 或单轮输入估算超过可用空间时以 `context_window_exceeded` 失败。Token 估算和固定开销不是供应商实际计数的保证。未配置 `context_window` 时仍依赖框架默认摘要阈值，应在模型目录填入经验证的窗口大小。

运行时将已登记的部署窗口传入模型 profile，完整请求预算同时包含消息、系统指令、实际开放的工具契约和预留输出；最大输出留空时仍按窗口的四分之一预留生成空间，并提前摘要，不修改 SDK 的空输出参数；中文正文及工具参数使用更保守的 Token 估算，费用和累计用量仍以上游报告为准。摘要检查只计入已授权的平台工具及精简的文本读取契约，避免未开放的默认工具消耗预算。估算仍不能代替供应商的实际分词结果。

自动摘要转存到当前线程 `/conversation_history/` 的原文可通过授权 `read_file` 分段取回。历史文件中的长行按每 512 字符软换行，每页完整响应最多 8192 字符；`offset` 为软换行视图中的零基行号，按响应的 `next_offset` 继续，见到 `EOF` 才表示本次快照已读完。首次读取固定本轮文件快照，避免后续摘要追加读取记录使分页不断增长；原历史文件保持原文，Skill 文件继续使用原有读取规则。这是摘要历史的容量保护，不等同于上传文档逐页审查的覆盖证明。

模板选择以本轮固定 Skill 版本为准。`template_name` 省略、`null`、空字符串或纯空白均视为未指定；唯一模板时直接选用，多模板时必须明确指定。名称无效、不存在或同名歧义时返回 `invalid_tool_arguments` 和当前固定版本的 `allowed_templates`，由模型修正后重新调用；不猜测模板，不回退到其他版本，也不访问名称指定的宿主路径。没有模板、权限失效、文件缺失或完整性错误仍按真实故障处理。

报告工具的 `skill_id` 必须使用本轮已读取 SKILL.md 中的固定 ID，不能使用示例或猜测值。传入未绑定 ID 时，工具返回 `invalid_tool_arguments`、`/skill_id` 字段提示及 `allowed_skills`（仅包含本轮仍可访问的模板 Skill ID、固定版本和模板名），由模型在本轮预算内修正后再次调用；不查询或披露其他 Skill 是否存在，不静默替换为某个默认 Skill。正确绑定资源被禁用、撤权或损坏时仍按真实故障处理。

模板渲染的 `report_data` 必须符合固定版本字段契约。字段不符时工具以错误状态返回 `invalid_tool_arguments`、字段路径、校验规则及可用/必填字段，最多反馈前 20 个字段校验问题，不包含报告字段值；模型可在本轮预算内修正后重新调用，验证失败不会生成文件。权限、模板完整性及存储故障继续中止执行，不将这类故障作为字段修正处理。

摘要或普通模型调用失败时，公开消息不追加成功的助手回答，已发生的模型调用仍计入预算。失败、超时或取消前已发布的 `text_delta` 会由会话历史接口汇总为 `partial_content`，连同 `run_state/failure_code` 在原用户消息上返回；页面单独显示未完成内容、可折叠失败原因和复制操作，流式转终态、刷新及后续新轮次均保留。它不进入后续模型上下文，不把工具参数、内部摘要、未公开片段或私有检查点转成正文。没有已公开正文的历史不能补造。下一轮只在能证明检查点归属和工具副作用安全时继续：无工具调用、已授权 Skill 文件读取、`list_session_artifacts` 只读附件列表、具有明确结束事件的历史文件 `read_file` 与生成产物 `read_generated_artifact` 回读，以及固定版本声明为只读且调用结束事件与审计完整一致的用户 Tool 适用受控恢复。报告工具失败且该轮没有生成任何产物时，可一并放弃该失败调用；无法证明时返回 `checkpoint_mismatch`。首轮空起点与历史检查点的分支规则见“失败轮次后的继续对话”，会话 ID、历史消息和原失败状态保留，失败输入不自动带入新轮。

工具事件 `tool_started`、`tool_completed`、`tool_failed` 只公开工具名和关联 ID，不把参数、结果、异常正文或文件内容塞进 SSE。文本文件读取工具只有在 Run 开启且模型工具能力已验证时才可用；`list_text_artifacts` 返回授权文件摘要，`read_text_artifact` 需要所有者审批，执行时再次检查文件和权限。具备工具能力的模型可自行调用生成工具，无需前端传 `publish_answer_artifact`；生成文件先暂存，Run 完成时与助手消息一同发布并写入 `artifact_published`。审批入口为 `GET /api/v1/runs/{run_id}/approval` 与 `POST /api/v1/runs/{run_id}/approval/decision`；拒绝会取消 Run，同意只保存决定，worker 才会根据检查点恢复执行。产物也可用 `GET /api/v1/artifacts?run_id={run_id}` 查询，下载仍走授权文件接口。

具备已验证工具能力的模型还可调用 `list_session_artifacts` 查看本会话最近已发布产物的 ID、名称和类型，再用 `read_generated_artifact` 按指定 ID 读取文本。本轮选入上下文的用户上传附件已在消息的“参考文件内容”中提供，直接使用该正文；上传附件 ID 不适用于此工具。读取 ID 不属于当前可读生成产物（包括跨会话、已删除或过期）时，返回 `status=error`、`error=generated_artifact_unavailable` 的可修正提示，模型可继续使用已有附件正文或重新列出可用产物；拒绝读取不登记产物来源，不泄漏目标文件是否存在或其正文。授权失效和文件损坏等实际读取故障仍按原流程处理。读取时再次核对当前 Run 租约、用户、会话、产物状态、期限、大小和 SHA-256；TXT、Markdown、HTML、JSON、CSV、PDF、DOCX、XLSX 的可提取文本单次最多 20 MiB。HTML 只提取可见文本；扫描 PDF、复杂嵌入对象及超限文件不能据此完整续编辑。工具结果属于模型内部上下文，不写入公开消息或 SSE，但可能保留在检查点中；删除源文件不会自动清除已有检查点内容。模型仅在判断需要时调用工具，多个候选文件时应先确认目标；服务端不会在每轮自动读取所有产物。依次列出、读取、回答通常至少需要 3 次模型调用和 2 次工具调用；再生成新版文件还需预留额外预算。

| 模型生成工具 | 产物格式 | 输入与边界 |
| --- | --- | --- |
| `create_text_artifact` | `.txt`、`.md`、`.html`、`.json`、`.csv` | TXT/Markdown 保留 UTF-8 正文；HTML 由 Markdown 转换并清洗活动标签；JSON 解析后保存；CSV 限制行列并把公式样式的单元格转为文本。 |
| `create_document_artifact` | `.docx`、`.pdf` | 从 Markdown 风格正文生成标题、段落和列表；输出后重新打开校验。当前不保留复杂表格、图片、模板或精细分页。 |
| `create_spreadsheet_artifact` | `.xlsx` | 模型提供有名称和二维 `rows` 的工作表，最多 5 表、每表 1000 行、总计 10000 个单元格；字符串不作为公式执行，保存后重新打开校验。 |
| `render_skill_document` | `.docx` | 按本轮固定 Skill 版本的模板及独立字段契约渲染；有多个模板时必须指定 `template_name`。适用流程、模板发布和版本规则见[对话附件与 Skill 固定模板产物](skills.md#对话附件与-skill-固定模板产物)。 |

生成产物单文件最多 10 MiB、每轮最多 5 个；HTML 下载使用附件方式，不作为站内可信页面执行。Agent 会话中，用户明确要求生成或导出文件时，worker 在完成前核对本轮模型工具暂存的产物；若指定 TXT、Markdown、HTML、JSON、CSV、DOCX、PDF 或 XLSX，还会核对格式。首次只有文字且剩余预算足以调用生成工具并回复时，运行时在同一 Run 内最多补一次内部纠正回合；这会增加模型调用与用量，内部提示只保存在检查点，不成为业务消息。校验前不公开本轮文字增量；最终缺失或格式不符则返回 `failed/artifact_required`，不追加助手最终消息，失败后同一会话仍可继续提交。该判定基于明确的文字指令，不能覆盖所有含糊表达；也不会把 `publish_answer_artifact` 的异步 TXT 回答导出当作模型生成的文件。前端仅在助手消息的 `artifacts` 非空时展示产物，并以授权下载接口确认当前可用性。生成文件的实际类型由服务端校验，不能只依据扩展名展示预览。

### 会话内模型切换

Agent 的 `default_model_id` 只决定新会话的初始模型；`yp_agent_session.model_id` 保存当前会话的选择，`yp_agent_run.model_id` 固定每轮实际使用的模型。修改 Agent 默认模型不会改变旧会话。用户在输入框上方选择模型，确认后从下一条消息开始持续生效；历史助手回复继续标明所属 Run 的模型。目录名称与供应商名称取当前目录，模型 ID 来自历史 Run，名称不是不可变快照。

历史会话的模型字段为空时，服务端按“最近完成 Run 的模型 → 最近 Run 的模型 → Agent 默认模型”解析；只读查询不回写数据库，新提交会固定解析出的会话模型。部署时先执行 `uv run alembic upgrade head`，再确保 API 与 worker 加载同一版本代码并沿用原检查点存储。

| 接口 | 模型选择契约 |
| --- | --- |
| `GET /api/v1/agents/{agent_id}/sessions/{session_id}` | 返回会话当前 `model_id` 与 `version`。 |
| `GET /api/v1/agents/{agent_id}/sessions/{session_id}/models` | 返回已验证文本模型的名称、供应商、已验证能力、窗口，以及 `selectable/reason/notice`；不返回凭据或连接配置。 |
| `PUT /api/v1/agents/{agent_id}/sessions/{session_id}` | 提交 `expected_version` 和 `model_id`；只修改本人的当前会话，不修改 Agent 或历史 Run。 |
| `POST /api/v1/agents/{agent_id}/sessions/{session_id}/runs` | 使用会话当前模型；可携带 `model_id`、`expected_session_version` 防止过期页面误用模型。这两个参数用于校验，不执行切换。 |
| `GET /api/v1/agents/{agent_id}/sessions/{session_id}/messages` | 每条消息附带所属 Run 的 `model_id` 及当前目录的 `model_name/provider_name`；目录缺失时保留 Run 模型 ID。 |

切换请求示例：

```json
{
  "expected_version": 3,
  "model_id": 2
}
```

会话有排队、执行、等待审批或取消中的 Run 时禁止切换；归档会话和停用 Agent 同样不可切换。服务端复核使用者对 Agent 的访问权、会话所有权、模型及供应商状态、文本能力、窗口与成本价格；配置了 Tool、Skill 或知识库时还要求已验证工具调用能力。当前模型失效时明确报错，用户可选择其他可用模型，不自动回退到 Agent 默认模型。本轮附件仍按目标模型的视觉能力及既有格式、大小规则校验。

显式切换到不同模型时，业务事务更新会话模型、版本和执行线程，保留会话 ID、历史消息、旧 Run 与检查点。新线程首次运行时，`session_models.handoff_messages()` 从同一用户、Agent 和会话中读取本轮之前已完成的可见文字消息，转换为新的用户/助手消息，再交给运行时。迁移最多携带最近 100 个完成轮次、262144 个字符，按完整轮次从最近向前保留；超过容量时向模型加入省略说明，后续仍受目标模型窗口、摘要和预算限制，不能保证保留全部早期细节。

每轮构建运行时时，系统提示都会补入当前 Run 模型的 ID、目录名称和上游 `external_id`，并提示模型不要沿用历史助手的自我介绍。该提示同样作用于已经切换过的现有线程，保留 Agent 的业务角色和原有消息。目录名称表示平台接入配置，不能作为底层训练身份的独立认证；模型回答“我是谁”也不能证明路由。核对切换是否生效，应关联 `yp_agent_run.model_id`、`yp_model_call_record` 中同一 `run_id` 的 `provider_id/external_id`，必要时核对检查点响应元数据或供应商日志。

历史附件正文、图片、工具原始结果、供应商专用状态和旧审批不自动迁移；失败或取消轮次的输入也不导入。需要文件时重新附加，或让具备工具能力的模型通过现有会话文件工具重新授权读取旧产物。知识库使用本轮快照与当前授权重新检索，历史工具动作不会因切换重放。跨供应商切换会把保留的文字历史发送给目标供应商，界面确认框会事先说明。

会话更新与提交均使用版本比较，所有线程中的活动 Run 都阻止切换；当前及历史会话线程不能通过独立 `POST /runs` 复用。相同提交键和相同请求重试优先返回原 Run，即使之后会话已切换模型；若显式携带的模型与原 Run 不一致，则返回 409。原有客户端不传新增校验字段时仍可提交，但会话仍使用自身保存的模型。

冲突保留 HTTP 409 和业务码 `10005`，并以 `data.reason` 区分 `session_busy`、`session_archived`、`session_version_changed`、`session_model_changed`、`model_unavailable`、`model_capability_mismatch`、`model_context_insufficient`、`model_pricing_missing` 和 `model_thinking_mode_unsupported`。收到版本或选择冲突后应刷新会话与运行状态，保留输入草稿后再提交。

### 对话产物内容预览

助手消息的生成产物卡片提供眼睛图标「预览」，点击后打开预览抽屉；桌面使用宽面板，手机占满可用宽度。抽屉内可下载原件，文本与 Markdown 可复制正文，关闭后保留对话位置。预览按需请求，每次打开重新授权，不在浏览器持久化正文；旧产物只要文件仍有效，也可以直接预览，无需重新生成。

| 产物格式 | 预览方式 |
| --- | --- |
| TXT、JSON | 保留文本换行；JSON 在长度允许时缩进展示，不执行内容。 |
| Markdown | 展示标题、列表、表格及代码块，保留代码复制；外部图片不会自动加载。 |
| HTML | 服务端按标签白名单清洗，再由无权限沙箱与 CSP 隔离展示；不执行脚本或加载外部资源。 |
| DOCX | 按正文顺序展示标题、段落和表格；图片、页眉页脚及精细排版须下载原件查看。 |
| PDF | 按页展示可提取的文字；扫描页、图片和原始版式须下载原件查看。 |
| CSV、XLSX | 展示行列内容；XLSX 支持切换工作表，不计算公式、不加载外部链接，图表与样式以原件为准。 |

`GET /api/v1/artifacts/{artifact_id}/preview` 沿用下载接口的文件所有权、状态、逻辑删除和有效期校验，返回 `artifact_id/filename/media_type/kind/content/sheets/truncated/notice`。展示方式以服务端核验的媒体类型及返回的 `kind` 为准，不依据文件名推断。接口复用私有本地/S3 存储读取，不生成公共链接、不新增预览文件或数据库表，响应带 `Cache-Control: no-store`。未登录返回 401；不存在、跨用户、未发布、已删除或过期的文件返回 404；不支持的格式返回 415，内容解析失败返回 422；抽屉内保留错误说明、重试和原件下载入口。

预览读取上限取对话上传配置与生成产物 10 MiB 上限的较大值（默认 20 MiB），正文共享 100000 字符预算；PDF 最多 20 页；表格最多 5 张工作表，每表 200 行、50 列，总计 5000 个单元格。超限通过 `truncated=true` 和界面提示说明仅展示部分内容，完整内容仍通过原件下载。Office 解析前限制 ZIP 条目和解压体积；解析在后台线程执行。预览不会更改文件、消息、Run 状态或模型上下文。

### 对话思考、耗时与用量

当运行快照的思考偏好为 `auto` 或 `enabled`，且模型实际返回公开思考文字时，worker 将其保存为独立的 `reasoning_delta` 事件；正文仍使用 `text_delta`。兼容 Chat Completions 的 `reasoning_content` 和已标准化的公开思考块分别提取，不读取私有检查点里的隐藏信息，也不公开签名、加密块或未知扩展。`disabled` 不发布思考；未返回思考文字时不显示空面板，开启思考不保证供应商一定返回该内容。原有正文预算及模板交付校验继续生效。

会话在正文上方提供思考面板，思考时默认展开、进入正文或调用工具后默认收起，用户可随时通过鼠标或键盘切换；使用较小的等宽字体、独立边线和受限高度，与最终回答分开。面板内滚动跟随新增内容，向上阅读时保留位置。消息接口增加 `reasoning_content`，从本轮已保存的公开事件按序恢复；刷新和重连不会重新调用模型，旧运行没有保存的思考不补造。失败或取消轮次仍可在用户消息对应的过程区域查看已保存思考，它不是最终回答。

会话消息增加 `metrics.elapsed_ms` 和 `metrics.total_tokens`。耗时取提交时间到首次终态事件的实际毫秒数，包含排队、模型、工具和审批等待，不受后续元数据更新时间影响；缺少终态事件时为空。Token 为本轮所有模型生成及摘要累计已报告总数，缺失时显示「Token 未报告」；可能不包含失败请求或未报告用量的调用，不能当作完整账单，也不能把未知值当成零。运行中页面显示持续计时，完成后从消息接口恢复统计。附件和产物卡片通过已有 `byte_size` 显示文件大小。

整个会话的累计统计固定显示在对话标题下方。`GET /api/v1/agents/{agent_id}/sessions/{session_id}/metrics` 复核当前用户及 Agent 权限，汇总该会话的全部 Run，返回 `run_count/active_run_count/elapsed_ms/total_tokens/missing_duration_runs/calculated_at`，使用 `Cache-Control: no-store`。累计耗时是各轮从提交到首次终态的耗时之和，包含排队、审批、失败和取消；正在运行的轮次累计到统计时刻。它不包含轮次之间的空闲时间，不额外叠加工具耗时，不依赖已加载的消息页或最新 20 条 Run，也不会把用户消息和助手回答重复计数。历史轮次没有终态事件时不以更新时间猜测，显示「至少」和未知轮次数。空会话显示 0；非空会话完全没有已报告用量时显示「Token 未报告」。

会话累计 Token 来自各轮累计已报告计数器，包含已有摘要用量，仍可能缺少上游未报告或失败请求的消耗。页面在提交、结束、刷新和会话切换后重新读取，进行中每 2.5 秒同步并本地持续计时；临时同步失败会标明显示上次统计并提供重试，权限失效则清空统计。切换会话时中止旧请求，迟到结果不能覆盖新会话。

流式展示使用缓冲分帧、组合字符边界和随积压量调整的速度，独立于原始事件和去重游标；缓冲清空后停止动画。终态正文以消息接口为准，若内容延续当前预览则接续剩余字符，内容不同则立即使用最终文本；失败或取消的正文增量不作为最终回答。系统选择减少动态效果时直接显示文本。SSE 有积压时连续补发，空闲查询间隔为 200 毫秒；心跳与空闲关闭分别按实际 10 秒、60 秒计时，等待间隔释放数据库事务。该轮询频率需结合部署并发规模评估。 浏览器专项可运行 `npm --prefix web run verify:chat-activity`，默认连接 5173，可用 `WEB_BASE_URL` 指定开发地址；脚本以受控 API 和本地 SSE 验证交互，不调用真实模型。

### 对话中的 Skill、Tool 与知识库调用状态

会话助手回复在正文上方展示本轮实际调用的类型、名称与状态。名称和结果来自已持久化的 `tool_started/tool_completed/tool_failed` 事件，不依据回答文字或 Agent 关联配置推断。Skill 展示事件中的名称及版本，用户 Tool 展示调用时名称；知识库检索展示本轮固定且有索引的检索目标名称，按当前权限过滤，已删除或撤权的名称替换为不可用提示。知识库名称使用当前可访问名称，不是调用时名称的历史快照。

`GET /api/v1/runs/{run_id}/calls` 返回按首次事件顺序排列的 `call_id/kind/name/status/sequence/started_at/finished_at/elapsed_ms/failure_code/failure_reason`，每次复核运行所有权与 Agent 当前访问权。会话 `GET /api/v1/agents/{agent_id}/sessions/{session_id}/messages` 的每条消息同时携带所属运行的 `calls` 数组，按页批量查询，不逐条回放完整文本事件；用户消息也携带该摘要，以便没有最终助手回答的失败轮次恢复调用记录。前端仅在助手一侧展示，运行时由工具事件触发重读并定时补齐，完成后以消息内摘要恢复历史。同名工具的多次调用按关联 ID 分别保留，不混并成功和失败。

调用类型包括 `skill/tool/mcp/knowledge`，MCP 通过本轮固定 Tool 关联识别，不根据名称猜测。每次调用分别展示开始至完成或失败的耗时；正在执行的调用可按开始时间持续计时。缺少开始或结束事件时返回空耗时并显示未知，结果未确认的调用不会用整轮结束时间伪造执行耗时。

失败调用提供可展开的「查看失败原因」，支持点击和键盘操作，展开后显示中文原因、处理建议及受控错误码。新失败事件通过既有 `failure_code` 保存超时、参数不符、脚本主动拒绝输入、文件不可用、权限/认证拒绝、连接失败、限流、远端异常、脚本依赖或输出异常等类别；同时覆盖抛出异常与返回错误 ToolMessage。未知异常使用安全的通用说明，不保存或显示原始异常、请求地址、凭据、脚本 stderr 或工具输入输出；历史事件没有原因时明确显示「历史记录未保存具体原因」。结果未确认的调用提供中断说明，提示先核实外部写入结果；展开详情不会重新执行工具。

调用摘要按需读取 `yp_agent_run_event`、`yp_agent_run` 及本轮知识库关联和当前授权资源。没有可汇总调用事件的运行返回 `data: []`，会话消息对应 `calls: []`；旧运行仅能恢复已保存的事件，不补造历史调用。独立摘要接口使用 `Cache-Control: no-store`；运行不存在、跨所有者或 Agent 当前授权已撤销时返回 HTTP 404。

状态为 `running`（执行中）、`succeeded`（成功）、`failed`（失败）和 `interrupted`（结果未确认）。只有开始事件而运行已终结时显示结果未确认，不能据整轮成功或失败猜测调用结果；已成功工具不会因后续模型失败而改为失败。Skill 成功表示对应文件读取完成；知识库成功表示检索调用完成，无命中仍可成功，也不证明最终回答使用了检索片段。摘要不返回工具参数、工具结果、查询正文、文档原文或凭据；原有知识引用详情仍通过独立授权接口查看。

## Agent 对话业务流程

Agent 所有者调用 `POST /api/v1/agents/{agent_id}/shares` 并传 `{"user_id":2}` 授权指定的有效用户；`GET /api/v1/agents/{agent_id}/shares` 查看授权名单，`DELETE /api/v1/agents/{agent_id}/shares/{user_id}` 撤权。接受方从 `GET /api/v1/agents/shared` 或 `GET /api/v1/agents/shared/{agent_id}` 获取名称、描述、图标、欢迎语及所有者 ID；共享视图不返回系统指令。接受方沿用 `/api/v1/agents/{agent_id}/sessions` 创建和管理自己的会话，文件上传、产物、Run、消息及检查点线程均属于接受方。共享 Agent 不自动授予所有者的附件、产物或其他资源访问权。授权与撤权写入用户操作审计。

撤权立即阻止接受方通过 Agent、会话和 Run 接口访问该 Agent 的配置及既有会话；已排队或等待审批的 Run 取消，执行中的 Run 收到取消请求，worker 停止模型和工具，已建立的 SSE 流关闭。服务端在提交、领取、执行心跳和工具调用时复核授权。接受方自己上传或生成的文件仍归其所有，撤权不转移或删除这些文件；重新授权后可访问自己原有的会话。删除 Agent 会清理其指定用户授权并取消接受方未结束的 Run。

路径中的 `{agent_id}`、`{session_id}`、`{run_id}` 取对应创建响应的 `data.id`，`{artifact_id}` 取上传响应数组中的文件 ID。除浏览器 SSE 外，以下业务请求均使用 Bearer。服务端维护会话当前的 `thread_id`，前端在每轮提交时不需要传它。

### 正常对话与持久化

| 顺序 | 前端调用与关键响应 | 业务数据落库和流转 |
| --- | --- | --- |
| 登录 | `POST /api/v1/auth/login`；取 `data.access_token`。 | 更新用户最近登录时间，创建登录会话和审计记录；后续请求核对用户与会话。 |
| 创建 Agent | `POST /api/v1/agents`，例如 `{"name":"需求助手","default_model_id":1,"system_prompt":"请简洁回答"}`；取 `agent_id`。 | 校验默认模型，创建 `yp_agent_config`，记录所有者、配置、`enabled` 和从 1 开始的版本；写审计。 |
| 创建会话 | `POST /api/v1/agents/{agent_id}/sessions`，例如 `{"title":"需求讨论"}`；取 `session_id`。 | 创建 `yp_agent_session`，保存所有者、Agent 与唯一的当前 `thread_id`，初始未归档、版本 1。不同会话使用不同检查点线程。 |
| 提交本轮 | `POST /api/v1/agents/{agent_id}/sessions/{session_id}/runs`，传 `{"request_key":"turn-1-uuid","input_text":"请记住代号 ALPHA-42"}`；取新 `run_id`。要让模型按需生成文件，只需在 `input_text` 提出格式要求；`publish_answer_artifact:true` 专用于把最终回答额外导出为 `answer.txt`。 | 同一事务内使会话版本递增，并创建 `yp_agent_run`（初始 `queued`、事件序号 1、会话线程及 Agent 配置快照）、`yp_agent_run_message` 的用户消息和 `yp_agent_run_event` 的 `queued` 事件。提交键在同一用户下唯一；每轮使用新键，网络重试沿用原键。 |
| 执行与完成 | `GET /api/v1/runs/{run_id}` 可查询状态；收到 `completed` 后用 `GET /api/v1/runs/{run_id}/messages` 取完整回答。 | Worker 领取后写 `running` 事件，按会话线程读取历史检查点，模型增量写事件表；完成时在一个业务事务中写 `assistant` 消息、`completed` 事件和 `checkpoint_id`。 |
| 继续对话 | 向同一会话提交新 `request_key`，例如 `{"request_key":"turn-2-uuid","input_text":"代号是什么？"}`。 | 新 Run 的事件序号重新从 1 开始，通常沿用会话当前的 `thread_id` 从 Checkpointer 恢复历史；无成功回答且无工具副作用的失败历史可切换新线程。上一轮未终结时拒绝再提交。 |
| 独立文本运行（另一入口） | 无需创建 Agent 或会话，使用 Bearer 调用 `POST /api/v1/runs`，例如 `{"model_id":1,"input_text":"请介绍一下你自己","request_key":"direct-1"}`；取响应 `data.id` 作为 `run_id`。可选传 `thread_id` 续接独立运行的检查点线程。 | 服务端校验模型和供应商，调用与会话入口相同的 Run 创建逻辑，保存排队 Run、用户消息和 `queued` 事件；不保存 Agent 配置快照或会话附件，也不关联 Agent/会话。之后共用 `/api/v1/runs/{run_id}` 的状态、消息、快照和 SSE 接口。会话入口不会通过 HTTP 调用此入口。 |

### 失败轮次后的继续对话

已有成功回答的会话在后续 Run 失败、取消或超时后，可以使用新的 `request_key` 提交下一轮。若最新检查点属于同一用户、Agent、会话和线程中已终结的轮次，且其全部工具调用满足下述只读安全条件，worker 会从更早的已确认检查点分支继续。此前已完成的对话上下文和产物保留，失败轮次不会自动重放，旧 Run、事件与检查点保留用于核对。

首轮失败后也可以继续同一会话：失败轮次没有工具调用，或仅调用已授权 Skill 文件、只读附件列表、会话历史/生成产物回读及固定版本声明为只读的用户 Tool 时，新请求可从空起点或最近已确认的检查点分支。Skill 读取和 `list_session_artifacts` 沿用只读开始事件恢复边界；历史 `read_file`、`read_generated_artifact` 及知识检索 `search_knowledge_base` 必须有完整且匹配的开始/成功或失败结束事件，无结束记录不放行。产物 ID 不可用的错误 ToolMessage 可由模型同轮纠正；后续供应商超时仍独立记录 `provider_timeout`，不会因此永久阻断下一轮。用户 Tool 必须有完整且相互一致的开始/结束事件、固定版本摘要和执行审计，外部写工具、未知执行结果、缺失或矛盾审计均不放行。该轮 `render_skill_document` 仅收到明确失败事件且没有任何生成产物记录时，可与这些只读调用一并安全放弃。恢复不继承失败输入、内部补救提示或待执行工具，不重放已执行脚本，不修改原检查点或历史 Run。历史产物补救检查点的 `run_id:retry` 元数据仍追溯原 Run，并核对所有者、Agent、会话、线程及时间顺序。每次重新发送使用新的 `request_key` 和当前会话版本；首轮任务未完成时，继续执行该任务需重新发送原任务内容。

供应商连接的 `timeout_seconds` 控制 SDK 请求等待，Agent 的 `model_timeout_seconds` 控制单次模型调用时限；整轮不再设置总倒计时，供应商设置较短时仍会提前中断。OpenAI/Anthropic SDK、HTTPX 及模型 SDK 流式数据块等待超时归为 `timed_out/provider_timeout`，仅平台单次计时实际到期时归为 `timed_out/model_execution_timeout`，历史整轮超时或内部调用方显式时限保护保留 `timed_out/execution_timeout`，工具执行超时为 `timed_out/tool_execution_timeout`，图步数耗尽为 `failed/graph_step_limit_exceeded`；Run、状态事件与调用记录同步保存原因，页面展示可操作的提示。其他模型错误仍为 `failed/model_error`，不向响应暴露原始 SDK 异常、请求头或凭据。连续失败时先查上游响应、供应商超时和 Agent 预算；失败后新一轮使用新 `request_key`，沿用旧键只会返回原失败 Run，不会重新执行。平台不会为超时自动切换模型或重放工具。

知识库检索返回 `no_answer=true`（无命中）是正常工具结果，本身不会终止对话；后续回答仍受智能体提示词、模型和运行限额约束。如果检索完成后模型生成超时，当前轮次如实记录 `model_execution_timeout` 或 `provider_timeout`；当该轮全部调用满足上述只读安全条件时，下次新提问可恢复到最近已确认的成功检查点，首轮失败则可回到干净起点。历史上已出现 `checkpoint_mismatch` 的同一会话也可在满足检查条件后继续，无需删除会话或知识库。若要再次询问失败轮次的问题，请重新发送该问题；失败输入不会自动加入后续模型上下文。

模型生成和历史摘要对 OpenAI/Anthropic SDK 的连接错误增加有限恢复：未收到任何模型数据时，默认等待 1 秒后仅重试当前模型请求一次；SDK 内部 `max_retries` 仍为 0，避免两层重复重试。每次尝试均重新校验租约及调用次数、已报告 Token、费用预算，等待和重试不重置原单次时限，用户取消可立即打断等待。任意流式数据块（含空正文的思考、工具参数块及原生流式协议事件）到达后不再重试；SDK 超时、HTTP 状态错误和业务工具也不因此自动重放。两个新环境变量由 worker 全局读取，修改后需重启 worker，不写入 Agent/Run 配置快照。恢复只处理瞬时故障；上游持续不可达时仍按原模型错误结束，断连尝试没有返回的用量无法准确补计，已有用量与预算不清零。

排查多步任务时，先按事件时间线找到最后一次 `tool_completed`，再检查之后的模型调用；脚本校验成功不代表后续模板渲染已经完成。worker 对工具循环内的模型调用记录“模型调用开始 / 完成 / 超时 / 失败”日志，可按 `run_id` 核对 `timeout_seconds` 和 `elapsed_ms`，失败日志额外记录异常类名及有界 `cause_types` 类型链，不包含异常原文、请求地址、提示、工具参数或模型正文。主模型与历史摘要均区分平台时限和上游读取超时，不再把 SDK 内置 `TimeoutError` 一律当作 Agent 预算耗尽。整轮执行超过单次时限并不一定异常，多次调用分别计时；已返回调用的累计 Token 也不能说明当前未返回调用正在做什么。

模型一次返回多个工具调用时，同一 Run 会先按全局、用户和 Run 并发上限的最小值在进程内排队，再领取数据库资源租约；排队不占工具名额或数据库连接，也不计入实际工具执行时限。跨进程总量仍由数据库校验，其他运行占满资源时保存 `failed/tool_capacity_exceeded`，不会因此漏记终态并等待租约过期；排队不会自动重放已经执行的工具。

如果长文档审核在工具成功后仍耗尽单次模型时限，可先核对上游服务是否在排队，以及 `thinking_mode=auto` 是否启用了耗时推理。对支持 `chat_template_kwargs.enable_thinking` 的 OpenAI 兼容服务，可将该 Agent 的 `thinking_mode` 改为 `disabled` 后，用新请求键复验；此选项不保证复杂任务的耗时或质量，需要检查实际报告。修改偏好只影响后续 Run，旧 Run 保留原快照和终态；提高默认值或允许上限不会自动改写既有 Agent 配置。

普通工具调用、缺失开始事件、重复调用 ID、结束事件不匹配、检查点归属不明或缺少可用的已确认检查点，不适用上述自动回退；无法通过其他既有恢复路径时仍返回 `checkpoint_mismatch`。该故障码不应通过把旧 Run 改回排队或删除检查点来绕过。尚无成功回答且全部历史轮次已终结、没有工具调用的会话，仍按既有规则在新提交时切换检查点线程；已发生或结果未知的外部写入不会因继续对话而自动撤销。

### 实时流与页面恢复

| 场景 | 接口与前端处理 | 数据来源及边界 |
| --- | --- | --- |
| 浏览器订阅 | 用 Bearer 调用 `POST /api/v1/runs/{run_id}/stream-ticket`，再用同源 `EventSource('/api/v1/runs/{run_id}/stream?after=0')`；通过 `addEventListener('state', ...)` 和 `addEventListener('text_delta', ...)` 处理命名事件。 | 5 分钟 HttpOnly Cookie 只用于该 Run 的 SSE，不新增 Run 或事件；流从已提交的事件表补发，事件 ID 为 `<run_id>:<sequence>`。票据到期后重新签发并按游标续传。 |
| 刷新、切换会话 | `GET /api/v1/agents/{agent_id}/sessions`、`GET /api/v1/agents/{agent_id}/sessions/{session_id}/runs?page=1&page_size=20` 和 `GET /api/v1/agents/{agent_id}/sessions/{session_id}/messages?after=<message_id>&limit=100`。 | 这些查询不创建新 Run。消息使用全局消息 ID 游标，事件使用各 Run 内序号，不能混用；每次提交 Run 也会使会话版本递增。每条助手消息的 `artifacts` 按产物创建顺序返回，前端无需逐个 Run 查询产物。 |
| 追平事件 | `GET /api/v1/runs/{run_id}/snapshot?after=<sequence>&limit=100` 返回 `run`、`events`、`latest_sequence`、`next_after`、`has_more`。逐页追平后订阅 `/stream?after=<next_after>`。 | 每页最多 200 条，快照上界只固定本次读取；按 `<run_id>:<sequence>` 去重。其他 Run 的 `Last-Event-ID` 或超前游标返回 400。 |
| 终态展示 | 收到 `completed`、`failed`、`cancelled`、`timed_out` 后停止加载；完成时读取最终消息，失败时展示 `failure_code`。未启用回答导出时可关闭 `EventSource`。 | 文本增量只是预览，失败时可能没有最终助手消息。模型调用生成工具时，文件与 `completed` 同事务发布，完成后的助手消息直接携带 `artifacts`；前端按其中 `artifact_id` 下载。若 `publish_answer_artifact=true`，worker 还会异步发布 `answer.txt`，可继续等待其 `artifact_published` 后重读消息。文件删除后历史引用保留且 `available=false`，下载返回 404。 |

会话页在提交后立即订阅 SSE，运行中显示已持久化的 `text_delta`，断线后按事件序号去重续接；仅在流连接异常或超时时轮询 Run 状态，避免状态查询先报告完成而中断增量预览。启用 Token/成本预算或明确要求交付文件的 Run 会暂缓公开文本，直到用量或产物验证通过，因此这些运行可能只先显示状态和工具进度，最终内容仍以消息接口为准。

### 附件与操作分支

| 场景 | 接口与前端处理 | 业务数据落库和流转 |
| --- | --- | --- |
| 上传并引用文件 | `POST /api/v1/artifacts/uploads` 上传；会话 Run 提交时传最多 5 个 `artifact_ids`，需要模型读取正文时再传其子集 `context_artifact_ids`。 | 文件元数据写入 `yp_artifact`，原文件在私有存储；用户消息和有序的 `yp_agent_message_attachment` 引用同事务提交。Worker 执行前重验所有权、期限、MIME、大小和哈希，提取选中的 TXT/Markdown、PDF、DOCX、XLSX 文本；单轮提取后连同文件标识受 `ATTACHMENT_CONTEXT_MAX_BYTES` 控制，默认 20 MiB。PNG/JPEG 每张最多 2 MiB、2048×2048 像素；每轮最多选择两张，且本轮会话模型须通过 `vision` 能力验证。图片去除元数据后作为多模态块进入模型和私有检查点，不进入公开消息或 SSE；旧检查点可能保留图片内容，删除原文件不会清除旧检查点。超限、加密、损坏或无法读取时 Run 以 `attachment_unavailable` 失败。TXT/Markdown 原文件在提交时另受本轮附件正文预算限制；未选中的附件只供展示和下载。 |
| 根据附件生成文件 | 本轮上传文件 ID 同时放入 `artifact_ids` 与 `context_artifact_ids`，`input_text` 写明所需格式；完成后读取会话 `messages`，从助手消息的 `artifacts` 获取生成文件 ID、`context_artifact_ids` 和 `read_source_artifact_ids`。Agent 的 `max_model_calls` 至少预留 2 次供工具调用与最终回复；需要首次未生成时自动纠正，则至少预留 3 次。 | 用户上传文件始终在用户消息的 `attachments`；模型生成文件写入私有存储，先记为 `pending`，完成时改为 `ready` 并与助手消息、事件同事务提交。产物列表和详情也返回本轮参考文件 ID，历史引用在原文件失效后保留。失败或取消后暂存文件不可下载，过期由清理任务回收。模型未调用工具时不会凭代码块自动生成文件。 |
| 继续使用本会话产物 | 用户说“修改刚才生成的文件”时无需把产物 ID 填入 `context_artifact_ids`；模型可先列出本会话产物，再按需读取目标。前端仍可从历史助手消息取得产物 ID，供用户明确指定。 | 产物仍存于私有文件存储，读取工具只访问同一用户、同一会话中已完成 Run 的有效产物，不把全部产物自动加入每轮上下文。成功读取记录在 `yp_run_artifact_read`；其后生成新文件时，将此前读取的旧产物 ID 固定在 `yp_artifact_source`。生成产物 ID 目前不能作为上传附件传给会话提交接口；模型未调用读取工具时，下一轮仅沿用原有检查点历史。 |
| 删除附件 | `DELETE /api/v1/artifacts/{artifact_id}` 后重新读取消息；下载仍用 `GET /api/v1/artifacts/{artifact_id}/download`。 | 文件状态变为 `deleted`，私有正文尽力清理；历史附件快照仍保留，动态 `available=false`，下载及新 Run 引用返回 404。已进入旧检查点的内容不会自动抹除。 |
| 重复提交 | 同一会话、输入、附件顺序、上下文附件选择和回答产物开关使用相同 `request_key` 重试，返回原 `run_id`；改变任一项返回 409。 | 重试不新增 Run、消息或事件，也不再次调用模型；被拒绝的请求不占用提交键。 |
| 取消 | `POST /api/v1/runs/{run_id}/cancel`。排队中直接返回 `cancelled`；运行中先返回 `cancel_requested`，再等待 worker 确认。 | 排队取消写终态事件，不调用模型；worker 重启不会补执行已取消 Run。运行中取消不撤销已发生的外部副作用。 |
| 编辑 Agent | `PUT /api/v1/agents/{agent_id}` 必须传 `expected_version`，旧版本返回 409。 | 原子更新 Agent 并递增版本；已提交 Run 保留创建时的模型、指令、时限和预算快照。修改默认模型只影响新建会话，旧会话继续使用自身保存的模型；需要改用其他模型时，通过会话模型切换接口操作。 |
| 归档与恢复 | `PUT /api/v1/agents/{agent_id}/sessions/{session_id}`，传 `expected_version` 和 `archived`。 | 会话版本递增；活动 Run 阻止归档，归档后拒绝新提交，恢复后可再次提交。 |
| 会话与用户隔离 | 同一 Agent 的不同会话分别查询消息；访问未共享的 Agent、他人会话、Run 或文件。 | 会话拥有独立 `thread_id`；服务端按所有权和指定共享授权，跨用户资源按不存在处理。若检查点与业务库不一致，后续 Run 可能报 `checkpoint_mismatch`，不能只换提交键假定历史已恢复。 |
