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
      if (path === "/api/v1/mcp-connections") return send([{ id: 1, name: "共享文档服务", transport: "streamable_http", version: 1, enabled: true }]);
      if (path === "/api/v1/mcp-connections/1/discover") return send([{ name: "lookup", description: "查询", input_schema: { type: "object" } }]);
      if (path === "/api/v1/tools" && method === "POST") {
        const draft = req.postDataJSON();
        saved.push(draft);
        const id = items.length + 1;
        const item = { ...draft, id, name: `tool_fixture_${id}`, model_name: `user_tool_${id}_tool_fixture_${id}`, owner_id: 1, current_version: 1, verified: true, enabled: true };
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
    for (const requiresApproval of [false, true]) {
      await page.getByRole("button", { name: "新建 Tool", exact: true }).click();
      assert.equal(await toggle.getAttribute("aria-checked"), "false", "新工具默认不需要审批");
      await drawer.getByText(/保存后下一轮对话生效；已开始的轮次保持原设置/).waitFor();
      await drawer.getByText("MCP", { exact: true }).click();
      assert.equal(await toggle.getAttribute("aria-checked"), "false");
      await page.getByLabel("显示名称", { exact: true }).fill(requiresApproval ? "confirmed_lookup" : "direct_lookup");
      await page.getByLabel("用途", { exact: true }).fill("读取公开资料");
      await drawer.locator(".ant-select").filter({ has: page.getByRole("combobox", { name: "Tool MCP 连接", exact: true }) }).click();
      await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").filter({ hasText: "共享文档服务 · streamable_http · v1" }).click();
      await page.getByRole("button", { name: "发现工具", exact: true }).click();
      await drawer.locator(".ant-select").filter({ has: page.getByRole("combobox", { name: "MCP 远端函数名", exact: true }) }).getByText("lookup", { exact: true }).waitFor();
      if (requiresApproval) await toggle.click();
      await toggle.scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: screenshotPath(`web-tool-approval-${requiresApproval}-${width}.png`), animations: "disabled" });
      await save();
      assert.equal(saved.at(-1).requires_approval, requiresApproval, "保存时明确传递布尔值，包括 false");
      assert.equal(saved.at(-1).mcp_connection_id, 1);
      assert.equal(saved.at(-1).mcp_url, undefined);
    }
    const edit = async index => {
      const buttons = width < 768 ? page.locator(".resource-mobile-list").getByRole("button", { name: "编辑", exact: true }) : page.getByRole("button", { name: "编辑", exact: true });
      await buttons.nth(index).click();
      await toggle.waitFor();
    };
    await edit(1);
    assert.equal(await toggle.getAttribute("aria-checked"), "true", "编辑已启用审批的工具时保留设置");
    await page.getByLabel("用途", { exact: true }).fill("更新用途并保留审批");
    await save();
    assert.equal(saved.at(-1).requires_approval, true);
    await edit(1);
    await toggle.click();
    await save();
    assert.equal(saved.at(-1).requires_approval, false);
    await edit(1);
    assert.equal(await toggle.getAttribute("aria-checked"), "false", "重新编辑时恢复已保存的关闭设置");
    assert.deepEqual(errors, []);
    assert.deepEqual(unexpected, []);
    await context.close();
    console.log(`${width}px：默认免审批、显式开启、编辑保留与关闭及保存请求通过`);
  }
} finally { await browser.close(); }
