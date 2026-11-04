import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const now = '2026-11-04T12:00:00+08:00';
const entries = [
  ['markdown', '差旅报告.md', 'markdown', '# 差旅报告\n\n- 一份可阅读的报告\n\n```python\nprint("hello")\n```'],
  ['text', '记录.txt', 'text', '# 普通文本\n<script>不执行</script>'],
  ['json', '数据.json', 'text', '{\n  "amount": 100\n}'],
  ['html', '页面.html', 'html', '<h1>HTML 报告</h1><p>可阅读正文</p><script>window.parent.previewInjected=true</script><img src="https://preview-remote.example/track">'],
  ['docx', '文档.docx', 'html', '<h1>Word 报告</h1><p>正文段落</p><table><tr><td>金额</td><td>100</td></tr></table>'],
  ['pdf', '报告.pdf', 'html', '<h2>第 1 页</h2><pre>PDF 文字内容</pre>'],
  ['xlsx', '统计.xlsx', 'sheets', ''], ['csv', '统计.csv', 'sheets', ''],
  ['large', '长文档.txt', 'text', '有界的部分内容'], ['retry', '重试.md', 'markdown', '# 重试成功'],
  ['forbidden', '撤权.md', 'markdown', ''], ['missing', '缺失.md', 'markdown', ''],
  ['broken', '损坏.docx', 'text', ''], ['unsupported', '未知.bin', 'text', ''],
  ['expired', '续期.md', 'markdown', '# 续期成功'], ['slow', '等待.md', 'markdown', '# 旧的预览'], ['deleted', '已删除.md', 'markdown', ''],
];
const envelope = data => ({ code: 200, message: '操作成功', data });
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let checks = 0;
try {
  for (const [layout, width] of [['desktop', 1280], ['mobile', 375]]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, acceptDownloads: true, permissions: ['clipboard-read', 'clipboard-write'] });
    const page = await context.newPage();
    await context.addInitScript(() => { if (window.top !== window) return; sessionStorage.setItem('yp.access_token', 'fixture'); sessionStorage.setItem('yp.refresh_token', 'fixture-refresh'); });
    const errors = [], unexpected = [], requests = [], remote = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', req => { if (req.url().includes('preview-remote.example')) remote.push(req.url()); });
    let retryAllowed = false, releaseSlow, slowSeen;
    const slowGate = new Promise(resolve => { releaseSlow = resolve; });
    const slowStarted = new Promise(resolve => { slowSeen = resolve; });
    const session = { id: 'preview', agent_id: 11, title: '产物预览验证', archived: false, version: 1, create_time: now, update_time: now };
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      requests.push({ path, auth: request.headers().authorization });
      const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? envelope(data) : { code: status, message: data }) });
      if (path === '/api/v1/auth/me') return send({ id: 1, username: 'preview-tester', role: 'user' });
      if (path === '/api/v1/auth/refresh') return send({ access_token: 'renewed', refresh_token: 'fixture-refresh', expires_in: 3600 });
      if (path === '/api/v1/agents/11') return send({ id: 11, name: '预览验证', enabled: true, default_model_id: 1 });
      if (path === '/api/v1/agents/11/sessions') return send({ items: [session], total: 1, page: 1, page_size: 20 });
      if (path.endsWith('/sessions/preview')) return send(session);
      if (path.endsWith('/preview/runs')) return send({ items: [], total: 0, page: 1, page_size: 20 });
      if (path.endsWith('/preview/messages')) return send([
        { id: 1, run_id: 'run-preview', role: 'user', content: '请生成文档', create_time: now, attachments: [], artifacts: [] },
        { id: 2, run_id: 'run-preview', role: 'assistant', content: '文档已生成。', create_time: now, attachments: [], artifacts: entries.map(([id, filename]) => ({ artifact_id: id, filename, available: id !== 'deleted' })) },
      ]);
      if (path.startsWith('/api/v1/artifacts/')) {
        assert.ok(request.headers().authorization?.startsWith('Bearer ')); checks++;
        const id = path.split('/')[4], item = entries.find(entry => entry[0] === id);
        if (path.endsWith('/download')) return route.fulfill({ contentType: 'application/octet-stream', headers: { 'Content-Disposition': "attachment; filename*=UTF-8''report-original.md" }, body: '原始文件内容' });
        if (!item) return send('不存在', 404);
        if (id === 'retry' && !retryAllowed) return send('预览暂不可用', 503);
        if (id === 'missing') return send('文件不存在', 404);
        if (id === 'forbidden') return send('无权预览文件', 403);
        if (id === 'broken') return send('文件内容无法预览，请下载原件查看', 422);
        if (id === 'unsupported') return send('暂不支持该类型的预览，请下载原件查看', 415);
        if (id === 'expired' && request.headers().authorization === 'Bearer fixture') return send('登录已过期', 401);
        if (id === 'slow') { slowSeen(); await slowGate; }
        const [, filename, kind, content] = item;
        return send({ artifact_id: id, filename, media_type: 'fixture', kind, content, truncated: id === 'large', notice: ['docx', 'pdf'].includes(id) ? '内容预览不保留原文件的完整排版。' : null,
          sheets: kind === 'sheets' ? [{ name: '统计', rows: [['月份', '金额'], ['十一月', '100'], ['公式', '=1+1'], ['文本', '<script>不可执行</script>']] }, ...(id === 'xlsx' ? [{ name: '备注', rows: [['第二个工作表']] }] : [])] : [] });
      }
      unexpected.push(path); return send('未定义的测试接口', 404);
    });
    await page.goto(`${base}/agents/11/sessions/preview`);
    const generated = page.getByRole('group', { name: '生成产物', exact: true });
    await generated.waitFor();
    assert.equal(requests.filter(req => req.path.endsWith('/preview') && req.path.startsWith('/api/v1/artifacts')).length, 0); checks++;
    assert.equal(await generated.getByRole('button', { name: '预览 已删除.md', exact: true }).isDisabled(), true); checks++;
    const open = async filename => { await generated.getByRole('button', { name: `预览 ${filename}`, exact: true }).click(); await page.getByRole('dialog', { name: '文件预览', exact: true }).waitFor(); };
    const drawer = () => page.getByRole('dialog', { name: '文件预览', exact: true });
    const close = async () => { await drawer().getByRole('button', { name: 'Close', exact: true }).click(); await drawer().waitFor({ state: 'hidden' }); };
    for (const [id, filename, kind, content] of entries.slice(0, 9)) {
      await open(filename);
      await drawer().getByText('正在加载预览…', { exact: true }).waitFor({ state: 'hidden' });
      if (kind === 'html') {
        const frame = page.frameLocator('.artifact-preview-html');
        await frame.locator('body').getByText(id === 'html' ? 'HTML 报告' : id === 'docx' ? 'Word 报告' : '第 1 页', { exact: true }).waitFor();
        assert.equal(await drawer().locator('iframe').getAttribute('sandbox'), ''); checks++;
        assert.ok((await frame.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute('content')).includes("default-src 'none'")); checks++;
      } else if (kind === 'sheets') {
        await drawer().getByRole('cell', { name: '十一月', exact: true }).waitFor();
        assert.equal(await drawer().getByRole('cell', { name: '=1+1', exact: true }).count(), 1); checks++;
        assert.equal(await drawer().locator('script').count(), 0); checks++;
        if (id === 'xlsx') { await drawer().getByRole('tab', { name: '备注', exact: true }).click(); await drawer().getByRole('cell', { name: '第二个工作表', exact: true }).waitFor(); checks++; }
      } else if (kind === 'markdown') {
        await drawer().getByRole('heading', { name: '差旅报告', exact: true }).waitFor();
        assert.equal(await drawer().locator('.message-code-block').count(), 1); checks++;
        await drawer().getByRole('button', { name: '复制文档内容', exact: true }).click();
        assert.equal(await page.evaluate(() => navigator.clipboard.readText()), content); checks++;
        const download = page.waitForEvent('download'); await drawer().getByRole('button', { name: '下载原件', exact: true }).click();
        assert.equal((await download).suggestedFilename(), 'report-original.md'); checks++;
      } else {
        await drawer().locator('.artifact-preview-text').waitFor();
        assert.equal(await drawer().locator('.artifact-preview-text').innerText(), content); checks++;
        assert.equal(await drawer().locator('script').count(), 0); checks++;
      }
      if (id === 'large') { await drawer().getByText('内容较长，仅展示部分预览；请下载原件查看完整内容。', { exact: true }).waitFor(); checks++; }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); checks++;
      if (id === 'xlsx' || id === 'markdown') await page.screenshot({ path: join(tmpdir(), `y-point-preview-${id}-${layout}.png`), animations: 'disabled' });
      const position = await page.locator('.sessions-history').evaluate(el => el.scrollTop);
      await close();
      assert.equal(await page.locator('.sessions-history').evaluate(el => el.scrollTop), position); checks++;
    }
    for (const [filename, error] of [['重试.md', '预览暂不可用'], ['撤权.md', '无权预览文件'], ['缺失.md', '文件不存在'], ['损坏.docx', '文件内容无法预览，请下载原件查看'], ['未知.bin', '暂不支持该类型的预览，请下载原件查看']]) {
      await open(filename); await drawer().getByText(error, { exact: true }).waitFor();
      assert.equal(await drawer().locator('.artifact-preview-content').count(), 0); checks++;
      if (filename === '重试.md') { retryAllowed = true; await drawer().getByRole('button', { name: /重\s*试/ }).click(); await drawer().getByRole('heading', { name: '重试成功', exact: true }).waitFor(); checks++; }
      await close();
    }
    await open('续期.md'); await drawer().getByRole('heading', { name: '续期成功', exact: true }).waitFor();
    assert.equal(requests.filter(req => req.path === '/api/v1/auth/refresh').length, 1); checks++;
    assert.equal(requests.filter(req => req.path.endsWith('/expired/preview')).at(-1).auth, 'Bearer renewed'); checks++;
    await close();
    const priorMarkdownReads = requests.filter(req => req.path.endsWith('/markdown/preview')).length;
    await open('等待.md'); await slowStarted; await drawer().getByText('正在加载预览…').waitFor(); await close();
    await open('差旅报告.md'); releaseSlow();
    await drawer().getByRole('heading', { name: '差旅报告', exact: true }).waitFor();
    assert.equal(await drawer().getByRole('heading', { name: '旧的预览', exact: true }).count(), 0); checks++;
    assert.ok(requests.filter(req => req.path.endsWith('/markdown/preview')).length > priorMarkdownReads); checks++;
    await close();
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); assert.deepEqual(remote, []); checks += 3;
    assert.equal(await page.evaluate(() => window.previewInjected), undefined); checks++;
    await context.close();
    console.log(`${layout}: 文档预览、工作表、隔离、鉴权、错误重试、关闭竞态及下载通过`);
  }
  console.log(`产物预览浏览器契约回归通过：${checks} 项断言；使用虚构接口响应。`);
} finally { await browser.close(); }
