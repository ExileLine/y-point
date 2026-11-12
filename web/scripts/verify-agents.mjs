import assert from "node:assert/strict";
import { chromium } from "playwright";

const screenshotDir = process.env.AGENT_SCREENSHOT_DIR || "../docs";
const baseURL = process.env.WEB_BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }),
});

const owned = {
  id: 11,
  name: "需求分析助手",
  description: "整理需求背景、约束和验收范围。",
  icon: "bot",
  system_prompt: "请梳理需求",
  welcome_message: "请描述需求",
  default_model_id: 1,
  temperature: "1.00",
  max_output_tokens: 4096,
  thinking_mode: "auto",
  knowledge_mode: "auto",
  model_timeout_seconds: 120,
  tool_timeout_seconds: 120,
  max_model_calls: null,
  max_tool_calls: null,
  max_total_tokens: null,
  max_estimated_cost: null,
  enabled: true,
  version: 2,
  update_time: "2026-10-29T09:00:00",
};
const shared = {
  id: 21,
  name: "项目复盘助手",
  description: "帮助团队梳理项目结论。",
  icon: "bot",
  welcome_message: "请输入项目背景",
  enabled: true,
  owner_id: 7,
};
const models = [{ id: 1, display_name: "主要模型", context_window: 8192 }];
const envelope = (data) => ({ code: 200, message: "操作成功", data });

async function withFixture(page, role) {
  await page.addInitScript(() => {
    sessionStorage.setItem("yp.access_token", "fixture-access");
    sessionStorage.setItem("yp.refresh_token", "fixture-refresh");
  });
  let lastUpdate = null;
  let lastCreate = null;
  let lastGrant = null;
  let lastDelete = null;
  let shares = [{ user_id: 7, username: "teammate" }];
  await page.route("**/api/v1/**", (route) => {
    const { pathname } = new URL(route.request().url());
    const method = route.request().method();
    if (method === "POST" && pathname === "/api/v1/agents") {
      lastCreate = JSON.parse(route.request().postData() || "{}");
      return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ code: 201, message: "创建成功", data: { ...owned, ...lastCreate, id: 12, version: 1 } }) });
    }
    if (method === "PUT" && pathname === "/api/v1/agents/11") {
      lastUpdate = JSON.parse(route.request().postData() || "{}");
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope({ ...owned, ...lastUpdate, version: 3 })) });
    }
    if (pathname === "/api/v1/agents/11/shares" && method === "GET") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope(shares)) });
    if (pathname === "/api/v1/agents/share-targets" && method === "GET") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope([{ user_id: 8, username: "new-teammate" }])) });
    if (pathname === "/api/v1/agents/11/shares" && method === "POST") {
      lastGrant = JSON.parse(route.request().postData() || "{}");
      shares = [...shares, { user_id: lastGrant.user_id, username: "new-teammate" }];
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope(shares.at(-1))) });
    }
    if (pathname === "/api/v1/agents/11/shares/7" && method === "DELETE") {
      shares = shares.filter((item) => item.user_id !== 7);
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope(null)) });
    }
    if (pathname === "/api/v1/agents/11" && method === "DELETE") {
      lastDelete = pathname;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope(null)) });
    }
    if (pathname === "/api/v1/agents/runtime-limits") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(envelope({ default_max_output_tokens: 4096, max_output_tokens: 1000000, default_model_timeout_seconds: 120, max_model_timeout_seconds: 3600, default_tool_timeout_seconds: 120, max_tool_timeout_seconds: 3600, recursion_limit: 9999, mcp_call_timeout_seconds: 120, knowledge_search_timeout_seconds: 120, script_execution_timeout_seconds: 120 })) });
    const data = pathname === "/api/v1/auth/me" ? { id: 1, username: "tester", nickname: "测试用户", role }
      : pathname === "/api/v1/agents" ? { items: [owned], total: 1, page: 1, page_size: 20 }
      : pathname === "/api/v1/agents/shared" ? [shared]
      : pathname === "/api/v1/models/available" ? models
      : pathname === "/api/v1/agents/11" ? owned
      : pathname === "/api/v1/agents/shared/21" ? shared : null;
    return route.fulfill({ status: data === null ? 404 : 200, contentType: "application/json", body: JSON.stringify(envelope(data)) });
  });
  return { getUpdate: () => lastUpdate, getCreate: () => lastCreate, getGrant: () => lastGrant, getDelete: () => lastDelete };
}

