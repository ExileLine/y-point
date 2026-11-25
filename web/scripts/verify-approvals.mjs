import { agUiData, agUiPost, agUiFrames } from './ag-ui-fixtures.mjs';
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { screenshotPath } from "./screenshot-paths.mjs";

const baseURL = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });

async function fixture(page, options = {}) {
  const now = new Date().toISOString();
  let run = { id: "run-approval", session_id: "session-a", agent_id: 11, state: options.live ? "running" : "waiting_for_approval", failure_code: null, create_time: now, update_time: now };
  let approval = { id: "approval-1", run_id: run.id, state: options.expired ? "expired" : "pending", actions: [{ name: "user_tool_22_mcp_docs_query", arguments_hash: "a".repeat(64), action_id: "action-one", preview: { order_id: "order-7", quantity: 2 }, resource_name: null }], expires_at: new Date(Date.now() + 3600000).toISOString(), decided_at: null };
  if (options.mixed) approval.actions.push({ name: "delete_orders", arguments_hash: "b".repeat(64), action_id: "action-two", preview: { order_id: "order-8" } });
  let policy = { mode: "ask", version: 1, tools: [], allow_all_available: true, force_approval: false };
  if (options.scopes) approval.allowed_scopes = options.scopes;
  const calls = [];
  let releaseDecision;
  const decisionGate = options.hold ? new Promise(resolve => { releaseDecision = resolve; }) : Promise.resolve();
  let loadFailures = options.loadFailure ? Infinity : 0;
  await page.addInitScript(() => {
    sessionStorage.setItem("yp.access_token", "fixture-access");
    sessionStorage.setItem("yp.refresh_token", "fixture-refresh");
    window.approvalStreams = [];
    window.EventSource = class extends EventTarget {
      constructor() { super(); this.closed = false; window.approvalStreams.push(this); setTimeout(() => this.onopen?.(new Event("open")), 0); }
      close() { this.closed = true; }
    };
  });
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const body = route.request().postDataJSON();
    calls.push({ path, method, body });
    let data;
    let status = 200;
    const session = { id: "session-a", agent_id: 11, thread_id: "thread-a", title: "MCP 审批测试", archived: false, version: 1, create_time: now, update_time: now };
    if (path === "/api/v1/auth/me") data = { id: 1, username: "tester", nickname: "测试用户", role: "user" };
    else if (path === "/api/v1/agents/runtime-limits") data = { upload_max_bytes: 33554432, context_max_bytes: 16384, max_files: 5, recursion_limit: 50, default_model_timeout_seconds: 120, max_model_timeout_seconds: 600, default_tool_timeout_seconds: 120, max_tool_timeout_seconds: 600, default_max_output_tokens: 4096, max_output_tokens: 65536 };
    else if (path === "/api/v1/agents/11") data = { id: 11, name: "MCP 助手", enabled: true, default_model_id: 1 };
    else if (path === "/api/v1/agents/11/sessions") data = { items: [session], total: 1, page: 1, page_size: 20 };
    else if (path === "/api/v1/agents/11/sessions/session-a") data = session;
    else if (path.endsWith("/approval-policy")) {
      if (method === "DELETE") {
        assert.equal(new URL(route.request().url()).searchParams.get("expected_version"), String(policy.version));
        policy = { ...policy, mode: "ask", version: policy.version + 1, tools: [] };
      }
      data = policy;
    }
    else if (path.endsWith("/models")) data = [{ id: 1, display_name: "测试模型", provider_name: "模拟连接", verified_capabilities: ["text", "tools"], context_window: 65536, selectable: true, reason: null, notice: null }];
    else if (path.endsWith("/calls") || path.endsWith("/knowledge-retrievals")) data = [];
    else if (path.endsWith("/metrics")) data = { run_count: 1, active_run_count: 1, elapsed_ms: 0, total_tokens: 0, missing_duration_runs: 0, calculated_at: now };
    else if (path.includes("/ag-ui/threads/")) data = [{ id: 1, run_id: run.id, role: "user", content: "请查阅 FastAPI 文档", attachments: [], artifacts: [], create_time: now }];
    else if (path.endsWith("/runs")) data = { items: [run], total: 1, page: 1, page_size: 20 };
    else if (path.endsWith("/snapshot")) data = { run, events: [], next_after: 0, latest_sequence: 0, has_more: false };
    else if (path.endsWith("/stream-ticket")) data = { expires_in: 300 };
    else if (path.endsWith("/approval") && method === "GET") {
      if (loadFailures > 0) { loadFailures--; status = 503; }
      else data = approval;
    } else if (path.endsWith("/ag-ui") && method === "POST") {
      await decisionGate;
      if (options.conflict) {
        status = 409;
        approval = { ...approval, state: "cancelled" };
        run = { ...run, state: "cancelled" };
      } else {
        assert.equal(body.resume[0].interruptId, approval.id);
        const decision = body.resume[0].payload.decision;
        if (body.resume[0].payload.scope && body.resume[0].payload.scope !== "once") {
          policy = { ...policy, mode: body.resume[0].payload.scope === "all" ? "allow_all" : "allow_tools", version: policy.version + 1,
            tools: [{ tool_id: 22, tool_version: 1, name: "浏览器点击" }] };
        }
        approval = { ...approval, state: decision === "approve" ? "approved" : ["reject_continue", "resolve"].includes(decision) ? "resolved" : "rejected", decision, decided_at: now };
        if (["reject", "stop"].includes(decision)) run = { ...run, state: "cancelled", failure_code: "approval_rejected" };
        return agUiPost(route, run);
      }
    } else if (path.endsWith("/cancel") && method === "POST") {
      approval = { ...approval, state: "cancelled" };
      run = { ...run, state: "cancelled" };
      data = run;
    } else if (path === "/api/v1/runs/run-approval") data = run;
    else { status = 404; }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(status === 200 ? { code: 200, message: "操作成功", data: agUiData(path, data) } : { code: status, message: status === 409 ? "审批请求已变化，请刷新" : "审批加载失败" }) });
  });
  await page.goto(`${baseURL}/agents/11/sessions/session-a`);
  return {
    calls,
    releaseDecision,
    recoverLoad: () => { loadFailures = 0; },
    async transition(state, nextApproval = false) {
      run = { ...run, state };
      if (nextApproval) approval = { ...approval, id: "approval-2", state: "pending", decided_at: null };
      await page.waitForFunction(() => window.approvalStreams.some(stream => !stream.closed));
      await page.evaluate(state => {
        window.approvalSequence = (window.approvalSequence || 0) + 1;
        for (const stream of window.approvalStreams.filter(item => !item.closed)) {
          const event = state === "running"
            ? { type: "RUN_STARTED", runId: "next-attempt", threadId: "session-a", protocolVersion: "1.0" }
            : { type: "ACTIVITY_SNAPSHOT", messageId: "run-state", activityType: "y-point.run", content: { version: 1, state } };
          stream.onmessage?.(new MessageEvent("message", { lastEventId: `run-approval:${window.approvalSequence}`, data: JSON.stringify(event) }));
        }
      }, state);
    },
  };
}

