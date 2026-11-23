// -*- coding: utf-8 -*-
// @Time    : 2026-11-23
// @Author  : Codex
// @File    : verify-tool-callability.mjs

import assert from "node:assert/strict";
import { chromium } from "playwright";
import { screenshotPath } from "./screenshot-paths.mjs";

const base = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
const direct = { status: "direct", reason: null, message: "可直接调用", action: null,
  policy: { mode: "writes", source: "connection", requires_approval: false, constraints: [], tool_policy_version: 1, connection_policy_version: 3 } };
const blocked = { status: "blocked", reason: "credentials_revoked", message: "连接凭据已轮换，此版本已撤销", action: "使用新凭据重新发布工具", policy: null };
try {
  for (const width of [1280, 375]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: "reduce" });
    await context.addInitScript(() => sessionStorage.setItem("yp.access_token", "fixture-token"));
    const page = await context.newPage(), errors = [], unexpected = [], toolSaves = [], agentSaves = [];
    page.on("pageerror", error => errors.push(error.message));
    let tool = { id: 1, owner_id: 1, name: "lookup", display_name: "工单查询", model_name: "user_tool_1_lookup", description: "查询工单",
      kind: "mcp", visibility: "private", enabled: true, current_version: 1, verified: true, input_schema: { type: "object", properties: {} },
      mcp_connection_id: 1, mcp_connection_version: 1, mcp_tool_name: "lookup", effect: "read", effect_source: "remote_read", approval_mode: "inherit", requires_approval: false,
      callability: direct, callability_checked_at: "2026-11-23T18:00:00+08:00" };
    let agent = { id: 11, name: "查询助手", description: null, welcome_message: null, default_model_id: 1, enabled: true, version: 1,
      system_prompt: "查询", temperature: "1.00", max_output_tokens: null, model_timeout_seconds: null, tool_timeout_seconds: 120,
      thinking_mode: "auto", knowledge_mode: "auto", max_model_calls: null, max_tool_calls: null, max_total_tokens: null, max_estimated_cost: null,
      knowledge_base_ids: [], skill_ids: [], tool_ids: [1], mcp_connections: [{ connection_id: 1, tools: [{ tool_id: 1, tool_version: 1 }] }] };
    let failTools = false, failConnections = false, failDetail = false, missingConnection = false, holdDetail = false;
    let releaseDetail, detailArrived;
    await page.route("**/api/v1/**", async route => {
      const req = route.request(), path = new URL(req.url()).pathname, method = req.method();
      const send = (data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ code: status, message: status < 400 ? "操作成功" : data, data: status < 400 ? data : null }) });
      if (path === "/api/v1/auth/me") return send({ id: 1, username: "owner", role: "admin" });
      if (path === "/api/v1/tools/catalog") return failTools ? send("工具目录暂时不可用", 503) : send({ builtin: [], user_tools: [tool] });
      if (path === "/api/v1/tools/1" && method === "PUT") { toolSaves.push(req.postDataJSON()); return send("发布版本已变化，请刷新后重试", 409); }
      if (path === "/api/v1/tools/1") {
        const snapshot = structuredClone(tool);
        if (holdDetail) { holdDetail = false; detailArrived?.(); await new Promise(resolve => { releaseDetail = resolve; }); }
        return failDetail ? send("详情暂时不可用", 503) : send(snapshot);
      }
      if (path === "/api/v1/mcp-connections/catalog") return failConnections ? send("连接目录暂时不可用", 503) : send(missingConnection ? [] : [{
        id: 1, name: "工单连接", owner_id: 1, version: 3, enabled: true, can_edit: true, share_version: null, allow_credentials: true,
        callability_checked_at: tool.callability_checked_at, tools: [{ tool_id: 1, display_name: tool.display_name, model_name: tool.model_name,
          tool_version: tool.current_version, mcp_connection_version: tool.mcp_connection_version, callability_checked_at: tool.callability_checked_at, callability: tool.callability }],
      }]);
      if (path === "/api/v1/mcp-connections") return send([{ id: 1, name: "工单连接", transport: "streamable_http", version: 3, enabled: true, args: [], url: "http://fixture.invalid/mcp", has_credentials: true, share_credentials: false, approval_mode: "writes" }]);
      if (path === "/api/v1/agents/runtime-limits") return send({ default_max_output_tokens: null, max_output_tokens: 1000000, default_model_timeout_seconds: null, max_model_timeout_seconds: 3600, default_tool_timeout_seconds: 120, max_tool_timeout_seconds: 3600 });
      if (path === "/api/v1/agents/11" && method === "PUT") {
        const body = req.postDataJSON(); agentSaves.push(body);
        if ((body.tool_ids ?? agent.tool_ids).length || (body.mcp_connections ?? agent.mcp_connections).length) return send("配置版本已变化，请刷新后重试", 409);
        agent = { ...agent, ...body, version: agent.version + 1 }; return send(agent);
      }
      if (path === "/api/v1/agents") return send({ items: [agent], total: 1, page: 1, page_size: 12 });
      if (path === "/api/v1/agents/11") return send(agent);
      if (path === "/api/v1/models/available") return send([{ id: 1, display_name: "测试模型", context_window: 8192 }]);
      if (path === "/api/v1/agents/shared" || path === "/api/v1/skills") return send([]);
      if (path === "/api/v1/knowledge-bases") return send({ items: [], total: 0, page: 1, page_size: 100 });
      unexpected.push(`${method} ${path}`); return send("未定义请求", 404);
    });
    await page.goto(`${base}/tools`);
    await page.getByText("工单查询", { exact: true }).waitFor();
    await page.getByText("可直接调用", { exact: true }).click();
    await page.getByRole("tooltip").filter({ hasText: "判断时间" }).waitFor();
    await page.getByText("可直接调用", { exact: true }).click();
    await page.getByRole("tooltip").waitFor({ state: "hidden" });
    failTools = true;
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await page.getByText("工具目录暂时不可用", { exact: true }).waitFor();
    assert.equal(await page.getByText("工单查询", { exact: true }).count(), 1, "刷新失败保留已有目录");
    failTools = false;
    await page.getByRole("button", { name: /^重\s*试$/ }).click();
    await page.getByText("工具目录暂时不可用", { exact: true }).waitFor({ state: "hidden" });
    failDetail = true;
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    const editor = page.getByRole("dialog", { name: "编辑 Tool", exact: true });
    await editor.getByText("工具详情加载失败：详情暂时不可用", { exact: true }).waitFor();
    failDetail = false;
    await editor.getByRole("button", { name: "重试详情", exact: true }).click();
    await editor.getByText("当前发布版本 v1：可直接调用", { exact: true }).waitFor();
    await editor.locator("#tool-name").fill("尚未保存的工具草稿");
    failDetail = true;
    await editor.getByRole("button", { name: "刷新发布状态", exact: true }).click();
    await editor.getByText("状态查询失败：详情暂时不可用", { exact: true }).waitFor();
    assert.equal(await editor.locator("#tool-name").inputValue(), "尚未保存的工具草稿");
    failDetail = false;
    tool = { ...tool, current_version: 2, callability: blocked, callability_checked_at: "2026-11-23T18:05:00+08:00" };
    await editor.getByRole("button", { name: "刷新发布状态", exact: true }).click();
    await editor.getByText("当前发布版本 v2：连接凭据已轮换，此版本已撤销", { exact: true }).waitFor();
    await editor.getByText("发布版本已变为 v2，当前草稿基于 v1", { exact: true }).waitFor();
    assert.equal(await editor.locator("#tool-name").inputValue(), "尚未保存的工具草稿");
    await editor.getByRole("button", { name: /^保\s*存$/ }).click();
    await editor.getByText("发布版本已变化，请刷新后重试", { exact: true }).waitFor();
    assert.equal(toolSaves[0].expected_version, 1, "状态刷新不得静默提高草稿的提交版本");
    assert.equal(toolSaves[0].display_name, "尚未保存的工具草稿");
    await editor.locator(".ant-drawer-body").evaluate(node => { node.scrollTop = 0; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: screenshotPath(`tool-callability-${width}.png`), animations: "disabled" });
    await editor.getByRole("button", { name: "Close", exact: true }).click();
    await editor.waitFor({ state: "hidden" });
    // 关闭详情后，新草稿不能被迟到响应覆盖或重新打开旧工具。
    holdDetail = true;
    const held = new Promise(resolve => { detailArrived = resolve; });
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await held;
    await editor.getByText("正在加载工具详情与状态…", { exact: true }).waitFor();
    await editor.getByRole("button", { name: "Close", exact: true }).click();
    await page.getByRole("button", { name: "新建 Tool", exact: true }).click();
    const fresh = page.getByRole("dialog", { name: "新建 Tool", exact: true });
    await fresh.locator("#tool-name").fill("新建草稿");
    const late = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/tools/1");
    releaseDetail(); await late;
    assert.equal(await fresh.locator("#tool-name").inputValue(), "新建草稿");
    assert.equal(await editor.count(), 0);
    await fresh.getByRole("button", { name: "Close", exact: true }).click();
    // Agent 初次查询失败与重试、已有数据刷新失败、固定旧版本与撤权修复。
    failTools = true; failConnections = true;
    await page.goto(`${base}/agents`);
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    const agentEditor = page.getByRole("dialog", { name: "编辑 Agent", exact: true });
    await agentEditor.getByText("工具目录加载失败：工具目录暂时不可用", { exact: true }).waitFor();
    await agentEditor.getByText("连接目录加载失败：连接目录暂时不可用", { exact: true }).waitFor();
    assert.equal(await agentEditor.getByText("连接授权不可用；请所有者授权或移除此连接。", { exact: true }).count(), 0);
    await agentEditor.getByLabel("名称", { exact: true }).fill("Agent 未保存草稿");
    failTools = false; failConnections = false;
    await agentEditor.getByRole("button", { name: "刷新工具状态", exact: true }).click();
    await agentEditor.getByRole("button", { name: "重试目录", exact: true }).click();
    await agentEditor.getByText("工单连接", { exact: true }).waitFor();
    await agentEditor.getByText(/已固定旧版；当前发布 v2/).waitFor();
    await agentEditor.getByText(/连接配置 v3 · 所有者/).waitFor();
    failTools = true; failConnections = true;
    await agentEditor.getByRole("button", { name: "刷新工具状态", exact: true }).click();
    await agentEditor.getByRole("button", { name: "刷新连接状态", exact: true }).click();
    await agentEditor.getByText("工具目录加载失败：工具目录暂时不可用", { exact: true }).waitFor();
    await agentEditor.getByText("连接目录加载失败：连接目录暂时不可用", { exact: true }).waitFor();
    await agentEditor.getByText("工单连接", { exact: true }).waitFor();
    assert.equal(await agentEditor.getByLabel("名称", { exact: true }).inputValue(), "Agent 未保存草稿");
    await agentEditor.getByRole("button", { name: /^保\s*存$/ }).click();
    await agentEditor.getByText(/配置版本已变化，请刷新后重试/).waitFor();
    assert.equal("tool_ids" in agentSaves[0], false, "目录失败不得误提交空工具列表");
    assert.equal("mcp_connections" in agentSaves[0], false, "保留原固定版本，不静默提交升级");
    await agentEditor.getByText("1 个已选工具", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await agentEditor.locator(".ant-drawer-body").evaluate(node => { node.scrollTop = 0; });
    await agentEditor.getByRole("button", { name: "刷新工具状态", exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath(`agent-callability-${width}.png`), animations: "disabled" });
    await agentEditor.getByText("查看已选工具状态", { exact: true }).click();
    await agentEditor.getByText(/已固定旧版，以上是当前发布版本的预检/).waitFor();
    await agentEditor.locator(".resource-surface").screenshot({ path: screenshotPath(`agent-callability-group-${width}.png`), animations: "disabled" });
    failConnections = false; missingConnection = true;
    await agentEditor.getByRole("button", { name: "重试目录", exact: true }).click();
    await agentEditor.getByText("连接授权不可用；请所有者授权或移除此连接。", { exact: true }).waitFor();
    await agentEditor.getByRole("button", { name: "移除此连接", exact: true }).click();
    const directSelect = agentEditor.locator(".ant-form-item").filter({ has: page.locator("#tool_ids") });
    await directSelect.locator(".ant-select-selection-item-remove").click();
    await agentEditor.getByRole("button", { name: /^保\s*存$/ }).click();
    await agentEditor.waitFor({ state: "hidden" });
    assert.deepEqual(agentSaves[1].tool_ids, []); assert.deepEqual(agentSaves[1].mcp_connections, []);
    assert.equal(agentSaves[1].name, "Agent 未保存草稿");
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    await context.close();
    console.log(`${width}px: 目录/详情失败重试、缓存与草稿保留、状态变化、版本冲突、迟到响应隔离、Agent 固定旧版及撤权修复通过`);
  }
} finally { await browser.close(); }
