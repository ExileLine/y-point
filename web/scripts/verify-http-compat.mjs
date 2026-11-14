import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { chromium } from "playwright";

// 使用非 localhost 的 HTTP 来源，实际触发浏览器的非安全上下文限制。
const base = process.env.WEB_BASE_URL || "http://ypoint-http.test:18080";
const source = await readFile(new URL("../src/browser-compat.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const browser = await chromium.launch({
  headless: true,
  args: ["--host-resolver-rules=MAP ypoint-http.test 127.0.0.1", "--no-proxy-server"],
  ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }),
});
try {
  const page = await browser.newPage();
  await page.goto(`${base}/healthz`);
  assert.equal(await page.evaluate(() => isSecureContext), false);
  assert.equal(await page.evaluate(() => typeof crypto.randomUUID), "undefined");
  assert.equal(await page.evaluate(() => typeof navigator.clipboard), "undefined");
  const result = await page.evaluate(async code => {
    const url = URL.createObjectURL(new Blob([code], { type: "text/javascript" }));
    const { createClientId, copyText } = await import(url);
    URL.revokeObjectURL(url);
    const identifiers = Array.from({ length: 1000 }, () => createClientId());
    const button = document.createElement("button");
    button.textContent = "验证 HTTP 复制";
    button.onclick = async () => {
      try {
        await copyText("内网 HTTP 复制验证");
        document.body.dataset.copyStatus = "ok";
      } catch (error) { document.body.dataset.copyStatus = String(error); }
    };
    document.addEventListener("copy", () => {
      document.body.dataset.copiedText = document.activeElement?.value;
    }, { once: true });
    document.body.append(button);
    return identifiers;
  }, compiled);
  assert.equal(new Set(result).size, 1000);
  assert.ok(result.every(value => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)));
  await page.getByRole("button", { name: "验证 HTTP 复制" }).click();
  await page.waitForFunction(() => document.body.dataset.copyStatus);
  assert.equal(await page.evaluate(() => document.body.dataset.copyStatus), "ok");
  assert.equal(await page.evaluate(() => document.body.dataset.copiedText), "内网 HTTP 复制验证");
  assert.equal(await page.locator("textarea").count(), 0);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "验证 HTTP 复制");
  console.log("http-compat: 9 assertions passed (real insecure HTTP origin; UUID and clipboard)");
} finally { await browser.close(); }