try {
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const state = await fixture(page, { hold: true });
    const approve = page.getByRole("button", { name: "同意执行", exact: true });
    await page.getByText("等待审批", { exact: true }).first().waitFor();
    await approve.waitFor();
    await page.screenshot({ path: screenshotPath(`web-approval-pending-${width}.png`), animations: "disabled" });
    assert.equal(await approve.count(), 1, "等待审批时必须提供同意/拒绝入口");
    await approve.click();
    await page.waitForFunction(() => document.querySelector('button[aria-label="同意执行"]')?.disabled === true);
    assert.equal(await page.getByRole("button", { name: "停止整个任务", exact: true }).isDisabled(), true);
    state.releaseDecision();
    await page.getByText("已同意，等待执行器继续运行", { exact: true }).waitFor();
    assert.equal(state.calls.filter(call => call.path.endsWith("/ag-ui")).length, 1);
    assert.deepEqual(state.calls.find(call => call.path.endsWith("/ag-ui")).body.resume[0], { interruptId: "approval-1", status: "resolved", payload: { decision: "approve" } });
    await state.transition("running");
    await page.getByRole("region", { name: "工具审批" }).waitFor({ state: "hidden" });
    await state.transition("waiting_for_approval", true);
    await approve.waitFor();
    await page.getByRole("button", { name: "停止整个任务", exact: true }).click();
    await page.getByText("已取消", { exact: true }).first().waitFor();
    assert.deepEqual(state.calls.filter(call => call.path.endsWith("/ag-ui")).at(-1).body.resume[0], { interruptId: "approval-2", status: "resolved", payload: { decision: "stop" } });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: screenshotPath(`web-approval-${width}.png`), animations: "disabled" });
    await page.close();
    console.log(`${width}: approval restore, submit lock, same-run next approval and rejection passed`);
  }
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const state = await fixture(page);
    await page.getByText("操作预览：order_id: order-7 · quantity: 2", { exact: true }).waitFor();
    await page.getByRole("button", { name: "拒绝本次并继续", exact: true }).click();
    await page.getByText("已记录逐项决定，等待执行器继续整理结果", { exact: true }).waitFor();
    assert.equal(state.calls.filter(call => call.path.endsWith("/ag-ui")).at(-1).body.resume[0].payload.decision, "reject_continue");
    await state.transition("running");
    await page.getByRole("region", { name: "工具审批" }).waitFor({ state: "hidden" });
    await page.close();
    const mixed = await browser.newPage({ viewport: { width, height: 900 } });
    const batch = await fixture(mixed, { mixed: true });
    assert.equal(await mixed.getByRole("button", { name: "提交逐项决定", exact: true }).isDisabled(), true);
    await mixed.getByRole("group", { name: "操作 1 的决定", exact: true }).getByText("批准此项", { exact: true }).click();
    assert.equal(await mixed.getByRole("button", { name: "提交逐项决定", exact: true }).isDisabled(), true);
    await mixed.getByRole("group", { name: "操作 2 的决定", exact: true }).getByText("拒绝此项", { exact: true }).click();
    await mixed.getByRole("button", { name: "停止整个任务", exact: true }).scrollIntoViewIfNeeded();
    await mixed.screenshot({ path: screenshotPath(`web-approval-mixed-${width}.png`), animations: "disabled" });
    assert.equal(await mixed.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await mixed.getByRole("button", { name: "提交逐项决定", exact: true }).click();
    await mixed.getByText("已记录逐项决定，等待执行器继续整理结果", { exact: true }).waitFor();
    assert.deepEqual(batch.calls.filter(call => call.path.endsWith("/ag-ui")).at(-1).body.resume[0].payload, {
      decision: "resolve", decisions: [{ action_id: "action-one", decision: "approve" }, { action_id: "action-two", decision: "reject" }],
    });
    await mixed.close();
    console.log(`${width}: reject-and-continue, sanitized preview and mixed action decisions passed`);
  }
  {
    const page = await browser.newPage();
    const state = await fixture(page, { live: true });
    assert.equal(await page.getByRole("button", { name: "同意执行", exact: true }).count(), 0);
    await state.transition("waiting_for_approval");
    await page.getByRole("button", { name: "同意执行", exact: true }).waitFor();
    assert.equal(state.calls.filter(call => call.path.endsWith("/ag-ui")).length, 0);
    await page.close();
    console.log("live state: SSE reveals approval controls without reload or automatic approval");
  }
  for (const width of [1280, 375]) {
    for (const scope of ["tool", "all"]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      const state = await fixture(page, { scopes: ["once", "tool", "all"] });
      await page.getByRole("button", { name: scope === "tool" ? "本对话允许此工具" : "本对话全部允许", exact: true }).click();
      await page.getByText("已同意，等待执行器继续运行", { exact: true }).waitFor();
      assert.equal(state.calls.filter(call => call.path.endsWith("/ag-ui")).at(-1).body.resume[0].payload.scope, scope);
      await page.locator(".session-approval-policy summary").click();
      await page.getByRole("button", { name: "刷新放行策略", exact: true }).click();
      await page.getByText("浏览器点击 · v1", { exact: true }).waitFor();
      await page.getByRole("button", { name: "撤销全部放行", exact: true }).click();
      await page.getByRole("button", { name: "确认撤销全部放行", exact: true }).click();
      await page.getByText("本对话 MCP 放行：未设置", { exact: true }).waitFor();
      assert.equal(state.calls.filter(call => call.path.endsWith("/approval-policy") && call.method === "DELETE").length, 1);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: screenshotPath(`web-approval-scope-${scope}-${width}.png`), animations: "disabled" });
      await page.close();
    }
    const forced = await browser.newPage({ viewport: { width, height: 900 } });
    await fixture(forced, { scopes: ["once"] });
    await forced.getByRole("button", { name: "同意执行", exact: true }).waitFor();
    assert.equal(await forced.getByRole("button", { name: "本对话允许此工具", exact: true }).count(), 0);
    assert.equal(await forced.getByRole("button", { name: "本对话全部允许", exact: true }).count(), 0);
    await forced.close();
    console.log(`${width}: session tool/all scopes, policy refresh, versioned revoke and constrained controls passed`);
  }
  for (const options of [{ expired: true }, { conflict: true }, { loadFailure: true }]) {
    const page = await browser.newPage();
    const state = await fixture(page, options);
    if (options.expired) {
      await page.getByText("审批已过期，请重新发起请求", { exact: true }).waitFor();
      assert.equal(await page.getByRole("button", { name: "同意执行", exact: true }).count(), 0);
    } else if (options.conflict) {
      await page.getByRole("button", { name: "同意执行", exact: true }).click();
      await page.getByText("已取消", { exact: true }).first().waitFor();
      assert.equal(state.calls.filter(call => call.path.endsWith("/ag-ui")).length, 1);
    } else {
      await page.getByText("审批加载失败", { exact: true }).waitFor();
      assert.equal(await page.getByRole("button", { name: "同意执行", exact: true }).count(), 0);
      state.recoverLoad();
      await page.getByRole("button", { name: "重新加载审批", exact: true }).click();
      await page.getByRole("button", { name: "同意执行", exact: true }).waitFor();
    }
    await page.close();
  }
  {
    const page = await browser.newPage();
    await fixture(page);
    await page.getByRole("button", { name: "同意执行", exact: true }).waitFor();
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByText("已取消", { exact: true }).first().waitFor();
    await page.getByRole("textbox", { name: "消息内容" }).fill("取消后可以继续发送");
    assert.equal(await page.getByRole("button", { name: "发送", exact: true }).isEnabled(), true);
    assert.equal(await page.getByRole("region", { name: "工具审批" }).count(), 0);
    await page.close();
  }
  console.log("expired approval, concurrent conflict and loading failure recovery passed; mocked API, no real approvals submitted");
} finally {
  await browser.close();
}
