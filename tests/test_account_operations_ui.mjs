import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const root = new URL('../', import.meta.url);
const html = readFileSync(new URL('web_dist/accounts/index.html', root), 'utf8');
const chunk = html.match(/session-import-[a-f0-9]+\.js/)[0];
const bundle = readFileSync(new URL('web_dist/_next/static/chunks/' + chunk, root), 'utf8');
const handler = bundle.match(/function F\(e\)([\s\S]+?)function q\(e\)/)[0].replace(/function q\(e\)$/, '');
const record = {platform:'openai', type:'oauth', name:'fixture@example.test', credentials:{access_token:'fixture-access', refresh_token:'fixture-refresh', expires_at:1234}};
const result = runInNewContext(`(${handler})(${JSON.stringify(record)})`);
assert.equal(result.credentials.refresh_token, 'fixture-refresh');
assert.equal(result.access_token, 'fixture-access');
assert.equal(runInNewContext(`(${handler})(${JSON.stringify({...record,platform:'anthropic'})})`), null);
const bridge = bundle.match(/\/\* account-operations bridge:start \*\/([\s\S]*?)\/\* account-operations bridge:end \*\//);
assert.ok(bridge);
let states=[];
const effects=[];
const events=new Map();
runInNewContext(bridge[1], {
  s:{useEffect:(fn)=>effects.push(fn)}, a:[{access_token:'do-not-expose-access',refresh_token:'do-not-expose-refresh',account_ref:'a'.repeat(64),email:'fixture@example.test',has_refresh_token:true}],
  c:['do-not-expose-access'],ee:false,ef:()=>{},f:()=>{},
  window:{dispatchEvent:e=>states.push(e.detail),addEventListener:(name,fn)=>events.set(name,fn),removeEventListener:()=>{}},
  CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
});
effects.forEach(fn=>fn());
assert.deepEqual(Array.from(states[0].selected),['a'.repeat(64)]);
assert.equal(JSON.stringify(states).includes('do-not-expose'),false);
assert.ok(bundle.includes('"data-account-ref":e.account_ref'));
console.log('完整 JSON 导入与账号操作事件桥测试通过');
