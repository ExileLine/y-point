import assert from "node:assert/strict";
import { chromium } from "playwright";
import { screenshotPath } from "./screenshot-paths.mjs";

const base = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
try {
  for (const width of [1280, 375]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: "reduce" });
    await context.addInitScript(() => sessionStorage.setItem("yp.access_token", "fixture-token"));
    const page = await context.newPage();
    const errors = [], unexpected = [], saved = [];
    const items = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/v1/**", async route => {
      const req = route.request(), path = new URL(req.url()).pathname, method = req.method();
      const send = data => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ code: 200, message: "操作成功", data }) });
      if (path === "/api/v1/auth/me") return send({ id: 1, username: "tester", role: "admin" });
      if (path === "/api/v1/tools/catalog") return send({ builtin: [], user_tools: items });
      if (path === "/api/v1/mcp-connections") return send([{ id: 1, name: "共享文档服务", transport: "streamable_http", version: 1, enabled: true, approval_mode: "writes" }]);
      if (path === "/api/v1/mcp-connections/1/discover") return send([{ name: "lookup", description: "查询", input_schema: { type: "object" }, read_only: true }]);
      if (path === "/api/v1/tools" && method === "POST") {
        const draft = req.postDataJSON();
        saved.push(draft);
        const id = items.length + 1;
        const item = { ...draft, id, name: `tool_fixture_${id}`, model_name: `user_tool_${id}_tool_fixture_${id}`, owner_id: 1, current_version: 1, verified: true, enabled: true };
        if (item.kind === "mcp") {
          item.effect = "read"; item.effect_source = "remote_read";
          item.requires_approval = item.approval_mode === "always";
          item.callability = { status: item.requires_approval ? "approval_required" : "direct", message: item.requires_approval ? "审批后可调用" : "可直接调用", reason: null, action: null, policy: null };
        }
        items.push(item);
        return send(item);
      }
      const match = /^\/api\/v1\/tools\/(\d+)$/.exec(path);
      if (match) {
        const index = items.findIndex(item => item.id === Number(match[1]));
        if (index >= 0 && method === "GET") return send(items[index]);
        if (index >= 0 && method === "PUT") {
          const changes = req.postDataJSON();
          assert.equal(changes.expected_version, items[index].current_version);
          saved.push(changes);
          items[index] = { ...items[index], ...changes, current_version: items[index].current_version + 1 };
          if (items[index].kind === "mcp") {
            items[index].requires_approval = items[index].approval_mode === "always";
            items[index].callability = { status: items[index].requires_approval ? "approval_required" : "direct", message: items[index].requires_approval ? "审批后可调用" : "可直接调用", reason: null, action: null, policy: null };
          }
          return send(items[index]);
        }
      }
      unexpected.push(`${method} ${path}`);
      return route.fulfill({ status: 404, body: "unexpected fixture request" });
    });
    await page.goto(`${base}/tools`);
    const toggle = page.getByRole("switch", { name: "调用前需要审批", exact: true });
    const drawer = page.locator(".ant-drawer-open");
    const save = async () => {
      await drawer.getByRole("button", { name: /保\s*存/ }).click();
      await drawer.waitFor({ state: "hidden" });
    };
    const approvalSelect = page.getByRole("combobox", { name: "工具审批策略", exact: true });
    const choosePolicy = async label => {
      await drawer.locator(".ant-select").filter({ has: approvalSelect }).click();
      await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").getByText(label, { exact: true }).click();
    };
    for (const requiresApproval of [false, true]) {
      await page.getByRole("button", { name: "新建 Tool", exact: true }).click();
      assert.equal(await toggle.getAttribute("aria-checked"), "false", "新工具默认不需要审批");
      await drawer.getByText(/保存后下一轮对话生效/).waitFor();
      await drawer.getByText("MCP", { exact: true }).click();
      await approvalSelect.waitFor();
      assert.equal(await toggle.count(), 0, "MCP 使用分层策略而非免审批开关");
      await page.getByLabel("显示名称", { exact: true }).fill(requiresApproval ? "confirmed_lookup" : "direct_lookup");
      await page.getByLabel("用途", { exact: true }).fill("读取公开资料");
      await drawer.locator(".ant-select").filter({ has: page.getByRole("combobox", { name: "Tool MCP 连接", exact: true }) }).click();
      await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").filter({ hasText: "共享文档服务 · streamable_http · v1" }).click();
      await page.getByRole("button", { name: "发现工具", exact: true }).click();
      await drawer.locator(".ant-select").filter({ has: page.getByRole("combobox", { name: "MCP 远端函数名", exact: true }) }).getByText("lookup", { exact: true }).waitFor();
      if (requiresApproval) await choosePolicy("每次调用均审批");
      await approvalSelect.scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: screenshotPath(`web-tool-approval-${requiresApproval}-${width}.png`), animations: "disabled" });
      await save();
      assert.equal(saved.at(-1).approval_mode, requiresApproval ? "always" : "inherit", "保存显式审批覆盖或继承");
      assert.equal(saved.at(-1).requires_approval, undefined, "避免新旧审批字段冲突");
      assert.equal(saved.at(-1).mcp_connection_id, 1);
      assert.equal(saved.at(-1).mcp_url, undefined);
    }
    const edit = async index => {
      const buttons = width < 768 ? page.locator(".resource-mobile-list").getByRole("button", { name: "编辑", exact: true }) : page.getByRole("button", { name: "编辑", exact: true });
      await buttons.nth(index).click();
      await approvalSelect.waitFor();
    };
    await edit(1);
    await drawer.getByText("每次调用均审批", { exact: true }).waitFor();
    await page.getByLabel("用途", { exact: true }).fill("更新用途并保留审批");
    await save();
    assert.equal(saved.at(-1).approval_mode, "always");
    await edit(1);
    await choosePolicy("写入和未知操作需审批");
    await save();
    assert.equal(saved.at(-1).approval_mode, "writes");
    await edit(1);
    await drawer.getByText("写入和未知操作需审批", { exact: true }).waitFor();
    await drawer.getByRole("button", { name: "Close", exact: true }).click();
    await drawer.waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "新建 Tool", exact: true }).click();
    await page.getByLabel("显示名称", { exact: true }).fill("script_approval");
    await page.getByLabel("用途", { exact: true }).fill("验证脚本兼容审批开关");
    await toggle.click();
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith("/api/v1/tools") && response.request().method() === "POST"),
      drawer.getByRole("button", { name: /保\s*存/ }).click(),
    ]);
    await drawer.getByRole("button", { name: "试运行", exact: true }).waitFor();
    assert.equal(saved.at(-1).requires_approval, true);
    assert.equal(saved.at(-1).approval_mode, undefined);
    assert.deepEqual(errors, []);
    assert.deepEqual(unexpected, []);
    await context.close();
    console.log(`${width}px：MCP 默认继承、显式审批、编辑保留与覆盖及脚本兼容保存请求通过`);
  }
} finally { await browser.close(); }
