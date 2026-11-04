import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
const now = '2026-11-04T12:00:00+08:00';
const binary = Buffer.from([0x50, 0x4b, 0, 255, 128, 10, 13]);
const html = '<!doctype html><script>window.artifactExecuted=true</script><h1>报告</h1>';
const files = [
  { artifact_id: 'report', filename: '原始报告.docx', available: true },
  { artifact_id: 'html', filename: '页面.html', available: true },
  { artifact_id: 'json', filename: '数据.json', available: true },
  { artifact_id: 'missing', filename: '已清理.txt', available: true },
  { artifact_id: 'forbidden', filename: '权限变化.txt', available: true },
  { artifact_id: 'offline', filename: '网络故障.txt', available: true },
  { artifact_id: 'expired', filename: '刷新令牌.txt', available: true },
  { artifact_id: 'retry', filename: '重试.txt', available: true },
  { artifact_id: 'deleted', filename: '已删除.txt', available: false },
];
let assertions = 0;
async function fixture(page, refreshAllowed = true) {
  await page.addInitScript(() => {
    sessionStorage.setItem('yp.access_token', 'fixture');
    sessionStorage.setItem('yp.refresh_token', 'refresh-fixture');
  });
  const session = { id: 'downloads', agent_id: 11, title: '产物下载验证', archived: false, version: 1, create_time: now, update_time: now };
  const calls = [], unexpected = [];
  let releaseReport, reportSeen, delayed = true, retry = 0, late = false;
  const reportReady = new Promise(resolve => { reportSeen = resolve; });
  const reportGate = new Promise(resolve => { releaseReport = resolve; });
  await page.route('**/api/v1/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    calls.push({ path, auth: req.headers().authorization });
    let data;
    if (/\/sessions\/[^/]+\/models$/.test(path)) data = [];
    else if (path === '/api/v1/auth/me') data = { id: 1, username: 'tester', role: 'user' };
    else if (path === '/api/v1/auth/refresh') {
      assert.equal(req.postDataJSON().refresh_token, 'refresh-fixture'); assertions++;
      if (!refreshAllowed) return route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ code: 10001, message: '登录已过期' }) });
      data = { access_token: 'renewed-fixture', refresh_token: 'refresh-fixture', expires_in: 3600 };
    } else if (path === '/api/v1/agents/11') data = { id: 11, name: '下载验证', enabled: true, default_model_id: 1 };
    else if (path === '/api/v1/agents/11/sessions') data = { items: [session], total: 1, page: 1, page_size: 20 };
    else if (path.endsWith('/sessions/downloads')) data = session;
    else if (path.endsWith('/downloads/runs')) data = { items: [], total: 0, page: 1, page_size: 20 };
    else if (path.endsWith('/downloads/messages')) data = [
      { id: 1, run_id: 'run-file', role: 'user', content: '请生成文件', create_time: now, attachments: [{ artifact_id: 'input', filename: '参考.md', available: true, included_in_context: true }], artifacts: [] },
      { id: 2, run_id: 'run-file', role: 'assistant', content: '文件已生成。', create_time: now, attachments: [], artifacts: files },
      ...(late ? [{ id: 3, run_id: 'late-run', role: 'assistant', content: '回答导出', create_time: now, attachments: [], artifacts: [{ artifact_id: 'late', filename: 'answer.txt', available: true }] }] : []),
    ];
    else if (path.startsWith('/api/v1/artifacts/')) {
      assert.ok(req.headers().authorization?.startsWith('Bearer ')); assertions++;
      const id = path.split('/')[4];
      if (id === 'report') {
        if (delayed) { reportSeen(); await reportGate; delayed = false; }
        return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', headers: { 'Content-Disposition': `attachment; filename*=utf-8''${encodeURIComponent('差旅分析报告.docx')}` }, body: binary });
      }
      if (id === 'html') return route.fulfill({ contentType: 'text/html', headers: { 'Content-Disposition': 'attachment; filename="report.html"' }, body: html });
      if (id === 'json') return route.fulfill({ contentType: 'application/json', headers: { 'Content-Disposition': "attachment; filename*=utf-8''%ZZ" }, body: '{"ok":true}' });
      if (id === 'missing' || id === 'forbidden' || (id === 'retry' && retry++ === 0)) return route.fulfill({ status: id === 'forbidden' ? 403 : 404, contentType: 'application/json', body: JSON.stringify({ code: 10002, message: id === 'forbidden' ? '无权下载文件' : '文件不存在' }) });
      if (id === 'offline') return route.abort('failed');
      if (id === 'expired' && req.headers().authorization === 'Bearer fixture') return route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ code: 10001, message: '登录已过期' }) });
      if (['expired', 'retry', 'input', 'late'].includes(id)) return route.fulfill({ contentType: 'text/plain', body: `file:${id}` });
      unexpected.push(path); return route.abort();
    } else { unexpected.push(path); return route.abort(); }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, message: '操作成功', data }) });
  });
  return { calls, unexpected, reportReady, releaseReport, publish() { late = true; } };
}
async function bytes(download) {
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  return Buffer.concat(chunks);
}
try {
  for (const [layout, width, height] of [['desktop', 1280, 900], ['mobile', 375, 812]]) {
    const context = await browser.newContext({ viewport: { width, height }, acceptDownloads: true });
    const page = await context.newPage(), errors = [], downloads = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('download', download => downloads.push(download));
    const f = await fixture(page);
    await page.goto(`${base}/agents/11/sessions/downloads`);
    const artifacts = page.getByRole('group', { name: '生成产物', exact: true });
    await artifacts.getByRole('button', { name: '下载 原始报告.docx', exact: true }).waitFor();
    await page.getByRole('button', { name: '文件不可用，无法下载', exact: true }).isDisabled().then(disabled => assert.equal(disabled, true)); assertions++;
    assert.ok((await page.getByRole('group', { name: '消息附件' }).innerText()).includes('已提供给模型')); assertions++;
    const button = artifacts.getByRole('button', { name: '下载 原始报告.docx', exact: true });
    const first = page.waitForEvent('download');
    await button.click(); await f.reportReady;
    await button.dispatchEvent('click');
    assert.equal(f.calls.filter(call => call.path.endsWith('/report/download')).length, 1); assertions++;
    await page.waitForFunction(() => document.querySelector('[aria-label="下载 原始报告.docx"]').classList.contains('ant-btn-loading'));
    const position = await page.locator('.sessions-history').evaluate(el => el.scrollTop);
    f.releaseReport();
    const report = await first;
    assert.equal(report.suggestedFilename(), '差旅分析报告.docx');
    assert.deepEqual(await bytes(report), binary); assertions += 2;
    await page.waitForFunction(() => !document.querySelector('[aria-label="下载 原始报告.docx"]').classList.contains('ant-btn-loading'));
    assert.equal(await page.locator('.sessions-history').evaluate(el => el.scrollTop), position); assertions++;
    await page.screenshot({ path: join(tmpdir(), `y-point-artifacts-${layout}.png`), animations: 'disabled' });
    for (const [label, name, content] of [
      ['页面.html', 'report.html', html], ['数据.json', '数据.json', '{"ok":true}'],
      ['参考.md', '参考.md', 'file:input'], ['刷新令牌.txt', '刷新令牌.txt', 'file:expired'],
    ]) {
      const download = page.waitForEvent('download');
      await page.getByRole('button', { name: `下载 ${label}`, exact: true }).click();
      const result = await download;
      assert.equal(result.suggestedFilename(), name); assert.equal((await bytes(result)).toString(), content); assertions += 2;
    }
    assert.equal(f.calls.filter(call => call.path === '/api/v1/auth/refresh').length, 1);
    assert.equal(f.calls.filter(call => call.path.endsWith('/expired/download')).at(-1).auth, 'Bearer renewed-fixture'); assertions += 2;
    for (const [label, error] of [['已清理.txt', '文件不存在'], ['权限变化.txt', '无权下载文件'], ['网络故障.txt', '无法连接服务端'], ['重试.txt', '文件不存在']]) {
      const count = downloads.length;
      await page.getByRole('button', { name: `下载 ${label}`, exact: true }).click();
      await page.locator('.ant-message-notice').getByText(error, { exact: true }).last().waitFor();
      assert.equal(downloads.length, count); assertions++;
    }
    const retry = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载 重试.txt', exact: true }).click();
    assert.equal((await bytes(await retry)).toString(), 'file:retry'); assertions++;
    f.publish();
    await page.getByRole('button', { name: '刷新会话', exact: true }).click();
    const late = page.getByRole('button', { name: '下载 answer.txt', exact: true }); await late.waitFor();
    const exported = page.waitForEvent('download'); await late.click();
    assert.equal((await bytes(await exported)).toString(), 'file:late'); assertions++;
    assert.equal(await page.evaluate(() => window.artifactExecuted), undefined);
    assert.equal(new URL(page.url()).pathname, '/agents/11/sessions/downloads');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []); assertions += 5;
    const target = await late.boundingBox(); assert.ok(target.width >= 44 && target.height >= 44); assertions++;
    await context.close(); console.log(`${layout}: 产物/附件下载、文件名与字节、HTML 安全、鉴权刷新、失败重试与延迟产物通过`);
  }
  const expiredContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const expiredPage = await expiredContext.newPage();
  const expiredDownloads = [];
  expiredPage.on('download', download => expiredDownloads.push(download));
  const expiredFixture = await fixture(expiredPage, false);
  await expiredPage.goto(`${base}/agents/11/sessions/downloads`);
  await expiredPage.getByRole('button', { name: '下载 刷新令牌.txt', exact: true }).click();
  await expiredPage.getByRole('button', { name: /登\s*录/, exact: true }).waitFor();
  assert.equal(await expiredPage.evaluate(() => sessionStorage.getItem('yp.access_token')), null);
  assert.equal(await expiredPage.evaluate(() => sessionStorage.getItem('yp.refresh_token')), null);
  assert.equal(expiredFixture.calls.filter(call => call.path.endsWith('/expired/download')).length, 1);
  assert.equal(expiredFixture.calls.filter(call => call.path === '/api/v1/auth/refresh').length, 1);
  assert.equal(expiredDownloads.length, 0); assertions += 5;
  await expiredContext.close();
  console.log(`产物下载浏览器契约回归通过：${assertions} 项断言；使用虚构接口响应，不访问真实业务数据。`);
} finally { await browser.close(); }
