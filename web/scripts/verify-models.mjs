import { screenshotPath } from "./screenshot-paths.mjs";
import assert from "node:assert/strict";
import { chromium } from "playwright";

const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
const envelope = (data) => ({ code: 200, message: "操作成功", data });
const kinds = [
  { kind: "openai_compatible", display_name: "OpenAI 兼容接口", required_fields: ["name", "kind", "base_url"], optional_fields: ["api_key", "custom_headers", "timeout_seconds", "remark", "status"], suggested_base_url: null },
  { kind: "ollama", display_name: "Ollama", required_fields: ["name", "kind", "base_url"], optional_fields: ["api_key", "timeout_seconds", "remark", "status"], suggested_base_url: "http://127.0.0.1:11434/v1" },
];

function fixture(page, options = {}) {
  let discoveryAttempts = 0;
  const calls = [];
  let providers = [{ id: 1, name: "本地 Ollama", kind: "ollama", base_url: "http://127.0.0.1:11434/v1", has_api_key: false, has_custom_headers: false, api_version: null, region: null, timeout_seconds: 15, remark: null, status: 1 }];
  if (options.discovery) providers.push({ ...providers[0], id: 9, name: "远程连接" });
  let models = [{ id: 3, provider_id: 1, external_id: "llama3.2", display_name: "Llama 3.2", capabilities: ["text", "streaming", "tools", "structured_output", "vision", "reasoning_summary"], verified_capabilities: [], capabilities_verified: false, context_window: 8192, input_price: null, output_price: null, price_currency: "USD", is_default: false, status: 1 }];
  page.addInitScript(() => {
    sessionStorage.setItem("yp.access_token", "fixture-access");
    sessionStorage.setItem("yp.refresh_token", "fixture-refresh");
  });
  page.route("**/api/v1/**", async (route) => {
    const { pathname } = new URL(route.request().url());
    const method = route.request().method();
    const body = route.request().postData() ? JSON.parse(route.request().postData()) : null;
    calls.push({ method, pathname, body });
    let data = null;
    if (pathname === "/api/v1/auth/me") data = { id: 1, username: "root", nickname: "管理员", role: "admin" };
    else if (pathname === "/api/v1/agents/runtime-limits") data = { default_model_timeout_seconds: 120, max_model_timeout_seconds: 600, default_tool_timeout_seconds: 120, max_tool_timeout_seconds: 600, default_max_output_tokens: 4096, max_output_tokens: 65536 };
    else if (pathname === "/api/v1/providers/kinds") data = kinds;
    else if (pathname === "/api/v1/providers" && method === "GET") data = providers;
    else if (pathname === "/api/v1/models" && method === "GET") data = models;
    else if (pathname === "/api/v1/providers" && method === "POST") {
      data = { ...providers[0], ...body, id: 2, has_api_key: Boolean(body.api_key) };
      providers = [...providers, data];
    } else if (pathname === "/api/v1/providers/1" && method === "PUT") {
      providers = providers.map((item) => item.id === 1 ? { ...item, ...body } : item);
      data = providers[0];
    } else if (pathname === "/api/v1/providers/1/test") data = { available: true, latency_ms: 18, upstream_status_code: 200, failure: null };
    else if (options.discovery && pathname === "/api/v1/providers/9/available-models") {
      discoveryAttempts += 1;
      data = discoveryAttempts === 1
        ? { available: false, failure: "timeout", latency_ms: 10, models: [] }
        : { available: true, failure: null, latency_ms: 10, models: [{ external_id: "remote-id", display_name: "远程模型", already_added: false }] };
    }
    else if (pathname === "/api/v1/models" && method === "POST") {
      data = { ...models[0], ...body, id: 8 };
      models = [...models, data];
    }
    else if (pathname === "/api/v1/providers/1/available-models") data = { available: true, latency_ms: 18, upstream_status_code: 200, failure: null, models: [{ external_id: "llama3.2", display_name: "Llama 3.2", already_added: true }, { external_id: "qwen3", display_name: "Qwen 3", already_added: false }] };
    else if (pathname === "/api/v1/models/import") {
      const next = { ...models[0], id: 4, external_id: "qwen3", display_name: "Qwen 3", capabilities: ["text"] };
      models = [...models, next];
      data = { created: [next], skipped_existing: [] };
    } else if (pathname === "/api/v1/models/3/capabilities/test") {
      models = models.map((item) => item.id === 3 ? { ...item, verified_capabilities: ["text"], capabilities_verified: true } : item);
      data = { capability: "text", result: { available: true, latency_ms: 42, upstream_status_code: 200, failure: null }, verified_capabilities: ["text"] };
    } else if (pathname === "/api/v1/models/3/default") {
      models = models.map((item) => ({ ...item, is_default: item.id === 3 }));
      data = models[0];
    } else if (pathname === "/api/v1/models/3" && method === "PUT") {
      models = models.map((item) => item.id === 3 ? { ...item, ...body } : item);
      data = models[0];
    }
    await route.fulfill({ status: data === null ? 404 : 200, contentType: "application/json", body: JSON.stringify(envelope(data)) });
  });
  return calls;
}

