import { screenshotDirectory } from "./screenshot-paths.mjs";
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const baseURL = process.env.WEB_BASE_URL || 'http://127.0.0.1:5174';
const screenshots = await screenshotDirectory(process.env.KNOWLEDGE_FORMAT_SCREENSHOT_DIR);
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let assertions = 0;
const check = (condition, label) => { assert.ok(condition, label); assertions++; };
const now = new Date().toISOString();
const formats = [
  { format: 'html', source: { kind: 'html', format: 'html', element: 'p', element_id: 'approval', heading_path: ['差旅制度', '审批与额度'], line: 8, role: 'text' },
    content: '审批部门为财务部。<script>window.untrustedExecuted=true</script>', label: 'HTML 来源', expected: ['HTML 主文', '审批与额度', '源文件第 8 行'] },
  { format: 'json', source: { kind: 'config', format: 'json', pointer: '/service', paths: ['/service/approval_department', '/service/' + 'long_path_'.repeat(15)] },
    content: '/service/approval_department（str）= "财务部"', label: '配置来源', expected: ['JSON 配置 · /service', 'JSON Pointer', '/service/approval_department'] },
  { format: 'yaml', source: { kind: 'config', format: 'yaml', pointer: '/service', paths: ['/service/retry_count'], line_start: 4, line_end: 5 },
    content: '/service/retry_count（int）= 3', label: '配置来源', expected: ['YAML 配置 · /service', '值定义于源文件第 4–5 行', '/service/retry_count'] },
  { format: 'pptx', source: { kind: 'slide', format: 'pptx', slide: 2, slide_title: '差旅报销制度', shape_id: '4', role: 'notes', reading_order: 'shape-tree' },
    content: '讲解备注：报销凭证需保留原件。', label: '幻灯片来源', expected: ['PPTX · 第 2 页', '演讲者备注 · 与正文分开', '对象 4'] },
];
try {
  for (const width of [1280, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 920 } });
    await page.routeWebSocket('**', socket => socket.close());
    await page.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture'));
    const errors = [], unexpected = [], uploads = [];
    let denied = false;
    page.on('pageerror', error => errors.push(error.message));
    const base = { id: 1, owner_id: 1, name: '分类文档知识库', revision: 5, active_index_id: 2, search_mode: 'keyword', embedding_model_id: null, description: null,
      chunk_size: 800, chunk_overlap: 100, embedding_token_budget: null, embedding_cost_budget: null, processing_mode: 'auto', processing_model_id: null,
      processing_token_budget: null, processing_cost_budget: null, create_time: now, update_time: now };
    const documents = formats.map((item, index) => ({ id: index + 1, filename: `policy.${item.format}`, current_version: 1, update_time: now }));
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, message: status === 404 ? '知识文档不可访问' : '操作成功', data }) });
      if (path.endsWith('/auth/me')) return send({ id: 1, username: 'format-reviewer', role: 'admin' });
      if (path === '/api/v1/knowledge-bases/1') return send(base);
      if (path.endsWith('/jobs')) return send([]);
      if (path.endsWith('/documents') && request.method() === 'POST') {
        uploads.push(request.postData() || '');
        return send({ revision: 6, job_id: null, document: documents[0], deduplicated: true }, 202);
      }
      if (path.endsWith('/documents')) return send(documents);
      const match = path.match(/\/documents\/(\d+)\/processing-preview$/);
      if (match) {
        if (denied) return send(null, 404);
        const id = Number(match[1]), item = formats[id - 1];
        const evidence = { citation: `kb:1:i:2:c:${id}`, knowledge_base_id: 1, index_id: 2, document_id: id, document_version: 1, chunk_id: id, filename: documents[id - 1].filename,
          sha256: 'a'.repeat(64), start_offset: 0, end_offset: item.content.length, page: item.source.slide || null, heading: null, content: item.content,
          source_locator: item.source, knowledge_notes: item.format === 'json' ? '模型生成的字段解释，原始值请核对证据。' : null };
        return send({ index_id: 2, current_version: 1, indexed_version: 1, processing_options: {},
          processing_metadata: { format: item.format, block_count: 1, selection: 'main', excluded_regions: 4, slide_count: 3, hidden_slides: 1, notes_blocks: 1,
            warnings: item.format === 'pptx' ? ['图片、图表及复杂版面请核对原文件。'] : [] }, items: [evidence], has_more: false, next_after: 0 });
      }
      unexpected.push(path); return route.abort();
    });
    await page.goto(`${baseURL}/knowledge-bases/1`);
    for (let index = 0; index < formats.length; index++) {
      const item = formats[index], name = documents[index].filename;
      const row = page.locator('tr, .ant-list-item').filter({ hasText: name });
      await row.getByRole('button', { name: '加工结果', exact: true }).click();
      const preview = page.getByRole('dialog', { name: `加工结果 · ${name}`, exact: true });
      await preview.getByLabel(item.label).waitFor();
      await page.waitForFunction(width => {
        const panel = document.querySelector('.ant-drawer-open [role="dialog"]');
        const bounds = panel?.getBoundingClientRect();
        return bounds && bounds.width > 0 && bounds.x >= -1 && bounds.right <= width + 1;
      }, width);
      const bounds = await preview.boundingBox();
      check(bounds && bounds.x >= -1 && bounds.x + bounds.width <= width + 1, `${width} ${item.format} 抽屉完整进入视口`);
      const text = await preview.innerText();
      for (const expected of item.expected) check(text.includes(expected), `${width} ${item.format} 来源：${expected}`);
      check(text.includes('已按文档结构提取 1 条证据'), `${width} ${item.format} 解析摘要`);
      check((await preview.locator('.kb-quoted-content').innerText()) === item.content, `${width} ${item.format} 原文独立展示`);
      if (item.format === 'json') check(text.includes('模型解释 · 辅助检索'), `${width} 模型解释与配置值分开`);
      if (item.format === 'pptx') check(text.includes('排除隐藏页 1 页') && text.includes('备注证据 1 条'), `${width} 幻灯片隐藏/备注摘要`);
      check(!await page.evaluate(() => Boolean(window.untrustedExecuted)), `${width} 原始文本不执行 HTML`);
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width} ${item.format} 长来源无横向溢出`);
      await page.screenshot({ path: `${screenshots}/web-knowledge-format-${item.format}-${width}.png`, animations: 'disabled' });
      await preview.getByRole('button', { name: 'Close', exact: true }).click();
      await preview.waitFor({ state: 'hidden' });
    }
    for (const extension of ['html', 'htm', 'json', 'yaml', 'yml', 'pptx']) {
      await page.getByRole('button', { name: '上传文档', exact: true }).click();
      const upload = page.getByRole('dialog', { name: '上传文档', exact: true });
      await upload.locator('input[type=file]').setInputFiles({ name: `new.${extension}`, mimeType: 'application/octet-stream', buffer: Buffer.from('fixture for UI contract') });
      await upload.getByText('按格式保留可追溯结构', { exact: true }).waitFor();
      check(await upload.getByRole('button', { name: '上传并建立索引' }).isEnabled(), `${width} 允许上传 ${extension}`);
      await upload.getByRole('button', { name: '上传并建立索引' }).click();
      await upload.waitFor({ state: 'hidden' });
      check(uploads.at(-1).includes(`filename="new.${extension}"`), `${width} ${extension} 进入正式 multipart 上传`);
    }
    denied = true;
    await page.locator('tr, .ant-list-item').filter({ hasText: documents[0].filename }).getByRole('button', { name: '加工结果', exact: true }).click();
    await page.getByText('知识文档不可访问', { exact: true }).waitFor();
    check(await page.locator('.kb-quoted-content').count() === 0, `${width} 权限错误清除旧证据`);
    check(errors.length === 0, `${width} 页面无异常：${errors.join(', ')}`);
    check(unexpected.length === 0, `${width} 接口覆盖完整：${unexpected.join(', ')}`);
    await page.close();
  }
  console.log(`knowledge formats verified: ${assertions} assertions; screenshots ${screenshots}`);
} finally { await browser.close(); }
