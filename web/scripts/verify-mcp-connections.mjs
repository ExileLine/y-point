import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { screenshotPath } from './screenshot-paths.mjs';

const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' }) });
let checks = 0;
try {
  for (const [layout, width] of [['desktop', 1280], ['mobile', 375]]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
    await context.addInitScript(() => sessionStorage.setItem('yp.access_token', 'fixture-token'));
    const page = await context.newPage();
    const connections = [], tools = [], saved = [], errors = [], unexpected = [];
    let conflict = false, toolConflict = false, catalogRequests = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/v1/**', async route => {
      const req = route.request(), path = new URL(req.url()).pathname, method = req.method();
      const send = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status, message: status === 200 ? '操作成功' : data, data: status === 200 ? data : null }) });
      if (path === '/api/v1/auth/me') return send({ id: 1, username: 'mcp-tester', role: 'admin' });
      if (path === '/api/v1/tools/catalog') { catalogRequests++; return send({ builtin: [], user_tools: tools }); }
      if (path === '/api/v1/mcp-connections/stdio-profiles') return send([{ name: 'local_docs', env_keys: ['API_KEY'] }]);
      if (path === '/api/v1/mcp-connections' && method === 'GET') return send(connections);
      const output = (body, previous) => ({ id: previous?.id || connections.length + 1, name: body.name, transport: body.transport,
        version: (previous?.version || 0) + 1, enabled: body.enabled, share_credentials: body.share_credentials,
        has_credentials: Boolean(body.bearer_token || Object.keys(body.env || {}).length || previous?.has_credentials),
        url: body.url || null, stdio_profile: body.stdio_profile || null, args: body.args || [], credentials_revoked_through: 0 });
      if (path === '/api/v1/mcp-connections' && method === 'POST') {
        const body = req.postDataJSON(); saved.push({ method, body });
        const item = output(body); connections.push(item); return send(item);
      }
      const match = /^\/api\/v1\/mcp-connections\/(\d+)(\/discover)?$/.exec(path);
      if (match) {
        const id = Number(match[1]), index = connections.findIndex(item => item.id === id);
        if (index < 0) return send('连接不存在', 404);
        if (match[2]) return send([{ name: 'lookup', description: '查询资料', input_schema: { type: 'object' } }]);
        if (method === 'PUT') {
          const body = req.postDataJSON(); saved.push({ method, body });
          if (conflict) { conflict = false; return send('MCP 连接版本已变化，请刷新后重试', 409); }
          assert.equal(body.expected_version, connections[index].version); checks++;
          connections[index] = output(body, connections[index]); return send(connections[index]);
        }
        if (method === 'DELETE') { connections.splice(index, 1); return send(null); }
      }
      if (path === '/api/v1/tools' && method === 'POST') {
        const body = req.postDataJSON(); saved.push({ method, body });
        if (toolConflict) { toolConflict = false; return send('同名 Tool 已存在', 409); }
        const id = tools.length + 1;
        const tool = { ...body, id, name: `tool_fixture_${id}`, model_name: `user_tool_${id}_tool_fixture_${id}`, owner_id: 1, current_version: 1, enabled: true, verified: true };
        tools.push(tool); return send(tool);
      }
      const toolMatch = /^\/api\/v1\/tools\/(\d+)$/.exec(path);
      if (toolMatch) {
        const tool = tools.find(item => item.id === Number(toolMatch[1]));
        if (!tool) return send('Tool 不存在', 404);
        if (method === 'GET') return send(tool);
        if (method === 'PUT') {
          const body = req.postDataJSON(); saved.push({ method, body });
          assert.equal(body.expected_version, tool.current_version); checks++;
          Object.assign(tool, body, { current_version: tool.current_version + 1 });
          return send(tool);
        }
      }
      unexpected.push(`${method} ${path}`); return send('unexpected fixture request', 404);
    });
    await page.goto(`${base}/tools`);
    await page.getByRole('button', { name: '新建 Tool', exact: true }).click();
    const originalDraft = page.getByRole('dialog', { name: '新建 Tool', exact: true });
    await originalDraft.getByText('MCP', { exact: true }).click();
    await originalDraft.getByLabel('显示名称', { exact: true }).fill('preserved_draft');
    await originalDraft.getByRole('button', { name: '管理 MCP 连接', exact: true }).click();
    const manager = page.getByRole('dialog', { name: 'MCP 连接', exact: true });
    await manager.getByText('尚未配置 MCP 连接', { exact: true }).waitFor(); checks++;
    await manager.getByRole('button', { name: '新建连接', exact: true }).click();
    const editor = () => page.getByRole('dialog', { name: /^(新建|编辑) MCP 连接$/ });
    await editor().getByLabel('连接名称', { exact: true }).click();
    const frontmost = await editor().getByLabel('连接名称', { exact: true }).evaluate(element => {
      const rect = element.getBoundingClientRect();
      return document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2) === element;
    });
    assert.equal(frontmost, true, '连接编辑输入框必须位于最上层且能点击'); checks++;
    await editor().getByLabel('连接名称', { exact: true }).fill('共享资料服务');
    await editor().getByLabel('连接 MCP 地址', { exact: true }).fill('http://mcp.example.com:8080/mcp');
    await editor().getByLabel('连接 Bearer 凭据', { exact: true }).fill('fixture-private-token');
    await editor().getByRole('button', { name: '保存连接', exact: true }).click();
    await editor().waitFor({ state: 'hidden' });
    assert.equal(saved.at(-1).body.bearer_token, 'fixture-private-token'); checks++;
    assert.equal(saved.at(-1).body.url, 'http://mcp.example.com:8080/mcp'); checks++;
    assert.equal(tools.length, 0, '保存连接不会把连接误当作函数加入目录'); checks++;
    assert.equal(await manager.getByText('fixture-private-token', { exact: true }).count(), 0); checks++;
    await manager.getByRole('button', { name: '测试连接并发现工具', exact: true }).click();
    await manager.getByText('共享资料服务：lookup', { exact: true }).waitFor(); checks++;
    await manager.getByRole('button', { name: 'Close', exact: true }).click();
    await originalDraft.getByRole('button', { name: '管理 MCP 连接', exact: true }).click();
    await manager.getByRole('button', { name: '编辑连接', exact: true }).click();
    await editor().getByLabel('连接名称', { exact: true }).click();
    assert.equal(await editor().getByLabel('连接 Bearer 凭据', { exact: true }).inputValue(), ''); checks++;
    await editor().getByRole('switch', { name: '共享 MCP 连接凭据', exact: true }).click();
    conflict = true;
    await editor().getByRole('button', { name: '保存连接', exact: true }).click();
    await editor().getByText('MCP 连接版本已变化，请刷新后重试', { exact: true }).waitFor(); checks++;
    assert.equal(await editor().getByLabel('连接名称', { exact: true }).inputValue(), '共享资料服务'); checks++;
    await editor().getByRole('button', { name: '保存连接', exact: true }).click();
    await editor().waitFor({ state: 'hidden' });
    assert.equal(saved.at(-1).body.bearer_token, undefined); checks++;
    assert.equal(saved.at(-1).body.share_credentials, true); checks++;
    await manager.getByRole('button', { name: '新建连接', exact: true }).click();
    await editor().getByLabel('连接名称', { exact: true }).fill('本地资料服务');
    await editor().locator('.ant-select').filter({ has: page.getByRole('combobox', { name: 'MCP 传输方式', exact: true }) }).click();
    await page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').filter({ hasText: 'stdio（受控容器）' }).click();
    await editor().locator('.ant-select').filter({ has: page.getByRole('combobox', { name: 'stdio 启动配置', exact: true }) }).click();
    await page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').filter({ hasText: 'local_docs' }).click();
    await editor().getByLabel('stdio 追加参数', { exact: true }).fill('["--read-only"]');
    await editor().getByLabel('stdio 凭据环境变量', { exact: true }).fill('{"API_KEY":"fixture-env-key"}');
    await editor().getByRole('button', { name: '保存连接', exact: true }).click();
    await editor().waitFor({ state: 'hidden' });
    assert.equal(saved.at(-1).body.transport, 'stdio'); checks++;
    assert.deepEqual(saved.at(-1).body.env, { API_KEY: 'fixture-env-key' }); checks++;
    assert.equal(saved.at(-1).body.command, undefined); checks++;
    assert.equal(saved.at(-1).body.image, undefined); checks++;
    await manager.getByRole('button', { name: '编辑连接', exact: true }).nth(1).click();
    assert.equal(await editor().getByLabel('stdio 凭据环境变量', { exact: true }).inputValue(), '{}'); checks++;
    await editor().getByText(/环境变量加密保存，不回显/).waitFor(); checks++;
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); checks++;
    await page.waitForFunction(() => !document.querySelector(".ant-message-notice"));
    await page.screenshot({ path: screenshotPath(`y-point-mcp-stdio-${layout}.png`), animations: 'disabled' });
    await editor().getByRole('button', { name: 'Close', exact: true }).click();
    await manager.getByRole('button', { name: 'Close', exact: true }).click();
    assert.equal(await originalDraft.getByLabel('显示名称', { exact: true }).inputValue(), 'preserved_draft'); checks++;
    await originalDraft.getByRole('button', { name: 'Close', exact: true }).click();
    await page.getByRole('button', { name: '新建 Tool', exact: true }).click();
    const toolEditor = page.getByRole('dialog', { name: '新建 Tool', exact: true });
    await toolEditor.getByText('MCP', { exact: true }).click();
    await toolEditor.getByLabel('显示名称', { exact: true }).fill('本地资料查询 Local Lookup');
    await toolEditor.getByLabel('用途', { exact: true }).fill('读取本地资料');
    await toolEditor.getByRole('button', { name: '管理 MCP 连接', exact: true }).click();
    await manager.getByRole('button', { name: '测试连接并发现工具', exact: true }).nth(1).click();
    await manager.getByRole('button', { name: '用于当前 Tool', exact: true }).click();
    await manager.waitFor({ state: 'hidden' });
    assert.equal(await toolEditor.getByLabel('显示名称', { exact: true }).inputValue(), '本地资料查询 Local Lookup'); checks++;
    assert.equal(await toolEditor.getByLabel('用途', { exact: true }).inputValue(), '读取本地资料'); checks++;

    await toolEditor.locator('.ant-select').filter({ has: page.getByRole('combobox', { name: 'Tool MCP 连接', exact: true }) }).click();
    await page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').filter({ hasText: '本地资料服务 · stdio · v1' }).click();
    await toolEditor.getByRole('button', { name: '发现工具', exact: true }).click();
    await toolEditor.locator('.ant-select').filter({ has: page.getByRole('combobox', { name: 'MCP 远端函数名', exact: true }) }).getByText('lookup', { exact: true }).waitFor();
    await toolEditor.getByRole('button', { name: /保\s*存/ }).click();
    await toolEditor.waitFor({ state: 'hidden' });
    assert.equal(saved.at(-1).body.mcp_connection_id, 2); checks++;
    assert.equal(saved.at(-1).body.mcp_url, undefined); checks++;
    assert.equal(saved.at(-1).body.mcp_bearer_token, undefined); checks++;
    assert.equal(tools.length, 1); checks++;
    await page.getByRole('button', { name: 'MCP 连接', exact: true }).click();
    await manager.getByRole('button', { name: '测试连接并发现工具', exact: true }).first().click();
    await page.waitForFunction(() => !document.querySelector('.ant-message-notice'));
    await page.screenshot({ path: `/tmp/yp-mcp-discovered-${layout}.png`, animations: 'disabled' });
    await manager.getByRole('button', { name: '添加为 Tool', exact: true }).click();
    await manager.waitFor({ state: 'hidden' });
    const imported = page.getByRole('dialog', { name: '新建 Tool', exact: true });
    assert.equal(await imported.getByLabel('显示名称', { exact: true }).inputValue(), 'lookup'); checks++;
    assert.equal(await imported.getByLabel('用途', { exact: true }).inputValue(), '查询资料'); checks++;
    await imported.getByText('共享资料服务 · streamable_http · v2', { exact: true }).waitFor(); checks++;
    assert.equal(tools.length, 1, '选择远端函数后应先确认 Tool 信息再保存'); checks++;
    toolConflict = true;
    await imported.getByRole('button', { name: /保\s*存/ }).click();
    await imported.getByText('同名 Tool 已存在', { exact: true }).waitFor(); checks++;
    assert.equal(await imported.getByLabel('显示名称', { exact: true }).inputValue(), 'lookup'); checks++;
    await imported.getByLabel('显示名称', { exact: true }).fill('远端天气 Weather');
    const previousCatalogRequests = catalogRequests;
    await imported.getByRole('button', { name: /保\s*存/ }).click();
    await imported.waitFor({ state: 'hidden' });
    await page.locator('.tools-page').getByText('远端天气 Weather', { exact: true }).waitFor(); checks++;
    assert.equal(tools.length, 2); checks++;
    assert.equal(saved.at(-1).body.mcp_connection_id, 1); checks++;
    assert.equal(saved.at(-1).body.mcp_tool_name, 'lookup'); checks++;
    assert.equal(saved.at(-1).body.display_name, '远端天气 Weather'); checks++;
    assert.equal(saved.at(-1).body.name, undefined, '前端只提交显示名称，由系统生成内部标识'); checks++;
    const stableModelName = tools[1].model_name;
    const editButtons = width < 768 ? page.locator('.resource-mobile-list').getByRole('button', { name: '编辑', exact: true }) : page.getByRole('button', { name: '编辑', exact: true });
    await editButtons.nth(1).click();
    const editing = page.getByRole('dialog', { name: '编辑 Tool', exact: true });
    assert.equal(await editing.getByLabel('显示名称', { exact: true }).inputValue(), '远端天气 Weather'); checks++;
    assert.equal(await editing.getByLabel('显示名称', { exact: true }).isEnabled(), true); checks++;
    await editing.getByText(stableModelName, { exact: true }).waitFor(); checks++;
    await editing.getByLabel('显示名称', { exact: true }).fill('天气预报 新版 V2');
    await page.waitForFunction(() => !document.querySelector('.ant-message-notice'));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); checks++;
    await page.screenshot({ path: `/tmp/yp-tool-display-name-${layout}.png`, animations: 'disabled' });
    await editing.getByRole('button', { name: /保\s*存/ }).click();
    await editing.waitFor({ state: 'hidden' });
    await page.locator('.tools-page').getByText('天气预报 新版 V2', { exact: true }).waitFor(); checks++;
    assert.equal(tools[1].model_name, stableModelName); checks++;
    assert.equal(saved.at(-1).body.display_name, '天气预报 新版 V2'); checks++;
    assert.equal(saved.at(-1).body.name, undefined); checks++;
    assert.equal(saved.at(-1).body.mcp_tool_name, 'lookup'); checks++;

    assert.equal(catalogRequests > previousCatalogRequests, true, 'Tool 保存成功后必须刷新工具目录'); checks++;
    assert.deepEqual(errors, []); checks++;
    assert.deepEqual(unexpected, []); checks++;
    await context.close();
    console.log(`${layout}: HTTP/stdio 连接、重复打开抽屉点击、草稿保留、发现函数创建 Tool 及目录刷新通过`);
  }
  console.log(`MCP 连接浏览器回归通过：${checks} 项断言；使用受控接口响应。`);
} finally { await browser.close(); }
