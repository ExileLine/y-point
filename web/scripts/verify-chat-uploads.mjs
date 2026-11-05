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
    uploadFail = false;
    await page.getByRole('button', { name: '重试上传 待审合同.txt' }).click();
    await page.getByText('上传完成', { exact: false }).waitFor();
    assert.equal(uploads.length, 2); checks++;
    await input.setInputFiles(Array.from({ length: 5 }, (_, index) => ({ name: `附件${index + 2}.txt`, mimeType: 'text/plain', buffer: Buffer.from('小型参考材料') })));
    await page.waitForFunction(() => document.querySelectorAll('.chat-file').length === 5 && [...document.querySelectorAll('.chat-file small')].every(item => item.textContent.includes('上传完成')));
    assert.equal(uploads.length, 6); checks++;
    assert.equal(await page.locator('.chat-file').count(), 5); checks++;
    await page.screenshot({ path: `/tmp/y-point-chat-upload-multiple-${width}.png`, animations: "disabled" });
    const sendBox = await page.getByRole('button', { name: '发送', exact: true }).boundingBox();
    assert.ok(sendBox && sendBox.y + sendBox.height <= 1000); checks++;
    for (const index of [2, 3, 4, 5]) await page.getByRole('button', { name: `移除 附件${index}.txt` }).click();

    await page.screenshot({ path: `/tmp/y-point-chat-upload-${width}.png`, animations: "disabled" });
    assert.equal(await page.getByRole('checkbox', { name: '供模型读取' }).isChecked(), true); checks++;
    await page.getByRole('button', { name: '发送', exact: true }).click();
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
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
    await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
    const content = '---\nname: contract-review\ndescription: 审查合同\n---\n\n按任务使用 A_template.docx 或 B_template.docx 生成报告。';
    const schemaA = { type: 'object', properties: { title: { type: 'string', maxLength: 100 } }, required: ['title'] };
    const schemaB = { type: 'object', properties: { summary: { type: 'string', maxLength: 200 } }, required: ['summary'] };
    let skill = { id: 4, owner_id: 1, name: 'contract-review', description: '审查合同', category: null, visibility: 'private', enabled: true, current_version: 1, files: { 'SKILL.md': content }, manifest: [], report_templates: [{ template_key: 'base.docx', artifact_id: 'base', filename: 'base.docx', sha256: 'old', fields_schema: schemaA }] };
    let publishes = 0, publishFail = true;
    const file = name => ({ name, mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('fixture-only') });
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const send = (data, status = 200, message = '操作成功') => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, message, data }) });
      if (path.endsWith('/auth/me')) return send({ id: 1, username: 'reviewer', role: 'user' });
      if (path.endsWith('/skills')) return send([skill]);
      if (path.endsWith('/skills/4') && request.method() === 'PUT') {
        const draft = request.postDataJSON();
        assert.equal(draft.expected_version, skill.current_version); checks++;
        skill = { ...skill, ...draft, current_version: skill.current_version + 1 };
        return send(skill);
      }
      if (path.endsWith('/skills/4')) return send(skill);
      if (path.endsWith('/skills/4/templates')) {
        publishes++;
        const form = await new Response(request.postDataBuffer(), { headers: { 'content-type': request.headers()['content-type'] } }).formData();
        assert.deepEqual([...form.keys()].sort(), ['expected_version', 'fields_schema', 'files', 'files']); checks++;
        assert.equal(Number(form.get('expected_version')), skill.current_version); checks++;
        assert.deepEqual(form.getAll('files').map(item => item.name), ['A_template.docx', 'B_template.docx']); checks++;
        assert.deepEqual(JSON.parse(form.get('fields_schema')), { 'A_template.docx': schemaA, 'B_template.docx': schemaB }); checks++;
        if (publishFail) return send(null, 400, 'B 模板校验失败，整批未发布');
        skill = { ...skill, current_version: skill.current_version + 1, report_templates: [...skill.report_templates,
          { template_key: 'a_template.docx', artifact_id: 'template-a', filename: 'A_template.docx', sha256: 'a', fields_schema: schemaA },
          { template_key: 'b_template.docx', artifact_id: 'template-b', filename: 'B_template.docx', sha256: 'b', fields_schema: schemaB }], report_template: null };
        return send(skill, 201);
      }
      if (path === '/api/v1/artifacts/template-b/download') return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', body: Buffer.from('fixture-download') });
      throw new Error(`未定义接口 ${path}`);
    });
    await page.goto(`${base}/skills`);
    await page.getByRole('button', { name: '编辑', exact: true }).first().click();
    await page.getByRole('heading', { name: 'Skill 产物模板' }).waitFor();
    const input = page.locator('.skill-report-template input[type=file]');
    assert.notEqual(await input.getAttribute('multiple'), null); checks++;
    await input.setInputFiles([file('A_template.docx'), file('B_template.docx')]);
    const schemaInputA = page.getByRole('textbox', { name: '报告字段 JSON Schema A_template.docx', exact: true });
    const schemaInputB = page.getByRole('textbox', { name: '报告字段 JSON Schema B_template.docx', exact: true });
    const publish = page.getByRole('button', { name: /发布模板新版本/ });
    await schemaInputA.waitFor();
    assert.equal(await page.locator('.skill-template-card textarea').count(), 2); checks++;
    await page.getByRole('button', { name: '移除待发布模板 B_template.docx' }).click();
    assert.equal(await schemaInputB.count(), 0); checks++;
    await input.setInputFiles(file('B_template.docx'));
    await input.setInputFiles(file('A_template.docx'));
    assert.equal(await page.locator('.skill-template-card textarea').count(), 2); checks++;
    await input.setInputFiles({ ...file('large.docx'), buffer: Buffer.alloc(2 * 1024 * 1024 + 1) });
    assert.equal(await page.locator('.skill-template-card textarea').count(), 2); checks++;
    await page.getByRole('textbox', { name: 'SKILL.md', exact: true }).fill(content + '\n正文补充');
    assert.equal(await publish.isDisabled(), true); checks++;
    const afterSave = page.waitForResponse(response => response.url().endsWith('/api/v1/skills') && response.request().method() === 'GET');
    await page.locator('.resource-editor').getByRole('button', { name: /保\s*存/, exact: true }).click();
    await afterSave;
    await page.evaluate(() => new Promise(requestAnimationFrame));
    await publish.waitFor({ state: 'visible' });
    await page.waitForFunction(() => ![...document.querySelectorAll('button')].find(item => item.textContent.startsWith('发布模板新版本'))?.disabled);
    assert.equal(await page.locator('.skill-template-card textarea').count(), 2); checks++;
    await schemaInputA.fill('{invalid');
    await schemaInputB.fill(JSON.stringify(schemaB));
    await publish.click();
    await page.locator('.resource-editor .ant-alert-error').waitFor();
    assert.equal(publishes, 0); checks++;
    assert.deepEqual(JSON.parse(await schemaInputB.inputValue()), schemaB); checks++;
    await schemaInputA.fill(JSON.stringify(schemaA));
    await publish.click();
    await page.getByText('B 模板校验失败，整批未发布', { exact: true }).waitFor();
    assert.equal(await page.locator('.skill-template-card textarea').count(), 2); checks++;
    publishFail = false;
    await publish.click();
    await page.getByRole('button', { name: '下载模板 B_template.docx', exact: true }).waitFor();
    assert.equal(publishes, 2); checks++;
    assert.equal(await page.locator('.skill-template-card textarea').count(), 0); checks++;
    assert.equal(await page.getByRole('button', { name: /^下载模板 / }).count(), 3); checks++;
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载模板 B_template.docx', exact: true }).click();
    assert.equal((await download).suggestedFilename(), 'B_template.docx'); checks++;
    await input.setInputFiles(file('B_template.docx'));
    assert.deepEqual(JSON.parse(await schemaInputB.inputValue()), schemaB); checks++;
    assert.equal(await page.getByText('替换同名模板', { exact: true }).count(), 1); checks++;
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false); checks++;
    await page.locator('.ant-message-notice').last().waitFor({ state: 'hidden' });
    await page.screenshot({ path: `/tmp/y-point-multi-templates-${width}.png`, animations: 'disabled' });
    await context.close();
  }
  console.log(`chat-uploads: ${checks} assertions passed (desktop/mobile; mocked API, no real model)`);
} finally { await browser.close(); }
