import assert from 'node:assert/strict';
import {loadParsers,source,runTypescript,ts} from './frontend-source.mjs';
const {getAccountJsonAccount}=loadParsers();
const record={platform:'openai',type:'oauth',credentials:{access_token:'fixture-access',refresh_token:'fixture-refresh',expires_at:1234}};
assert.equal(getAccountJsonAccount(record).credentials.refresh_token,'fixture-refresh');
assert.equal(getAccountJsonAccount({...record,platform:'anthropic'}),null);
const page=source('frontend/src/app/accounts/page.tsx');
const tree=ts.createSourceFile('page.tsx',page,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let bridge;
function visit(node){
  if(ts.isCallExpression(node)&&node.expression.getText(tree)==='useEffect'&&node.arguments[0]?.getText(tree).includes('operationPrefix + "state"')) bridge=node.arguments[0].getText(tree);
  ts.forEachChild(node,visit);
}
visit(tree);assert.ok(bridge,'实际账号页面必须发布安全操作状态');
const states=[],events=new Map(),ref='a'.repeat(64);
runTypescript(`(${bridge})();`,{
  accounts:[{access_token:'private-access-value',refresh_token:'private-refresh-value',account_ref:ref,email:'fixture@example.test',status:'正常'}],
  selected:['private-access-value'],loading:false,operationPrefix:'chatgpt2api:account-operations:',
  window:{dispatchEvent:event=>states.push(event.detail),addEventListener:(name,fn)=>events.set(name,fn),removeEventListener:()=>{}},
  CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},
});
assert.deepEqual(Array.from(states[0].selected),[ref]);
assert.equal(JSON.stringify(states).includes('private-access-value'),false);
assert.equal(JSON.stringify(states).includes('private-refresh-value'),false);
assert.ok(page.includes('data-account-ref={account.account_ref}'));
console.log('完整 credentials 导入及真实页面事件桥的凭据隔离验证通过');
