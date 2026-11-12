import assert from 'node:assert/strict';
import { deflateSync } from 'node:zlib';
import { readFileSync } from 'node:fs';
import { chromium } from 'playwright';

const baseURL = process.env.WEB_BASE_URL || 'http://127.0.0.1:5174';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let assertions = 0;
const check = (condition, label) => { assert.ok(condition, label); assertions++; };
function crc(buffer) {
  let value = 0xffffffff;
  for (const byte of buffer) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
  }
  return (value ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const name = Buffer.from(type), size = Buffer.alloc(4), checksum = Buffer.alloc(4);
  size.writeUInt32BE(data.length); checksum.writeUInt32BE(crc(Buffer.concat([name, data])));
  return Buffer.concat([size, name, data, checksum]);
}
const header = Buffer.alloc(13); header.writeUInt32BE(600); header.writeUInt32BE(800, 4); header[8] = 8; header[9] = 2;
const pixels = Buffer.alloc(800 * (600 * 3 + 1), 255);
for (let row = 0; row < 800; row++) pixels[row * 1801] = 0;
const png = process.env.KNOWLEDGE_PDF_PAGE_FIXTURE ? readFileSync(process.env.KNOWLEDGE_PDF_PAGE_FIXTURE) : Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))]);
const now = new Date().toISOString();
try {
  for (const width of [1280, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 920 } });
    await page.routeWebSocket('**', socket => socket.close());
    await page.addInitScript(() => { sessionStorage.setItem('yp.access_token', 'fixture'); sessionStorage.setItem('yp.refresh_token', 'fixture'); });
    const errors = [], unexpected = [], requests = [];
    let denied = false;
    page.on('pageerror', error => errors.push(error.message));
    const base = { id: 1, owner_id: 1, name: 'PDF 制度库', revision: 3, active_index_id: 2, search_mode: 'keyword', embedding_model_id: null, description: null,
      chunk_size: 800, chunk_overlap: 100, embedding_token_budget: null, embedding_cost_budget: null, processing_mode: 'original', processing_model_id: null,
      processing_token_budget: null, processing_cost_budget: null, create_time: now, update_time: now };
    const document = { id: 1, knowledge_base_id: 1, filename: '报销扫描件.pdf', current_version: 1, processing_options: { pdf_mode: 'auto' }, update_time: now };
    const evidence = { citation: 'kb:1:i:2:c:16', knowledge_base_id: 1, index_id: 2, document_id: 1, document_version: 1, chunk_id: 16, filename: document.filename,
      sha256: 'a'.repeat(64), start_offset: 0, end_offset: 32, page: 1, heading: '第 1 页', content: 'Hotel allowance: 5000 CNY per trip.', knowledge_notes: null,
      source_locator: { kind: 'pdf', format: 'pdf', page: 1, page_kind: 'scan', extraction_method: 'ocr', bbox: [48, 138, 400, 155], page_width: 600, page_height: 800,
        confidence: 91.2, layout: 'unverified', review_required: true } };
    const job = { id: 2, index_id: 2, state: 'failed', owner_id: 1, attempts: 1, failure_code: 'ocr_low_confidence', progress: [], reserved_tokens: 0, embedding_tokens: 0,
      estimated_cost: null, processing_call_count: 0, processing_reserved_tokens: 0, processing_usage: [], create_time: now, update_time: now };
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      requests.push({ path, raw: request.postData() || '', auth: request.headers().authorization });
      const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, message: status === 404 ? '知识引用不存在' : '操作成功', data }) });
      if (path.endsWith('/auth/me')) return send({ id: 1, role: 'admin', username: 'pdf-reviewer' });
      if (path === '/api/v1/knowledge-bases/1') return send(base);
      if (path.endsWith('/documents/1/processing-preview')) return send({ index_id: 2, current_version: 1, indexed_version: 1, processing_options: document.processing_options,
        processing_metadata: { ocr_pages: 1, blank_pages: 1, pages: [{ page: 1, kind: 'scan', method: 'ocr', confidence: 91.2, blocks: 1 }, { page: 2, kind: 'blank', method: null, confidence: null, blocks: 0 }] },
        items: [evidence], has_more: false, next_after: 0 });
      if (path.endsWith('/citations/16/page-image')) return denied ? send(null, 404) : route.fulfill({ status: 200, contentType: 'image/png', headers: { 'Cache-Control': 'no-store' }, body: png });
      if (path.endsWith('/documents') && request.method() === 'POST') return send({ revision: 4, job_id: 2, document }, 202);
      if (path.endsWith('/documents')) return send([document]);
      if (path.endsWith('/jobs')) return send([job]);
      unexpected.push(path); return route.abort();
    });
    await page.goto(`${baseURL}/knowledge-bases/1`);
    await page.getByRole('button', { name: '加工结果', exact: true }).click();
    const preview = page.getByRole('dialog', { name: `加工结果 · ${document.filename}` });
    await preview.getByText('PDF 共 2 页 · OCR 1 页 · 空白 1 页', { exact: true }).waitFor();
    check((await preview.innerText()).includes('空白页（跳过）'), `${width} 空白页摘要`);
    check((await preview.innerText()).includes('91.2%'), `${width} OCR 置信度`);
    check((await preview.innerText()).includes('布局关系待核对'), `${width} OCR 布局提示`);
    check((await preview.innerText()).includes('原点为页面左上角'), `${width} 坐标方向`);
    await preview.getByRole('button', { name: '查看 PDF 原页 · 第 1 页' }).click();
    const original = page.getByRole('dialog', { name: `${document.filename} · 第 1 页`, exact: true });
    await original.locator('img').waitFor();
    await original.locator('img').evaluate(image => image.decode());
    check(await original.getByLabel('证据所在区域').count() === 1, `${width} 原页高亮`);
    check(requests.findLast(row => row.path.endsWith('page-image')).auth === 'Bearer fixture', `${width} 原页使用鉴权请求`);
    await page.screenshot({ path: `/tmp/y-point-knowledge-pdf-${width}.png`, fullPage: false, animations: 'disabled' });
    denied = true;
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await original.waitFor({ state: 'hidden' });
    check(await page.locator('.kb-pdf-page img').count() === 0, `${width} 聚焦复核清除旧原页`);
    await preview.getByRole('button', { name: '查看 PDF 原页 · 第 1 页' }).click();
    await original.getByRole('alert').filter({ hasText: '知识引用不存在' }).waitFor();
    check(await original.locator('img').count() === 0, `${width} 权限失败清除原页`);
    await original.getByRole('button', { name: 'Close', exact: true }).click();
    await preview.getByRole('button', { name: 'Close', exact: true }).click();
    await page.getByRole('button', { name: '上传文档', exact: true }).click();
    const upload = page.getByRole('dialog', { name: '上传文档', exact: true });
    await upload.locator('input[type=file]').setInputFiles({ name: '更新.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\nfixture') });
    await upload.locator('.ant-select[aria-label="PDF 页面提取"]').click();
    await page.locator('.ant-select-dropdown:visible').getByText('强制 OCR · 逐页识别', { exact: true }).click();
    await upload.getByRole('button', { name: '上传并建立索引' }).click();
    await upload.waitFor({ state: 'hidden' });
    check(requests.findLast(row => row.path.endsWith('/documents') && row.raw).raw.includes('"pdf_mode":"ocr"'), `${width} 提取策略随上传保存`);
    const task = page.getByRole('dialog', { name: '索引任务 #2', exact: true });
    await task.getByText('OCR 识别质量不足，未发布新索引。请上传更清晰、方向正确的扫描件或补充可选取文字的原件。').waitFor();
    check((await task.innerText()).includes('ocr_low_confidence'), `${width} 失败原因及类别`);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width} 手机无溢出`);
    check(errors.length === 0, `${width} 页面无异常：${errors.join(', ')}`);
    check(unexpected.length === 0, `${width} 接口覆盖完整：${unexpected.join(', ')}`);
    await page.close();
  }
  console.log(`knowledge PDF verified: ${assertions} assertions`);
} finally { await browser.close(); }
