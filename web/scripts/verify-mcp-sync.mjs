import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
try {
  for (const width of [1280, 375]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: "reduce" });
    await context.addInitScript(() => sessionStorage.setItem("yp.access_token", "fixture"));
    const page = await context.newPage(), errors = [], unexpected = [], saved = [], requests = [];
    page.on("pageerror", error => errors.push(error.message));
    let connection = { id: 1, name: "工单服务", transport: "streamable_http", url: "https://mcp.example/mcp", version: 1, enabled: true, args: [],
      has_credentials: true, has_auth_key: true, auth_type: "api_key", api_key_header: "x-key", header_names: ["x-tenant"], sensitive_header_names: ["x-secret"], share_credentials: false, approval_mode: "writes" };
    const agent = { id: 11, name: "工单助手", default_model_id: 1, enabled: true, version: 1, system_prompt: "处理工单", temperature: "1.00",
      max_output_tokens: null, model_timeout_seconds: null, tool_timeout_seconds: 120, thinking_mode: "auto", knowledge_mode: "auto", tool_ids: [1] };
    const schema = { type: "object", properties: { title: { type: "string" } }, required: ["title"] };
    const items = [
      { tool_id: 1, remote_name: "search", display_name: "工单查询", current_version: 1, state: "changed", changes: ["schema", "execution_config"], schema, previous_schema: { type: "object" }, requires_read_confirmation: true },
      { tool_id: null, remote_name: "create_ticket", display_name: "创建工单", current_version: null, state: "added", changes: [], schema, previous_schema: null },
      { tool_id: 3, remote_name: "removed", display_name: "旧工具", current_version: 1, state: "removed", changes: [], schema: null, previous_schema: schema },
      { tool_id: 4, remote_name: "same", display_name: "不变工具", current_version: 1, state: "unchanged", changes: [], schema, previous_schema: schema },
    ];
    await page.route("**/api/v1/**", async route => {
      const req = route.request(), path = new URL(req.url()).pathname, method = req.method();
      const send = (data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ code: status, message: status === 200 ? "操作成功" : data, data: status === 200 ? data : null }) });
      if (path === "/api/v1/auth/me") return send({ id: 1, username: "tester", role: "admin" });
      if (path === "/api/v1/tools/catalog") return send({ builtin: [], user_tools: [] });
      if (path === "/api/v1/mcp-connections/stdio-profiles") return send([]);
      if (path === "/api/v1/mcp-connections") return send([connection]);
      if (path === "/api/v1/mcp-connections/1" && method === "PUT") { const body = req.postDataJSON(); saved.push(body); connection = { ...connection, version: connection.version + 1 }; return send(connection); }
      if (path === "/api/v1/mcp-connections/1/sync-preview") return send({ connection_id: 1, connection_version: connection.version, preview_hash: "a".repeat(64), items });
      if (path === "/api/v1/mcp-connections/1/sync") {
        requests.push(req.postDataJSON());
        if (requests.length === 1) return send("响应丢失，请重试", 503);
        assert.deepEqual(requests[1], requests[0], "提交失败后的重试必须保留原 ID 与选择");
        return send({ request_id: requests[0].request_id, items: [{ tool_id: 1, remote_name: "search", version: 2, status: "published" },
          { tool_id: 2, remote_name: "create_ticket", status: "failed", message: "请修复配置后重新预览" }, { tool_id: 3, remote_name: "removed", status: "disabled" }] });
      }
      if (path === "/api/v1/agents/11/tool-upgrades" && method === "GET") return send({ agent_id: 11, agent_version: 1, preview_hash: "b".repeat(64), items: [
        { tool_id: 1, display_name: "工单查询", current_version: 1, target_version: 2, changed: true, changes: ["input_schema"], previous_schema: {}, schema, callability: { status: "approval_required", message: "审批后可调用" } },
        { tool_id: 3, display_name: "失效工具", current_version: 1, target_version: 2, changed: true, changes: ["access"], previous_schema: null, schema: null, callability: { status: "blocked", message: "工具授权已撤销" } }] });
      if (path === "/api/v1/agents/11/tool-upgrades" && method === "POST") { const body = req.postDataJSON(); assert.deepEqual(body.selections, [{ tool_id: 1, expected_version: 1, target_version: 2 }]); return send({ request_id: body.request_id, items: [{ tool_id: 1, version: 2, status: "upgraded" }] }); }
      if (path === "/api/v1/agents") return send({ items: [agent], total: 1, page: 1, page_size: 12 });
      if (path === "/api/v1/agents/11") return send(agent);
      if (path === "/api/v1/agents/11/shares" || path === "/api/v1/agents/shared") return send([]);
      if (path === "/api/v1/models/available") return send([{ id: 1, display_name: "测试模型" }]);
      unexpected.push(`${method} ${path}`); return send("unexpected", 404);
    });
    await page.goto(`${base}/tools`);
    await page.getByRole("button", { name: "MCP 连接", exact: true }).click();
    const manager = page.getByRole("dialog", { name: "MCP 连接", exact: true });
    await manager.getByRole("button", { name: "编辑连接", exact: true }).click();
    const editor = page.getByRole("dialog", { name: "编辑 MCP 连接", exact: true });
    assert.equal(await editor.getByLabel("连接 API Key", { exact: true }).inputValue(), "");
    assert.equal(await editor.getByLabel("普通 MCP Headers", { exact: true }).inputValue(), "");
    assert.equal(await editor.getByLabel("敏感 MCP Headers", { exact: true }).inputValue(), "{}");
    await editor.getByLabel("连接 API Key", { exact: true }).fill("fixture-secret");
    await editor.getByLabel("普通 MCP Headers", { exact: true }).fill('{"X-Tenant":"team-b"}');
    await editor.getByLabel("敏感 MCP Headers", { exact: true }).fill('{"X-Secret":null,"X-New":"fixture-header"}');
    await editor.getByRole("button", { name: "保存连接", exact: true }).click();
    await editor.waitFor({ state: "hidden" });
    assert.equal(saved[0].auth_type, "api_key"); assert.equal(saved[0].api_key, "fixture-secret"); assert.equal(saved[0].bearer_token, undefined);
    assert.deepEqual(saved[0].headers, { "X-Tenant": "team-b" }); assert.equal(saved[0].sensitive_headers["X-Secret"], null);
    await manager.getByRole("button", { name: "同步差异", exact: true }).click();
    const sync = page.getByRole("dialog", { name: "MCP 工具同步预览", exact: true });
    await sync.getByText("工单查询 · 有变化 · v1", { exact: true }).waitFor();
    assert.equal(await sync.getByRole("checkbox", { name: /不变工具/ }).isDisabled(), true);
    await sync.getByRole("checkbox", { name: /工单查询 · 有变化/ }).check();
    await sync.getByLabel("工单查询 只读确认理由").fill("已核对只执行查询");
    await sync.getByRole("checkbox", { name: /创建工单 · 新增/ }).check();
    await sync.getByRole("checkbox", { name: /旧工具 · 远端已移除/ }).check();
    await sync.getByRole("button", { name: "确认发布或停用所选工具", exact: true }).click();
    await sync.getByRole("button", { name: "重试本次提交", exact: true }).waitFor();
    assert.equal(await sync.getByRole("checkbox", { name: /工单查询 · 有变化/ }).isDisabled(), true);
    await sync.getByRole("button", { name: "重试本次提交", exact: true }).click();
    await sync.getByText("search · 已完成", { exact: true }).waitFor();
    await sync.getByText("create_ticket · 未完成", { exact: true }).waitFor();
    assert.equal(requests[0].selections[0].read_only_reason, "已核对只执行查询");
    assert.equal(requests[0].selections[2].action, "disable");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: `/tmp/yp-mcp-sync-${width}.png`, animations: "disabled" });
    await page.goto(`${base}/agents`);
    await page.getByRole("button", { name: "查看详情", exact: true }).click();
    const detail = page.getByRole("dialog");
    await detail.getByRole("button", { name: "检查工具版本更新", exact: true }).click();
    await detail.getByRole("checkbox", { name: /工单查询 · v1 → v2/ }).check();
    assert.equal(await detail.getByRole("checkbox", { name: /失效工具/ }).isDisabled(), true);
    await detail.getByRole("button", { name: "确认升级所选工具", exact: true }).click();
    await detail.getByText("Tool #1 · 已完成", { exact: true }).waitFor();
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    await context.close(); console.log(`${width}px: API Key、Headers、差异确认、部分失败稳定重试、移除停用和 Agent 显式升级通过`);
  }
} finally { await browser.close(); }
