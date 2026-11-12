import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const baseURL = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
const output = process.env.KNOWLEDGE_SCREENSHOT_DIR ? new URL(`file://${process.env.KNOWLEDGE_SCREENSHOT_DIR.replace(/\/$/, "")}/`) : new URL("../../docs/", import.meta.url);
await mkdir(output, { recursive: true });
const now = "2026-11-03 20:30:00";
const envelope = (data, code = 200, message = "操作成功") => ({ code, message, data });
const seed = { id: 1, owner_id: 1, name: "员工制度知识库", description: "员工手册、差旅与报销制度，让每个问题都有据可查。", revision: 3, active_index_id: 2, search_mode: "hybrid", embedding_model_id: 2, chunk_size: 800, chunk_overlap: 100, embedding_token_budget: 200000, embedding_cost_budget: null, create_time: now, update_time: now };
const citation = { citation: "kb:1:i:2:c:16", knowledge_base_id: 1, index_id: 2, document_id: 1, document_version: 1, chunk_id: 16, filename: "员工手册（差旅及报销制度）.md", sha256: "a".repeat(64), start_offset: 1441, end_offset: 1486, page: null, heading: "第六章 报销流程", content: "出差结束后 15 个工作日内提交报销申请。逾期超过 30 天须书面说明，超过 90 天原则上不予报销。" };
const jobSeed = { id: 2, index_id: 2, owner_id: 1, state: "completed", attempts: 1, lease_expires_at: null, failure_code: null, progress: [{ stage: "uploaded", time: now }, { stage: "embedding", skipped: false, time: now }, { stage: "completed", time: now }], reserved_tokens: 0, embedding_tokens: 300, estimated_cost: null, create_time: now, update_time: now };

