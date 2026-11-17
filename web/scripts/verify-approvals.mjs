import { agUiData, agUiPost, agUiFrames } from './ag-ui-fixtures.mjs';
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { screenshotPath } from "./screenshot-paths.mjs";

const baseURL = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });

async function fixture(page, options = {}) {
  const now = new Date().toISOString();
  let run = { id: "run-approval", session_id: "session-a", agent_id: 11, state: options.live ? "running" : "waiting_for_approval", failure_code: null, create_time: now, update_time: now };
  let approval = { id: "approval-1", run_id: run.id, state: options.expired ? "expired" : "pending", actions: [{ name: "user_tool_22_mcp_docs_query", arguments_hash: "a".repeat(64), resource_name: null }], expires_at: new Date(Date.now() + 3600000).toISOString(), decided_at: null };
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
        approval = { ...approval, state: decision === "approve" ? "approved" : "rejected", decided_at: now };
        if (decision === "reject") run = { ...run, state: "cancelled", failure_code: "approval_rejected" };
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
    assert.equal(await page.getByRole("button", { name: "拒绝执行", exact: true }).isDisabled(), true);
    state.releaseDecision();
    await page.getByText("已同意，等待执行器继续运行", { exact: true }).waitFor();
    assert.equal(state.calls.filter(call => call.path.endsWith("/ag-ui")).length, 1);
    assert.deepEqual(state.calls.find(call => call.path.endsWith("/ag-ui")).body.resume[0], { interruptId: "approval-1", status: "resolved", payload: { decision: "approve" } });
    await state.transition("running");
    await page.getByRole("region", { name: "工具审批" }).waitFor({ state: "hidden" });
    await state.transition("waiting_for_approval", true);
    await approve.waitFor();
    await page.getByRole("button", { name: "拒绝执行", exact: true }).click();
    await page.getByText("已取消", { exact: true }).first().waitFor();
    assert.deepEqual(state.calls.filter(call => call.path.endsWith("/ag-ui")).at(-1).body.resume[0], { interruptId: "approval-2", status: "resolved", payload: { decision: "reject" } });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: screenshotPath(`web-approval-${width}.png`), animations: "disabled" });
    await page.close();
    console.log(`${width}: approval restore, submit lock, same-run next approval and rejection passed`);
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
