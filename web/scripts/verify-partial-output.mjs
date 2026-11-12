import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createServer } from 'node:http';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5174';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
const sockets = new Set();
const server = createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': new URL(base).origin, 'Cache-Control': 'no-cache' });
  sockets.add(res);
  req.on('close', () => sockets.delete(res));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const streamUrl = `http://127.0.0.1:${server.address().port}/stream`;
let assertions = 0;
function check(value, label) { assert.ok(value, label); assertions++; }
const partial = '## 已完成的检查\n\n第一节的施工依据已核对。\n\n第二节仍在分析，报告尚未生成。';
const origin = new Date().toISOString();
const metrics = { elapsed_ms: 73450, total_tokens: 225991 };
try {
  for (const width of [1280, 390]) {
    for (const state of ['failed', 'timed_out', 'cancelled']) {
      const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
      await page.routeWebSocket('**', socket => socket.close());
      await page.addInitScript(() => { sessionStorage.setItem('yp.access_token', 'fixture-access'); sessionStorage.setItem('yp.refresh_token', 'fixture-refresh'); });
      const errors = [], unexpected = [], rows = [];
      page.on('pageerror', error => errors.push(error.message));
      let finished = false, releaseMessages = false, nextRound = false;
      let release;
      const waiting = new Promise(resolve => { release = resolve; });
      const failure = state === 'failed' ? 'model_output_limit_exceeded' : state === 'timed_out' ? 'provider_timeout' : null;
      const session = { id: 'partial', agent_id: 11, model_id: 4, thread_id: 'thread-partial', title: '中断内容保留', archived: false, version: 1, create_time: origin, update_time: origin };
      let run = { id: 'run-partial', session_id: session.id, agent_id: 11, model_id: 4, state: 'running', failure_code: null, reported_total_tokens: 0, create_time: origin, update_time: origin };
      const next = { ...run, id: 'run-next', state: 'completed', reported_total_tokens: 20 };
      await page.route('**/api/v1/**', async route => {
        const url = new URL(route.request().url()), path = url.pathname;
        let data;
        if (path === '/api/v1/agents/runtime-limits') data = { upload_max_bytes: 10485760, context_max_bytes: 20971520, max_files: 5 };
        else if (path === '/api/v1/auth/me') data = { id: 1, username: 'tester', role: 'user' };
        else if (path === '/api/v1/agents/11') data = { id: 11, name: '审查助手', enabled: true, default_model_id: 4, max_output_tokens: null };
        else if (path === '/api/v1/agents/11/sessions') data = { items: [session], total: 1, page: 1, page_size: 20 };
        else if (path.endsWith('/partial/models')) data = [];
        else if (path.endsWith('/partial/metrics')) data = { run_count: nextRound ? 2 : 1, active_run_count: finished ? 0 : 1, elapsed_ms: 73450, total_tokens: 225991, missing_duration_runs: 0, calculated_at: origin };
        else if (path.endsWith('/partial/messages')) {
          if (finished && !releaseMessages) await waiting;
          data = [{ id: 1, run_id: run.id, role: 'user', content: '请审核材料', run_state: run.state, failure_code: run.failure_code, partial_content: finished ? partial : '', reasoning_content: finished ? '先核对材料范围。' : '', calls: [], metrics, attachments: [], artifacts: [], create_time: origin },
            ...(nextRound ? [{ id: 3, run_id: next.id, role: 'user', content: '你好', attachments: [], artifacts: [], create_time: origin }, { id: 4, run_id: next.id, role: 'assistant', content: '你好，可以继续提问。', attachments: [], artifacts: [], metrics: { elapsed_ms: 100, total_tokens: 20 }, create_time: origin }] : [])]
            .filter(row => row.id > Number(url.searchParams.get('after') || 0));
        } else if (path.endsWith('/partial/runs')) data = { items: nextRound ? [next, run] : [run], total: nextRound ? 2 : 1, page: 1, page_size: 20 };
        else if (path.endsWith('/calls') || path.endsWith('/knowledge-retrievals')) data = [];
        else if (path.endsWith('/run-partial/snapshot')) {
          const after = Number(url.searchParams.get('after') || 0);
          data = { run, events: rows.filter(row => row.sequence > after), next_after: rows.at(-1)?.sequence || after, latest_sequence: rows.at(-1)?.sequence || 0, has_more: false };
        } else if (path.endsWith('/run-partial/stream-ticket')) data = { expires_in: 300 };
        else if (path.endsWith('/run-partial/stream')) return route.fulfill({ status: 307, headers: { location: streamUrl, 'Access-Control-Allow-Origin': '*' } });
        else if (path.endsWith('/run-partial')) data = run;
        else if (path.endsWith('/run-next')) data = next;
        else if (path.endsWith('/partial')) data = session;
        else { unexpected.push(path); return route.abort(); }
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, message: '操作成功', data }) });
      });
      await page.goto(`${base}/agents/11/sessions/partial`);
      await page.locator('[data-stream-run="run-partial"]').waitFor();
      const deadline = Date.now() + 10000;
      while (!sockets.size && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 30));
      check(sockets.size > 0, `${width}/${state} 建立真实 SSE`);
      const send = row => { rows.push(row); for (const socket of sockets) socket.write(`id: run-partial:${row.sequence}\nevent: ${row.event_type}\ndata: ${JSON.stringify(row)}\n\n`); };
      send({ sequence: 1, event_type: 'text_delta', state: 'running', content: partial });
      await page.getByText('第二节仍在分析，报告尚未生成。', { exact: true }).waitFor();
      finished = true;
      run = { ...run, state, failure_code: failure, reported_total_tokens: 225991 };
      send({ sequence: 2, event_type: 'state', state, failure_code: failure, content: null });
      const live = page.locator('[data-stream-run="run-partial"]');
      await live.locator('.session-partial-notice').waitFor();
      check((await live.locator('.message-markdown').innerText()).includes('第二节仍在分析'), `${width}/${state} 终态历史接口延迟时保留流式正文`);
      check(await live.locator('.message-stream-cursor').count() === 0, `${width}/${state} 终态停止输出光标`);
      releaseMessages = true; release();
      await page.reload();
      const saved = page.locator('[data-call-run="run-partial"]');
      await saved.locator('.session-partial-notice').waitFor();
      check((await saved.locator('.message-markdown').innerText()).includes('第二节仍在分析'), `${width}/${state} 刷新恢复未完成正文`);
      check((await saved.locator('.session-partial-notice').innerText()).includes('已保留未完成内容'), `${width}/${state} 明确标记未完成`);
      check(await saved.getByRole('button', { name: '复制未完成内容' }).count() === 1, `${width}/${state} 提供部分正文复制`);
      check(await page.locator('[data-message-id="2"]').count() === 0, `${width}/${state} 不伪造成功助手消息`);
      if (failure) {
        const detail = saved.locator('.partial-failure-details');
        check(await detail.getAttribute('open') === null, `${width}/${state} 失败详情默认收起`);
        await detail.locator('summary').focus();
        await page.keyboard.press('Enter');
        check(await detail.locator('p').isVisible(), `${width}/${state} 键盘展开失败原因`);
        await page.keyboard.press('Enter');
      }
      if (state === 'failed') {
        await saved.evaluate(node => node.scrollIntoView({ block: 'start' }));
        await page.screenshot({ path: `/tmp/y-point-partial-output-${width}.png`, fullPage: true });
      }
      nextRound = true;
      await page.reload();
      await page.locator('[data-message-id="4"]').waitFor();
      check((await page.locator('[data-call-run="run-partial"] .message-markdown').innerText()).includes('第一节的施工依据'), `${width}/${state} 新轮次后历史部分内容仍存在`);
      check((await page.locator('[data-message-id="4"]').innerText()).includes('你好，可以继续提问'), `${width}/${state} 正常后续回答独立展示`);
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}/${state} 无横向溢出`);
      check(errors.length === 0, `${width}/${state} 页面无异常：${errors.join(', ')}`);
      check(unexpected.length === 0, `${width}/${state} 接口契约完整：${unexpected.join(', ')}`);
      await page.close();
    }
  }
  console.log(`${assertions} assertions passed`);
} finally {
  for (const socket of sockets) socket.end();
  server.close();
  await browser.close();
}