async function fixture(page, opts = {}) {
  const calls = [];
  let base = { ...seed, ...(opts.base || {}), owner_id: opts.shared ? 7 : 1 };
  let docs = [{ id: 1, knowledge_base_id: 1, filename: citation.filename, current_version: 1, update_time: now }];
  let jobs = [{ ...jobSeed }];
  let shares = [7];
  let revoked = false;
  let listError = Boolean(opts.listError);
  let searchMode = "hits";
  let unsafe = false;
  let conflict = false;
  let duplicate = false;
  let cancelOn = false;
  let retryOn = false;
  let uploadExpired = false;
  let citationDenied = false;
  let slowCitation = false;
  let agentPayload;
  await page.addInitScript(() => { sessionStorage.setItem("yp.access_token", "fixture-access"); sessionStorage.setItem("yp.refresh_token", "fixture-refresh"); });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/v1", "");
    const method = req.method();
    const raw = req.postData() || "";
    const body = req.headers()["content-type"]?.startsWith("application/json") && raw ? JSON.parse(raw) : null;
    calls.push({ path, method, body, raw, search: url.search, headers: req.headers() });
    const send = (data, status = 200, text = "操作成功") => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(envelope(data, status, text)) });
    if (/\/sessions\/[^/]+\/metrics$/.test(path)) return send({ run_count: 0, active_run_count: 0, elapsed_ms: 0, total_tokens: 0, missing_duration_runs: 0, calculated_at: new Date().toISOString() });
    if (path === "/agents/runtime-limits") return send({ upload_max_bytes: 10485760, context_max_bytes: 20971520, max_files: 5, recursion_limit: 9999,
      mcp_connect_timeout_seconds: 10, mcp_discovery_timeout_seconds: 15, mcp_call_timeout_seconds: 120, knowledge_search_timeout_seconds: 60,
      knowledge_embedding_timeout_seconds: 60, script_execution_timeout_seconds: 30, script_test_timeout_seconds: 10,
      default_model_timeout_seconds: 120, max_model_timeout_seconds: 3600, default_tool_timeout_seconds: 120, max_tool_timeout_seconds: 3600,
      default_max_output_tokens: 4096, max_output_tokens: 65536 });
    if (path === "/auth/me") return send({ id: 1, username: "tester", nickname: "测试用户", role: opts.shared ? "user" : "admin" });
    if (path === "/models") return send(opts.models || [{ id: 2, display_name: "embeddinggemma", external_id: "embeddinggemma:latest", status: 1, capabilities: ["embedding"], verified_capabilities: ["embedding"] }]);
    if (path === "/models/2/embedding-test") return send({ success: true, failure_code: null, dimensions: 768, latency_ms: 77 });
    if (path === "/agents/share-targets") return send([{ user_id: 8, username: "colleague" }]);
    if (path === "/knowledge-bases" && method === "GET") {
      if (listError) return send(null, 503, "知识库服务暂不可用");
      const items = revoked || opts.empty ? [] : [base, ...(opts.shared ? [] : [{ ...base, id: 9, owner_id: 7, name: "产品文档", search_mode: "keyword", active_index_id: null, embedding_model_id: null }])];
      return send({ items, total: items.length, page: 1, page_size: 20 });
    }
    if (path === "/knowledge-bases" && method === "POST") { base = { ...base, ...body, revision: 1, active_index_id: null }; return send(base, 201); }
    if (path.startsWith("/knowledge-bases/1")) {
      if (revoked) return send(null, 404, "知识库不存在");
      if (path === "/knowledge-bases/1" && method === "GET") return send(base);
      if (path === "/knowledge-bases/1" && method === "PUT") {
        if (conflict) { base.revision++; return send(null, 409, "知识库版本已变化"); }
        base = { ...base, ...body, revision: base.revision + 1 }; delete base.expected_revision;
        return send({ knowledge_base: base, job_id: 3 });
      }
      if (path === "/knowledge-bases/1" && method === "DELETE") { revoked = true; return send(null); }
      if (path === "/knowledge-bases/1/documents" && method === "GET") return send(docs);
      if (path === "/knowledge-bases/1/documents" && method === "POST" || path === "/knowledge-bases/1/documents/1" && method === "PUT") {
        if (uploadExpired) { uploadExpired = false; base.revision++; return send(null, 409, "知识库版本已变化"); }
        if (duplicate) return send({ document: docs[0], revision: base.revision, job_id: null, deduplicated: true }, 202);
        base.revision++;
        if (method === "PUT") docs = [{ ...docs[0], current_version: docs[0].current_version + 1 }];
        jobs = [{ ...jobSeed, id: 3, index_id: 3, state: "uploaded", attempts: 0, progress: [{ stage: "uploaded", time: now }] }, ...jobs];
        return send({ document: docs[0], revision: base.revision, job_id: 3, deduplicated: false }, 202);
      }
      if (path === "/knowledge-bases/1/documents/1" && method === "DELETE") {
        if (conflict) return send(null, 409, "嵌入模型未通过独立验证或当前不可用");
        docs = []; base.revision++; return send({ revision: base.revision, job_id: 3 });
      }
      if (path === "/knowledge-bases/1/rebuild") { base.revision++; return send({ revision: base.revision, job_id: 3 }, 202); }
      if (path === "/knowledge-bases/1/jobs") {
        if (cancelOn) jobs[0] = { ...jobs[0], state: "cancelled" };
        if (retryOn) jobs[0] = { ...jobs[0], state: "uploaded" };
        return send(jobs);
      }
      if (path === "/knowledge-bases/1/shares" && method === "GET") return send({ user_ids: shares });
      if (path === "/knowledge-bases/1/shares" && method === "POST") { shares.push(body.user_id); return send(null, 201); }
      if (path === "/knowledge-bases/1/shares/7") { shares = shares.filter((id) => id !== 7); return send(null); }
      if (path === "/knowledge-bases/1/search") {
        if (searchMode === "failure") return send(null, 409, "知识检索后端不可用或超过预算");
        if (searchMode === "slow") await new Promise((resolve) => setTimeout(resolve, 500));
        return send({ hits: searchMode === "empty" ? [] : [{ ...citation, content: unsafe ? `${citation.content}\n<script>window.__unsafe = true</script>` : citation.content, score: 0.0322 }], no_answer: searchMode === "empty", byte_count: 150, untrusted_data: true });
      }
      if (path === "/knowledge-bases/1/citations/16") {
        if (slowCitation) await new Promise((resolve) => setTimeout(resolve, 500));
        return citationDenied ? send(null, 404, "知识引用不存在") : send(citation);
      }
    }
    if (path === "/knowledge-bases/jobs/3/cancel") { cancelOn = true; retryOn = false; return send(null); }
    if (path === "/knowledge-bases/jobs/3/retry") { retryOn = true; cancelOn = false; return send(null); }
    const session = { id: "session-a", agent_id: 11, thread_id: "thread-a", title: "制度问答", archived: false, version: 1, create_time: now, update_time: now };
    if (path === "/agents/11/sessions") return send({ items: [session], total: 1, page: 1, page_size: 20 });
    if (/\/sessions\/[^/]+\/models$/.test(path)) return send([]);
    if (path === "/agents/11/sessions/session-a") return send(session);
    if (path === "/agents/11/sessions/session-a/messages") return send([{ id: 1, role: "assistant", run_id: "run-kb", content: opts.answer || "请在 15 个工作日内提交，参考 kb:1:i:2:c:16。", create_time: now, attachments: [], artifacts: [] }]);
    if (path === "/agents/11/sessions/session-a/runs") return send({ items: [], total: 0, page: 1, page_size: 20 });
    if (path === "/runs/run-kb/knowledge-retrievals") return send(opts.noRetrieval ? [] : [{ id: 1, run_id: "run-kb", references: opts.noHits ? [] : [citation], hit_count: opts.noHits ? 0 : 1, byte_count: opts.noHits ? 0 : 150, latency_ms: 20, embedding_usage: [{ model_id: 2, dimensions: 768, tokens: 12, cost: null }] }]);
    const agent = { id: 11, name: "员工助手", description: "制度问答", system_prompt: "引用资料回答", welcome_message: "欢迎", default_model_id: 1, temperature: "1", max_output_tokens: 4096, thinking_mode: "auto", model_timeout_seconds: 120, tool_timeout_seconds: 120, max_model_calls: null, max_tool_calls: null, max_total_tokens: null, max_estimated_cost: null, enabled: true, version: 2, skill_ids: [], tool_ids: [], knowledge_base_ids: [1], update_time: now };
    if (path === "/agents" && method === "GET") return send({ items: [agent], total: 1, page: 1, page_size: 20 });
    if (path === "/agents/shared") return send([]);
    if (path === "/agents/11" && method === "GET") return send(agent);
    if (path === "/agents/11" && method === "PUT") { agentPayload = body; return send({ ...agent, ...body }); }
    if (path === "/models/available") return send([{ id: 1, display_name: "文本模型", context_window: 65536 }]);
    if (path === "/skills") return send([]);
    if (path === "/tools/catalog") return send({ builtin: [], user_tools: [] });
    return send(null, 404, `未处理 fixture: ${method} ${path}`);
  });
  return { calls, setUnsafe: () => { unsafe = true; }, setRevoked: () => { revoked = true; }, setListError: (value) => { listError = value; }, setSearch: (value) => { searchMode = value; }, setConflict: (value) => { conflict = value; }, setDuplicate: () => { duplicate = true; }, setUploadExpired: () => { uploadExpired = true; }, denyCitation: () => { citationDenied = true; }, slowCitation: () => { slowCitation = true; }, agentPayload: () => agentPayload, failJob: () => { jobs = [{ ...jobSeed, id: 3, index_id: 3, state: "failed", failure_code: "ocr_required" }]; }, completeJob: () => { jobs[0] = { ...jobs[0], state: "completed" }; base.active_index_id = jobs[0].index_id; } };
}
const shot = async (page, name) => {
  const overlay = await page.locator(".ant-drawer-open:visible, .ant-modal-wrap:visible").count();
  if (overlay) await page.waitForFunction(() => [...document.querySelectorAll(".ant-drawer-open .ant-drawer-content-wrapper")].every((node) => {
    const rect = node.getBoundingClientRect();
    return rect.left >= -1 && rect.right <= window.innerWidth + 1;
  }));
  await page.screenshot({ path: new URL(`web-knowledge-${name}.png`, output).pathname, fullPage: !overlay, animations: "disabled" });
};
const noOverflow = async (page, width) => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
const tab = (page, name) => page.getByRole("tab", { name, exact: true });
const confirm = async (page) => page.locator(".ant-popconfirm .ant-btn-primary").click();
const uploadFile = (page) => page.locator('input[type="file"]').setInputFiles({ name: "测试资料.md", mimeType: "text/markdown", buffer: Buffer.from("# 差旅制度\n15 个工作日内提交报销申请。") });
let scenarios = 0;
try {
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const state = await fixture(page, {
      base: { search_mode: "hybrid", embedding_model_id: null },
      models: [
        { id: 2, display_name: "embeddinggemma", status: 1, capabilities: ["text"], verified_capabilities: ["embedding"], embedding_dimensions: 768 },
        { id: 3, display_name: "待验证嵌入", status: 1, capabilities: ["embedding"], verified_capabilities: [] },
        { id: 4, display_name: "仅文本模型", status: 1, capabilities: ["text"], verified_capabilities: ["text"] },
        { id: 5, display_name: "已停用嵌入", status: 0, capabilities: ["embedding"], verified_capabilities: ["embedding"] },
      ],
    });
    await page.goto(`${baseURL}/knowledge-bases/1`);
    const modelsResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/models");
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await modelsResponse;
    const field = page.locator(".ant-form-item").filter({ has: page.getByText("嵌入模型", { exact: true }) });
    await field.locator(".ant-select-selector").click();
    const dropdown = page.locator(".ant-select-dropdown:visible");
    await dropdown.waitFor();
    const labels = await dropdown.innerText();
    assert.ok(labels.includes("embeddinggemma · 已验证"), `${width}: 已验证但未声明的嵌入模型应可选，实际：${labels}`);
    assert.ok(labels.includes("待验证嵌入 · 待验证"));
    assert.ok(!labels.includes("仅文本模型") && !labels.includes("已停用嵌入"));
    await dropdown.getByText("embeddinggemma · 已验证", { exact: true }).click();
    await page.getByRole("button", { name: "保存并重建" }).click();
    await page.getByRole("dialog", { name: "知识库设置" }).waitFor({ state: "hidden" });
    assert.equal(state.calls.findLast(call => call.path === "/knowledge-bases/1" && call.method === "PUT").body.embedding_model_id, 2);
    scenarios++;
    await page.close();
  }
  for (const [size, width, height] of [["desktop", 1280, 900], ["mobile", 375, 812]]) {
    const page = await browser.newPage({ viewport: { width, height } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const state = await fixture(page);
    await page.goto(`${baseURL}/knowledge-bases`);
    await page.getByRole("heading", { name: "员工制度知识库", exact: true }).waitFor();
    await noOverflow(page, width); await shot(page, `${size}`); scenarios++;
    await page.getByLabel("搜索本页知识库").fill("不存在");
    await page.getByText("本页没有匹配结果，可清除筛选或切换分页").waitFor();
    await page.getByRole("button", { name: "清除筛选" }).click();
    await page.getByRole("button", { name: "打开知识库 员工制度知识库" }).click();
    await page.getByText(citation.filename, { exact: true }).first().waitFor();
    await noOverflow(page, width); await shot(page, `detail-${size}`); scenarios++;
    await tab(page, "检索测试").click();
    await page.getByRole("textbox", { name: "检索问题" }).fill("出差回来后最晚什么时候需要报销？");
    await page.getByRole("button", { name: "检索测试", exact: true }).click();
    await page.locator(".kb-hit").waitFor();
    assert.equal(state.calls.findLast((call) => call.path.endsWith("/search")).body.keyword_only, false);
    assert.equal(await page.evaluate(() => window.__unsafe), undefined);
    await noOverflow(page, width); await shot(page, `search-${size}`); scenarios++;
    await page.getByRole("button", { name: "查看引用" }).click();
    await page.getByText("已重新核验引用访问权限").waitFor();
    await noOverflow(page, width); await shot(page, `citation-${size}`);
    await page.getByRole("button", { name: "Close", exact: true }).click();
    const strategy = page.getByRole("combobox", { name: "检索方式" });
    await strategy.scrollIntoViewIfNeeded();
    await page.locator(".ant-select").filter({ has: strategy }).click();
    await page.locator(".ant-select-dropdown:visible").getByText("仅关键词", { exact: true }).click();
    await page.getByRole("button", { name: "检索测试", exact: true }).click();
    await page.locator(".kb-hit").waitFor();
    assert.equal(state.calls.findLast((call) => call.path.endsWith("/search")).body.keyword_only, true); scenarios++;
    state.setSearch("empty");
    await page.getByRole("button", { name: "检索测试", exact: true }).click();
    await page.getByText("未找到足够相关的片段，请调整问题或核对文档内容").waitFor(); scenarios++;
    state.setSearch("failure");
    await page.getByRole("button", { name: "检索测试", exact: true }).click();
    await page.getByText("知识检索后端不可用或超过预算", { exact: true }).waitFor();
    assert.equal(await page.locator(".kb-hit").count(), 0); scenarios++;
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("textbox", { name: "名称", exact: true }).fill("员工制度知识库 · 修订");
    await noOverflow(page, width); await shot(page, `settings-${size}`);
    await page.getByRole("button", { name: "保存并重建" }).click();
    await page.getByRole("heading", { name: "员工制度知识库 · 修订", exact: true }).waitFor();
    const update = state.calls.findLast((call) => call.path === "/knowledge-bases/1" && call.method === "PUT");
    assert.equal(update.body.expected_revision, 3); assert.equal(update.body.chunk_size, 800); assert.equal(update.body.embedding_model_id, 2); scenarios++;
    await tab(page, "文档 · 1").click();
    await page.getByRole("button", { name: "上传文档", exact: true }).click();
    await uploadFile(page);
    await page.getByRole("button", { name: "上传并建立索引" }).click();
    await page.getByRole("dialog", { name: "上传文档", exact: true }).waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "Close", exact: true }).click();
    const upload = state.calls.findLast((call) => call.path.endsWith("/documents") && call.method === "POST");
    assert.match(upload.headers["content-type"], /^multipart\/form-data; boundary=/);
    assert.match(upload.raw, /name="expected_revision"\r\n\r\n4/);
    assert.match(upload.raw, /name="file"/); scenarios++;
    await page.getByText("等待处理", { exact: true }).waitFor();
    await shot(page, `jobs-${size}`); await noOverflow(page, width);
    state.completeJob();
    await page.locator(".kb-summary").filter({ hasText: "已发布索引" }).getByText("#3", { exact: true }).waitFor(); scenarios++;
    await tab(page, "共享设置").click();
    await page.getByRole("combobox", { name: "搜索共享用户" }).fill("coll");
    await page.getByText("colleague · #8", { exact: true }).click();
    await page.getByRole("button", { name: "授权", exact: true }).click();
    await page.getByText("用户 #8", { exact: true }).waitFor();
    assert.equal(state.calls.findLast((call) => call.path.endsWith("/shares") && call.method === "POST").body.user_id, 8);
    await page.getByRole("button", { name: "撤销用户 7 的授权" }).click(); await confirm(page);
    await page.getByText("用户 #7", { exact: true }).waitFor({ state: "hidden" }); scenarios++;
    assert.deepEqual(errors, []); await page.close();
  }
  // Shared users cannot call management endpoints; periodic access revalidation clears evidence.
  {
    const page = await browser.newPage(); const state = await fixture(page, { shared: true });
    await page.goto(`${baseURL}/knowledge-bases/1`); await tab(page, "检索测试").waitFor();
    assert.equal(await page.getByRole("button", { name: "设置", exact: true }).count(), 0);
    assert.equal(await tab(page, "索引任务").count(), 0); assert.equal(await tab(page, "共享设置").count(), 0);
    assert.equal(state.calls.filter((call) => /\/(jobs|shares|models)$/.test(call.path)).length, 0);
    await tab(page, "检索测试").click(); await page.getByRole("textbox", { name: "检索问题" }).fill("报销期限");
    await page.getByRole("button", { name: "检索测试", exact: true }).click(); await page.locator(".kb-hit").waitFor();
    state.setRevoked(); await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForURL(`${baseURL}/knowledge-bases`); assert.equal(await page.locator(".kb-hit").count(), 0); scenarios++; await page.close();
  }
  // Recoverable list error and empty state stay distinct.
  {
    const page = await browser.newPage({ viewport: { width: 375, height: 812 } }); const state = await fixture(page, { listError: true, empty: true });
    await page.goto(`${baseURL}/knowledge-bases`); await page.getByText("知识库服务暂不可用").waitFor(); await shot(page, "error-mobile");
    assert.equal(await page.getByText("还没有可访问的知识库").count(), 0);
    state.setListError(false); await page.getByRole("button", { name: /^重\s*试$/ }).click();
    await page.getByText("还没有可访问的知识库").waitFor(); await noOverflow(page, 375); await shot(page, "empty-mobile"); scenarios++;
    await page.getByRole("button", { name: "创建第一个知识库" }).click(); await page.getByRole("textbox", { name: "名称", exact: true }).fill("测试知识库");
    await page.getByRole("button", { name: /^创\s*建$/ }).click(); await page.waitForURL(`${baseURL}/knowledge-bases/1`);
    const created = state.calls.findLast((call) => call.path === "/knowledge-bases" && call.method === "POST").body;
    assert.equal(created.search_mode, "keyword");
    assert.equal(created.processing_token_budget, null);
    assert.equal(created.embedding_token_budget, null);
    scenarios++; await page.close();
  }
  // Failed jobs, cancel handshake, retry compare-and-swap; conflict keeps document visible.
  {
    const page = await browser.newPage(); const state = await fixture(page); state.failJob();
    await page.goto(`${baseURL}/knowledge-bases/1`); await tab(page, "索引任务").click();
    await page.getByRole("button", { name: /^重\s*试$/ }).click(); await page.getByText("等待处理", { exact: true }).waitFor();
    assert.equal(state.calls.findLast((call) => call.path.endsWith("/retry")).search, "?expected_attempts=1");
    await page.getByRole("button", { name: "取消任务" }).click(); await confirm(page);
    await page.getByText("已取消", { exact: true }).waitFor(); scenarios++;
    await tab(page, "文档 · 1").click(); state.setConflict(true);
    await page.getByRole("button", { name: `删除文档 ${citation.filename}` }).click(); await confirm(page);
    await page.getByText(/嵌入模型未通过独立验证或当前不可用/).waitFor();
    await page.getByText(citation.filename, { exact: true }).waitFor();
    const refreshed = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/knowledge-bases/1");
    await page.evaluate(() => window.dispatchEvent(new Event("focus"))); await refreshed;
    await page.getByText(/嵌入模型未通过独立验证或当前不可用/).waitFor(); scenarios++;
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "保存并重建" }).click(); await page.getByText("知识库版本已变化", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "保存并重建" }).isDisabled(), true); scenarios++; await page.close();
  }
  // Upload stale revision, explicit retry with refreshed revision; dedup produces no new task.
  {
    const page = await browser.newPage(); const state = await fixture(page);
    await page.goto(`${baseURL}/knowledge-bases/1`); await page.getByRole("button", { name: "上传文档", exact: true }).click();
    state.setUploadExpired(); await uploadFile(page); await page.getByRole("button", { name: "上传并建立索引" }).click();
    await page.getByText(/已刷新版本，请核对同名文档/).waitFor();
    state.setDuplicate(); await page.getByRole("button", { name: "上传并建立索引" }).click();
    await page.getByRole("dialog", { name: "上传文档", exact: true }).waitFor({ state: "hidden" });
    const requests = state.calls.filter((call) => call.path.endsWith("/documents") && call.method === "POST");
    assert.equal(requests.length, 2); assert.match(requests[1].raw, /name="expected_revision"\r\n\r\n4/);
    assert.equal(await page.getByRole("dialog", { name: /索引任务/ }).count(), 0); scenarios++; await page.close();
  }
  // Citation deletion invalidates rendered hits; no stale evidence remains after failed reauthorization.
  {
    const page = await browser.newPage(); const state = await fixture(page);
    await page.goto(`${baseURL}/knowledge-bases/1`); await tab(page, "检索测试").click();
    await page.getByRole("textbox", { name: "检索问题" }).fill("报销期限"); await page.getByRole("button", { name: "检索测试", exact: true }).click();
    await page.locator(".kb-hit").waitFor(); state.denyCitation(); await page.getByRole("button", { name: "查看引用" }).click();
    await page.getByText("知识引用不存在", { exact: true }).waitFor(); assert.equal(await page.locator(".kb-hit").count(), 0); scenarios++; await page.close();
  }
  // Agent preserves associations on unrelated edits and sends selected IDs only when changed.
  {
    const page = await browser.newPage(); const state = await fixture(page);
    await page.goto(`${baseURL}/agents`); await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByRole("textbox", { name: "名称", exact: true }).fill("员工助手新版");
    await page.getByRole("button", { name: /保\s*存/ }).click();
    await page.waitForFunction(() => !document.querySelector(".ant-drawer-open"));
    assert.equal(state.agentPayload().knowledge_base_ids, undefined);
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByRole("combobox", { name: "知识库", exact: true }).click();
    await page.getByText("产品文档 · 未发布索引", { exact: true }).click();
    await page.getByRole("textbox", { name: "名称", exact: true }).click();
    await page.getByRole("button", { name: /保\s*存/ }).click();
    await page.waitForFunction(() => !document.querySelector(".ant-drawer-open"));
    assert.deepEqual(state.agentPayload().knowledge_base_ids, [1, 9]); scenarios++; await page.close();
  }
  // Document updates send PUT and current revision; successful delete is reflected only after response.
  {
    const page = await browser.newPage(); const state = await fixture(page);
    await page.goto(`${baseURL}/knowledge-bases/1`); await page.getByRole("button", { name: "更新版本", exact: true }).click();
    await page.locator('input[type="file"]').setInputFiles({ name: "错误格式.exe", mimeType: "application/octet-stream", buffer: Buffer.from("not a document") });
    await page.getByText("仅支持 TXT、Markdown、PDF、DOCX、XLSX、CSV", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "上传并建立索引" }).isDisabled(), true);
    await uploadFile(page); await page.getByRole("button", { name: "上传并建立索引" }).click();
    await page.getByRole("dialog", { name: /更新文档版本/ }).waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "Close", exact: true }).click();
    assert.equal(state.calls.findLast((call) => call.path.endsWith("/documents/1")).method, "PUT");
    await tab(page, "文档 · 1").click(); await page.getByText("v2", { exact: true }).waitFor();
    await page.getByRole("button", { name: `删除文档 ${citation.filename}` }).click(); await confirm(page);
    await tab(page, "文档 · 0").waitFor();
    assert.equal(state.calls.findLast((call) => call.path.endsWith("/documents/1")).search, "?expected_revision=4"); scenarios++;
    await tab(page, "索引任务").click(); await page.getByRole("button", { name: "重建索引", exact: true }).click(); await confirm(page);
    await page.waitForFunction(() => document.body.innerText.includes("配置版本 r6"));
    assert.equal(state.calls.findLast((call) => call.path.endsWith("/rebuild")).body.expected_revision, 5); scenarios++;
    await tab(page, "管理").click(); await page.getByRole("button", { name: "删除知识库", exact: true }).click(); await confirm(page);
    await page.waitForURL(`${baseURL}/knowledge-bases`);
    assert.equal(state.calls.findLast((call) => call.path === "/knowledge-bases/1" && call.method === "DELETE").search, "?expected_revision=6"); scenarios++; await page.close();
  }
  // Switching tabs discards a delayed search response instead of showing another view's stale evidence.
  {
    const page = await browser.newPage(); const state = await fixture(page); state.setSearch("slow");
    await page.goto(`${baseURL}/knowledge-bases/1`); await tab(page, "检索测试").click();
    await page.getByRole("textbox", { name: "检索问题" }).fill("报销期限");
    const response = page.waitForResponse((response) => response.url().endsWith("/search"));
    await page.getByRole("button", { name: "检索测试", exact: true }).click();
    await tab(page, "文档 · 1").click(); await response;
    await tab(page, "检索测试").click(); assert.equal(await page.locator(".kb-hit").count(), 0); scenarios++; await page.close();
  }
  // Retrieved HTML and scripts remain literal text.
  {
    const page = await browser.newPage(); const state = await fixture(page); state.setUnsafe();
    await page.goto(`${baseURL}/knowledge-bases/1`); await tab(page, "检索测试").click();
    await page.getByRole("textbox", { name: "检索问题" }).fill("报销期限"); await page.getByRole("button", { name: "检索测试", exact: true }).click();
    await page.locator(".kb-hit-content").waitFor(); assert.match(await page.locator(".kb-hit-content").innerText(), /<script>/);
    assert.equal(await page.locator(".kb-hit-content script").count(), 0); assert.equal(await page.evaluate(() => window.__unsafe), undefined); scenarios++; await page.close();
  }
  // Conversation evidence is loaded on demand and does not equate retrieval with final answer adoption.
  {
    const page = await browser.newPage({ viewport: { width: 375, height: 812 } }); const state = await fixture(page);
    await page.goto(`${baseURL}/agents/11/sessions/session-a`);
    await page.getByRole("button", { name: "本轮知识检索记录" }).waitFor();
    assert.equal(state.calls.some((call) => call.path.endsWith("/knowledge-retrievals")), false);
    await page.getByRole("button", { name: "本轮知识检索记录" }).click();
    await page.getByRole("button", { name: "kb:1:i:2:c:16", exact: true }).click();
    await page.locator(".kb-quoted-content").waitFor();
    await noOverflow(page, 375); await shot(page, "run-evidence-mobile");
    state.denyCitation(); await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByText("知识引用不存在", { exact: true }).waitFor();
    assert.equal(await page.locator(".kb-quoted-content").count(), 0); scenarios++; await page.close();
  }
  {
    const page = await browser.newPage({ viewport: { width: 375, height: 812 } }); await fixture(page, { noHits: true });
    await page.goto(`${baseURL}/agents/11/sessions/session-a`);
    await page.getByRole("button", { name: "本轮知识检索记录" }).click();
    await page.getByText("未找到相关内容 · 20 ms", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: /^kb:/ }).count(), 0);
    await noOverflow(page, 375); scenarios++; await page.close();
  }
  // 重放真实历史尾注：编号不能猜测绑定，入口按需读取本轮记录，无记录时也不借用其他轮次。
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const state = await fixture(page, {
      answer: "根据知识库中的《差旅报销管理规定》，差旅报销申请须在**十五个工作日**内提交。\n\ncitation: [1]",
      noRetrieval: width === 375,
    });
    await page.goto(`${baseURL}/agents/11/sessions/session-a`);
    await page.getByRole("button", { name: "来源说明", exact: true }).waitFor();
    assert.doesNotMatch(await page.locator(".session-message-content").innerText(), /citation:\s*\[1\]/);
    assert.equal(state.calls.some(call => call.path.endsWith("/knowledge-retrievals")), false);
    await shot(page, `legacy-source-${width}`);
    await page.getByRole("button", { name: "来源说明", exact: true }).click();
    await page.getByText("历史引用编号未与原文绑定", { exact: true }).waitFor();
    assert.equal(state.calls.some(call => call.path.includes("/citations/")), false);
    if (width === 375) {
      await page.getByText("本轮没有知识检索记录", { exact: true }).waitFor();
      assert.equal(await page.getByRole("button", { name: /^kb:/ }).count(), 0);
    } else {
      await page.getByRole("button", { name: "kb:1:i:2:c:16", exact: true }).click();
      await page.locator(".kb-quoted-content").waitFor();
      state.denyCitation(); await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await page.getByText("知识引用不存在", { exact: true }).waitFor();
      assert.equal(await page.locator(".kb-quoted-content").count(), 0);
    }
    await noOverflow(page, width); await shot(page, `legacy-source-explanation-${width}`);
    scenarios++; await page.close();
  }
  console.log(`知识库浏览器契约回归通过：${scenarios} 个场景，桌面 1280px / 手机 375px，截图已保存。使用拦截响应，未执行真实后端或模型验收。`);
} finally { await browser.close(); }
