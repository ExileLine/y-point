import assert from 'node:assert/strict';
import { chromium } from 'playwright';
const base = process.env.WEB_BASE_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {channel:'chrome'})});
const now = '2026-11-03T12:00:00+08:00';
const content = '## 格式示例\n\n**重点内容**与列表：\n\n- 第一项\n- 第二项\n\n| 名称 | 状态 |\n| --- | --- |\n| 搜索 | 可用 |\n\n```python\ndef greet(name):\n    return f"Hello {name}"\n```\n\n```html\n<script>window.chatInjected = true</script>\n<div>HTML 示例</div>\n```\n\n[安全链接](https://example.com)\n\n<script>window.chatInjected = true</script>';
const rawHtml = '<!doctype html><html><body><script>window.chatInjected=true</script><h1>HTML 文档</h1></body></html>';
const rawJson = '{"ok":true,"count":3}';
const message = (id, role, text, run='old') => ({id,role,content:text,run_id:run,create_time:now,attachments:[],artifacts:[]});
function deferred() { let resolve; const promise=new Promise(r=>resolve=r); return {promise,resolve}; }
async function fixture(page) {
  await page.addInitScript(()=>sessionStorage.setItem('yp.access_token','fixture'));
  const session={id:'session-a',agent_id:11,thread_id:'thread-a',title:'交互验证',archived:false,version:1,create_time:now,update_time:now};
  let messages=Array.from({length:30},(_,i)=>message(i+1,i%2?'assistant':'user',`第 ${i+1} 条历史消息。\n`+'用于检查长对话滚动位置。'.repeat(10)));
  messages[29]=message(30,'assistant',rawHtml);
  messages[27]=message(28,'assistant',rawJson);
  messages[25]=message(26,'assistant','```custom-format\nunknown syntax stays readable\n```');
  messages.push(message(31,'assistant',content));
  let run=null, hold=false;
  const historySeen=deferred(), releaseHistory=deferred();
  const calls=[];
  const streamText = "正在流式生成。".repeat(50);
  await page.route('**/api/v1/**',async route=>{
    const req=route.request(), url=new URL(req.url()),path=url.pathname,method=req.method();calls.push({path,method});
    let data;
    if(/\/sessions\/[^/]+\/models$/.test(path)) data=[];
    else if(path==='/api/v1/auth/me')data={id:1,username:'tester',role:'user'};
    else if(path==='/api/v1/agents/11')data={id:11,name:'体验验证助手',description:'测试',enabled:true,default_model_id:1,welcome_message:'欢迎'};
    else if(path==='/api/v1/agents/11/sessions')data={items:[session],total:1,page:1,page_size:20};
    else if(path==='/api/v1/agents/11/sessions/session-a')data=session;
    else if(path.endsWith('/session-a/messages')) {
      if(hold){historySeen.resolve();await releaseHistory.promise;}
      data=messages.filter(m=>m.id>Number(url.searchParams.get('after')||0)).slice(0,100);
    } else if(path.endsWith('/session-a/runs')&&method==='GET')data={items:run?[run]:[],total:run?1:0,page:1,page_size:20};
    else if(path.endsWith('/session-a/runs')&&method==='POST'){
      run={id:'run-chat',agent_id:11,session_id:'session-a',state:'running',failure_code:null,create_time:now,update_time:now};
      messages.push(message(32,'user',req.postDataJSON().input_text,'run-chat'));hold=true;data=run;
    }else if(path==='/api/v1/runs/run-chat/stream-ticket')data={expires_in:300};
    else if(path==='/api/v1/runs/run-chat/snapshot')data={run,events:[],latest_sequence:0,next_after:0,has_more:false};
    else if(path==='/api/v1/runs/run-chat/stream')return route.fulfill({contentType:'text/event-stream',body:`id: run-chat:1\nevent: text_delta\ndata: ${JSON.stringify({sequence:1,event_type:'text_delta',state:'running',content:streamText})}\n\n`});
    else if(path.endsWith('/calls'))data=[];
    else if(path==='/api/v1/runs/run-chat')data=run;
    else {throw new Error(`未定义 fixture: ${method} ${path}`);}
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({code:200,message:'操作成功',data})});
  });
  return {historySeen:historySeen.promise, release(){hold=false;releaseHistory.resolve();}, complete(){run={...run,state:'completed'};messages.push(message(33,'assistant',streamText,'run-chat'));},calls,streamText};
}
let assertions = 0;
try {
  for (const [layout,width,height] of [['desktop',1280,900],['mobile',375,812]]) {
    const context=await browser.newContext({viewport:{width,height}, permissions:['clipboard-read','clipboard-write']});
    const page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const f=await fixture(page);
    await page.goto(`${base}/agents/11/sessions/session-a`);
    await page.locator('.session-message').nth(30).waitFor();
    const history=page.locator('.sessions-history');
    const body=page.locator('[data-message-id="31"]');
    await body.getByRole('heading',{name:'格式示例',exact:true}).waitFor(); assertions++;
    assert.equal(await body.locator('table tbody tr').count(),1);assert.equal(await body.locator('.message-code-block').count(),2);assert.ok(await body.locator('.hljs-keyword').count()>0); assertions+=3;
    assert.equal(await page.locator('[data-message-id="30"] code.language-html').count(),1);
    assert.equal(await page.locator('[data-message-id="28"] code.language-json').count(),1);
    assert.equal(await page.locator('[data-message-id="26"] code.language-custom-format').textContent(),'unknown syntax stays readable\n'); assertions+=3;
    assert.equal(await page.evaluate(()=>window.chatInjected),undefined);assert.equal(await body.locator('script').count(),0); assertions+=2;
    const code=body.locator('.message-code-block').first();await code.getByRole('button',{name:'复制代码',exact:true}).click();
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'def greet(name):\n    return f"Hello {name}"\n'); assertions++;
    await body.getByRole('button',{name:'复制消息',exact:true}).click();assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),content); assertions++;
    const actions=body.getByRole('group',{name:'消息操作',exact:true});
    assert.deepEqual(await actions.getByRole('button').evaluateAll(buttons=>buttons.map(button=>button.getAttribute('aria-label'))),['复制消息','重新生成','喜欢','不喜欢','分享']); assertions++;
    assert.equal((await actions.innerText()).trim(),''); assertions++;
    for (const label of ['重新生成','喜欢','不喜欢','分享']) {
      const button=actions.getByRole('button',{name:label,exact:true});
      await button.hover();await page.getByRole('tooltip',{name:label,exact:true}).waitFor(); assertions++;
      await button.click();await page.locator('.ant-message-notice').filter({hasText:`${label}功能待接入`}).first().waitFor(); assertions++;
    }
    await actions.getByRole('button',{name:'喜欢',exact:true}).focus();
    await page.getByRole('tooltip',{name:'喜欢',exact:true}).waitFor(); assertions++;
    const positions=await page.evaluate(()=>{const u=document.querySelector('.session-message-user'),a=document.querySelector('.session-message-assistant');return {uAvatar:u.querySelector('.session-message-avatar').getBoundingClientRect().x,uMain:u.querySelector('.session-message-main').getBoundingClientRect().x,aAvatar:a.querySelector('.session-message-avatar').getBoundingClientRect().x,aMain:a.querySelector('.session-message-main').getBoundingClientRect().x};});
    assert.ok(positions.uAvatar>positions.uMain&&positions.aAvatar<positions.aMain); assertions++;
    await history.evaluate(el=>el.scrollTop=el.scrollHeight);
    await page.locator('[data-message-id="1"]').evaluate(el=>el.dataset.retained='yes');
    await page.evaluate(()=>{window.streamLengths=[];window.streamWatch=new MutationObserver(()=>{const n=document.querySelector('.message-streaming .message-markdown')?.textContent?.length;if(n)window.streamLengths.push(n);});window.streamWatch.observe(document.querySelector('.sessions-history'),{childList:true,characterData:true,subtree:true});});
    await page.getByRole('textbox',{name:'消息内容'}).fill('新消息');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await f.historySeen;
    assert.ok(await page.locator('.session-message').count()>=31,'发送后的后台消息同步不应卸载已有消息或替换为骨架屏');
    assert.ok(await history.evaluate(el=>el.scrollTop>0),'发送后的后台同步不应让历史消息滚回顶部'); assertions+=2;
    await page.waitForFunction(length=>{const text=document.querySelector('.message-streaming .message-markdown')?.textContent;return text&&text.length>0&&text.length<length;},f.streamText.length); assertions++;
    const previewActions=page.locator('[data-stream-run="run-chat"]').getByRole('group',{name:'消息操作',exact:true});
    assert.deepEqual(await previewActions.getByRole('button').evaluateAll(buttons=>buttons.map(button=>button.getAttribute('aria-label'))),['复制当前回复','重新生成','喜欢','不喜欢','分享']); assertions++;
    assert.equal((await previewActions.innerText()).trim(),''); assertions++;
    await history.evaluate(el=>el.scrollTop=300);
    await page.getByRole('button',{name:'回到最新消息',exact:true}).waitFor();
    await page.waitForFunction(length=>document.querySelector('.message-streaming .message-markdown')?.textContent?.length>=length,f.streamText.length);
    assert.ok(await page.evaluate(()=>{window.streamWatch.disconnect();return new Set(window.streamLengths).size>=3;}),'流式输出应经过多个可见帧'); assertions++;
    assert.ok(Math.abs(await history.evaluate(el=>el.scrollTop)-300)<2,'查看旧消息时，流式更新不应抢夺滚动位置'); assertions++;
    f.release();await page.locator('[data-message-id="32"]').waitFor();
    assert.equal(await page.locator('[data-message-id="1"]').getAttribute('data-retained'),'yes'); assertions++;
    await page.evaluate(()=>{window.replyDisappeared=false;window.replyWatch=new MutationObserver(()=>{if(!document.querySelector('[data-stream-run="run-chat"]')&&!document.querySelector('[data-message-id="33"]'))window.replyDisappeared=true;});window.replyWatch.observe(document.querySelector('.sessions-history'),{childList:true,subtree:true});});
    f.complete();await page.locator('[data-message-id="33"]').waitFor({timeout:8000});
    assert.equal(await page.evaluate(()=>{window.replyWatch.disconnect();return window.replyDisappeared;}),false,'终态同步期间不应移除回复');
    assert.equal(await page.locator('[data-stream-run="run-chat"]').count(),0);assert.equal(await page.locator('[data-message-id="33"]').count(),1); assertions+=3;
    await history.evaluate(el=>el.scrollTop=250);
    await page.getByRole('button',{name:'回到最新消息',exact:true}).waitFor();
    const read=page.waitForResponse(response=>response.url().includes('/session-a/messages'));
    await page.getByRole('button',{name:'刷新会话',exact:true}).click();await read;
    assert.ok(Math.abs(await history.evaluate(el=>el.scrollTop)-250)<2,'刷新会话应保留阅读位置'); assertions++;
    await page.getByRole('button',{name:'回到最新消息',exact:true}).click();
    assert.ok(await history.evaluate(el=>el.scrollHeight-el.scrollTop-el.clientHeight<2));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true); assertions+=2;
    await page.locator('.ant-message-notice').waitFor({state:'hidden'});
    await page.mouse.move(0,0);
    await body.locator('.message-code-block').first().evaluate(el=>{const h=el.closest('.sessions-history');h.scrollTop+=el.getBoundingClientRect().top-h.getBoundingClientRect().top-12;});
    await page.screenshot({path:`../docs/web-chat-rich-${layout}.png`,fullPage:true,animations:'disabled'});
    assert.deepEqual(errors,[]); assertions++;
    await context.close();console.log(`${layout}: 消息稳定同步、阅读位置、流式分帧、左右布局、Markdown/代码复制与安全渲染通过`);
  }
  const reduced=await browser.newContext({viewport:{width:1280,height:900},reducedMotion:'reduce'});
  const page=await reduced.newPage();const f=await fixture(page);await page.goto(`${base}/agents/11/sessions/session-a`);await page.locator('[data-message-id="31"]').waitFor();
  await page.getByRole('textbox',{name:'消息内容'}).fill('减少动画');await page.getByRole('button',{name:'发送',exact:true}).click();await f.historySeen;
  await page.waitForFunction(length=>document.querySelector('.message-streaming .message-markdown')?.textContent?.length===length,f.streamText.length);assertions++;
  f.release();await reduced.close();
  console.log(`对话浏览器契约回归通过：${assertions} 项断言；使用拦截响应，不访问真实模型或业务数据。`);
}finally{await browser.close();}
