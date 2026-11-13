import { screenshotPath } from "./screenshot-paths.mjs";
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let checks = 0;
let activePage;
try {
  for (const width of [1280, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
    await context.addInitScript(() => { if (window === window.top) sessionStorage.setItem('yp.access_token', 'fixture'); });
    const page = await context.newPage(); activePage = page; const errors = [], unexpected = [], saves = [];
    page.on('pageerror', error => errors.push(error.message));
    const texts = { 'SKILL.md': '---\nname: review-package\ndescription: 审查合同并生成报告\n---\n\n# 审查步骤\n\n读取 [准则](references/policy.md)，调用 scripts/check.py，再按 assets/templates/report.docx 生成报告。', 'references/policy.md': '原始审查准则', 'scripts/check.py': 'import json\nprint(json.dumps(True))', 'y-point.yaml': 'scripts:\n  scripts/check.py:\n    input_schema:\n      type: object\n      properties: {}\n' };
    const longFile = '这是一个用于验证图标与长文件名单行对齐的参考文档.md';
    texts[`references/${longFile}`] = '长文件名布局验证';
    texts['assets/schemas/report.json'] = JSON.stringify({ type: 'object', properties: { title: { type: 'string' } } });
    const binaries = new Set(['assets/templates/report.docx']);
    const uploads = [];
    let revision = 1, published = 1, conflict = true, verified = false;
    const entry = path => ({ path, bytes: 90, sha256: `${path}:${texts[path] || 'docx'}`, is_text: path in texts, media_type: path in texts ? 'text/plain' : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', package_file: true });
    const files = () => [...Object.keys(texts), ...binaries].map(entry);
    const settings = { category: '审查', visibility: 'private', enabled: true };
    const draft = () => ({ revision, base_version: published, content_hash: 'fixed-hash', files: files(), settings });
    const skill = () => ({ id: 7, name: 'review-package', description: '审查合同并生成报告', owner_id: 1, category: '审查', visibility: 'private', enabled: true, current_version: published });
    const versions = [{ version: 1, content_hash: 'original-hash', create_time: '2026-11-06T11:00:00', manifest: files() }];
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
      const send = (data, status = 200, message = '操作成功') => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status < 400 ? status : 10005, message, data }) });
      if (path.endsWith('/auth/me')) return send({ id: 1, username: 'tester', role: 'admin' });
      if (path === '/api/v1/skills') return send([skill()]);
      if (path.endsWith('/workspace')) return send({ skill: skill(), draft: draft(), readonly: false, versions });
      if (path.endsWith('/files/preview')) return send({ artifact_id: 'template', filename: 'report.docx', media_type: 'docx', kind: 'html', content: '<h1>报告模板 {{ title }}</h1>', sheets: [], notice: '正文和表格内容预览', truncated: false });
      if (path.endsWith('/files') && method === 'GET') {
        const file = url.searchParams.get('path');
        if (url.searchParams.get('download')) return route.fulfill({ contentType: 'application/octet-stream', headers: { 'Content-Disposition': 'attachment; filename="report.docx"' }, body: 'fixture-docx' });
        return route.fulfill({ contentType: 'text/plain', body: texts[file] ?? '' });
      }
      if (path.endsWith('/draft') && method === 'PUT') {
        const body = request.postDataJSON(); saves.push(body);
        if (conflict) return send(null, 409, '草稿已变化，请刷新后比较修改');
        assert.equal(body.expected_revision, revision);
        Object.assign(texts, body.files); (body.delete_paths || []).forEach(file => delete texts[file]);
        for (const [old, target] of Object.entries(body.renames || {})) { texts[target] = texts[old]; delete texts[old]; }
        revision++; return send(draft());
      }
      if (path.endsWith('/draft') && method === 'POST') { revision++; return send(draft()); }
      if (path.endsWith('/draft/files')) {
        const form = await new Response(request.postDataBuffer(), { headers: { 'content-type': request.headers()['content-type'] } }).formData();
        assert.equal(Number(form.get('expected_revision')), revision);
        const paths = JSON.parse(form.get('paths')); uploads.push(paths); paths.forEach(path => binaries.add(path)); revision++; return send(draft());
      }
      if (path.endsWith('/draft/validate')) return send({ revision, content_hash: 'fixed-hash', valid: verified, errors: verified ? [] : ['scripts/check.py: 当前完整包尚未通过试运行'], warnings: [], config: { templates: texts['y-point.yaml'].startsWith('{') ? JSON.parse(texts['y-point.yaml']).templates || {} : {}, scripts: { 'scripts/check.py': { input_schema: { type: 'object', properties: {} } } }, required_tools: [] }, scripts: [{ path: 'scripts/check.py', verified, environment: { state: 'not_required' } }] });
      if (path.endsWith('/scripts/prepare')) return send({ path: 'scripts/check.py', verified, environment: { state: 'not_required' } });
      if (path.endsWith('/scripts/test')) { verified = true; return send({ path: 'scripts/check.py', verified, result: '{"valid":true}' }); }
      if (path.endsWith('/draft/publish')) { published++; revision++; versions.unshift({ version: published, content_hash: 'fixed-hash', create_time: '2026-11-06T11:30:00', manifest: files() }); return send(skill(), 201); }
      if (path.endsWith('/export')) return route.fulfill({ contentType: 'application/zip', headers: { 'Content-Disposition': 'attachment; filename="skill.zip"' }, body: 'fixture-zip' });
      unexpected.push(`${method} ${path}`); return send(null, 404, 'unexpected');
    });
    async function section(label) {
      if (width >= 768) { await page.getByRole('tab', { name: label, exact: true }).click(); return; }
      const input = page.getByRole('combobox', { name: 'Skill 工作区栏目', exact: true });
      await page.locator('.ant-select').filter({ has: input }).click();
      await page.locator('.ant-select-dropdown:visible').getByText(label, { exact: true }).click();
    }
    await page.goto(`${base}/skills`);
    await page.getByRole('button', { name: '工作区', exact: true }).click();
    await page.getByRole('textbox', { name: '编辑 SKILL.md', exact: true }).waitFor();
    assert.ok((await page.getByRole('textbox', { name: '编辑 SKILL.md', exact: true }).inputValue()).includes('审查步骤')); checks++;
    const pathFailures = [];
    for (const directory of ['', 'references', 'scripts', 'assets/templates', 'assets/schemas', 'assets/examples']) {
      const target = page.getByRole('combobox', { name: '上传目标目录', exact: true });
      await page.locator('.ant-select').filter({ has: target }).click();
      await page.locator('.ant-select-dropdown:visible').getByText(directory ? `${directory}/` : '根目录', { exact: true }).click();
      await page.getByRole('button', { name: '新建文件', exact: true }).click();
      const expected = directory ? `${directory}/new.md` : 'new.md';
      const actual = await page.getByRole('textbox', { name: '新文件路径', exact: true }).inputValue();
      if (actual !== expected) pathFailures.push({ directory, expected, actual });
      await page.getByRole('button', { name: /^(取消|Cancel)$/ }).click(); checks++;
    }
    const treeLayout = await page.locator('.skill-file-tree .ant-tree-node-content-wrapper').evaluateAll(nodes => nodes.flatMap(node => {
      const icon = node.querySelector('.ant-tree-iconEle'), title = node.querySelector('.ant-tree-title');
      if (!icon || !title) return [];
      const a = icon.getBoundingClientRect(), b = title.getBoundingClientRect();
      return Math.abs((a.top + a.bottom - b.top - b.bottom) / 2) > 3 ? [{ title: title.textContent, iconTop: a.top, titleTop: b.top }] : [];
    }));
    assert.deepEqual({ pathFailures, treeLayout }, { pathFailures: [], treeLayout: [] }, '新建路径跟随所选目录，文件树图标和名称保持同行'); checks++;
    const initialTarget = page.getByRole('combobox', { name: '上传目标目录', exact: true });
    await page.locator('.ant-select').filter({ has: initialTarget }).click();
    await page.locator('.ant-select-dropdown:visible').getByText('references/', { exact: true }).click();
    await page.getByRole('textbox', { name: '编辑 SKILL.md', exact: true }).fill('保留未保存正文');
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    await page.getByText('草稿已变化，请刷新后比较修改', { exact: true }).waitFor();
    assert.equal(await page.getByRole('textbox', { name: '编辑 SKILL.md', exact: true }).inputValue(), '保留未保存正文'); checks++;
    conflict = false;
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    await page.getByText('草稿已保存', { exact: true }).waitFor();
    assert.deepEqual(saves[1].files, { 'SKILL.md': '保留未保存正文' }); checks++;
    await page.getByRole('button', { name: '新建文件', exact: true }).click();
    await page.getByRole('textbox', { name: '新文件路径', exact: true }).fill('references/new.md');
    await page.getByRole('button', { name: '保存到草稿', exact: true }).click();
    await page.getByRole('textbox', { name: '编辑 references/new.md', exact: true }).waitFor();
    await page.getByRole('textbox', { name: '编辑 references/new.md', exact: true }).fill('# 新参考资料\n\n- 校验事实\n- 生成报告');
    await page.locator('.ant-segmented').getByText('预览', { exact: true }).click();
    await page.getByRole('heading', { name: '新参考资料', exact: true }).waitFor(); checks++;
    await page.getByRole('button', { name: '保存草稿', exact: true }).click();
    await page.getByText('草稿已保存', { exact: true }).waitFor();
    const target = page.getByRole('combobox', { name: '上传目标目录', exact: true });
    await page.locator('.ant-select').filter({ has: target }).click();
    await page.locator('.ant-select-dropdown:visible').getByText('assets/templates/', { exact: true }).click();
    await page.locator('input[type=file]').first().setInputFiles(['summary.docx', 'detail.docx'].map(name => ({ name, mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('fixture-docx') })));
    await page.getByText('2 个文件已保存到草稿', { exact: true }).waitFor();
    assert.deepEqual(uploads[0], ['assets/templates/summary.docx', 'assets/templates/detail.docx']); checks++;
    await section('执行与资源配置');
    const schemaSelect = page.getByRole('combobox', { name: 'assets/templates/summary.docx 字段契约', exact: true });
    await page.locator('.ant-select').filter({ has: schemaSelect }).click();
    await page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').filter({ hasText: /^assets\/schemas\/report\.json$/ }).click();
    await page.getByRole('button', { name: '保存配置并校验', exact: true }).click();
    await page.getByText('scripts/check.py: 当前完整包尚未通过试运行', { exact: true }).waitFor();
    assert.equal(JSON.parse(saves.at(-1).files['y-point.yaml']).templates['assets/templates/summary.docx'], 'assets/schemas/report.json'); checks++;
    await section('文件工作区');
    await page.getByText('report.docx', { exact: true }).click();
    const frame = page.frameLocator('iframe[title="文档内容预览"]');
    await frame.getByRole('heading', { name: '报告模板 {{ title }}', exact: true }).waitFor(); checks++;
    const download = page.waitForEvent('download'); await page.getByRole('button', { name: '下载当前文件', exact: true }).click(); assert.equal((await download).suggestedFilename(), 'report.docx'); checks++;
    await page.screenshot({ path: screenshotPath(`y-point-skill-workspace-${width}.png`), fullPage: true, animations: 'disabled' });
    await section('脚本试运行');
    await page.getByText('待试运行', { exact: true }).waitFor();
    await page.getByRole('button', { name: '试运行脚本', exact: true }).click();
    await page.getByText('已验证', { exact: true }).waitFor(); checks++;
    await page.getByRole('button', { name: '发布 Skill', exact: true }).click();
    await page.locator('.ant-modal-confirm-title:visible').waitFor();
    await page.getByRole('button', { name: '发布版本', exact: true }).click();
    await page.getByText('完整文件包已发布', { exact: true }).waitFor(); checks++;
    await section('版本历史');
    await page.getByText('v2', { exact: true }).waitFor();
    await page.getByRole('button', { name: '比较正文', exact: true }).first().click();
    await page.getByRole('heading', { name: '当前草稿正文', exact: true }).waitFor(); checks++;
    await page.getByRole('button', { name: 'Close', exact: true }).click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `移动端宽度 ${width} 无页面横向溢出`); checks++;
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); checks += 2;
    console.log(`width ${width}: passed`); await context.close();
  }
  console.log(`Skill 工作区 ${checks} 项浏览器断言通过（桌面和移动端，受控 API）`);
} catch (failure) { if (activePage) { await activePage.screenshot({ path: screenshotPath('y-point-skill-failure.png'), fullPage: true }); console.error(await activePage.locator('body').innerText()); } throw failure; } finally { await browser.close(); }
