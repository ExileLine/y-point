import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const baseURL = process.env.WEB_BASE_URL || 'http://127.0.0.1:5174';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let assertions = 0;
function check(condition, label) { assert.ok(condition, label); assertions++; }
const now = new Date().toISOString();
try {
  for (const width of [1280, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 920 } });
    await page.routeWebSocket('**', socket => socket.close());
    await page.addInitScript(() => { sessionStorage.setItem('yp.access_token', 'fixture'); sessionStorage.setItem('yp.refresh_token', 'fixture-refresh'); });
    const errors = [], unexpected = [], requests = [];
    page.on('pageerror', error => errors.push(error.message));
    let denied = false, previewIndex = 2;
    let knowledge = { id: 1, owner_id: 1, name: '采购资料', revision: 3, active_index_id: 2, search_mode: 'keyword', embedding_model_id: null,
      processing_mode: 'enhanced', processing_model_id: 1, processing_token_budget: 100000, processing_cost_budget: null,
      description: null, chunk_size: 800, chunk_overlap: 100, embedding_token_budget: 200000, embedding_cost_budget: null, create_time: now, update_time: now };
    const documents = [{ id: 1, knowledge_base_id: 1, filename: '报价.xlsx', current_version: 2, processing_options: { header_rows: 2, sheet_names: ['报价'] }, update_time: now }];
    const evidence = { citation: 'kb:1:i:2:c:16', knowledge_base_id: 1, index_id: 2, document_id: 1, document_version: 1, chunk_id: 16, filename: '报价.xlsx', sha256: 'a'.repeat(64),
      start_offset: 0, end_offset: 35, page: null, heading: '报价 · A1:C8', content: '供应商[A]="甲公司"；单价（元）[B]="3800"；数量（吨）[C]="50"',
      knowledge_notes: '采购定价依据，单价单位为元，数量单位为吨。', source_locator: { kind: 'table', format: 'xlsx', sheet: '报价', range: 'A3:C3', header_range: 'A1:C2', columns: ['A', 'B', 'C'], hidden_excluded: true } };
    const job = { id: 2, index_id: 2, owner_id: 1, state: 'completed', attempts: 1, failure_code: null, progress: [{ stage: 'enriching', time: now }, { stage: 'completed', time: now }],
      reserved_tokens: 0, embedding_tokens: 0, estimated_cost: null, processing_call_count: 2, processing_tokens: 120, processing_reserved_tokens: 3500, processing_estimated_cost: '0.000140', processing_usage: [{ currency: 'USD' }], create_time: now, update_time: now };
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
      const raw = request.postData() || '';
      const body = request.headers()['content-type']?.includes('application/json') && raw ? JSON.parse(raw) : null;
      requests.push({ path, method, raw, body });
      const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, message: status === 404 ? '知识文档不存在' : '操作成功', data }) });
      if (path.endsWith('/auth/me')) return send({ id: 1, username: 'table-reviewer', role: 'admin' });
      if (path === '/api/v1/models') return send([{ id: 1, display_name: '文档理解模型', status: 1, capabilities: ['text'], verified_capabilities: ['text'] }]);
      if (path.endsWith('/documents/1/processing-preview')) {
        if (denied) return send(null, 404);
        const after = Number(url.searchParams.get('after') || -1);
        return send({ index_id: previewIndex, current_version: 2, indexed_version: 1, processing_options: { header_rows: 2, sheet_names: ['报价'] },
          items: after < 0 ? [evidence] : [{ ...evidence, citation: 'kb:1:i:2:c:17', chunk_id: 17, content: '供应商[A]="乙公司"；单价（元）[B]="3900"' }], has_more: after < 0, next_after: after < 0 ? 0 : 1 });
      }
      if (path === '/api/v1/knowledge-bases/1/documents' && method === 'POST') return send({ revision: ++knowledge.revision, job_id: 2, deduplicated: false, document: documents[0] }, 202);
      if (path === '/api/v1/knowledge-bases/1/documents') return send(documents);
      if (path === '/api/v1/knowledge-bases/1/jobs') return send([job]);
      if (path === '/api/v1/knowledge-bases/1/search') return send({ hits: [{ ...evidence, score: 0.016 }], no_answer: false, byte_count: 200, untrusted_data: true });
      if (path === '/api/v1/knowledge-bases/1/citations/16') return send(evidence);
      if (path === '/api/v1/knowledge-bases/1' && method === 'PUT') {
        knowledge = { ...knowledge, ...body, revision: knowledge.revision + 1 };
        return send({ knowledge_base: knowledge, job_id: 2 });
      }
      if (path === '/api/v1/knowledge-bases/1') return send(knowledge);
      unexpected.push(`${method} ${path}`);
      return route.abort();
    });
    await page.goto(`${baseURL}/knowledge-bases/1`);
    await page.getByRole('button', { name: '加工结果', exact: true }).click();
    const preview = page.getByRole('dialog', { name: '加工结果 · 报价.xlsx' });
    await preview.getByText('当前展示旧版本，新版本尚未完整发布').waitFor();
    check((await preview.innerText()).includes('A3:C3'), `${width} 显示单元格来源`);
    check((await preview.innerText()).includes('隐藏行列'), `${width} 说明隐藏区域排除`);
    check((await preview.locator('.kb-quoted-content').innerText()).includes('3800'), `${width} 保留原始数值`);
    check((await preview.locator('.kb-generated-notes').innerText()).includes('模型生成'), `${width} 模型解释独立标记`);
    await preview.getByRole('button', { name: '加载更多证据' }).click();
    await preview.getByText('乙公司', { exact: false }).waitFor();
    check(await preview.locator('.kb-quoted-content').count() === 2, `${width} 预览分页接续`);
    await page.screenshot({ path: `/tmp/y-point-knowledge-processing-${width}.png`, fullPage: false, animations: 'disabled' });
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await preview.getByRole('button', { name: '加载更多证据' }).waitFor();
    previewIndex = 3;
    await preview.getByRole('button', { name: '加载更多证据' }).click();
    await preview.getByText('索引 #3 · 已索引文档 v1 · 当前上传 v2', { exact: true }).waitFor();
    check(await preview.locator('.kb-quoted-content').count() === 1 && (await preview.locator('.kb-quoted-content').innerText()).includes('甲公司'), `${width} 切代后丢弃旧游标并从首页预览`);
    denied = true;
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await preview.getByRole('alert').filter({ hasText: '知识文档不存在' }).waitFor();
    check(await preview.locator('.kb-quoted-content').count() === 0, `${width} 权限失效清除证据`);
    await preview.getByRole('button', { name: 'Close', exact: true }).click();
    await page.getByRole('button', { name: '设置', exact: true }).click();
    const settings = page.getByRole('dialog', { name: '知识库设置' });
    await settings.locator('.ant-form-item').filter({ hasText: '文档处理方式' }).locator('.ant-select-selector').click();
    await page.locator('.ant-select-dropdown:visible').getByText('保留原文 · 不调用加工模型', { exact: true }).click();
    check((await settings.innerText()).includes('不产生加工模型费用'), `${width} 原文模式说明`);
    await settings.getByRole('button', { name: '保存并重建' }).click();
    await settings.waitFor({ state: 'hidden' });
    const update = requests.findLast(row => row.method === 'PUT');
    check(update.body.processing_mode === 'original' && update.body.processing_model_id === 1, `${width} 加工配置随版本保存`);
    check(update.body.processing_token_budget === 100000, `${width} 加工预算独立保存`);
    check(update.body.embedding_token_budget === 200000, `${width} 保留既有嵌入预算`);
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await settings.getByText('分块与索引预算', { exact: true }).click();
    const processingBudget = settings.getByLabel('单次加工 Token 预留上限', { exact: true });
    const embeddingBudget = settings.getByLabel('单次索引嵌入 Token 预算', { exact: true });
    check(await processingBudget.inputValue() === '100000' && await embeddingBudget.inputValue() === '200000', `${width} 既有预算回显`);
    await processingBudget.fill('');
    await embeddingBudget.fill('');
    await settings.getByRole('button', { name: '保存并重建' }).click();
    await settings.waitFor({ state: 'hidden' });
    const uncapped = requests.findLast(row => row.method === 'PUT');
    check(uncapped.body.processing_token_budget === null && uncapped.body.embedding_token_budget === null, `${width} 清空预算显式提交 null`);
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await settings.getByText('分块与索引预算', { exact: true }).click();
    check(await processingBudget.inputValue() === '' && await embeddingBudget.inputValue() === '', `${width} 不限预算重新打开仍为空`);
    check((await settings.innerText()).includes('模型单次请求限制与费用预算仍生效'), `${width} 说明累计预算和单次模型限制`);
    await settings.getByRole('button', { name: 'Close', exact: true }).click();
    await page.getByRole('button', { name: '上传文档', exact: true }).click();
    const upload = page.getByRole('dialog', { name: '上传文档', exact: true });
    await upload.locator('input[type=file]').setInputFiles({ name: '采购.csv', mimeType: 'text/csv', buffer: Buffer.from('供应商,单价\n甲,3800\n') });
    await upload.getByLabel('表头行数', { exact: true }).fill('2');
    await upload.getByRole('button', { name: '上传并建立索引' }).click();
    await upload.waitFor({ state: 'hidden' });
    const posted = requests.findLast(row => row.method === 'POST' && row.path.endsWith('/documents'));
    check(posted.raw.includes('采购.csv') && posted.raw.includes('processing_options'), `${width} CSV 与处理覆盖选项提交`);
    check(posted.raw.includes('"header_rows":2'), `${width} 表头行数传入正式上传请求`);
    const task = page.getByRole('dialog', { name: '索引任务 #2', exact: true });
    await task.getByText('加工模型调用', { exact: true }).waitFor();
    check((await task.innerText()).includes('120'), `${width} 展示加工已报告用量`);
    check((await task.innerText()).includes('USD'), `${width} 加工费用标记币种`);
    check((await task.innerText()).includes('生成知识解释'), `${width} 展示独立加工阶段`);
    await task.getByRole('button', { name: 'Close', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.ant-drawer-content-wrapper:not(.ant-drawer-content-wrapper-hidden)'));
    await page.getByRole('tab', { name: '检索测试', exact: true }).click();
    await page.getByRole('textbox', { name: '检索问题' }).fill('采购定价依据');
    await page.getByRole('button', { name: '检索测试', exact: true }).click();
    await page.locator('.kb-hit').waitFor();
    check((await page.locator('.kb-hit').innerText()).includes('A3:C3'), `${width} 检索展示表格定位`);
    check((await page.locator('.kb-hit').innerText()).includes('模型解释'), `${width} 检索区分解释和证据`);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width} 页面无横向溢出`);
    check(errors.length === 0, `${width} 页面无异常：${errors.join(', ')}`);
    check(unexpected.length === 0, `${width} 所有接口契约已覆盖：${unexpected.join(', ')}`);
    await page.close();
  }
  console.log(`knowledge processing verified: ${assertions} assertions`);
} finally { await browser.close(); }
