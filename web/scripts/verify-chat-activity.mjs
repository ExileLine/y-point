import { screenshotPath } from "./screenshot-paths.mjs";
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let assertions = 0;
function check(value, message) { assert.ok(value, message); assertions++; }
const origin = new Date().toISOString();
const reasoning = '先核对文档范围，再逐项检查依据。\n这是一段模型公开返回的思考内容。';
const answer = '## 审查结果\n\n' + '已检查文档中的约束与风险，保留需要人工核实的事项。'.repeat(65) + '\n\n- 结论一\n- 结论二\n\n👨‍👩‍👧‍👦 完成';
const calls = [
  { call_id: 'skill', kind: 'skill', name: 'construction-review@v3', status: 'succeeded', sequence: 3, elapsed_ms: 125 },
  { call_id: 'mcp', kind: 'mcp', name: 'standards_lookup', status: 'succeeded', sequence: 5, elapsed_ms: 2345 },
  { call_id: 'tool', kind: 'tool', name: 'check_report.py', status: 'failed', sequence: 7, elapsed_ms: 490, failure_code: 'tool_timeout', failure_reason: '调用超过单次执行时限，请检查工具或 MCP 的响应时间与超时设置。' },
  { call_id: 'unknown', kind: 'tool', name: 'legacy_tool', status: 'interrupted', sequence: 9, elapsed_ms: null },
];
const metrics = { elapsed_ms: 73450, total_tokens: 18234 };
const artifact = { artifact_id: 'report', filename: '施工审查报告.docx', byte_size: 34335, available: true };
const sockets = new Set();
const server = createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': new URL(base).origin, 'Cache-Control': 'no-cache' });
  sockets.add(res);
  req.on('close', () => sockets.delete(res));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const streamUrl = `http://127.0.0.1:${server.address().port}/stream`;
function event(row) { return `id: run-live:${row.sequence}\nevent: ${row.event_type}\ndata: ${JSON.stringify(row)}\n\n`; }