try {
  // Exercise discovery inside the manual model editor, including unavailable lists and switching.
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const calls = fixture(page, { discovery: true });
    await page.goto(`${process.env.WEB_BASE_URL || "http://127.0.0.1:5173"}/models`, { waitUntil: "networkidle" });
    await page.getByRole("tab", { name: /模型/ }).click();
    await page.getByRole("button", { name: "录入模型", exact: true }).click();
    const drawer = page.locator(".ant-drawer-open");
    const id = drawer.getByRole("combobox", { name: "供应商模型 ID" });
    assert.equal(await id.isDisabled(), true);
    assert.equal(await drawer.locator(".ant-select-selection-item").count(), 6);
    await drawer.locator(".ant-select").filter({ has: page.getByRole("combobox", { name: "供应商连接", exact: true }) }).click();
    await page.locator(".ant-select-dropdown:visible").getByText("本地 Ollama", { exact: true }).click();
    await drawer.getByText(/已发现 2 个模型/).waitFor();
    await id.click();
    await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").getByText("Qwen 3 · qwen3", { exact: true }).click();
    assert.equal(await id.inputValue(), "qwen3");
    assert.equal(await drawer.getByRole("textbox", { name: "显示名称" }).inputValue(), "Qwen 3");
    await drawer.getByRole("textbox", { name: "显示名称" }).fill("自定义模型名称");
    await drawer.locator(".ant-select").filter({ has: page.getByRole("combobox", { name: "供应商连接", exact: true }) }).click();
    await page.locator(".ant-select-dropdown:visible").getByText("远程连接", { exact: true }).click();
    await drawer.getByText(/模型发现暂不可用：连接超时/).waitFor();
    assert.equal(await id.inputValue(), "");
    assert.equal(await drawer.getByRole("textbox", { name: "显示名称" }).inputValue(), "自定义模型名称");
    assert.equal(await id.isDisabled(), false); // manual entry remains possible after discovery failure
    await id.fill("manual-fallback-id");
    assert.equal(await id.inputValue(), "manual-fallback-id");
    await drawer.getByRole("button", { name: "重新发现" }).click();
    await drawer.getByText(/已发现 1 个模型/).waitFor();
    await id.fill("");
    await id.click();
    await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").getByText("远程模型 · remote-id", { exact: true }).click();
    assert.equal(await drawer.getByRole("textbox", { name: "显示名称" }).inputValue(), "自定义模型名称");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await drawer.getByRole("textbox", { name: "显示名称" }).click();
    await drawer.locator(".ant-form-item-explain-error").waitFor({ state: "hidden" });
    await page.screenshot({ path: screenshotPath(`web-model-entry-${width}.png`), animations: "disabled" });
    await drawer.getByRole("button", { name: /^保\s*存$/ }).click();
    await drawer.waitFor({ state: "hidden" });
    const saved = calls.find(call => call.pathname === "/api/v1/models" && call.method === "POST").body;
    assert.equal(saved.provider_id, 9);
    assert.equal(saved.external_id, "remote-id");
    assert.equal(saved.display_name, "自定义模型名称");
    assert.deepEqual(saved.capabilities, ["text", "streaming", "tools", "structured_output", "vision", "reasoning_summary"]);
    assert.equal(calls.filter(call => call.pathname === "/api/v1/providers/9/available-models").length, 2);
    await page.close();
    console.log(`${width}: model discovery, retry, provider switching, selected ID payload and capability defaults passed`);
  }
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    fixture(page, { discovery: true });
    let releaseOld;
    let markStarted;
    let markFinished;
    const oldReleased = new Promise(resolve => { releaseOld = resolve; });
    const oldStarted = new Promise(resolve => { markStarted = resolve; });
    const oldFinished = new Promise(resolve => { markFinished = resolve; });
    await page.route("**/providers/*/available-models", async route => {
      if (route.request().url().includes("/providers/1/")) {
        markStarted();
        await oldReleased;
        try {
          await route.fulfill({ json: envelope({ available: true, models: [{ external_id: "stale-model", display_name: "过期模型", already_added: false }] }) });
        } finally { markFinished(); }
      } else await route.fulfill({ json: envelope({ available: true, models: [{ external_id: "current-model", display_name: "当前模型", already_added: false }] }) });
    });
    await page.goto(`${process.env.WEB_BASE_URL || "http://127.0.0.1:5173"}/models`, { waitUntil: "networkidle" });
    await page.getByRole("tab", { name: /模型/ }).click();
    await page.getByRole("button", { name: "录入模型", exact: true }).click();
    const drawer = page.locator(".ant-drawer-open");
    const provider = drawer.locator(".ant-select").filter({ has: page.getByRole("combobox", { name: "供应商连接", exact: true }) });
    await provider.click();
    await page.locator(".ant-select-dropdown:visible").getByText("本地 Ollama", { exact: true }).click();
    await oldStarted;
    assert.equal(await drawer.getByRole("combobox", { name: "供应商模型 ID" }).isDisabled(), true);
    await provider.click();
    await page.locator(".ant-select-dropdown:visible").getByText("远程连接", { exact: true }).click();
    await drawer.getByText(/已发现 1 个模型/).waitFor();
    releaseOld();
    await oldFinished;
    await drawer.getByRole("combobox", { name: "供应商模型 ID" }).click();
    await page.locator(".ant-select-dropdown:visible").getByText("当前模型 · current-model", { exact: true }).waitFor();
    assert.equal(await page.getByText("过期模型 · stale-model", { exact: true }).count(), 0);
    await page.close();
    console.log("discovery race: aborted stale provider response cannot overwrite current options");
  }
  for (const [name, width, height] of [["desktop", 1280, 800], ["mobile", 375, 812]]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const calls = fixture(page);
    await page.goto(`${process.env.WEB_BASE_URL || "http://127.0.0.1:5173"}/models`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.querySelector('[role="tab"][aria-selected="true"]')?.textContent?.includes("供应商"));
    assert.match((await page.getByRole("tab").allTextContents())[0], /供应商/);
    await page.getByRole("tab", { name: /模型/ }).click();
    await page.locator(".catalog-desktop-table:visible, .catalog-mobile-items:visible").getByText("Llama 3.2").first().waitFor();
    assert.equal(await page.locator(".catalog-capabilities:visible .capability-tag").count(), 6);
    assert.equal(await page.locator(".catalog-capabilities:visible .capability-tag[data-verified=false]").count(), 6);
    const palette = await page.locator(".catalog-capabilities:visible .capability-tag").evaluateAll(tags => tags.map(tag => getComputedStyle(tag).color));
    assert.equal(new Set(palette).size, 6);
    await page.screenshot({ path: screenshotPath(`web-models-${name}.png`), fullPage: true, animations: "disabled" });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    await page.getByRole("tab", { name: /供应商/ }).click();
    await page.waitForFunction(() => document.querySelector('[role="tab"][aria-selected="true"]')?.textContent?.includes("供应商"));
    await page.waitForTimeout(250);
    await page.screenshot({ path: screenshotPath(`web-providers-${name}.png`), fullPage: true, animations: "disabled" });
    if (name === "desktop") {
      await page.getByRole("tab", { name: /模型/ }).click();
      await page.getByRole("button", { name: "验证能力 Llama 3.2" }).click();
      await page.getByText("验证文本", { exact: true }).click();
      await page.getByText(/Llama 3.2：可用/).waitFor();
      await page.getByRole("button", { name: "设为默认 Llama 3.2" }).click();
      await page.getByText("默认", { exact: true }).waitFor();
      await page.getByRole("button", { name: "更多操作 Llama 3.2" }).click();
      await page.getByText("编辑模型", { exact: true }).click();
      await page.getByRole("textbox", { name: "显示名称" }).fill("Llama 3.2 最新版");
      await page.getByRole("button", { name: /保\s*存/ }).click();
      await page.waitForFunction(() => !document.body.innerText.includes("编辑模型"));
      const update = calls.find((call) => call.method === "PUT" && call.pathname === "/api/v1/models/3");
      assert.deepEqual(update.body, { display_name: "Llama 3.2 最新版" });
      await page.getByRole("tab", { name: /供应商/ }).click();
      await page.getByRole("button", { name: "测试连接 本地 Ollama" }).click();
      await page.getByText(/本地 Ollama：可用/).waitFor();
      await page.getByRole("button", { name: "发现模型 本地 Ollama" }).click();
      await page.getByText("Qwen 3").last().waitFor();
      await page.getByRole("checkbox", { name: /Qwen 3/ }).check();
      await page.getByRole("button", { name: "导入 (1)" }).click();
      await page.getByText("导入 1 个模型").waitFor();
      assert.deepEqual(calls.find((call) => call.pathname === "/api/v1/models/import")?.body, { provider_id: 1, models: [{ external_id: "qwen3", display_name: "Qwen 3", capabilities: ["text"] }] });
      await page.getByRole("button", { name: "编辑连接 本地 Ollama" }).click();
      await page.getByRole("textbox", { name: "连接名称" }).fill("本地模型");
      await page.getByRole("button", { name: /保\s*存/ }).click();
      await page.waitForFunction(() => !document.body.innerText.includes("编辑供应商连接"));
      assert.deepEqual(calls.find((call) => call.method === "PUT" && call.pathname === "/api/v1/providers/1")?.body, { name: "本地模型" });
      await page.getByRole("button", { name: "新建连接" }).click();
      const providerDrawer = page.locator(".ant-drawer-open");
      assert.equal(await providerDrawer.locator('input:not([role="combobox"]), textarea').evaluateAll(inputs => inputs.every(input => Boolean(input.placeholder))), true);
      await page.screenshot({ path: screenshotPath("web-provider-create-desktop.png"), animations: "disabled" });
      await page.getByRole("textbox", { name: "连接名称" }).fill("备用连接");
      await page.getByRole("textbox", { name: "接口地址" }).fill("http://example.local/v1");
      await page.getByRole("button", { name: /保\s*存/ }).click();
      await page.waitForFunction(() => !document.body.innerText.includes("新建供应商连接"));
      assert.equal(calls.find((call) => call.method === "POST" && call.pathname === "/api/v1/providers")?.body.kind, "openai_compatible");
    } else {
      await page.getByRole("button", { name: "新建连接" }).click();
      await page.getByRole("textbox", { name: "连接名称" }).waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    }
    assert.deepEqual(errors, []);
    console.log(`${name}: catalog layout and interaction passed`);
    await page.close();
  }
} finally {
  await browser.close();
}