try {
  for (const [name, width, height] of [["desktop", 1280, 800], ["mobile", 375, 812]]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const { getUpdate, getCreate, getGrant, getDelete } = await withFixture(page, "admin");
    await page.goto(`${baseURL}/agents`, { waitUntil: "networkidle" });
    await page.getByText("需求分析助手").first().waitFor();
    await page.screenshot({ path: `${screenshotDir}/web-agent-${name}.png`, fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);

    await page.getByRole("tab", { name: /共享给我/ }).click();
    await page.getByText("项目复盘助手").waitFor();
    await page.getByRole("button", { name: "查看详情" }).click();
    await page.getByText("用户 #7").last().waitFor();
    assert.equal(await page.getByText("请梳理需求").count(), 0);
    await page.getByRole("button", { name: "Close" }).click();

    await page.getByRole("tab", { name: /我的 Agent/ }).click();
    await page.getByRole("button", { name: "查看详情" }).click();
    await page.getByText("请梳理需求").waitFor();
    await page.getByText("teammate", { exact: true }).waitFor();
    await page.waitForTimeout(350);
    await page.screenshot({ path: `${screenshotDir}/web-agent-detail-${name}.png`, fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    if (name === "desktop") {
      await page.getByRole("combobox", { name: "搜索用户名" }).fill("new");
      await page.getByText("new-teammate").last().click();
      await page.getByRole("button", { name: "授权", exact: true }).click();
      await page.locator(".agent-shares .ant-list-item").getByText("new-teammate", { exact: true }).waitFor();
      assert.deepEqual(getGrant(), { user_id: 8 });
      await page.getByRole("button", { name: "撤销授权 teammate" }).click();
      await page.locator(".ant-popconfirm .ant-btn-primary").click();
      await page.locator(".agent-shares .ant-list-item").getByText("teammate", { exact: true }).waitFor({ state: "hidden" });
    }
    await page.getByRole("button", { name: "编辑" }).last().click();
    await page.getByRole("textbox", { name: "名称" }).waitFor();
    await page.waitForTimeout(350);
    await page.screenshot({ path: `${screenshotDir}/web-agent-editor-${name}.png`, fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    await page.getByRole("textbox", { name: "名称" }).fill("需求分析助手新版");
    await page.getByRole("combobox", { name: "知识回答模式" }).press("ArrowDown");
    await page.getByText("仅知识库 · 依据资料回答", { exact: true }).click();
    await page.locator(".ant-form-item").filter({ has: page.locator("#knowledge_mode") }).screenshot({ path: `${screenshotDir}/web-agent-knowledge-mode-${name}.png` });
    await page.getByRole("button", { name: /保\s*存/ }).click();
    await page.waitForFunction(() => !document.body.innerText.includes("编辑 Agent"));
    assert.equal(getUpdate()?.expected_version, 2);
    assert.equal(getUpdate()?.name, "需求分析助手新版");
    assert.equal(getUpdate()?.knowledge_mode, "knowledge_only");
    assert.equal(Object.keys(getUpdate()).length, 3);

    if (name === "desktop") {
      await page.getByRole("button", { name: "新建 Agent" }).click();
      await page.getByRole("textbox", { name: "名称" }).fill("新建助手");
      await page.getByRole("combobox", { name: "默认模型" }).click();
      await page.getByText("主要模型").last().click();
      await page.getByText("运行设置", { exact: true }).click();
      await page.getByRole("spinbutton", { name: "模型调用上限" }).fill("4");
      await page.getByRole("spinbutton", { name: "最大输出 Token" }).fill("2048");
      await page.getByRole("button", { name: /保\s*存/ }).click();
      await page.locator(".ant-drawer-open").waitFor({ state: "hidden", timeout: 3000 }).catch(async () => {
        console.log("create diagnostic", JSON.stringify({ request: getCreate(), drawer: await page.locator(".ant-drawer-open").innerText() }));
        throw new Error("创建抽屉未关闭");
      });
      assert.equal(getCreate()?.name, "新建助手");
      assert.equal(getCreate()?.knowledge_mode, "auto");
      assert.equal(getCreate()?.default_model_id, 1);
      assert.equal(getCreate()?.max_output_tokens, 2048);
      assert.equal(getCreate()?.max_model_calls, 4);
      await page.getByRole("button", { name: "查看详情" }).first().click();
      await page.getByRole("button", { name: "删除 Agent" }).click();
      await page.locator(".ant-popconfirm .ant-btn-primary").click();
      await page.waitForFunction(() => !document.body.innerText.includes("Agent 详情"));
      assert.equal(getDelete(), "/api/v1/agents/11");
    }

    await page.getByText("表格", { exact: true }).click();
    await page.getByRole("columnheader", { name: "默认模型" }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    assert.deepEqual(errors, []);
    console.log(`${name}: list, shared privacy, detail, edit, table and width passed`);
    await page.close();
  }

  const page = await browser.newPage({ viewport: { width: 375, height: 812 } });
  await withFixture(page, "user");
  await page.goto(`${baseURL}/agents`, { waitUntil: "networkidle" });
  await page.getByText("需求分析助手").first().waitFor();
  await page.getByRole("button", { name: "新建 Agent" }).click();
  await page.getByRole("combobox", { name: "默认模型" }).click();
  await page.getByText("主要模型").last().waitFor();
  await page.getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "编辑" }).click();
  assert.equal(await page.getByRole("combobox", { name: "默认模型" }).isVisible(), true);
  console.log("user: create model selection and own edit available");
  await page.close();

  const loginPage = await browser.newPage({ viewport: { width: 375, height: 812 } });
  await loginPage.goto(`${baseURL}/agents`, { waitUntil: "networkidle" });
  assert.equal(await loginPage.getByRole("heading", { name: "登录" }).isVisible(), true);
  assert.equal(await loginPage.evaluate(() => document.documentElement.scrollWidth), 375);
  console.log("login: unauthenticated route and width passed");
  await loginPage.close();

  const expiredPage = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await withFixture(expiredPage, "admin");
  await expiredPage.goto(`${baseURL}/agents`, { waitUntil: "networkidle" });
  await expiredPage.getByText("需求分析助手").first().waitFor();
  await expiredPage.route("**/api/v1/agents**", (route) => route.fulfill({
    status: 401,
    contentType: "application/json",
    body: JSON.stringify({ code: 401, message: "未授权或令牌已失效" }),
  }));
  await expiredPage.route("**/api/v1/auth/refresh", (route) => route.fulfill({
    status: 401,
    contentType: "application/json",
    body: JSON.stringify({ code: 401, message: "未授权或令牌已失效" }),
  }));
  await expiredPage.getByRole("button", { name: "刷新列表" }).click();
  await expiredPage.getByRole("heading", { name: "登录" }).waitFor();
  console.log("auth: expired session returns to login");
  await expiredPage.close();
} finally {
  await browser.close();
}
