import assert from "node:assert/strict";
import { chromium } from "playwright";

const baseURL = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
const envelope = (data) => ({ code: 200, message: "操作成功", data });
const now = "2026-10-30T10:00:00";

function fixture(page, shared = false, options = {}) {
  const calls = [];
  const agent = { id: 11, name: "需求分析助手", description: "需求分析", welcome_message: "请描述需求", enabled: true, default_model_id: 1, system_prompt: "仅所有者可见" };
  let sessions = [{ id: "session-a", agent_id: 11, thread_id: "thread-a", title: "需求讨论", archived: false, version: 1, create_time: now, update_time: now }];
  if (options.secondSession) sessions.push({ id: "session-b", agent_id: 11, thread_id: "thread-b", title: "第二个会话", archived: false, version: 1, create_time: now, update_time: now });
  if (options.many) sessions = [...sessions, ...Array.from({ length: 21 }, (_, index) => ({ id: `session-extra-${index}`, agent_id: 11, thread_id: `thread-extra-${index}`, title: `历史会话 ${index + 1}`, archived: false, version: 1, create_time: now, update_time: now }))];
  let messages = [{ id: 1, run_id: "run-old", role: "user", content: "已有问题", create_time: now, attachments: [], artifacts: [] }, { id: 2, run_id: "run-old", role: "assistant", content: "已有回答", create_time: now, attachments: [], artifacts: [] }];
  let run = null;
  let runReads = 0;
  let submitAttempts = 0;
  let streamReads = 0;
  page.addInitScript(() => {
    sessionStorage.setItem("yp.access_token", "fixture-access");
    sessionStorage.setItem("yp.refresh_token", "fixture-refresh");
  });
  page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    const body = route.request().postData() ? JSON.parse(route.request().postData()) : null;
    calls.push({ path, method, body, search: url.search });
    let data = null;
    let status = 200;
    if (/\/sessions\/[^/]+\/metrics$/.test(path)) data = { run_count: 0, active_run_count: 0, elapsed_ms: 0, total_tokens: 0, missing_duration_runs: 0, calculated_at: new Date().toISOString() };
    else if (/\/sessions\/[^/]+\/models$/.test(path)) data = [];
    else if (path.startsWith("/api/v1/runs/") && path.endsWith("/calls")) data = [];
    else if (path === "/api/v1/auth/me") data = { id: 1, username: "tester", nickname: "测试用户", role: "user" };
    else if (path === "/api/v1/agents/11" && method === "GET") {
      if (shared) status = 404;
      else data = agent;
    } else if (path === "/api/v1/agents/shared/11") data = { id: 11, name: agent.name, description: agent.description, welcome_message: agent.welcome_message, owner_id: 2, enabled: true };
    else if (path === "/api/v1/agents/11/sessions" && method === "GET") {
      const archived = url.searchParams.get("archived") === "true";
      const search = url.searchParams.get("search") || "";
      const visible = sessions.filter((item) => item.archived === archived && item.title.includes(search));
      const pageNumber = Number(url.searchParams.get("page") || 1);
      data = { items: visible.slice((pageNumber - 1) * 20, pageNumber * 20), total: visible.length, page: pageNumber, page_size: 20 };
    } else if (path === "/api/v1/agents/11/sessions" && method === "POST") {
      data = { id: `session-${sessions.length + 1}`, agent_id: 11, thread_id: `thread-${sessions.length + 1}`, title: body.title, archived: false, version: 1, create_time: now, update_time: now };
      sessions = [data, ...sessions];
      status = 201;
    } else if (/^\/api\/v1\/agents\/11\/sessions\/[^/]+$/.test(path)) {
      const id = path.split("/").at(-1);
      const item = sessions.find((entry) => entry.id === id);
      if (!item) status = 404;
      else if (method === "GET") data = item;
      else if (method === "PUT") {
        if (body.expected_version !== item.version) status = 409;
        else { Object.assign(item, body, { version: item.version + 1 }); delete item.expected_version; data = item; }
      } else if (method === "DELETE") {
        sessions = sessions.filter((entry) => entry.id !== id);
        data = {};
      }
    } else if (path.endsWith("/messages")) {
      data = messages.filter((item) => item.id > Number(url.searchParams.get("after") || 0)).slice(0, Number(url.searchParams.get("limit") || 100));
    } else if (path.endsWith("/runs") && method === "GET") data = { items: run ? [run] : [], total: run ? 1 : 0, page: 1, page_size: 20 };
    else if (path.endsWith("/runs") && method === "POST") {
      submitAttempts += 1;
      if (options.failFirstSubmit && submitAttempts === 1) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: 503, message: "暂时不可用" }) });
      if (options.modelSwitchConflict) return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ code: 10005, message: "会话模型已变化，请刷新后重试", data: { reason: "session_model_changed" } }) });
      run = { id: "run-new", agent_id: 11, session_id: path.split("/").at(-2), state: "queued", failure_code: null, create_time: now, update_time: now };
      const item = sessions.find((entry) => entry.id === run.session_id);
      if (item) item.version += 1;
      messages = [...messages, { id: 3, run_id: run.id, role: "user", content: body.input_text, create_time: now, attachments: [], artifacts: [] }];
      data = run;
      status = 201;
    } else if (path === "/api/v1/runs/run-new/stream-ticket" && method === "POST") {
      data = { expires_in: 300 };
    } else if (path === "/api/v1/runs/run-new/snapshot" && method === "GET") {
      const after = Number(url.searchParams.get("after") || 0);
      const events = [
        { sequence: 1, event_type: "state", state: "queued", content: null },
        ...(options.reconnect && streamReads > 0 ? [{ sequence: 2, event_type: "text_delta", state: "running", content: "正在生成的内容" }, { sequence: 3, event_type: "text_delta", state: "running", content: "，续接成功" }] : []),
      ].filter((event) => event.sequence > after);
      data = { run, events, latest_sequence: options.reconnect && streamReads > 0 ? 3 : 1, next_after: events.at(-1)?.sequence || after, has_more: false };
    } else if (path === "/api/v1/runs/run-new/stream" && method === "GET") {
      streamReads += 1;
      if (options.reconnect && streamReads > 1) return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
      if (options.pollCompletesBeforeStream) await new Promise((resolve) => setTimeout(resolve, 1400));
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: `id: run-new:2\nevent: text_delta\ndata: ${JSON.stringify({ sequence: 2, event_type: "text_delta", state: "running", content: "正在生成的内容" })}\n\n` });
    } else if (path === "/api/v1/runs/run-new" && method === "GET") {
      runReads += 1;
      if ((runReads >= 2 || options.pollCompletesBeforeStream) && !options.holdCompletion) {
        run = { ...run, state: "completed" };
        if (!options.pollCompletesBeforeStream && !messages.some((item) => item.id === 4)) messages = [...messages, { id: 4, run_id: run.id, role: "assistant", content: "这是最终回答", create_time: now, attachments: [], artifacts: [] }];
      }
      data = run;
    } else if (path === "/api/v1/runs/run-new/cancel") {
      run = { ...run, state: "cancel_requested" };
      data = run;
    }
    await route.fulfill({ status: data === null ? status === 200 ? 404 : status : status, contentType: "application/json", body: JSON.stringify(data === null ? { code: status, message: "不存在" } : status === 201 ? { code: 201, message: "创建成功", data } : envelope(data)) });
  });
  return { calls, getSessions: () => sessions };
}

