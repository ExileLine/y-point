import { screenshotPath } from "./screenshot-paths.mjs";
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const now = '2026-11-05T10:00:00+08:00';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let checks = 0;
try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
    await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
    const errors = [], submissions = [], uploads = [], preflights = [];
    let uploadFail = true, checkFail = true, submitFail = true;
    const session = { id: 'review', agent_id: 11, thread_id: 'thread', model_id: 1, title: '合同审查', archived: false, version: 1, create_time: now, update_time: now };
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const send = (data, status = 200, message = '操作成功') => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status < 400 ? status : 10006, message, data }) });
      if (/\/sessions\/[^/]+\/metrics$/.test(path)) return send({ run_count: 0, active_run_count: 0, elapsed_ms: 0, total_tokens: 0, missing_duration_runs: 0, calculated_at: new Date().toISOString() });
      if (path === '/api/v1/agents/runtime-limits') return send({ upload_max_bytes: 33554432, context_max_bytes: 16384, max_files: 5, recursion_limit: 50, default_model_timeout_seconds: 120, max_model_timeout_seconds: 600, default_tool_timeout_seconds: 120, max_tool_timeout_seconds: 600, default_max_output_tokens: 4096, max_output_tokens: 65536 });
      if (path.endsWith('/auth/me')) return send({ id: 1, username: 'reviewer', role: 'user' });
      if (path === '/api/v1/agents/11') return send({ id: 11, name: '合同审查助手', enabled: true, default_model_id: 1 });
      if (path.endsWith('/sessions')) return send({ items: [session], total: 1, page: 1, page_size: 20 });
      if (path.endsWith('/sessions/review')) return send(session);
      if (path.endsWith('/models')) return send([{ id: 1, display_name: '审核模型', provider_name: '测试', verified_capabilities: ['text', 'tools'], selectable: true }]);
      if (path.endsWith('/messages')) return send([]);
      if (path.endsWith('/runs') && request.method() === 'GET') return send({ items: [], total: 0, page: 1, page_size: 20 });
      if (path.endsWith('/artifacts/uploads')) {
        uploads.push(request.postDataBuffer());
        if (uploadFail) return send(null, 400, '测试上传失败');
        return send([{ id: 'uploaded-file', filename: '待审合同.txt', media_type: 'text/plain', byte_size: 32 }], 201);
      }
      if (path.endsWith('/attachments/check')) {
        preflights.push(request.postDataJSON());
        return checkFail ? send(null, 400, '附件正文超过 16 KiB，请拆分') : send({ valid: true, context_bytes: 100 });
      }
      if (path.endsWith('/runs') && request.method() === 'POST') {
        submissions.push(request.postDataJSON());
        if (submitFail) return route.abort();
        return send({ id: 'review-run', state: 'queued', session_id: session.id, agent_id: 11, model_id: 1, create_time: now, update_time: now }, 201);
      }
      if (path.endsWith('/stream-ticket')) return send({ expires_in: 300 });
      if (path.endsWith('/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': waiting\n\n' });
      if (path.endsWith('/snapshot')) return send({ run: { id: 'review-run', state: 'queued' }, events: [], latest_sequence: 0, next_after: 0, has_more: false });
      if (path.endsWith('/calls')) return send([]);
      if (path.endsWith('/runs/review-run')) return send({ id: 'review-run', state: 'queued', create_time: now, update_time: now });
      throw new Error(`未定义接口 ${path}`);
    });
    await page.goto(`${base}/agents/11/sessions/review`);
    await page.getByRole('textbox', { name: '消息内容' }).fill('请审查附件并按模板生成报告');
    assert.equal(await page.getByRole('combobox', { name: '指定报告 Skill' }).count(), 0, '对话不提供报告 Skill 选择，由模型按任务读取 Skill'); checks++;
    const input = page.locator('.chat-attachments input[type=file]');
    await input.setInputFiles({ name: '待审合同.txt', mimeType: 'text/plain', buffer: Buffer.from('合同正文：15个工作日内报销') });
    await page.getByText('测试上传失败', { exact: false }).waitFor();
    assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true); checks++;
    await page.getByRole('textbox', { name: '消息内容' }).press('Enter');
    assert.equal(submissions.length, 0, '附件上传失败时按回车不能提交'); checks++;
    uploadFail = false;
    await page.getByRole('button', { name: '重试上传 待审合同.txt' }).click();
    await page.getByText('上传完成', { exact: false }).waitFor();
    assert.equal(uploads.length, 2); checks++;
    await input.setInputFiles(Array.from({ length: 5 }, (_, index) => ({ name: `附件${index + 2}.txt`, mimeType: 'text/plain', buffer: Buffer.from('小型参考材料') })));
    await page.waitForFunction(() => document.querySelectorAll('.chat-file').length === 5 && [...document.querySelectorAll('.chat-file small')].every(item => item.textContent.includes('上传完成')));
    assert.equal(uploads.length, 6); checks++;
    assert.equal(await page.locator('.chat-file').count(), 5); checks++;
    await page.screenshot({ path: screenshotPath(`y-point-chat-upload-multiple-${width}.png`), animations: "disabled" });
    const sendBox = await page.getByRole('button', { name: '发送', exact: true }).boundingBox();
    assert.ok(sendBox && sendBox.y + sendBox.height <= 1000); checks++;
    for (const index of [2, 3, 4, 5]) await page.getByRole('button', { name: `移除 附件${index}.txt` }).click();

    await page.screenshot({ path: screenshotPath(`y-point-chat-upload-${width}.png`), animations: "disabled" });
    assert.equal(await page.getByRole('checkbox', { name: '供模型读取' }).isChecked(), true); checks++;
    await page.getByRole('textbox', { name: '消息内容' }).press('Enter');
    await page.getByText('附件正文超过 16 KiB，请拆分', { exact: false }).waitFor();
    assert.deepEqual(preflights[0], { artifact_ids: ['uploaded-file'], context_artifact_ids: ['uploaded-file'] }, '附件检查只能发送两个附件 ID 列表，不能发送完整 Run 草稿'); checks++;
    assert.equal(submissions.length, 0); checks++;
    assert.equal(await page.getByRole('textbox', { name: '消息内容' }).inputValue(), '请审查附件并按模板生成报告'); checks++;
    await page.getByRole('checkbox', { name: '供模型读取' }).uncheck();
    await page.getByRole('checkbox', { name: '供模型读取' }).check();
    checkFail = false;
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByRole('button', { name: '重试提交' }).waitFor();
    const first = submissions[0];
    assert.deepEqual(preflights[1], preflights[0]); checks++;
    assert.equal(first.input_text, '请审查附件并按模板生成报告'); checks++;
    assert.ok(first.request_key); checks++;
    assert.equal(first.model_id, 1); checks++;
    assert.equal(first.expected_session_version, 1); checks++;
    assert.equal(first.publish_answer_artifact, false); checks++;
    assert.equal(Object.hasOwn(first, 'required_skill_id'), false); checks++;
    assert.deepEqual(first.artifact_ids, ['uploaded-file']); checks++;
    assert.deepEqual(first.context_artifact_ids, ['uploaded-file']); checks++;
    submitFail = false;
    await page.getByRole('button', { name: '重试提交' }).click();
    await page.getByText('待审合同.txt', { exact: true }).waitFor({ state: 'hidden' });
    assert.deepEqual(submissions[1], first); checks++;
    assert.equal(await page.getByRole('textbox', { name: '消息内容' }).inputValue(), ''); checks++;
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false); checks++;
    assert.deepEqual(errors, []); checks++;
    await context.close();
  }
  // Skill 文件包编辑、模板及脚本验证见 verify-skill-workspace.mjs。
  console.log(`chat-uploads: ${checks} assertions passed (desktop/mobile; mocked API, no real model)`);
} finally { await browser.close(); }
