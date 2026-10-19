import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

// 仓库只有构建产物，直接验证账号页实际加载的处理器，避免遗漏前端丢字段问题。
const root = new URL('../', import.meta.url);
const html = readFileSync(new URL('web_dist/accounts/index.html', root), 'utf8');
const chunk = html.match(/session-import-[a-f0-9]+\.js/)[0];
const bundle = readFileSync(new URL(`web_dist/_next/static/chunks/${chunk}`, root), 'utf8');
const start = bundle.indexOf('ei=async()=>');
assert.ok(start >= 0);
const handler = bundle.slice(start + 3, bundle.indexOf(',ec=async()=>', start));
const sessionToken = `${'a'.repeat(32)}..${'b'.repeat(16)}.${'c'.repeat(16)}.${'d'.repeat(16)}`;

async function submit(input) {
  const calls = [];
  const errors = [];
  await runInNewContext(`(${handler})()`, {
    h: typeof input === 'string' ? input : JSON.stringify(input),
    es: async (...args) => calls.push(JSON.parse(JSON.stringify(args))),
    N: { toast: { error: message => errors.push(message) } },
  });
  return { calls, errors };
}

const complete = await submit({ accessToken: 'fixture-access', sessionToken, user: { private: 'omit' } });
assert.deepEqual(complete.calls[0][2], [{ access_token: 'fixture-access', sessionToken, source_type: 'web' }]);
assert.equal(complete.errors.length, 0);

const legacy = await submit({ accessToken: 'fixture-access' });
assert.deepEqual(legacy.calls[0][2], [{ access_token: 'fixture-access', source_type: 'web' }]);

for (const input of [null, [], {}, { accessToken: 42 },
  { accessToken: 'bad\nheader' }, { accessToken: 'x'.repeat(32769) },
  ...[42, {}, 'invalid', sessionToken + '\nheader', sessionToken + 'x'.repeat(16384)]
    .map(value => ({ accessToken: 'fixture-access', sessionToken: value })),
  '{secret-session-fixture', 'x'.repeat(262145)]) {
  const result = await submit(input);
  assert.equal(result.calls.length, 0);
  assert.ok(result.errors.length);
  assert.ok(!result.errors.join('').includes('secret-session-fixture'));
}
console.log('Session JSON 导入：完整会话、旧格式、非法输入与错误脱敏验证通过');
