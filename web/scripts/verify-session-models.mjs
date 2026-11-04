import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const now = '2026-11-04T12:00:00+08:00';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let assertions = 0;
const options = [
  { id: 1, display_name: 'Model A', provider_name: 'Provider A', verified_capabilities: ['text', 'tools'], context_window: 32768, selectable: true, reason: null, notice: null },
  { id: 2, display_name: 'Model B', provider_name: 'Provider B', verified_capabilities: ['text', 'tools'], context_window: 32768, selectable: true, reason: null, notice: null },
  { id: 3, display_name: 'Model Disabled', provider_name: 'Provider C', verified_capabilities: ['text'], context_window: 2048, selectable: false, reason: 'model_context_insufficient', notice: '模型上下文窗口无法容纳输出上限和最低输入空间' },
];
async function fixture(page) {
  await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
  let session = { id: 'models', agent_id: 11, thread_id: 'thread-a', model_id: 1, title: '模型切换验收', archived: false, version: 1, create_time: now, update_time: now };
  let run = null, busyConflict = false, modelError = false;
  const calls = [], unexpected = [];
  const messages = Array.from({ length: 28 }, (_, index) => ({
    id: index + 1, run_id: `history-${Math.floor(index / 2)}`, role: index % 2 ? 'assistant' : 'user',
    content: `第 ${index + 1} 条对话，已完成的业务结论。`.repeat(10), model_id: 1, model_name: 'Model A', provider_name: 'Provider A',
    create_time: now, attachments: [], artifacts: [], calls: [],
  }));
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname, method = req.method();
    const body = req.postData() ? req.postDataJSON() : null;
    calls.push({ path, method, body });
    const send = (data, status = 200, detail = '') => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status === 409 ? 10005 : status, message: detail || '操作成功', data }) });
    if (path === '/api/v1/auth/me') return send({ id: 1, username: 'tester', role: 'user' });
    if (path === '/api/v1/agents/11') return send({ id: 11, name: '模型切换助手', enabled: true, default_model_id: 1 });
    if (path === '/api/v1/agents/11/sessions') return send({ items: [session], total: 1, page: 1, page_size: 20 });
    if (path.endsWith('/sessions/models/models')) return modelError ? send(null, 503, '目录暂不可用') : send(options);
    if (path.endsWith('/sessions/models') && method === 'GET') return send(session);
    if (path.endsWith('/sessions/models') && method === 'PUT') {
      if (busyConflict) {
        run = { id: 'busy-run', session_id: 'models', agent_id: 11, model_id: 1, state: 'waiting_for_approval', failure_code: null, create_time: now, update_time: now };
        return send({ reason: 'session_busy' }, 409, '会话仍有未结束的运行，请等待本轮结束后操作');
      }
      assert.equal(body.expected_version, session.version); assertions++;
      session = { ...session, model_id: body.model_id, thread_id: `thread-${body.model_id}`, version: session.version + 1 };
      return send(session);
    }
    if (path.endsWith('/models/messages')) return send(messages.filter(item => item.id > Number(url.searchParams.get('after') || 0)));
    if (path.endsWith('/models/runs') && method === 'GET') return send({ items: run ? [run] : [], total: run ? 1 : 0, page: 1, page_size: 20 });
    if (path.endsWith('/models/runs') && method === 'POST') {
      run = { id: 'new-run', session_id: 'models', agent_id: 11, model_id: session.model_id, state: 'queued', failure_code: null, create_time: now, update_time: now };
      messages.push({ id: 29, run_id: run.id, role: 'user', content: body.input_text, create_time: now, attachments: [], artifacts: [] });
      session = { ...session, version: session.version + 1 };
      return send(run, 201);
    }
    if (path.endsWith('/calls')) return send([]);
    if (path.endsWith('/snapshot')) return send({ run, events: [], latest_sequence: 0, next_after: 0, has_more: false });
    if (path.endsWith('/stream-ticket')) return send({ expires_in: 300 });
    if (path.endsWith('/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': waiting\n\n' });
    if (/^\/api\/v1\/runs\/[^/]+$/.test(path)) return send(run);
    unexpected.push(`${method} ${path}`); return route.abort();
  });
  return { calls, unexpected, failure(code) {
    run = { id: 'failed-run', session_id: 'models', agent_id: 11, model_id: 1, state: code === 'model_error' ? 'failed' : 'timed_out', failure_code: code, create_time: now, update_time: now };
  }, conflict() { busyConflict = true; }, failModels() { modelError = true; }, restoreModels() { modelError = false; } };
}
async function chooseB(page) {
  await page.locator('.sessions-model-select .ant-select-selector').click();
  await page.locator('.ant-select-item-option').filter({ hasText: 'Model B' }).click();
  await page.getByRole('dialog', { name: '切换当前对话模型' }).waitFor();
}
try {
  for (const [layout, width, height] of [['desktop', 1440, 900], ['mobile', 375, 812]]) {
    const context = await browser.newContext({ viewport: { width, height } });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const f = await fixture(page);
    await page.goto(`${base}/agents/11/sessions/models`);
    await page.locator('[data-message-id="28"]').waitFor();
    const picker = page.getByRole('combobox', { name: '当前对话模型' });
    await page.waitForFunction(() => !document.querySelector('#session-model')?.disabled);
    await page.getByRole('textbox', { name: '消息内容' }).fill('需要保留的草稿');
    await page.locator('.sessions-model-select .ant-select-selector').click();
    await page.locator('.ant-select-item-option').filter({ hasText: 'Model Disabled' }).waitFor();
    assert.ok((await page.locator('.ant-select-item-option').filter({ hasText: 'Model Disabled' }).getAttribute('class')).includes('disabled')); assertions++;
    await page.keyboard.press('Escape');
    const history = page.locator('.sessions-history');
    await history.evaluate(element => { element.scrollTop = 200; });
    await page.locator('[data-message-id="1"]').evaluate(element => element.dataset.retained = 'yes');
    await chooseB(page);
    assert.match(await page.getByRole('dialog').innerText(), /历史文字会发送给目标供应商/); assertions++;
    await page.getByRole('dialog').getByRole('button', { name: /取\s*消/ }).hover();
    await page.screenshot({ path: `/tmp/y-point-session-model-${layout}.png`, animations: "disabled" });
    await page.getByRole('button', { name: /取\s*消/ }).click();
    assert.equal(f.calls.filter(call => call.method === 'PUT').length, 0); assertions++;
    await chooseB(page);
    await page.getByRole('button', { name: '切换模型', exact: true }).click();
    await page.getByText('模型已切换，从下一条消息开始使用', { exact: true }).waitFor();
    assert.equal(await page.getByRole('textbox', { name: '消息内容' }).inputValue(), '需要保留的草稿'); assertions++;
    assert.equal(await page.locator('[data-message-id="1"]').getAttribute('data-retained'), 'yes'); assertions++;
    assert.ok(Math.abs(await history.evaluate(element => element.scrollTop) - 200) < 3); assertions++;
    assert.equal(f.calls.some(call => call.path === '/api/v1/agents/11' && call.method === 'PUT'), false); assertions++;
    assert.equal(await page.locator('[data-message-id="28"] .session-message-model').innerText(), 'Model A'); assertions++;
    await page.reload();
    await page.locator('.sessions-model-select .ant-select-selection-item').filter({ hasText: 'Model B' }).waitFor();
    await page.getByRole('textbox', { name: '消息内容' }).fill('使用模型 B 继续');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.locator('[data-message-id="29"]').waitFor();
    const submitted = f.calls.findLast(call => call.method === 'POST' && call.path.endsWith('/models/runs'));
    assert.equal(submitted.body.model_id, 2); assert.equal(submitted.body.expected_session_version, 2); assertions += 2;
    assert.equal(await picker.isDisabled(), true); assertions++;
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); assertions++;
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []); assertions += 2;
    await context.close(); console.log(`${layout}: 选择、确认、取消、草稿、滚动、刷新恢复、模型标签和活动运行限制通过`);
  }
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 850 } });
    const f = await fixture(page);
    await page.goto(`${base}/agents/11/sessions/models`);
    await page.locator('[data-message-id="28"]').waitFor();
    await page.getByRole('textbox', { name: '消息内容' }).fill('并发冲突也保留草稿');
    await chooseB(page); f.conflict();
    await page.getByRole('button', { name: '切换模型', exact: true }).click();
    await page.getByText('会话仍有未结束的运行，请等待本轮结束后操作', { exact: true }).waitFor();
    assert.equal(await page.getByRole('textbox', { name: '消息内容' }).inputValue(), '并发冲突也保留草稿'); assertions++;
    await page.waitForFunction(() => document.querySelector('#session-model')?.disabled);
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.getByRole('dialog').count(), 0); assertions++;
    assert.deepEqual(f.unexpected, []); assertions++;
    await page.close();
  }
  {
    const page = await browser.newPage();
    const f = await fixture(page); f.failModels();
    await page.goto(`${base}/agents/11/sessions/models`);
    await page.getByRole('button', { name: '重试模型目录' }).waitFor();
    f.restoreModels();
    await page.getByRole('button', { name: '重试模型目录' }).click();
    await page.waitForFunction(() => !document.querySelector('#session-model')?.disabled);
    await chooseB(page);
    assert.equal(await page.getByRole('dialog').count(), 1); assertions++;
    assert.deepEqual(f.unexpected, []); assertions++;
    await page.close();
  }
  for (const [code, label] of [
    ['provider_timeout', '模型供应商响应超时'], ['execution_timeout', '本轮已达到执行时限'], ['model_error', '模型请求失败'],
  ]) {
    const page = await browser.newPage();
    const f = await fixture(page); f.failure(code);
    await page.goto(`${base}/agents/11/sessions/models`);
    await page.locator('.sessions-run-status').filter({ hasText: label }).waitFor();
    await page.waitForFunction(() => !document.querySelector('#session-model')?.disabled);
    assert.equal(await page.getByRole('combobox', { name: '当前对话模型' }).isDisabled(), false); assertions++;
    await page.getByRole('textbox', { name: '消息内容' }).fill('失败后继续对话');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.locator('[data-message-id="29"]').waitFor();
    const submitted = f.calls.findLast(call => call.method === 'POST' && call.path.endsWith('/models/runs'));
    assert.equal(submitted.body.input_text, '失败后继续对话'); assertions++;
    assert.equal(submitted.body.model_id, 1); assertions++;
    assert.deepEqual(f.unexpected, []); assertions++;
    await page.close();
  }
  console.log(`会话模型浏览器契约回归通过：${assertions} 项断言；使用拦截响应，不调用真实模型。`);
} finally { await browser.close(); }
