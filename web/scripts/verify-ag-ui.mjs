import { emptySessionApprovalPolicy, agUiData, agUiPost, agUiFrames } from './ag-ui-fixtures.mjs';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { screenshotPath } from './screenshot-paths.mjs';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let checks = 0;
const check = (condition, label) => { assert.ok(condition, label); checks++; };
const delayUntil = async predicate => {
  const deadline = Date.now() + 10000;
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 30));
  assert.ok(predicate(), 'SSE connection established');
};
let frames = [], sockets = new Set();
const wire = frame => `id: run-live:${frame.sequence}\ndata: ${JSON.stringify(frame.event)}\n\n`;
const server = createServer((req, res) => {
  if (req.method === 'OPTIONS') { res.writeHead(204, { 'Access-Control-Allow-Origin': new URL(base).origin, 'Access-Control-Allow-Methods': 'POST, GET, OPTIONS', 'Access-Control-Allow-Headers': 'Authorization, Content-Type' }); res.end(); return; }
  res.writeHead(200, { 'X-Y-Point-Run-ID': 'run-live', 'X-Y-Point-After': new URL(req.url, 'http://localhost').searchParams.get('after') || '0', 'Access-Control-Expose-Headers': 'X-Y-Point-Run-ID, X-Y-Point-After', 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': new URL(base).origin, 'Cache-Control': 'no-cache' });
  res.flushHeaders();
  const after = Number(new URL(req.url, 'http://localhost').searchParams.get('after') || 0);
  for (const frame of frames) if (frame.sequence > after) res.write(wire(frame));
  sockets.add(res);
  req.on('close', () => sockets.delete(res));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const streamUrl = `http://127.0.0.1:${server.address().port}/stream`;
try {
  for (const width of [1280, 390]) {
    frames = []; sockets = new Set();
    const context = await browser.newContext({ viewport: { width, height: 920 }, reducedMotion: 'reduce' });
    const page = await context.newPage();
    const errors = [], unexpected = [], cursors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
    const origin = new Date().toISOString();
    const session = { id: 'activity', agent_id: 11, title: 'AG-UI 过程验证', archived: false, version: 1, model_id: 4, create_time: origin, update_time: origin };
    let run = { id: 'run-live', session_id: session.id, agent_id: 11, model_id: 4, state: 'running', failure_code: null, reported_total_tokens: 0, create_time: origin, update_time: origin };
    let approval = { id: 'approval-1', run_id: run.id, state: 'pending', actions: [{ name: '生成报告', arguments_hash: 'a'.repeat(64) }], expires_at: new Date(Date.now() + 3600000).toISOString(), decided_at: null };
    let completed = false;
    const answer = '## 审查结论\n\n已核对资料，报告已生成。';
    await page.route('**/api/v1/**', async route => {
      const url = new URL(route.request().url()), path = url.pathname;
      let data;
      if (/\/sessions\/[^/]+\/approval-policy$/.test(path)) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, data: emptySessionApprovalPolicy }) });
      if (path === '/api/v1/auth/me') data = { id: 1, username: 'tester', role: 'user' };
      else if (path === '/api/v1/agents/runtime-limits') data = { upload_max_bytes: 10485760, context_max_bytes: 20971520, max_files: 5 };
      else if (path === '/api/v1/agents/11') data = { id: 11, name: '审查助手', enabled: true, default_model_id: 4 };
      else if (path === '/api/v1/agents/11/sessions') data = { items: [session], total: 1, page: 1, page_size: 20 };
      else if (path.endsWith('/activity/models')) data = [];
      else if (path.endsWith('/activity/metrics')) data = { run_count: 1, active_run_count: completed ? 0 : 1, elapsed_ms: 1000, total_tokens: null, missing_duration_runs: 0, calculated_at: origin };
      else if (path.endsWith('/sessions/activity')) data = session;
      else if (path.endsWith('/activity/runs')) data = { items: [run], total: 1, page: 1, page_size: 20 };
      else if (path.endsWith('/ag-ui/threads/activity')) data = [
        { id: 1, run_id: run.id, role: 'user', content: '请审查并生成报告', attachments: [], artifacts: [], create_time: origin },
        ...(completed ? [{ id: 2, run_id: run.id, role: 'assistant', content: answer, attachments: [], artifacts: [], create_time: origin }] : []),
      ].filter(item => item.id > Number(url.searchParams.get('after') || 0));
      else if (path.endsWith('/run-live/ag-ui/snapshot')) {
        const after = Number(url.searchParams.get('after') || 0); cursors.push(after);
        data = { run, events: frames.filter(frame => frame.sequence > after), next_after: frames.length, latest_sequence: frames.length, through: frames.length, has_more: false };
      } else if (path.endsWith('/run-live/stream-ticket')) data = { expires_in: 300 };
      else if (path.endsWith('/run-live/stream')) return route.fulfill({ status: 307, headers: { location: `${streamUrl}?after=${url.searchParams.get('after') || 0}`, 'Access-Control-Allow-Origin': '*' } });
      else if (path.endsWith('/ag-ui') && route.request().method() === 'POST') {
        const body = route.request().postDataJSON();
        assert.equal(body.resume[0].interruptId, approval.id);
        assert.equal(body.resume[0].payload.decision, 'approve');
        approval = { ...approval, state: 'approved' };
        const after = frames.findLast(frame => frame.event.type === 'RUN_FINISHED' && frame.event.outcome?.type === 'interrupt').sequence;
        return route.fulfill({ status: 307, headers: { location: `${streamUrl}?after=${after}` } });
      }
      else if (path.endsWith('/run-live/approval')) data = approval;
      else if (path.endsWith('/run-live')) data = run;
      else if (path.endsWith('/knowledge-retrievals')) data = [];
      else { unexpected.push(path); return route.abort(); }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, message: '操作成功', data: agUiData(path, data) }) });
    });
    await page.goto(`${base}/agents/11/sessions/activity`);
    const reducerChecks = await page.evaluate(async () => {
      const { emptyProcess, reduceProcess } = await import('/src/ag-ui.ts');
      let state = reduceProcess(emptyProcess(), { sequence: 1, event: { type: 'RUN_STARTED', threadId: 't', runId: 'r' } });
      state = reduceProcess(state, { sequence: 2, event: { type: 'RUN_FINISHED', threadId: 't', runId: 'r', outcome: { type: 'success' } } });
      state = reduceProcess(state, { sequence: 3, event: { type: 'RUN_ERROR', message: 'late failure', code: 'late' } });
      const lateFailure = state.status === 'failed';
      state = reduceProcess(state, { sequence: 4, event: { type: 'TEXT_MESSAGE_START', messageId: 'late-content', role: 'assistant' } });
      const closed = state.entries.length === 0;
      const duplicate = reduceProcess(state, { sequence: 4, event: { type: 'RUN_STARTED', threadId: 't', runId: 'r' } }) === state;
      let gap = false;
      try { reduceProcess(state, { sequence: 7, event: { type: 'RUN_ERROR', message: 'gap' } }); } catch { gap = true; }
      return { lateFailure, closed, duplicate, gap };
    });
    check(reducerChecks.lateFailure && reducerChecks.closed, 'late RUN_ERROR marks failure without reopening content');
    check(reducerChecks.duplicate && reducerChecks.gap, 'reducer ignores duplicates and detects missing frames');
    await page.locator('[data-stream-run="run-live"]').waitFor();
    await delayUntil(() => sockets.size > 0);
    const send = event => {
      const frame = { sequence: frames.length + 1, event: { timestamp: Date.now(), ...event } };
      frames.push(frame); for (const socket of sockets) socket.write(wire(frame));
    };
    send({ type: 'RUN_STARTED', runId: 'attempt-1', threadId: session.id, protocolVersion: '1.0' });
    send({ type: 'REASONING_START', messageId: 'reason-1' });
    send({ type: 'REASONING_MESSAGE_START', messageId: 'reason-1', role: 'reasoning' });
    send({ type: 'REASONING_MESSAGE_CONTENT', messageId: 'reason-1', delta: '先核对资料来源。' });
    await page.getByRole('button', { name: /正在思考/ }).waitFor();
    check(await page.getByRole('button', { name: /正在思考/ }).getAttribute('aria-expanded') === 'true', 'active public reasoning expands');
    send({ type: 'REASONING_MESSAGE_END', messageId: 'reason-1' });
    send({ type: 'REASONING_END', messageId: 'reason-1' });
    send({ type: 'TEXT_MESSAGE_START', messageId: 'intermediate', role: 'assistant' });
    send({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'intermediate', delta: '先加载审查技能。' });
    send({ type: 'TEXT_MESSAGE_END', messageId: 'intermediate' });
    send({ type: 'TOOL_CALL_START', toolCallId: 'tool-1', toolCallName: 'skill:审查@v2' });
    send({ type: 'TOOL_CALL_ARGS', toolCallId: 'tool-1', delta: '{"displayOnly":true,"arguments":"redacted"}' });
    send({ type: 'TOOL_CALL_END', toolCallId: 'tool-1' });
    const call = { version: 1, toolCallId: 'tool-1', name: 'skill:审查@v2', kind: 'skill', status: 'running', startedAt: Date.now() };
    send({ type: 'ACTIVITY_SNAPSHOT', messageId: 'activity-1', activityType: 'y-point.call', content: call });
    const card = page.locator('[data-call-id="tool-1"]');
    await card.getByText('执行中', { exact: true }).waitFor();
    check(await card.getByText('执行中', { exact: true }).isVisible(), 'TOOL_CALL_END leaves execution running');
    const before = frames.length;
    for (const socket of sockets) socket.end();
    await delayUntil(() => cursors.some(cursor => cursor >= before));
    await delayUntil(() => sockets.size > 0);
    send({ type: 'TOOL_CALL_RESULT', messageId: 'result-1', toolCallId: 'tool-1', content: '{"status":"succeeded","displayOnly":true}' });
    send({ type: 'ACTIVITY_SNAPSHOT', messageId: 'activity-1', activityType: 'y-point.call', content: { ...call, status: 'succeeded', finishedAt: Date.now(), summary: '已加载技能说明' } });
    await page.getByText('已加载技能说明', { exact: true }).waitFor();
    check(await page.locator('[data-call-id="tool-1"]').count() === 1, 'reconnect replay does not duplicate calls');
    run = { ...run, state: 'waiting_for_approval' };
    send({ type: 'RUN_FINISHED', threadId: session.id, runId: 'attempt-1', outcome: { type: 'interrupt', interrupts: [{ id: approval.id, reason: 'tool_approval', message: '请确认' }] } });
    await page.getByRole('button', { name: '同意执行', exact: true }).click();
    run = { ...run, state: 'running' };
    send({ type: 'RUN_STARTED', runId: 'attempt-2', threadId: session.id, protocolVersion: '1.0' });
    send({ type: 'REASONING_START', messageId: 'reason-2' });
    send({ type: 'REASONING_MESSAGE_START', messageId: 'reason-2', role: 'reasoning' });
    send({ type: 'REASONING_MESSAGE_CONTENT', messageId: 'reason-2', delta: '依据已授权的工具结果生成结论。' });
    send({ type: 'REASONING_MESSAGE_END', messageId: 'reason-2' });
    send({ type: 'REASONING_END', messageId: 'reason-2' });
    send({ type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' });
    send({ type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: answer });
    await page.getByRole('heading', { name: '审查结论' }).waitFor();
    check(await page.getByText('模型公开思考', { exact: true }).count() === 2, 'reasoning is segmented across attempts');
    await page.screenshot({ path: screenshotPath(`y-point-ag-ui-active-${width}.png`), fullPage: true });
    send({ type: 'TEXT_MESSAGE_END', messageId: 'answer' });
    send({ type: 'CUSTOM', name: 'y-point.answer', value: { version: 1, messageId: 'answer' } });
    completed = true; run = { ...run, state: 'completed' };
    send({ type: 'RUN_FINISHED', threadId: session.id, runId: 'attempt-2', outcome: { type: 'success' } });
    await page.locator('[data-message-id="2"]').waitFor();
    await page.locator('[data-message-id="2"] [data-process-status="succeeded"]').waitFor();
    const toggle = page.locator('[data-message-id="2"] .run-activity-toggle');
    check(await toggle.getAttribute('aria-expanded') === 'false', 'completed history collapses by default');
    await toggle.focus(); await page.keyboard.press('Enter');
    check(await toggle.getAttribute('aria-expanded') === 'true', 'process can be expanded with keyboard');
    check(await page.locator('[data-message-id="2"] [data-call-id="tool-1"]').count() === 1, 'history restores process from protocol snapshot');
    check(!unexpected.some(path => path.endsWith('/calls')), 'live/history process requires no per-event calls refetch');
    check(unexpected.length === 0, `no unexpected API requests: ${unexpected}`);
    check(errors.length === 0, `no browser errors: ${errors}`);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'page fits viewport');
    await page.screenshot({ path: screenshotPath(`y-point-ag-ui-history-${width}.png`), fullPage: true });
    await context.close();
    for (const socket of sockets) socket.end();
  }
  console.log(`AG-UI verified: ${checks} assertions across desktop/mobile, real SSE, reconnect, approval and history`);
} finally {
  for (const socket of sockets) socket.end();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  await browser.close();
}
