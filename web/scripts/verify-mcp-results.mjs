import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdtemp, rm, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const webRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const directory = await mkdtemp(join(webRoot, '.mcp-results-verify-'));
const screenshotDirectory = process.env.SCREENSHOT_DIR || '/tmp/ypoint-mcp-e-screenshots';
await mkdir(screenshotDirectory, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
const sha = 'a'.repeat(64);
const source = { tool_name: 'orders', call_id: 'one-call', scope: 'single_response_redacted', received_complete: true, remote_dataset_complete: null, redacted: true };
const file = { artifact_id: 'result-a', filename: 'mcp-result-a.json', byte_size: 10000, sha256: sha, available: true, tool_result: source };
const budget = { tool_loading: { mode: 'dynamic', available_mcp_tools: 17, loaded_mcp_tools: 2 }, estimator: 'approximate_cjk_v2', estimated_input_tokens: 9000, serialized_input_bytes: 35000, context_window: 8192,
  output_reserve_tokens: 2048, input_limit_tokens: 5600, fits: false, components: { tools: { bytes: 20000, tokens: 6000 }, attachments: { bytes: 10000, tokens: 2000 } },
  notice: '保守近似估算，不能用于计费', suggestions: ['减少本轮关联工具或缩小工具 Schema', '降低输出预留或选择更大窗口模型'] };
try {
  await writeFile(join(directory, 'index.html'), `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module">
    import React from 'react'; import { createRoot } from 'react-dom/client';
    import MessageFiles from '/src/components/MessageFiles.tsx';
    import RunMetrics from '/src/components/RunMetrics.tsx';
    import RunToolResults from '/src/components/RunToolResults.tsx';
    import '/src/styles/design-tokens.css'; import '/src/styles/app.css'; import '/src/styles/sessions.css';
    function Harness() { return React.createElement(React.Fragment, null,
      React.createElement(RunMetrics, { metrics: { elapsed_ms: 500, total_tokens: 100, context_budget: ${JSON.stringify(budget)} } }),
      React.createElement(MessageFiles, { files: [${JSON.stringify(file)}], generated: true }),
      React.createElement(RunToolResults, { runId: 'failed-after-write', active: false })); }
    createRoot(document.getElementById('root')).render(React.createElement(Harness));
  </script></body></html>`);
  for (const width of [1280, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, acceptDownloads: true });
    const errors = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => { sessionStorage.setItem('yp.access_token', 'fixture'); });
    let failNext = false, expired = false, failDownload = false;
    await page.route('**/api/v1/**', async route => {
      const url = new URL(route.request().url());
      requests.push(url.pathname + url.search);
      const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, message: status === 200 ? 'ok' : data, data: status === 200 ? data : null }) });
      if (url.pathname === '/api/v1/artifacts') return send([{ ...file, id: 'result-b', filename: 'failed-run-result.json' }]);
      if (url.pathname.endsWith('/result-page')) {
        assert.equal(url.searchParams.get('expected_sha256'), sha);
        if (expired) return send('结果已过期', 404);
        const offset = Number(url.searchParams.get('offset'));
        if (offset > 0 && failNext) { failNext = false; return send('结果读取中断', 409); }
        return send({ artifact_id: 'result-a', sha256: sha, offset, end_offset: offset ? 100 : 50, next_offset: offset ? null : 50,
          total_characters: 100, saved_bytes: 10000, text_fragment: offset ? '下一段已读取' : '{"text":"<script>window.resultInjected=true</script>"}',
          fragment_format: 'json_text_fragment', complete: !!offset, source, notice: '单次响应快照' });
      }
      if (url.pathname.endsWith('/download')) {
        if (failDownload) return send('结果已过期', 404);
        return route.fulfill({ contentType: 'application/json', headers: { 'content-disposition': 'attachment; filename="result.json"' }, body: '{"complete_response":true}' });
      }
      throw new Error(`Unexpected API request ${url.pathname}`);
    });
    await page.goto(`${base}/${directory.split('/').pop()}/index.html`);
    await page.getByText('MCP 返回结果 · 单次响应快照', { exact: false }).first().waitFor();
    await page.getByText('failed-run-result.json', { exact: true }).waitFor();
    await page.locator('summary').filter({ hasText: '上下文估算' }).click();
    await page.getByText('工具 Schema', { exact: true }).waitFor();
    await page.getByText('参考附件', { exact: true }).waitFor();
    assert.ok((await page.locator('.context-budget-notice').innerText()).includes('减少本轮关联工具'));
    await page.getByText('按需加载 · 本轮固定 17 个 · 本次绑定 2 个', { exact: true }).waitFor();
    await page.screenshot({ path: join(screenshotDirectory, `mcp-budget-${width}.png`), animations: 'disabled' });
    await page.getByRole('button', { name: '预览 mcp-result-a.json', exact: true }).click();
    await page.getByLabel('MCP 结果分段预览').waitFor();
    await page.getByText('字符 0–50 / 100', { exact: false }).waitFor();
    assert.equal(await page.evaluate(() => window.resultInjected), undefined);
    assert.equal(await page.getByRole('button', { name: '上一段', exact: true }).isDisabled(), true);
    failNext = true;
    await page.getByRole('button', { name: '继续读取', exact: true }).click();
    await page.getByRole('button', { name: '重试读取', exact: true }).waitFor();
    await page.getByRole('button', { name: '重试读取', exact: true }).click();
    await page.getByText('下一段已读取', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '继续读取', exact: true }).isDisabled(), true);
    await page.getByRole('button', { name: '上一段', exact: true }).click();
    await page.getByText('字符 0–50 / 100', { exact: false }).waitFor();
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '下载原件', exact: true }).click()]);
    assert.ok(download.suggestedFilename().endsWith('.json'));
    await page.screenshot({ path: join(screenshotDirectory, `mcp-result-${width}.png`), animations: 'disabled' });
    expired = true;
    await page.getByRole('button', { name: '继续读取', exact: true }).click();
    await page.getByText('结果已过期', { exact: true }).waitFor();
    assert.ok(requests.filter(value => value.includes('/result-page')).length >= 5);
    assert.deepEqual(errors, []);
    console.log(`${width}: context breakdown, result label, stable hash paging, interruption/retry, EOF, safe text rendering, private download and expiry passed`);
    await page.close();
  }
} finally { await browser.close(); await rm(directory, { recursive: true, force: true }); }
