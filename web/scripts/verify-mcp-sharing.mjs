import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
try {
  for (const width of [1280, 375]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: "reduce" });
    await context.addInitScript(() => sessionStorage.setItem("yp.access_token", "fixture"));
    const page = await context.newPage(), errors = [], unexpected = [], updates = [], grants = [];
    page.on("pageerror", error => errors.push(error.message));
    const tools = Array.from({ length: 17 }, (_, index) => ({ tool_id: index + 1, display_name: `查询工具 ${index + 1}`, model_name: `tool_${index + 1}`, tool_version: index === 0 ? 2 : 1,
      callability: { status: index === 16 ? "blocked" : "direct", message: index === 16 ? "凭据借用未授权" : "可直接调用", reason: null, action: null, policy: null } }));
    const summary = { id: 1, name: "工单连接", owner_id: 1, enabled: true, can_edit: true, allow_credentials: true, tools };
    let failCatalog = false, failGrant = true, removed = false;
    let shares = [{ user_id: 2, username: "teammate", version: 3, tool_ids: [1, 2], allow_credentials: true }];
    let agent = { id: 11, name: "连接助手", description: null, welcome_message: null, default_model_id: 1, enabled: true, version: 1, system_prompt: "查询",
      temperature: "1.00", max_output_tokens: null, model_timeout_seconds: null, tool_timeout_seconds: 120, thinking_mode: "auto", knowledge_mode: "auto",
      max_model_calls: null, max_tool_calls: null, max_total_tokens: null, max_estimated_cost: null, knowledge_base_ids: [], skill_ids: [], tool_ids: [],
      mcp_connections: [{ connection_id: 1, tools: [{ tool_id: 1, tool_version: 1 }] }] };
    await page.route("**/api/v1/**", async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname, method = req.method();
      const send = (data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ code: status, message: status < 400 ? "操作成功" : data, data: status < 400 ? data : null }) });
      if (path === "/api/v1/auth/me") return send({ id: 1, username: "owner", role: "admin" });
      if (path === "/api/v1/mcp-connections/catalog") return failCatalog ? send("目录暂时不可用", 503) : send([summary]);
      if (path === "/api/v1/tools/catalog") return send({ builtin: [], user_tools: [] });
      if (path === "/api/v1/mcp-connections/stdio-profiles") return send([]);
      if (path === "/api/v1/mcp-connections") return send([{ id: 1, name: "工单连接", transport: "streamable_http", version: 1, url: "http://fixture.invalid/mcp", enabled: true, has_credentials: true, args: [], share_credentials: false, approval_mode: "writes" }]);
      if (path === "/api/v1/mcp-connections/1/shares") return send(shares);
      if (path === "/api/v1/mcp-connections/1/shares/2" && method === "PUT") {
        grants.push(req.postDataJSON());
        if (failGrant) { failGrant = false; return send("授权已变化，请刷新后重试", 409); }
        shares = [{ user_id: 2, username: "teammate", version: 4, tool_ids: grants.at(-1).tool_ids, allow_credentials: grants.at(-1).allow_credentials }];
        return send(shares[0]);
      }
      if (path === "/api/v1/mcp-connections/1/shares/2" && method === "DELETE") {
        assert.equal(url.searchParams.get("expected_version"), "4"); removed = true; shares = []; return send(null);
      }
      if (path === "/api/v1/agents/share-targets") return send([{ user_id: 2, username: "teammate" }]);
      if (path === "/api/v1/agents") return send({ items: [agent], total: 1, page: 1, page_size: 12 });
      if (path === "/api/v1/agents/11" && method === "PUT") { updates.push(req.postDataJSON()); agent = { ...agent, ...updates.at(-1), version: agent.version + 1 }; return send(agent); }
      if (path === "/api/v1/agents/11") return send(agent);
      if (path === "/api/v1/agents/shared" || path === "/api/v1/skills") return send([]);
      if (path === "/api/v1/knowledge-bases") return send({ items: [], total: 0, page: 1, page_size: 100 });
      if (path === "/api/v1/models/available") return send([{ id: 1, display_name: "测试模型", context_window: 8192 }]);
      if (path === "/api/v1/agents/runtime-limits") return send({ default_max_output_tokens: null, max_output_tokens: 1000000, default_model_timeout_seconds: null, max_model_timeout_seconds: 3600, default_tool_timeout_seconds: 120, max_tool_timeout_seconds: 3600 });
      unexpected.push(`${method} ${path}`); return send([]);
    });
    await page.goto(`${base}/agents`);
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    const editor = page.getByRole("dialog", { name: "编辑 Agent", exact: true });
    await editor.getByText("1 个已选工具", { exact: true }).waitFor();
    assert.match(await editor.locator(".resource-surface").innerText(), /v1/);
    await editor.getByRole("button", { name: "选择全部可用工具", exact: true }).click();
    await editor.getByText("16 个已选工具", { exact: true }).waitFor();
    // 清除 6 项，使示例明确只保存 10 个，而非默认全部授权。
    for (const id of [11, 12, 13, 14, 15, 16]) {
      const chip = editor.locator(".ant-select-selection-item").filter({ hasText: new RegExp(`^查询工具 ${id} ·`) });
      await chip.locator(".ant-select-selection-item-remove").click();
    }
    await editor.getByText("10 个已选工具", { exact: true }).waitFor();
    await editor.getByRole("button", { name: /^保\s*存$/ }).click();
    await editor.waitFor({ state: "hidden" });
    assert.equal(updates[0].mcp_connections[0].tools.length, 10);
    assert.equal(updates[0].mcp_connections[0].tools[0].tool_version, 1, "批量选择不应静默升级旧绑定");
    assert.equal(updates[0].mcp_connections[0].tools.some(tool => tool.tool_id === 17), false);
    // 目录失败保留选择，移除失效连接仍能保存修复。
    failCatalog = true;
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await editor.getByText(/连接目录加载失败/).waitFor();
    await editor.getByText("10 个已选工具", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: `/tmp/yp-mcp-sharing-picker-${width}.png`, animations: "disabled" });
    await editor.getByRole("button", { name: "移除此连接", exact: true }).click();
    await editor.getByRole("button", { name: /^保\s*存$/ }).click();
    await editor.waitFor({ state: "hidden" });
    assert.deepEqual(updates[1].mcp_connections, []);
    failCatalog = false;
    await page.goto(`${base}/tools`);
    await page.getByRole("button", { name: "MCP 连接", exact: true }).click();
    const manager = page.getByRole("dialog", { name: "MCP 连接", exact: true });
    await manager.getByRole("button", { name: "连接共享", exact: true }).click();
    const sharing = page.getByRole("dialog", { name: "工单连接 · 指定用户共享", exact: true });
    await sharing.getByRole("button", { name: "修改授权", exact: true }).click();
    assert.equal(await sharing.getByLabel("允许指定用户借用凭据").isChecked(), true);
    await sharing.getByLabel("允许指定用户借用凭据").click();
    await sharing.getByRole("button", { name: "保存连接授权", exact: true }).click();
    await sharing.getByText("授权已变化，请刷新后重试", { exact: true }).waitFor();
    await sharing.getByRole("button", { name: "保存连接授权", exact: true }).click();
    await sharing.getByText("2 个工具 · 未授予凭据借用", { exact: true }).waitFor();
    assert.deepEqual(grants[0], { expected_version: 3, tool_ids: [1, 2], allow_credentials: false });
    assert.deepEqual(grants[1], grants[0]);
    await sharing.getByRole("button", { name: "撤销连接授权", exact: true }).click();
    await page.getByRole("button", { name: /^(确\s*定|OK)$/ }).click();
    await sharing.getByText("尚无指定用户授权", { exact: true }).waitFor();
    assert.equal(removed, true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: `/tmp/yp-mcp-sharing-manager-${width}.png`, animations: "disabled" });
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    await context.close();
    console.log(`${width}px: 17 工具选择 10 个、保留旧版本、阻断禁选、目录失败保留/修复、独立凭据授权、冲突重试及撤销通过`);
  }
} finally { await browser.close(); }
