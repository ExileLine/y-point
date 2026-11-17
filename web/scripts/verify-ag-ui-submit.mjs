import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { chromium } from 'playwright';
import { agUiData, agUiFrames, agUiSnapshot, agUiSse } from './ag-ui-fixtures.mjs';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let state;
const server = createServer(async (req, res) => {
  const cors = { 'Access-Control-Allow-Origin': new URL(base).origin,
    'Access-Control-Allow-Headers': 'authorization,content-type,accept',
    'Access-Control-Allow-Methods': 'POST,OPTIONS',
    'Access-Control-Expose-Headers': 'X-Y-Point-Run-ID,X-Y-Point-After' };
  if (req.method === 'OPTIONS') { res.writeHead(204, cors); res.end(); return; }
  let text = '';
  for await (const chunk of req) text += chunk;
  const body = JSON.parse(text);
  state.submitted.push(body);
  assert.deepEqual(body.messages.slice(0, -1), state.history.messages);
  assert.equal(body.messages.at(-1).role, 'user');
  assert.ok(body.messages.at(-1).id && body.runId);
  assert.equal(body.protocolVersion, '1.0');
  assert.equal(body.forwardedProps.expected_session_version, 1);
  assert.equal(body.forwardedProps.model_id, 4);
  assert.deepEqual(body.tools, []);
  assert.deepEqual(body.context, []);
  const user = body.messages.at(-1);
  state.history.messages.push(user);
  state.history.messageDetails[user.id] = { id: 3, run_id: state.run.id, create_time: state.now, attachments: [], artifacts: [] };
  state.run.state = 'running';
  res.writeHead(200, { ...cors, 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
    'X-Y-Point-Run-ID': state.run.id, 'X-Y-Point-After': '0' });
  res.flushHeaders();
  const events = [
    { type: 'RUN_STARTED', threadId: 'submit', runId: body.runId, protocolVersion: '1.0' },
    { type: 'REASONING_START', messageId: 'reason' },
    { type: 'REASONING_MESSAGE_START', messageId: 'reason', role: 'reasoning' },
    { type: 'REASONING_MESSAGE_CONTENT', messageId: 'reason', delta: '先核对资料。' },
    { type: 'REASONING_MESSAGE_END', messageId: 'reason' },
    { type: 'REASONING_END', messageId: 'reason' },
    { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' },
    { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '标准 POST 的中文流式回答。' },
  ];
  const send = event => {
    const frame = { sequence: state.frames.length + 1, event };
    state.frames.push(frame);
    // Split the wire across byte boundaries to exercise incremental UTF-8/SSE decoding.
    const bytes = Buffer.from(agUiSse(state.run.id, [frame]).replaceAll('\n', '\r\n'));
    res.write(bytes.subarray(0, bytes.length - 5));
    res.write(bytes.subarray(bytes.length - 5));
  };
  res.write(': keepalive\r\n\r\n');
  events.forEach(send);
  await state.finish;
  send({ type: 'TEXT_MESSAGE_END', messageId: 'answer' });
  send({ type: 'CUSTOM', name: 'y-point.answer', value: { version: 1, messageId: 'answer' } });
  state.run.state = 'completed';
  state.history.messages.push({ id: 'answer', role: 'assistant', content: '标准 POST 的中文流式回答。' });
  state.history.messageDetails.answer = { id: 4, run_id: state.run.id, create_time: state.now, attachments: [], artifacts: [] };
  send({ type: 'RUN_FINISHED', threadId: 'submit', runId: body.runId, outcome: { type: 'success' } });
  res.end();
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 920 }, reducedMotion: 'reduce' });
    const page = await context.newPage();
    const errors = [], calls = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
    const now = new Date().toISOString();
    let finish;
    const session = { id: 'submit', agent_id: 11, title: '标准提交验证', model_id: 4, archived: false, version: 1, create_time: now, update_time: now };
    state = { now, frames: [], submitted: [], finish: new Promise(resolve => { finish = resolve; }),
      run: { id: 'run-post', session_id: 'submit', agent_id: 11, model_id: 4, state: 'queued', failure_code: null, create_time: now, update_time: now },
      history: agUiData('/api/v1/agents/11/ag-ui/threads/submit', [
        { id: 1, run_id: 'old', role: 'user', content: '此前的问题', create_time: now, attachments: [], artifacts: [] },
        { id: 2, run_id: 'old', role: 'assistant', content: '此前的回答', create_time: now, attachments: [], artifacts: [] },
      ]),
    };
    state.history.messages.push({ id: 'old-activity', role: 'activity', activityType: 'y-point.run', content: { version: 1, state: 'completed' } });
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      calls.push({ path, method: request.method() });
      const send = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, data }) });
      if (path.endsWith('/auth/me')) return send({ id: 1, username: 'tester', role: 'user' });
      if (path === '/api/v1/agents/11') return send({ id: 11, name: '协议助手', enabled: true, default_model_id: 4 });
      if (path.endsWith('/runtime-limits')) return send({ upload_max_bytes: 10485760, context_max_bytes: 10485760, max_files: 5 });
      if (path.endsWith('/sessions')) return send({ items: [session], total: 1, page: 1, page_size: 20 });
      if (path.endsWith('/sessions/submit')) return send(session);
      if (path.endsWith('/models')) return send([]);
      if (path.endsWith('/metrics')) return send({ run_count: 1, active_run_count: 0, elapsed_ms: 0, total_tokens: null, missing_duration_runs: 0, calculated_at: now });
      if (path.endsWith('/submit/runs')) return send({ items: [], total: 0, page: 1, page_size: 20 });
      if (path.endsWith('/ag-ui/threads/submit')) return send(state.history);
      if (path.endsWith('/agents/11/ag-ui') && request.method() === 'POST') return route.fulfill({ status: 307, headers: { location: `http://127.0.0.1:${server.address().port}/run` } });
      if (path === '/api/v1/runs/run-post') return send(state.run);
      if (path.endsWith('/knowledge-retrievals')) return send([]);
      if (path.endsWith('/ag-ui/snapshot')) {
        const runId = path.split('/')[4];
        const run = runId === state.run.id ? state.run : { id: runId, session_id: session.id, state: 'completed' };
        return send(agUiSnapshot(run, runId === state.run.id ? state.frames : agUiFrames(run, [])));
      }
      throw new Error(`Unexpected request: ${request.method()} ${path}`);
    });
    await page.goto(`${base}/agents/11/sessions/submit`);
    await page.getByText('此前的回答', { exact: true }).waitFor();
    await page.getByRole('textbox', { name: '消息内容' }).fill('请使用规范接口发送');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByText('标准 POST 的中文流式回答。', { exact: true }).waitFor();
    assert.equal(state.submitted.length, 1);
    assert.ok(!calls.some(call => /\/runs\/run-post\/(ag-ui\/snapshot|stream|stream-ticket)$/.test(call.path)), 'new run consumes POST without a duplicate GET subscription');
    assert.ok(!calls.some(call => /\/sessions\/[^/]+\/messages$/.test(call.path) || call.method === 'POST' && /\/sessions\/[^/]+\/runs$/.test(call.path)));
    finish();
    await page.locator('[data-message-id="4"]').waitFor();
    assert.equal(await page.locator('[data-message-id="4"] .session-message-content').innerText(), '标准 POST 的中文流式回答。');
    assert.deepEqual(errors, []);
    console.log(`${width}: full protocol history, stable input IDs, real POST SSE and no duplicate GET observer passed`);
    await context.close();
  }
} finally {
  await browser.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
