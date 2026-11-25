import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { agUiData, agUiFrames, agUiPost, agUiSnapshot } from './ag-ui-fixtures.mjs';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
try {
  for (const width of [1280, 375]) {
    for (const scenario of ['network', 'version']) {
      const page = await browser.newPage({ viewport: { width, height: 920 }, reducedMotion: 'reduce' });
      const errors = [], posts = [], calls = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
      const now = new Date().toISOString();
      const session = { id: 'version', agent_id: 11, title: '版本提交验证', model_id: 4, archived: false, version: 12, create_time: now, update_time: now };
      const messages = [
        { id: 1, run_id: 'old', role: 'user', content: '已有问题', create_time: now, attachments: [], artifacts: [] },
        { id: 2, run_id: 'old', role: 'assistant', content: '已有回答', create_time: now, attachments: [], artifacts: [] },
      ];
      let run = null;
      await page.route('**/api/v1/**', async route => {
        const req = route.request(), path = new URL(req.url()).pathname;
        calls.push({ path, method: req.method() });
        const send = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, data: agUiData(path, data) }) });
        if (path.endsWith('/auth/me')) return send({ id: 1, username: 'tester', role: 'user' });
        if (path === '/api/v1/agents/11') return send({ id: 11, name: '版本助手', enabled: true, default_model_id: 4 });
        if (path.endsWith('/runtime-limits')) return send({ upload_max_bytes: 10485760, context_max_bytes: 10485760, max_files: 5 });
        if (path.endsWith('/sessions')) return send({ items: [session], total: 1, page: 1, page_size: 20 });
        if (path.endsWith('/sessions/version')) return send(session);
        if (path.endsWith('/sessions/version/approval-policy')) return send({ mode: 'ask', version: 1, tools: [], allow_all_available: true, force_approval: false });
        if (path.endsWith('/models')) return send([]);
        if (path.endsWith('/metrics')) return send({ run_count: 1, active_run_count: 0, elapsed_ms: 0, total_tokens: null, missing_duration_runs: 0, calculated_at: now });
        if (path.endsWith('/version/runs')) return send({ items: run ? [run] : [], total: run ? 1 : 0, page: 1, page_size: 20 });
        if (path.endsWith('/ag-ui/threads/version')) return send(messages);
        if (path.endsWith('/agents/11/ag-ui') && req.method() === 'POST') {
          const body = req.postDataJSON();
          posts.push(body);
          assert.equal(body.messages.length, 1);
          if (posts.length === 1) {
            if (scenario === 'network') return route.abort('failed');
            session.version = 13;
            return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ code: 10005,
              message: '会话版本已变化，请刷新后重试', data: { reason: 'session_version_changed', version: 13 } }) });
          }
          run = { id: 'version-run', session_id: session.id, agent_id: 11, model_id: 4, state: 'completed', failure_code: null, create_time: now, update_time: now };
          session.version++;
          messages.push({ id: 3, run_id: run.id, role: 'user', content: body.messages[0].content, create_time: now, attachments: [], artifacts: [] });
          messages.push({ id: 4, run_id: run.id, role: 'assistant', content: '版本模式回答', create_time: now, attachments: [], artifacts: [] });
          return agUiPost(route, run, agUiFrames(run, [], { answer: '版本模式回答' }));
        }
        if (path.endsWith('/knowledge-retrievals')) return send([]);
        if (path.endsWith('/ag-ui/snapshot')) return send(agUiSnapshot(run || { id: 'old', state: 'completed' }, agUiFrames(run || { id: 'old', state: 'completed' }, [])));
        if (/\/runs\/[^/]+$/.test(path)) return send(run);
        throw new Error(`Unexpected request: ${req.method()} ${path}`);
      });
      await page.goto(`${base}/agents/11/sessions/version`);
      await page.getByText('已有回答', { exact: true }).waitFor();
      const input = page.getByRole('textbox', { name: '消息内容' });
      await input.fill('保留我的问题');
      const start = calls.length;
      await page.getByRole('button', { name: '发送', exact: true }).click();
      if (scenario === 'network') {
        await page.getByRole('button', { name: '重试提交', exact: true }).waitFor();
        assert.equal(await input.inputValue(), '保留我的问题');
        await page.getByRole('button', { name: '重试提交', exact: true }).click();
      } else {
        await page.getByText(/已保留输入，请确认会话最新状态后重新发送/).waitFor();
        assert.equal(await input.inputValue(), '保留我的问题');
        assert.equal(posts.length, 1, 'version conflict must not automatically submit again');
        assert.equal(await page.getByRole('button', { name: '重试提交', exact: true }).count(), 0);
        await page.getByRole('button', { name: '发送', exact: true }).click();
      }
      await page.getByText('版本模式回答', { exact: true }).waitFor();
      assert.equal(posts.length, 2);
      assert.equal(posts[0].forwardedProps.expected_session_version, 12);
      if (scenario === 'network') assert.deepEqual(posts[1], posts[0], 'network retry reuses input, run ID and message ID');
      else {
        assert.equal(posts[1].forwardedProps.expected_session_version, 13);
        assert.notEqual(posts[1].runId, posts[0].runId);
        assert.notEqual(posts[1].messages[0].id, posts[0].messages[0].id);
      }
      const postIndex = calls.findIndex((call, index) => index >= start && call.method === 'POST' && call.path.endsWith('/agents/11/ag-ui'));
      assert.ok(!calls.slice(start, postIndex).some(call => call.path.includes('/ag-ui/threads/')));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      console.log(`${width}: ${scenario} conflict/retry, draft preservation and version submission passed`);
      await page.close();
    }
  }
} finally {
  await browser.close();
}
