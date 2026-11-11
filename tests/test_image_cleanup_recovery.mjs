import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {createContext, runInContext} from 'node:vm';

const source = path => readFileSync(new URL('../' + path, import.meta.url), 'utf8');
const jobId = 'a'.repeat(32), accountId = 'b'.repeat(64);
const account = {label:'测试账号',phase:'ready',total:2,verified:0,remaining:2};
const json = (data, status = 200) => new Response(JSON.stringify(data), {status,headers:{'Content-Type':'application/json'}});
const html = status => new Response('<!DOCTYPE html><html>private-upstream-fixture</html>', {status,headers:{'Content-Type':'text/html'}});

function scheduler() {
  const pending = new Map(); let next = 0;
  return {
    pending,
    setTimeout(fn, delay) { const id = ++next; pending.set(id,{fn,delay}); return id; },
    clearTimeout(id) { pending.delete(id); },
    async runNext() {
      assert.ok(pending.size, '应安排下一次自动读取');
      const [id, task] = pending.entries().next().value; pending.delete(id); await task.fn(); return task.delay;
    },
  };
}

function apiHarness(fetch, clock = scheduler()) {
  const script = source('web/account-image-space.js');
  const start = script.indexOf('  async function api('), end = script.indexOf('  function hasStoredCleanup(');
  assert.ok(start >= 0 && end > start);
  const context = createContext({fetch,AbortController,key:async()=> 'fixture-admin-key',cachedKey:'fixture-admin-key',
    setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout});
  runInContext(script.slice(start,end) + ';globalThis.request = api;',context);
  return {request:(path,body)=>context.request(path,body,'image-space'),context,clock};
}

// 只模拟浏览器边界；请求解析、控制器、渲染与定时恢复均执行实际生产代码。
class Element {
  constructor() { this.nodes = new Map(); this.children = []; this.disabled = false; this.checked = false; this.hidden = false; }
  querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector,new Element()); return this.nodes.get(selector); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  showModal() { this.open = true; }
}

function controllerHarness(fetch, savedJob = false) {
  const clock = scheduler(), api = apiHarness(fetch,clock), dialog = new Element(), saved = new Map();
  if (savedJob) saved.set('account-image-cleanup-task',JSON.stringify({id:jobId,refs:[accountId]}));
  const context = createContext({document:{createElement:()=>new Element()},
    sessionStorage:{getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value),removeItem:key=>saved.delete(key)},
    setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout,window:{dispatchEvent(){}},
    CustomEvent:class { constructor(type, options) { this.type = type; this.detail = options.detail; } }});
  runInContext(source('web/account-image-cleanup.js').replace('export function','function') + ';globalThis.create = createCleanupController;',context);
  const controller = context.create({dialog,request:api.request,onChange(){}});
  return {controller,clock,saved,panel:dialog.children[0],node:id=>dialog.children[0].querySelector('#'+id)};
}

for (const status of [200,502,404,401,403]) {
  test(`HTML ${status} 保留状态并隐藏响应正文`, async () => {
    const {request,context,clock} = apiHarness(async()=>html(status));
    await assert.rejects(request('/jobs/'+jobId), error => {
      assert.equal(error.status,status);
      assert.equal(error.invalidResponse,true);
      assert.equal(error.retryable,![401,403].includes(status));
      assert.match(error.message,/服务|响应|登录|权限/);
      assert.doesNotMatch(error.message,/DOCTYPE|private-upstream|Unexpected token/);
      return true;
    });
    if (status === 401) assert.equal(context.cachedKey,'');
    assert.equal(clock.pending.size,0);
  });
}

test('GET 在 15 秒后取消，网络错误也使用安全提示', async () => {
  let signal;
  const api = apiHarness(async (_url, options) => {
    signal = options.signal;
    return new Promise((_resolve,reject)=>signal?.addEventListener('abort',()=>reject(new DOMException('private-url','AbortError'))));
  });
  const pending = api.request('/jobs/'+jobId);
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(signal,'GET 必须使用 AbortController');
  assert.equal(api.clock.pending.values().next().value.delay,15000);
  const checked = assert.rejects(pending,error=>error.retryable === true && /超时/.test(error.message));
  await api.clock.runNext(); await checked;
  assert.equal(signal.aborted,true);
  const network = apiHarness(async()=>{throw new TypeError('private-url');});
  await assert.rejects(network.request('/jobs/'+jobId),error=>error.retryable === true && !error.message.includes('private-url'));
});