try {
  const racePage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const raceFixture = fixture(racePage, false, { pollCompletesBeforeStream: true });
  await racePage.goto(`${baseURL}/agents/11/sessions/session-a`, { waitUntil: "networkidle" });
  await racePage.getByRole("textbox", { name: "消息内容" }).fill("流式竞态");
  await racePage.getByRole("button", { name: "发送" }).click();
  await racePage.getByText("正在生成的内容").waitFor({ timeout: 5000 });
  assert.ok(raceFixture.calls.some((call) => call.path === "/api/v1/runs/run-new/stream"));
  assert.equal(raceFixture.calls.filter((call) => call.path === "/api/v1/runs/run-new" && call.method === "GET").length, 0);
  console.log("race: SSE preview survives early status completion");
  await racePage.close();

  for (const [name, width, height] of [["desktop", 1280, 800], ["mobile", 375, 812]]) {
    const streamingPage = await browser.newPage({ viewport: { width, height } });
    const streamingFixture = fixture(streamingPage, false, { holdCompletion: true });
    await streamingPage.goto(`${baseURL}/agents/11/sessions/session-a`, { waitUntil: "networkidle" });
    await streamingPage.getByRole("textbox", { name: "消息内容" }).fill("请逐步回答");
    await streamingPage.getByRole("button", { name: "发送" }).click();
    await streamingPage.getByText("正在生成的内容").waitFor({ timeout: 5000 });
    assert.ok(streamingFixture.calls.some((call) => call.path === "/api/v1/runs/run-new/stream"));
    assert.equal(await streamingPage.evaluate(() => document.documentElement.scrollWidth), width);
    await streamingPage.screenshot({ path: `/tmp/y-point-streaming-${name}.png`, fullPage: true });
    console.log(`${name}: partial answer visible before run completion`);
    await streamingPage.close();
  }

  const reconnectPage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const reconnectFixture = fixture(reconnectPage, false, { holdCompletion: true, reconnect: true });
  await reconnectPage.goto(`${baseURL}/agents/11/sessions/session-a`, { waitUntil: "networkidle" });
  await reconnectPage.getByRole("textbox", { name: "消息内容" }).fill("断线后继续");
  await reconnectPage.getByRole("button", { name: "发送" }).click();
  await reconnectPage.getByText("正在生成的内容，续接成功").waitFor({ timeout: 7000 });
  assert.equal(await reconnectPage.locator(".sessions-messages").last().getByText("正在生成的内容，续接成功").count(), 1);
  assert.ok(reconnectFixture.calls.some((call) => call.path === "/api/v1/runs/run-new/snapshot" && call.search.includes("after=2")));
  console.log("stream: snapshot reconnect resumes without duplicate text");
  await reconnectPage.close();

  for (const [name, width, height] of [["desktop", 1280, 800], ["mobile", 375, 812]]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const { calls } = fixture(page);
    await page.goto(`${baseURL}/agents/11/sessions`, { waitUntil: "networkidle" });
    await page.locator(".sessions-list-open", { hasText: "需求讨论" }).waitFor();
    await page.screenshot({ path: `../docs/web-sessions-list-${name}.png`, fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    await page.locator(".sessions-list-open", { hasText: "需求讨论" }).click();
    await page.getByText("已有回答").waitFor();
    await page.screenshot({ path: `../docs/web-sessions-chat-${name}.png`, fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    await page.getByRole("textbox", { name: "消息内容" }).fill("新的问题");
    await page.getByRole("button", { name: "发送" }).click();
    await page.getByText("这是最终回答").waitFor({ timeout: 10000 });
    const submitted = calls.find((call) => call.method === "POST" && call.path.endsWith("/session-a/runs"));
    assert.equal(submitted.body.input_text, "新的问题");
    assert.equal(submitted.body.artifact_ids.length, 0);
    assert.ok(submitted.body.request_key);
    assert.equal(calls.filter((call) => call.method === "POST" && call.path.endsWith("/session-a/runs")).length, 1);
    if (name === "desktop") {
      await page.getByRole("button", { name: "当前会话操作" }).click();
      await page.getByText("重命名", { exact: true }).click();
      await page.getByRole("textbox", { name: "新标题" }).fill("需求评审");
      await page.locator(".ant-modal:visible .ant-modal-footer .ant-btn-primary").click();
      await page.getByText("需求评审").first().waitFor();
      assert.equal(calls.find((call) => call.method === "PUT" && call.body?.title === "需求评审")?.body.expected_version, 2);
      await page.getByRole("button", { name: "当前会话操作" }).click();
      await page.getByText("归档", { exact: true }).click();
      await page.locator(".sessions-current-title .ant-tag").getByText("已归档").waitFor();
      assert.equal(await page.getByRole("textbox", { name: "消息内容" }).isDisabled(), true);
      await page.getByRole("button", { name: "当前会话操作" }).click();
      await page.getByText("恢复", { exact: true }).click();
      await page.getByRole("textbox", { name: "消息内容" }).waitFor();
      await page.getByRole("button", { name: "当前会话操作" }).click();
      await page.getByText("删除", { exact: true }).click();
      await page.locator(".ant-modal:visible .ant-modal-footer .ant-btn-primary").click();
      await page.getByText("选择或新建会话").waitFor();
    } else {
      await page.getByRole("button", { name: "返回会话列表" }).click();
      await page.getByRole("button", { name: "新建会话" }).click();
      await page.getByRole("textbox", { name: "会话标题" }).fill("第二个会话");
      await page.locator(".ant-modal:visible .ant-modal-footer .ant-btn-primary").click();
      await page.locator(".sessions-current-title").getByText("第二个会话").waitFor();
      assert.equal(calls.find((call) => call.method === "POST" && call.path === "/api/v1/agents/11/sessions")?.body.title, "第二个会话");
    }
    assert.deepEqual(errors, []);
    console.log(`${name}: session list, messages, send and management passed`);
    await page.close();
  }

  const page = await browser.newPage({ viewport: { width: 375, height: 812 } });
  fixture(page, true);
  await page.goto(`${baseURL}/agents/11/sessions`, { waitUntil: "networkidle" });
  await page.locator(".sessions-list-open", { hasText: "需求讨论" }).waitFor();
  assert.equal(await page.getByText("仅所有者可见").count(), 0);
  console.log("shared: recipient session access without system prompt passed");
  await page.close();

  const retryPage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const retryFixture = fixture(retryPage, false, { failFirstSubmit: true });
  await retryPage.goto(`${baseURL}/agents/11/sessions/session-a`, { waitUntil: "networkidle" });
  await retryPage.getByRole("textbox", { name: "消息内容" }).fill("保留提交键");
  await retryPage.getByRole("button", { name: "发送" }).click();
  await retryPage.getByRole("button", { name: "重试提交" }).click();
  await retryPage.getByText("这是最终回答").waitFor({ timeout: 10000 });
  const attempts = retryFixture.calls.filter((call) => call.method === "POST" && call.path.endsWith("/session-a/runs"));
  assert.equal(attempts.length, 2);
  assert.deepEqual(attempts[0].body, attempts[1].body);
  console.log("retry: identical request key and payload passed");
  await retryPage.close();

  const conflictPage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  fixture(conflictPage, false, { modelSwitchConflict: true });
  await conflictPage.goto(`${baseURL}/agents/11/sessions/session-a`, { waitUntil: "networkidle" });
  await conflictPage.getByRole("textbox", { name: "消息内容" }).fill("使用新模型继续");
  await conflictPage.getByRole("button", { name: "发送" }).click();
  await conflictPage.getByText("会话模型已变化，请刷新后重试", { exact: true }).waitFor();
  assert.equal(await conflictPage.getByRole("dialog").count(), 0);
  assert.equal(await conflictPage.getByRole("button", { name: "重试提交" }).count(), 0);
  console.log("model selection conflict: refresh and preserve draft passed");
  await conflictPage.close();

  const switchPage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  fixture(switchPage, false, { secondSession: true });
  let sessionReads = 0;
  let releaseRefresh;
  let refreshStarted;
  const refreshGate = new Promise((resolve) => { releaseRefresh = resolve; });
  const refreshSeen = new Promise((resolve) => { refreshStarted = resolve; });
  await switchPage.route("**/api/v1/agents/11/sessions/session-a", async (route) => {
    if (route.request().method() === "GET" && ++sessionReads === 2) {
      refreshStarted();
      await refreshGate;
    }
    await route.fallback();
  });
  await switchPage.goto(`${baseURL}/agents/11/sessions/session-a`, { waitUntil: "networkidle" });
  await switchPage.getByRole("button", { name: "刷新会话" }).click();
  await refreshSeen;
  await switchPage.locator(".sessions-list-open", { hasText: "第二个会话" }).click();
  await switchPage.locator(".sessions-current-title", { hasText: "第二个会话" }).waitFor();
  const delayedResponse = switchPage.waitForResponse((response) => response.url().endsWith("/agents/11/sessions/session-a"));
  releaseRefresh();
  await delayedResponse;
  await switchPage.waitForTimeout(50);
  assert.equal((await switchPage.locator(".sessions-current-title").innerText()).trim(), "第二个会话");
  console.log("switch: stale session refresh ignored");
  await switchPage.close();

  const pagingPage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const pagingFixture = fixture(pagingPage, false, { many: true });
  await pagingPage.goto(`${baseURL}/agents/11/sessions`, { waitUntil: "networkidle" });
  await pagingPage.locator(".ant-pagination-item-2").click();
  await pagingPage.locator(".sessions-list-open", { hasText: "历史会话 21" }).waitFor();
  assert.ok(pagingFixture.calls.some((call) => call.path === "/api/v1/agents/11/sessions" && call.search.includes("page=2")));
  const searchResponse = pagingPage.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/v1/agents/11/sessions" && url.searchParams.get("search") === "需求";
  });
  await pagingPage.getByPlaceholder("搜索标题").fill("需求");
  await searchResponse;
  await pagingPage.locator(".sessions-list-open", { hasText: "需求讨论" }).waitFor();
  assert.ok(pagingFixture.calls.some((call) => call.path === "/api/v1/agents/11/sessions" && call.search.includes("search=%E9%9C%80%E6%B1%82")));
  console.log("directory: server pagination and search passed");
  await pagingPage.close();
} finally {
  await browser.close();
}
