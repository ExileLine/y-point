import assert from "node:assert/strict";
import { chromium } from "playwright";

const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
const envelope = (data) => ({ code: 200, message: "操作成功", data });
const kinds = [
  { kind: "openai_compatible", display_name: "OpenAI 兼容接口", required_fields: ["name", "kind", "base_url"], optional_fields: ["api_key", "custom_headers", "timeout_seconds", "remark", "status"], suggested_base_url: null },
  { kind: "ollama", display_name: "Ollama", required_fields: ["name", "kind", "base_url"], optional_fields: ["api_key", "timeout_seconds", "remark", "status"], suggested_base_url: "http://127.0.0.1:11434/v1" },
];

function fixture(page) {
  const calls = [];
  let providers = [{ id: 1, name: "本地 Ollama", kind: "ollama", base_url: "http://127.0.0.1:11434/v1", has_api_key: false, has_custom_headers: false, api_version: null, region: null, timeout_seconds: 15, remark: null, status: 1 }];
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
    await page.screenshot({ path: `../docs/web-models-${name}.png`, fullPage: true, animations: "disabled" });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    await page.getByRole("tab", { name: /供应商/ }).click();
    await page.waitForFunction(() => document.querySelector('[role="tab"][aria-selected="true"]')?.textContent?.includes("供应商"));
    await page.waitForTimeout(250);
    await page.screenshot({ path: `../docs/web-providers-${name}.png`, fullPage: true, animations: "disabled" });
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
