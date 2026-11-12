import assert from "node:assert/strict";
import { chromium } from "playwright";

const base = process.env.WEB_BASE_URL || "http://127.0.0.1:5174";
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: "chrome" }) });
try {
  const page = await browser.newPage();
  await page.goto(base);
  await page.evaluate(async () => {
    const React = (await import("/node_modules/.vite/deps/react.js")).default;
    const { createRoot } = (await import("/node_modules/.vite/deps/react-dom_client.js")).default;
    const { flushSync } = (await import("/node_modules/.vite/deps/react-dom.js")).default;
    const { MessageContent } = await import("/src/components/MessageContent.tsx");
    const host = document.createElement("div");
    host.id = "citation-fixture";
    host.style.cssText = "position:fixed;inset:0;background:white;z-index:1000;padding:24px;overflow:auto";
    document.body.append(host);
    const root = createRoot(host);
    window.renderCitation = (text, streaming = false, runId = "citation-run") => flushSync(() => root.render(React.createElement(MessageContent, { text, streaming, runId })));
  });
  const render = async (text, streaming = false, runId = "citation-run") => {
    await page.evaluate(({ text, streaming, runId }) => window.renderCitation(text, streaming, runId), { text, streaming, runId });
  };
  // 使用用户会话中真实保存的两种回答，走实际 Markdown 渲染路径。
  for (const text of [
    "根据知识库中的《差旅报销管理规定》，差旅报销申请须在**十五个工作日**内提交。\n\ncitation: [1]",
    "建议您咨询财务部门。citation: [1]",
  ]) {
    await render(text);
    assert.doesNotMatch(await page.locator("#citation-fixture").innerText(), /citation:\s*\[1\]/i, "用户仍然看到内部引用语法");
    assert.equal(await page.getByRole("button", { name: "来源说明", exact: true }).count(), 1);
  }
  await render("citation: [1]", true);
  assert.equal(await page.getByRole("button", { name: "来源说明", exact: true }).count(), 1);
  await render("报销期限。Citation：[1], [2]", true);
  assert.equal(await page.getByRole("button", { name: "来源说明", exact: true }).count(), 1);
  assert.match(await page.getByRole("button", { name: "来源说明", exact: true }).getAttribute("title"), /\[1\], \[2\]/);
  await render("报销期限。citation: [", true);
  assert.equal(await page.locator("#citation-fixture").innerText(), "报销期限。");
  await render("citation: [1", true);
  assert.equal((await page.locator("#citation-fixture").innerText()).trim(), "");
  await render("报销期限。\n\n来源：《差旅报销管理规定》· 报销流程");
  assert.match(await page.locator("#citation-fixture").innerText(), /来源：《差旅报销管理规定》/);
  assert.equal(await page.getByRole("button", { name: "来源说明", exact: true }).count(), 0);
  // 普通编号、代码、链接、用户对字段的解释均保留原样，不能全局替换字符串。
  for (const text of [
    "普通编号 [1]。",
    "`citation: [1]`",
    "```text\ncitation: [1]\n```",
    "[citation: [1]](https://example.com)",
    "字段 citation: [1] 是一个示例。",
    '{"citation: [1]": "保留 JSON"}',
    '<!doctype html><html><body>citation: [1]</body></html>',
  ]) {
    await render(text);
    assert.equal(await page.getByRole("button", { name: "来源说明", exact: true }).count(), 0, text);
    assert.match(await page.locator("#citation-fixture").innerText(), /\[1\]/, text);
  }
  await render("citation: [1]", false, null);
  assert.match(await page.locator("#citation-fixture").innerText(), /citation: \[1\]/, "产物预览不套用会话引用规则");
  console.log("真实会话引用显示回归通过：15 个用例，覆盖独立/行内尾注、多编号、流式片段、代码/链接/JSON/HTML 及产物预览");
} finally {
  await browser.close();
}
