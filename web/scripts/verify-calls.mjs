import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
const now = '2026-11-04T12:00:00+08:00';
const call = (call_id, kind, name, status, sequence) => ({ call_id, kind, name, status, sequence });
const finalCalls = [
  call('skill-1', 'skill', 'travel-policy@v2', 'succeeded', 1),
  call('tool-1', 'tool', 'calculate_cost', 'failed', 2),
  call('tool-2', 'tool', 'calculate_cost', 'succeeded', 3),
  call('kb-1', 'knowledge', '员工手册（差旅与报销制度）、财务规则及审批说明'.repeat(2), 'succeeded', 4),
];
const historical = [call('old-skill', 'skill', 'report-helper@v1', 'succeeded', 1), call('old-unknown', 'tool', 'remote_write', 'interrupted', 3)];
const message = (id, run_id, role, content, calls = []) => ({ id, run_id, role, content, calls, create_time: now, attachments: [], artifacts: [] });
let assertions = 0;
async function fixture(page) {
  await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
  const session = { id: 'calls', agent_id: 11, title: '调用记录验证', archived: false, version: 1, create_time: now, update_time: now };
  let run = { id: 'run-live', session_id: 'calls', agent_id: 11, state: 'running', failure_code: null, create_time: now, update_time: now };
  let phase = 'running', releaseStream, complete = false, fail = false, reads = 0, streamOpened;
  const gate = new Promise(resolve => { releaseStream = resolve; });
  const opened = new Promise(resolve => { streamOpened = resolve; });
  const paths = [], unexpected = [];
  const events = finalCalls.map((row, index) => ({ sequence: index + 1, event_type: row.status === 'failed' ? 'tool_failed' : 'tool_completed', state: 'running', content: null, tool_call_id: row.call_id, tool_name: row.kind === 'skill' ? `skill:${row.name}` : row.kind === 'knowledge' ? 'search_knowledge_base' : `user_tool_1_${row.name}` }));
  const textEvent = { sequence: 5, event_type: 'text_delta', state: 'running', content: '已完成本轮处理，工具失败不会掩盖其他成功调用。' };
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname; paths.push(path);
    let data;
    if (path === '/api/v1/auth/me') data = { id: 1, username: 'tester', role: 'user' };
    else if (path === '/api/v1/agents/11') data = { id: 11, name: '差旅助手', enabled: true, default_model_id: 1 };
    else if (path === '/api/v1/agents/11/sessions') data = { items: [session], total: 1, page: 1, page_size: 20 };
    else if (path.endsWith('/sessions/calls')) data = session;
    else if (path.endsWith('/calls/messages')) data = [
      message(1, 'run-old', 'user', '历史问题', historical),
      message(2, 'run-old', 'assistant', '这是历史回答。', historical),
      message(3, 'run-failed', 'user', '这个轮次没有最终回复', [call('failed-old', 'tool', 'weather_lookup', 'failed', 1)]),
      message(4, 'run-plain', 'user', '普通问题'),
      message(5, 'run-plain', 'assistant', '没有调用资源的回答。'),
      message(6, 'run-live', 'user', '请检查费用并检索差旅制度'),
      ...(complete ? [message(7, 'run-live', 'assistant', textEvent.content, finalCalls)] : []),
    ].filter(item => item.id > Number(url.searchParams.get('after') || 0));
    else if (path.endsWith('/calls/runs')) data = { items: [run], total: 1, page: 1, page_size: 20 };
    else if (path === '/api/v1/runs/run-live/calls') {
      reads++;
      if (fail) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ code: 500, message: '暂不可用' }) });
      data = phase === 'running' ? finalCalls.map(row => ({ ...row, status: 'running' })) : finalCalls;
    } else if (path === '/api/v1/runs/run-live/snapshot') {
      const all = phase === 'running' ? [] : [...events, textEvent];
      const after = Number(url.searchParams.get('after') || 0);
      data = { run, events: all.filter(row => row.sequence > after), latest_sequence: all.at(-1)?.sequence || 0, next_after: all.at(-1)?.sequence || 0, has_more: false };
    } else if (path === '/api/v1/runs/run-live/stream-ticket') data = { expires_in: 300 };
    else if (path === '/api/v1/runs/run-live/stream') {
      streamOpened(); await gate;
      return route.fulfill({ contentType: 'text/event-stream', body: [...events, events[2], textEvent].map(event => `id: run-live:${event.sequence}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`).join('') });
    } else if (path === '/api/v1/runs/run-live') data = run;
    else { unexpected.push(path); return route.abort(); }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, message: '操作成功', data }) });
  });
  return { paths, unexpected, opened, get reads() { return reads; }, eventsFail() { phase = 'finished'; fail = true; releaseStream(); }, restore() { fail = false; }, complete() { complete = true; run = { ...run, state: 'completed' }; } };
}
try {
  for (const [layout, width, height] of [['desktop', 1280, 900], ['mobile', 375, 812]]) {
    const context = await browser.newContext({ viewport: { width, height } });
    const page = await context.newPage(), errors = []; page.on('pageerror', error => errors.push(error.message));
    const f = await fixture(page);
    await page.goto(`${base}/agents/11/sessions/calls`);
    const history = page.locator('[data-message-id="2"]');
    await history.getByRole('region', { name: '本轮调用' }).waitFor();
    assert.equal(await history.locator('.run-call').count(), 2);
    assert.ok((await history.locator('[data-call-id="old-skill"]').innerText()).includes('成功'));
    assert.ok((await history.locator('[data-call-id="old-unknown"]').innerText()).includes('结果未确认')); assertions += 3;
    assert.equal(await page.locator('[data-message-id="1"] .run-calls').count(), 0);
    assert.equal(await page.locator('[data-message-id="5"] .run-calls').count(), 0);
    assert.ok((await page.locator('[data-call-run="run-failed"]').innerText()).includes('weather_lookup'));
    assert.ok((await page.locator('[data-call-run="run-failed"]').innerText()).includes('失败')); assertions += 4;
    assert.ok(await history.locator('.run-calls').evaluate(el => el.compareDocumentPosition(el.parentElement.querySelector('.session-message-content')) & Node.DOCUMENT_POSITION_FOLLOWING)); assertions++;
    const live = page.locator('[data-stream-run="run-live"]');
    await live.locator('.run-call-running').nth(3).waitFor();
    assert.equal(await live.locator('.run-call').count(), 4);
    assert.equal(await live.locator('.message-streaming').count(), 0);
    assert.equal(f.paths.some(path => /runs\/(run-old|run-failed|run-plain)\/calls/.test(path)), false); assertions += 3;
    await f.opened; const reads = f.reads;
    f.eventsFail();
    await live.getByRole('status').filter({ hasText: '调用状态暂时无法同步' }).waitFor();
    assert.ok(f.reads > reads);
    assert.equal(await live.locator('.run-call-failed').count(), 0); assertions += 2;
    f.restore(); await live.getByRole('button', { name: /重\s*试/, exact: true }).click();
    await live.locator('[data-call-id="tool-1"] .run-call-failed').waitFor();
    assert.equal(await live.locator('.run-call-succeeded').count(), 3);
    assert.equal(await live.locator('.run-call-failed').count(), 1);
    assert.equal(await live.locator('.run-call').count(), 4); assertions += 3;
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); assertions++;
    await live.scrollIntoViewIfNeeded(); await page.mouse.move(0, 0);
    await page.screenshot({ path: join(tmpdir(), `y-point-run-calls-${layout}.png`), animations: 'disabled' });
    f.complete();
    await page.locator('[data-message-id="7"]').waitFor({ timeout: 12000 });
    const final = page.locator('[data-message-id="7"]');
    assert.equal(await final.locator('.run-call').count(), 4);
    assert.equal(await page.locator('[data-stream-run="run-live"]').count(), 0);
    assert.equal(await final.locator('.run-call-failed').count(), 1); assertions += 3;
    await page.reload(); await final.waitFor();
    assert.equal(await final.locator('.run-call').count(), 4);
    assert.equal(await final.locator('.run-call-failed').count(), 1);
    assert.equal(await page.locator('[data-call-run="run-failed"]').count(), 1);
    assert.equal(await page.locator('[data-call-id="old-skill"]').count(), 1);
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []); assertions += 6;
    await context.close(); console.log(`${layout}: 历史与实时调用、逐次结果、失败轮次、错误重试、最终消息与刷新恢复通过`);
  }
  console.log(`调用状态浏览器契约回归通过：${assertions} 项断言；使用虚构接口响应，不调用真实模型和工具。`);
} finally { await browser.close(); }