test('一次 HTML 中断后自动读取原任务，账号失败不阻断后续进度', async () => {
  const calls = []; let reads = 0;
  const harness = controllerHarness(async (url,options) => {
    calls.push({url,method:options.method});
    if (options.method === 'POST') return json({id:jobId});
    reads += 1;
    if (reads === 2) return html(502);
    return json({status:reads < 3 ? 'scanning':'ready',accounts:[account,{...account,phase:'failed',total:7,preview_failed:true,error:'账号暂不可用'}]});
  });
  await harness.controller.start([accountId]);
  await harness.clock.runNext();
  assert.equal(harness.clock.pending.size,1);
  assert.equal(harness.saved.has('account-image-cleanup-task'),true);
  assert.match(harness.node('cleanup-error').textContent,/自动.*重试|重试.*读取/);
  await harness.clock.runNext();
  assert.equal(harness.node('cleanup-error').textContent,'');
  assert.match(harness.node('cleanup-summary').textContent,/共 2 张图片/);
  assert.match(harness.node('cleanup-summary').textContent,/1 个账号.*失败.*跳过/);
  assert.equal(harness.node('cleanup-confirm').disabled,false);
  assert.equal(calls.filter(call=>call.method === 'POST').length,1);
  assert.ok(calls.filter(call=>call.method === 'GET').every(call=>call.url.endsWith('/jobs/'+jobId)));
});

test('HTML 404 保留任务自动恢复，JSON 404 才标记过期', async () => {
  let reads = 0;
  const harness = controllerHarness(async()=>++reads === 1 ? html(404) : json({detail:{error:'任务不存在'}},404),true);
  await harness.controller.poll();
  assert.equal(harness.saved.has('account-image-cleanup-task'),true);
  assert.equal(harness.clock.pending.size,1);
  await harness.clock.runNext();
  assert.equal(harness.saved.has('account-image-cleanup-task'),false);
  assert.equal(harness.clock.pending.size,0);
  assert.match(harness.node('cleanup-summary').textContent,/过期/);
});

for (const status of [401,403]) {
  test(`JSON ${status} 停止自动重试并保留任务`, async () => {
    const harness = controllerHarness(async()=>json({detail:{error:'请重新登录'}},status),true);
    await harness.controller.poll();
    assert.equal(harness.clock.pending.size,0);
    assert.equal(harness.saved.has('account-image-cleanup-task'),true);
    assert.equal(harness.node('cleanup-poll').disabled,false);
  });
}

test('连续错误有退避上限，手动恢复后继续读取同一任务', async () => {
  let broken = true, requests = 0;
  const harness = controllerHarness(async()=>{requests++;return broken ? html(503) : json({status:'completed',accounts:[{...account,phase:'completed',verified:2}]});},true);
  await harness.controller.poll();
  const delays = [];
  while (harness.clock.pending.size && delays.length < 10) delays.push(await harness.clock.runNext());
  assert.ok(delays.length >= 3,'短暂错误需要自动重试');
  assert.ok(delays.every(delay=>delay > 0 && delay <= 15000));
  assert.ok(delays[1] > delays[0],'连续失败应退避');
  assert.equal(harness.clock.pending.size,0);
  assert.ok(requests <= 7,'连续失败不能无限快速轮询');
  assert.equal(harness.node('cleanup-poll').disabled,false);
  broken = false; await harness.controller.poll();
  assert.match(harness.node('cleanup-summary').textContent,/清理完成/);
  assert.equal(harness.node('cleanup-error').textContent,'');
});

test('执行响应丢失只查询原任务，执行失败仍计入总量、预览失败排除', async () => {
  const calls = []; let reads = 0;
  const harness = controllerHarness(async (url,options) => {
    calls.push({url,method:options.method});
    if (url.endsWith('/preview')) return json({id:jobId});
    if (url.endsWith('/execute')) return html(502);
    reads++;
    if (reads === 2) return html(502);
    const accounts = [account,{...account,phase:'failed',total:7,preview_failed:true}];
    if (reads >= 3) accounts.push({...account,phase:'failed',total:3,preview_failed:false});
    return json({status:reads === 1 ? 'ready':'running',accounts});
  });
  await harness.controller.start([accountId]);
  harness.node('cleanup-confirm').checked = true; harness.node('cleanup-confirm').onchange();
  assert.equal(harness.node('cleanup-execute').disabled,false);
  await harness.node('cleanup-execute').onclick();
  assert.equal(harness.clock.pending.size,1);
  await harness.clock.runNext();
  assert.match(harness.node('cleanup-summary').textContent,/0 \/ 5 张图片/);
  assert.equal(calls.filter(call=>call.url.endsWith('/execute')).length,1);
  assert.equal(calls.filter(call=>call.url.endsWith('/preview')).length,1);
  assert.equal(harness.clock.pending.size,1,'单账号失败时其余账号仍继续读取进度');
});