try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 920 }, reducedMotion: 'no-preference' });
    await context.routeWebSocket('**', socket => socket.close());
    const page = await context.newPage();
    const errors = [], unexpected = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
    const session = { id: 'activity', agent_id: 11, title: '施工审查 · 过程与用量', archived: false, version: 1, model_id: 4, create_time: origin, update_time: origin };
    let finished = false;
    let run = { id: 'run-live', session_id: session.id, agent_id: 11, model_id: 4, state: 'running', failure_code: null, reported_total_tokens: 0, create_time: origin, update_time: origin };
    const rows = [];
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      let data;
      if (path === '/api/v1/agents/runtime-limits') data = { upload_max_bytes: 10485760, context_max_bytes: 20971520, max_files: 5 };
      else if (path === '/api/v1/auth/me') data = { id: 1, username: 'tester', role: 'user' };
      else if (path === '/api/v1/agents/11') data = { id: 11, name: '施工审查助手', enabled: true, default_model_id: 4 };
      else if (path === '/api/v1/agents/11/sessions') data = { items: [session], total: 1, page: 1, page_size: 20 };
      else if (path.endsWith('/activity/models')) data = [];
      else if (path.endsWith('/activity/metrics')) data = {
        run_count: 109, active_run_count: finished ? 0 : 1, elapsed_ms: finished ? 227000 : 153550,
        total_tokens: finished ? 107500 : 89266, missing_duration_runs: 1, calculated_at: new Date().toISOString(),
      };
      else if (path.endsWith('/sessions/activity')) data = session;
      else if (path.endsWith('/activity/messages')) data = [
        { id: 1, run_id: 'run-live', role: 'user', content: '请检查施工方案并生成报告。', create_time: origin, attachments: [], artifacts: [] },
        ...(finished ? [{ id: 2, run_id: 'run-live', role: 'assistant', content: answer, reasoning_content: reasoning, metrics, calls, create_time: origin, attachments: [], artifacts: [artifact] }] : []),
      ].filter(row => row.id > Number(new URL(route.request().url()).searchParams.get('after') || 0));
      else if (path.endsWith('/activity/runs')) data = { items: [run], total: 1, page: 1, page_size: 20 };
      else if (path.endsWith('/run-live/calls')) data = calls;
      else if (path.endsWith('/run-live/snapshot')) {
        const after = Number(new URL(route.request().url()).searchParams.get('after') || 0);
        data = { run, events: rows.filter(row => row.sequence > after), next_after: rows.at(-1)?.sequence || after, latest_sequence: rows.at(-1)?.sequence || 0, has_more: false };
      } else if (path.endsWith('/run-live/stream-ticket')) data = { expires_in: 300 };
      else if (path.endsWith('/run-live/stream')) return route.fulfill({ status: 307, headers: { location: streamUrl, 'Access-Control-Allow-Origin': '*' } });
      else if (path.endsWith('/run-live')) data = run;
      else if (path.endsWith('/knowledge-retrievals')) data = [];
      else { unexpected.push(path); return route.abort(); }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, message: '操作成功', data }) });
    });
    await page.goto(`${base}/agents/11/sessions/activity`);
    await page.locator('[data-stream-run="run-live"]').waitFor();
    await page.waitForFunction(() => document.querySelector('[data-stream-run] .run-call') !== null);
    const deadline = Date.now() + 10000;
    while (!sockets.size && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 30));
    check(sockets.size > 0, '实际 SSE 连接建立');
    const send = row => { rows.push(row); for (const socket of sockets) socket.write(event(row)); };
    send({ sequence: 1, event_type: 'reasoning_delta', state: 'running', content: reasoning });
    await page.getByRole('button', { name: /正在思考/ }).waitFor();
    const reasoningButton = page.getByRole('button', { name: /正在思考/ });
    check(await reasoningButton.getAttribute('aria-expanded') === 'true', '实际思考流默认展开');
    await page.waitForFunction(text => document.querySelector('.reasoning-content')?.textContent === text, reasoning);
    await reasoningButton.click();
    check(await page.locator('.reasoning-content').isHidden(), '思考流可手动收起');
    await reasoningButton.focus();
    await page.keyboard.press('Enter');
    check(await page.locator('.reasoning-content').isVisible(), '键盘可展开思考');
    // 同一事件重放不会重复拼接；正文按真实网络块逐次传入。
    for (const socket of sockets) socket.write(event(rows[0]));
    send({ sequence: 2, event_type: 'text_delta', state: 'running', content: answer.slice(0, 900) });
    await page.waitForFunction(() => {
      const text = document.querySelector('[data-stream-run] .message-markdown')?.textContent || '';
      return text.length > 5 && text.length < 850;
    }, null, { timeout: 5000 }).catch(async error => { console.log(await page.evaluate(() => ({ hidden: document.hidden, reduced: matchMedia('(prefers-reduced-motion: reduce)').matches, text: document.querySelector('[data-stream-run] .message-markdown')?.textContent?.length, errors: document.body.innerText.slice(-1200) }))); throw error; });
    check(true, '大块正文分帧显示，未一次性跳出');
    send({ sequence: 3, event_type: 'text_delta', state: 'running', content: answer.slice(900) });
    // 在缓冲尚未追平时结束，检查最终消息接续与统计。
    finished = true;
    run = { ...run, state: 'completed', reported_total_tokens: metrics.total_tokens };
    send({ sequence: 4, event_type: 'state', state: 'completed', content: null });
    const final = page.locator('[data-message-id="2"]');
    await final.waitFor();
    await final.locator('.message-streaming[aria-busy="false"]').waitFor();
    check((await final.locator('.message-markdown').innerText()).includes('👨‍👩‍👧‍👦 完成'), '终态接续完整正文与组合字符');
    check(await final.locator('.run-metrics').innerText().then(text => text.includes('1 分 13 秒') && text.includes('18,234 Token')), '本轮耗时和累计 Token');
    check((await final.locator('.session-file').innerText()).includes('33.5 KB'), '产物显示字节换算大小');
    check((await final.locator('.run-calls').innerText()).includes('MCP'), 'MCP 独立类型');
    const failedCall = final.locator('[data-call-id="tool"]');
    check(await failedCall.locator('.run-call-explanation').isHidden(), '失败详情默认收起');
    await failedCall.locator('summary').focus();
    await page.keyboard.press('Enter');
    check((await failedCall.locator('.run-call-explanation').innerText()).includes('超过单次执行时限'), '键盘展开失败原因');
    check((await failedCall.locator('.run-call-error-code').innerText()).includes('tool_timeout'), '显示脱敏原因码');
    await failedCall.locator('summary').click();
    check(await failedCall.locator('.run-call-explanation').isHidden(), '失败原因可再次收起');
    const interrupted = final.locator('[data-call-id="unknown"]');
    await interrupted.locator('summary').click();
    check((await interrupted.locator('.run-call-explanation').innerText()).includes('无法确认执行结果'), '中断说明保留未知结果');
    const totals = page.getByLabel('整个会话统计');
    await page.waitForFunction(() => document.querySelector('.session-totals')?.textContent.includes('107,500 Token'));
    check((await totals.innerText()).includes('109 轮'), '总统计覆盖全会话而非当前消息数');
    check((await totals.innerText()).includes('至少 3 分 47 秒'), '总耗时明确历史缺失范围');
    check((await totals.innerText()).includes('1 轮历史耗时未知'), '提示历史未知时长');
    check((await final.locator('.run-calls').innerText()).includes('2.3 秒'), '单次调用耗时');
    check((await final.locator('.run-calls').innerText()).includes('耗时未知'), '历史缺少时间不伪造');
    check(await final.getByRole('button', { name: /思考过程/ }).getAttribute('aria-expanded') === 'false', '完成后思考默认折叠');
    await final.getByRole('button', { name: /思考过程/ }).click();
    check((await final.locator('.reasoning-content').innerText()) === reasoning, '历史思考没有重复事件');
    await page.reload();
    await page.locator('[data-message-id="2"]').waitFor();
    check(await page.getByRole('button', { name: /思考过程/ }).getAttribute('aria-expanded') === 'false', '刷新恢复可折叠思考');
    await page.waitForFunction(() => document.querySelector('.session-totals')?.textContent.includes('107,500 Token'));
    check((await page.getByLabel('整个会话统计').innerText()).includes('109 轮'), '刷新恢复整个会话统计');
    await page.locator('[data-call-id="tool"] summary').click();
    check((await page.locator('[data-call-id="tool"] .run-call-explanation').innerText()).includes('超过单次执行时限'), '刷新恢复失败详情');
    await page.getByRole('button', { name: /思考过程/ }).click();
    check((await page.locator('.reasoning-content').innerText()) === reasoning, '刷新恢复完整思考文本');
    const sizes = await page.locator('.reasoning-content').evaluate(node => ({ thought: getComputedStyle(node).fontFamily, body: getComputedStyle(document.querySelector('.message-markdown')).fontFamily }));
    check(sizes.thought !== sizes.body, '思考与正文字体不同');
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px 无页面横向溢出`);
    await page.locator('[data-call-id="tool"]').scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath(`y-point-chat-failure-${width}.png`), fullPage: true });
    await page.screenshot({ path: screenshotPath(`y-point-chat-activity-${width}.png`), fullPage: true });
    await page.locator('.sessions-history').evaluate(node => { node.scrollTop = node.scrollHeight; });
    await page.screenshot({ path: screenshotPath(`y-point-chat-metrics-${width}.png`), fullPage: true });
    check(errors.length === 0, `页面无异常：${errors.join(', ')}`);
    check(unexpected.length === 0, `接口均有契约：${unexpected.join(', ')}`);
    for (const socket of sockets) socket.end();
    sockets.clear();
    await context.close();
  }
  const root = dirname(dirname(fileURLToPath(import.meta.url)));
  const directory = await mkdtemp(join(root, '.verify-chat-motion-'));
  try {
    await writeFile(join(directory, 'index.html'), `<!doctype html><html><body><div id="root"></div><script type="module">
      import React from 'react';
      import { createRoot } from 'react-dom/client';
      import StreamingMessage from '/src/components/StreamingMessage.tsx';
      function Harness() {
        const [text, setText] = React.useState('开始 👨‍👩‍👧‍👦');
        const [mounted, setMounted] = React.useState(true);
        return React.createElement(React.Fragment, null,
          React.createElement('button', { onClick: () => setText('流式文本'.repeat(400) + ' 👨‍👩‍👧‍👦') }, '批量文本'),
          React.createElement('button', { onClick: () => setText('替换后的内容') }, '替换文本'),
          React.createElement('button', { onClick: () => setMounted(false) }, '卸载'),
          mounted && React.createElement(StreamingMessage, { text, runId: 'motion', active: false }));
      }
      createRoot(document.getElementById('root')).render(React.createElement(React.StrictMode, null, React.createElement(Harness)));
    </script></body></html>`);
    const page = await browser.newPage({ reducedMotion: 'no-preference' });
    await page.routeWebSocket('**', socket => socket.close());
    await page.addInitScript(() => {
      const schedule = requestAnimationFrame;
      window.rafCount = 0;
      window.requestAnimationFrame = callback => { window.rafCount++; return schedule(callback); };
    });
    await page.goto(`${base}/${directory.split('/').at(-1)}/index.html`);
    await page.locator('.message-streaming[aria-busy="false"]').waitFor();
    check((await page.locator('.message-markdown').innerText()) === '开始 👨‍👩‍👧‍👦', 'StrictMode 首次非空文本完成显示');
    await page.getByRole('button', { name: '批量文本' }).click();
    await page.waitForFunction(() => {
      const text = document.querySelector('.message-markdown').textContent;
      return text.length > 10 && text.length < 1500;
    });
    check(true, '连续帧中存在中间展示状态');
    await page.locator('.message-streaming[aria-busy="false"]').waitFor();
    check((await page.locator('.message-markdown').innerText()).endsWith('👨‍👩‍👧‍👦'), '组合字符完整');
    const before = await page.evaluate(() => window.rafCount);
    await page.waitForTimeout(100);
    check(await page.evaluate(() => window.rafCount) === before, '缓冲清空后停止动画帧');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.getByRole('button', { name: '替换文本' }).click();
    await page.waitForFunction(() => document.querySelector('.message-markdown')?.textContent === '替换后的内容');
    check((await page.locator('.message-markdown').innerText()) === '替换后的内容', '减少动态效果时直接显示且替换不残留');
    await page.getByRole('button', { name: '卸载' }).click();
    check(await page.locator('.message-streaming').count() === 0, '卸载停止展示');
    await page.close();
    await writeFile(join(directory, 'totals.html'), `<!doctype html><html><body><div id="root"></div><script type="module">
      import React from 'react';
      import { createRoot } from 'react-dom/client';
      import SessionMetrics from '/src/components/SessionMetrics.tsx';
      function Harness() {
        const [session, setSession] = React.useState('a');
        const [revision, setRevision] = React.useState(0);
        const [active, setActive] = React.useState(true);
        return React.createElement(React.Fragment, null,
          React.createElement('button', { onClick: () => setRevision(value => value + 1) }, '刷新统计'),
          React.createElement('button', { onClick: () => { setSession('b'); setActive(false); } }, '切换会话'),
          React.createElement(SessionMetrics, { key: session, agentId: 11, sessionId: session, refreshKey: revision, active }));
      }
      createRoot(document.getElementById('root')).render(React.createElement(React.StrictMode, null, React.createElement(Harness)));
    </script></body></html>`);
    const totalsPage = await browser.newPage();
    await totalsPage.routeWebSocket('**', socket => socket.close());
    let mode = 'ok', requests = 0, started, release;
    const held = new Promise(resolve => { started = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    await totalsPage.route('**/api/v1/**', async route => {
      const session = new URL(route.request().url()).pathname.split('/').at(-2);
      const currentMode = mode;
      requests++;
      if (session === 'a' && currentMode === 'hold') { started(); await gate; }
      const status = currentMode === 'error' ? 503 : currentMode === 'denied' ? 403 : 200;
      const data = { run_count: session === 'b' ? 0 : 109, active_run_count: session === 'b' ? 0 : 1,
        elapsed_ms: session === 'b' ? 0 : 65000, total_tokens: session === 'b' ? 0 : currentMode === 'unknown' ? null : 1234,
        missing_duration_runs: 0, calculated_at: new Date().toISOString() };
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, data }) }).catch(() => {});
    });
    await totalsPage.goto(`${base}/${directory.split('/').at(-1)}/totals.html`);
    await totalsPage.waitForFunction(() => document.body.textContent.includes('1,234 Token'));
    await totalsPage.waitForFunction(() => document.querySelector('[aria-label="整个会话统计"]').textContent.includes('1 分 6 秒'));
    check((await totalsPage.getByLabel('整个会话统计').innerText()).includes('计时中'), '会话累计支持活动计时');
    mode = 'error';
    await totalsPage.getByRole('button', { name: '刷新统计' }).click();
    await totalsPage.getByRole('status').waitFor();
    check((await totalsPage.getByRole('status').innerText()).includes('显示上次统计'), '同步失败明确标记上次统计');
    check((await totalsPage.getByLabel('整个会话统计').innerText()).includes('1,234 Token'), '临时错误保留历史统计');
    mode = 'ok';
    await totalsPage.getByRole('button', { name: '重试', exact: true }).click();
    await totalsPage.getByRole('status').waitFor({ state: 'hidden' });
    check(true, '手动重试恢复统计');
    mode = 'denied';
    await totalsPage.getByRole('button', { name: '刷新统计' }).click();
    await totalsPage.waitForFunction(() => document.body.textContent.includes('会话统计不可访问'));
    check(!(await totalsPage.getByLabel('整个会话统计').innerText()).includes('1,234'), '权限失效清空统计');
    mode = 'unknown';
    await totalsPage.getByRole('button', { name: '重试', exact: true }).click();
    await totalsPage.waitForFunction(() => document.body.textContent.includes('Token 未报告'));
    check(true, '未知 Token 不伪造为零');
    mode = 'hold';
    await totalsPage.getByRole('button', { name: '刷新统计' }).click();
    await held;
    mode = 'ok';
    await totalsPage.getByRole('button', { name: '切换会话' }).click();
    await totalsPage.waitForFunction(() => document.body.textContent.includes('0 Token'));
    release();
    await totalsPage.waitForTimeout(200);
    const empty = await totalsPage.getByLabel('整个会话统计').innerText();
    check(empty.includes('0 轮') && empty.includes('0 Token') && !empty.includes('109'), '切换会话丢弃迟到结果，空会话正确显示零');
    check(!empty.includes('计时中') && requests >= 7, '空会话不继续计时');
    await totalsPage.close();
  } finally { await rm(directory, { recursive: true, force: true }); }
  console.log(`chat activity verified: ${assertions} assertions; screenshots /tmp/y-point-chat-activity-{1280,390}.png`);
} finally {
  for (const socket of sockets) socket.end();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  await browser.close();
}
