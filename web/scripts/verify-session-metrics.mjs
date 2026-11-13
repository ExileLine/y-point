import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const directory = await mkdtemp(join(dirname(fileURLToPath(import.meta.url)), '../.metrics-verify-'));
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let assertions = 0;
function check(value, message) { assert.ok(value, message); assertions++; }
try {
  await writeFile(join(directory, 'index.html'), `<!doctype html><html><body><div id="root"></div><script type="module">
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import SessionMetrics from '/src/components/SessionMetrics.tsx';
    function Harness() {
      const [session, setSession] = React.useState('a');
      const [revision, setRevision] = React.useState(0);
      const [active, setActive] = React.useState(false);
      return React.createElement(React.Fragment, null,
        React.createElement('button', { onClick: () => setRevision(value => value + 1) }, '刷新统计'),
        React.createElement('button', { onClick: () => setActive(value => !value) }, '切换运行状态'),
        React.createElement('button', { onClick: () => { setSession('b'); setActive(false); } }, '切换会话'),
        React.createElement(SessionMetrics, { key: session, agentId: 13, sessionId: session, refreshKey: revision, active }));
    }
    createRoot(document.getElementById('root')).render(React.createElement(React.StrictMode, null, React.createElement(Harness)));
  </script></body></html>`);
  const page = await browser.newPage();
  page.setDefaultTimeout(10000);
  await page.routeWebSocket('**', socket => socket.close());
  await page.addInitScript(() => {
    window.metricsAborts = 0;
    const original = window.fetch;
    window.fetch = (input, init) => {
      if (String(input).endsWith('/metrics')) init?.signal?.addEventListener('abort', () => window.metricsAborts++, { once: true });
      return original(input, init);
    };
  });
  const held = [];
  let mode = 'ok', requests = 0;
  await page.route('**/api/v1/**', async route => {
    const session = new URL(route.request().url()).pathname.split('/').at(-2);
    const currentMode = mode;
    requests++;
    if (session === 'a' && currentMode === 'hold') await new Promise(resolve => held.push(resolve));
    const status = currentMode === 'denied' ? 403 : currentMode === 'error' ? 503 : 200;
    const data = {
      run_count: session === 'b' ? 0 : 9, active_run_count: 0,
      elapsed_ms: 59717, total_tokens: session === 'b' ? 0 : 92980,
      missing_duration_runs: 0, calculated_at: new Date().toISOString(),
    };
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, data }) }).catch(() => {});
  });
  await page.goto(`${base}/${directory.split('/').at(-1)}/index.html`);
  await page.waitForFunction(() => document.body.textContent.includes('92,980 Token'));
  check(true, 'StrictMode 挂载后显示会话累计统计');
  check(requests === 1 && await page.evaluate(() => window.metricsAborts) === 0, 'StrictMode 挂载检查不发出随后被取消的重复统计请求');
  mode = 'hold';
  await page.getByRole('button', { name: '刷新统计', exact: true }).click();
  await assertEventually(() => held.length > 0);
  const aborts = await page.evaluate(() => window.metricsAborts);
  const before = requests;
  await page.getByRole('button', { name: '切换运行状态', exact: true }).click();
  await page.getByRole('button', { name: '刷新统计', exact: true }).click();
  await page.getByRole('button', { name: '刷新统计', exact: true }).click();
  check(await page.evaluate(() => window.metricsAborts) === aborts, '运行状态及消息刷新不得取消同一会话的在途统计请求');
  check(requests === before, '多个刷新合并等待当前统计请求完成，不并发重复查询');
  mode = 'ok';
  held.splice(0).forEach(resolve => resolve());
  await assertEventually(() => requests > before);
  check(requests === before + 1, '请求完成后补发一次最新统计，覆盖期间的多次刷新');
  mode = 'error';
  await page.getByRole('button', { name: '刷新统计', exact: true }).click();
  await page.waitForFunction(() => document.body.textContent.includes('显示上次统计'));
  check((await page.getByLabel('整个会话统计').innerText()).includes('92,980 Token'), '临时错误保留上次统计并提示');
  mode = 'ok';
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await page.waitForFunction(() => !document.body.textContent.includes('显示上次统计'));
  mode = 'denied';
  await page.getByRole('button', { name: '刷新统计', exact: true }).click();
  await page.waitForFunction(() => document.body.textContent.includes('会话统计不可访问'));
  check(!(await page.getByLabel('整个会话统计').innerText()).includes('92,980 Token'), '失去访问权限立即清空统计');
  const deniedRequests = requests;
  await page.waitForTimeout(2700);
  check(requests === deniedRequests, '权限错误后停止自动轮询');
  mode = 'hold';
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await assertEventually(() => held.length > 0);
  const switchAborts = await page.evaluate(() => window.metricsAborts);
  mode = 'ok';
  await page.getByRole('button', { name: '切换会话', exact: true }).click();
  await page.waitForFunction(() => document.body.textContent.includes('0 Token'));
  check(await page.evaluate(() => window.metricsAborts) > switchAborts, '切换会话仍主动取消旧会话请求');
  held.splice(0).forEach(resolve => resolve());
  await page.waitForTimeout(100);
  check(!(await page.getByLabel('整个会话统计').innerText()).includes('92,980 Token'), '旧会话迟到响应不会覆盖新会话');
  const idleRequests = requests;
  await page.waitForTimeout(2700);
  check(requests === idleRequests, '无活动运行时不继续轮询');
  console.log(`session metrics verified: ${assertions} assertions`);
} finally {
  await browser.close();
  await rm(directory, { recursive: true, force: true });
}

async function assertEventually(predicate) {
  const deadline = Date.now() + 5000;
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
  assert.ok(predicate(), '统计请求在限定时间内发出');
}
