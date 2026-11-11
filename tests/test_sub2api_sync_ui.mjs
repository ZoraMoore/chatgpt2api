import assert from 'node:assert/strict';
import {test} from 'node:test';
import {source, runTypescript, ts} from './frontend-source.mjs';

const text = source('frontend/src/app/settings/components/sub2api-connections.tsx');
const tree = ts.createSourceFile('sub2api-connections.tsx', text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const handlers = new Map();
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.initializer && ts.isArrowFunction(node.initializer)) {
    handlers.set(node.name.getText(tree), node.initializer.getText(tree));
  }
  ts.forEachChild(node, visit);
}
visit(tree);

const server = {id: 'fixture-server', email: 'fixture@example.test'};
function harness(overrides = {}) {
  const calls = [];
  const states = {};
  const context = {
    syncSub2APIServer: async id => { calls.push(['sync', id]); return {status: 'authenticated', accounts: [{id: '7'}]}; },
    completeSub2APILogin: async (id, code) => { calls.push(['verify', id, code]); return {status: 'authenticated', accounts: [{id: '7'}]}; },
    fetchSub2APIServerAccounts: async () => { throw new Error('不能绕过登录直接读取账号'); },
    openAccountBrowser: (item, accounts) => calls.push(['browse', item.id, accounts.length]),
    setLoadingAccountsId: value => { states.loading = value; },
    setLoginServer: value => { states.loginServer = value; },
    setLoginEmail: value => { states.email = value; },
    setLoginCode: value => { states.code = value; },
    setLoginOpen: value => { states.loginOpen = value; },
    setIsVerifyingLogin: value => { states.verifying = value; },
    setIsStartingImport: value => { states.importing = value; },
    setImportStepUpCode: value => { states.importCode = value; },
    setBrowserOpen: value => { states.browserOpen = value; },
    setServers: updater => { states.servers = updater([server]); },
    startSub2APIImport: async (id, ids, code) => {
      calls.push(['import', id, ids, code]);
      return {import_job: {job_id: 'fixture-job', status: 'pending'}};
    },
    browserServer: server, selectedIds: ['7'], importStepUpCode: '654321',
    loginServer: server, loginCode: '123456',
    toast: {error: message => calls.push(['error', message]), success: () => {}},
    Error, ...overrides,
  };
  const invoke = async (name, ...args) => {
    assert.ok(handlers.has(name), `缺少处理函数 ${name}`);
    return runTypescript(`(${handlers.get(name)})`, context)(...args);
  };
  return {calls, states, invoke};
}

test('同步按钮先建立登录会话，成功后打开账号列表', async () => {
  const h = harness();
  await h.invoke('handleBrowseAccounts', server);
  assert.deepEqual(h.calls, [['sync', server.id], ['browse', server.id, 1]]);
  assert.equal(h.states.loading, null);
});

test('需要二次验证时只打开验证码弹窗，不提前读取账号', async () => {
  const h = harness({syncSub2APIServer: async () => ({status: 'login_2fa_required', user_email_masked: 'f***@example.test', accounts: []})});
  await h.invoke('handleBrowseAccounts', server);
  assert.equal(h.states.loginOpen, true);
  assert.equal(h.states.loginServer, server);
  assert.equal(h.states.email, 'f***@example.test');
  assert.equal(h.states.code, '');
  assert.equal(h.calls.length, 0);
});

test('验证码验证成功后打开账号列表并清除输入', async () => {
  const h = harness();
  await h.invoke('handleCompleteLogin');
  assert.deepEqual(h.calls, [['verify', server.id, '123456'], ['browse', server.id, 1]]);
  assert.equal(h.states.loginOpen, false);
  assert.equal(h.states.code, '');
  assert.equal(h.states.verifying, false);
});

test('验证码必须为六位数字，非法值不发送到后端', async () => {
  for (const loginCode of ['', '12345', '1234567', 'abcdef', '１２３４５６']) {
    const h = harness({loginCode});
    await h.invoke('handleCompleteLogin');
    assert.equal(h.calls[0]?.[0], 'error');
    assert.equal(h.calls.length, 1);
  }
});

test('导入选中账号时必须提交导入二次验证码', async () => {
  const h = harness();
  await h.invoke('handleStartImport');
  assert.deepEqual(h.calls, [['import', server.id, ['7'], '654321']]);
  assert.equal(h.states.importCode, '');
  assert.equal(h.states.browserOpen, false);
  assert.equal(h.states.servers[0].import_job.job_id, 'fixture-job');
});

test('导入验证码缺失或格式错误时不启动导入', async () => {
  for (const importStepUpCode of ['', '12345', '1234567', 'abcdef', '１２３４５６']) {
    const h = harness({importStepUpCode});
    await h.invoke('handleStartImport');
    assert.equal(h.calls[0]?.[0], 'error');
    assert.equal(h.calls.length, 1);
    assert.equal(h.states.importing, undefined);
  }
});

test('已配置自动二次验证时无需在浏览器重复填写验证码', async () => {
  const h = harness({browserServer: {...server, has_totp_secret: true}, importStepUpCode: ''});
  await h.invoke('handleStartImport');
  assert.deepEqual(h.calls, [['import', server.id, ['7'], '']]);
});

test('导入验证失败时保留账号窗口和选择，清除验证码以便重试', async () => {
  const h = harness({startSub2APIImport: async () => {throw new Error('验证码已使用，请输入下一组验证码');}});
  await h.invoke('handleStartImport');
  assert.equal(h.calls[0]?.[0], 'error');
  assert.equal(h.states.browserOpen, undefined);
  assert.equal(h.states.importCode, '');
  assert.equal(h.states.importing, false);
});

test('导入 API 将验证码放入 POST 请求体，不进入 URL', async () => {
  const api = ts.createSourceFile('api.ts', source('frontend/src/lib/api.ts'), ts.ScriptTarget.Latest, true);
  const fn = api.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'startSub2APIImport');
  assert.ok(fn);
  let sent;
  const code = fn.getText(api).replace(/^export /, '');
  await runTypescript(`${code}; startSub2APIImport('fixture-server', ['7'], '654321')`, {
    httpRequest: async (path, options) => {sent = JSON.parse(JSON.stringify({path, ...options}));},
  });
  assert.deepEqual(sent, {path:'/api/sub2api/servers/fixture-server/import', method:'POST', body:{account_ids:['7'], step_up_code:'654321'}});
});
