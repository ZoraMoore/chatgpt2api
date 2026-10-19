import fs from 'node:fs/promises';
import http from 'node:http';
import {spawn} from 'node:child_process';
import {createHash, randomUUID} from 'node:crypto';

// 工作进程仅通过私有 Unix socket 接收数据。浏览器不允许访问任何网络资源。
const socketPath = process.env.SENTINEL_SOCKET || '/run/sentinel/worker.sock';
const chromePath = process.env.CHROMIUM_PATH || '/opt/chromium/chrome';
const sdkPath = process.env.SENTINEL_SDK_PATH || '/app/sentinel-sdk.js';
const expectedHash = process.env.SENTINEL_SDK_SHA256;
const maxContexts = 2;
const contexts = new Map();
const sessions = new Map();
const pending = new Map();
let serial = 0, ws, server, chrome, closing = false, creating = 0;
const profile = await fs.mkdtemp('/tmp/image-sentinel-');

function safeError(code) {
  return Object.assign(new Error(code), {code});
}

function call(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => {pending.delete(id); reject(safeError('browser_timeout'));}, 15000);
    pending.set(id, {resolve, reject, timer});
    ws.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
  });
}

async function dispose(ticket) {
  const context = contexts.get(ticket);
  if (!context) return;
  contexts.delete(ticket);
  sessions.delete(context.sessionId);
  await call('Target.disposeBrowserContext', {browserContextId: context.browserContextId}).catch(() => {});
}

async function shutdown() {
  if (closing) return;
  closing = true;
  server?.close();
  await Promise.allSettled([...contexts.keys()].map(dispose));
  ws?.close();
  chrome?.kill('SIGTERM');
  await new Promise(resolve => setTimeout(resolve, 300));
  await fs.rm(profile, {recursive: true, force: true, maxRetries: 3}).catch(() => {});
  await fs.rm(socketPath, {force: true}).catch(() => {});
}

function validateStart(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw safeError('invalid_input');
  if (typeof input.html !== 'string' || Buffer.byteLength(input.html) > 2 * 1024 * 1024) throw safeError('invalid_input');
  if (typeof input.user_agent !== 'string' || !input.user_agent || input.user_agent.length > 1024 || /[\x00-\x1f\x7f]/.test(input.user_agent)) throw safeError('invalid_input');
  if (typeof input.device_id !== 'string' || !/^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(input.device_id)) throw safeError('invalid_input');
  if (typeof input.timezone !== 'string' || input.timezone.length > 128) throw safeError('invalid_input');
  try {new Intl.DateTimeFormat('en', {timeZone: input.timezone});} catch {throw safeError('invalid_input');}
}

async function evaluate(context, expression) {
  const result = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true, timeout: 10000}, context.sessionId);
  if (result.exceptionDetails || result.result?.subtype === 'error') throw safeError('sdk_execution_failed');
  return result.result?.value;
}

async function begin(input, sdk) {
  validateStart(input);
  if (contexts.size + creating >= maxContexts) throw safeError('worker_busy');
  creating++;
  const ticket = randomUUID();
  let context;
  try {
    const {browserContextId} = await call('Target.createBrowserContext');
    context = {browserContextId, created: Date.now(), html: input.html, busy: false};
    contexts.set(ticket, context);
    const {targetId} = await call('Target.createTarget', {url: 'about:blank', browserContextId});
    const {sessionId} = await call('Target.attachToTarget', {targetId, flatten: true});
    context.sessionId = sessionId;
    sessions.set(sessionId, context);
    await call('Page.enable', {}, sessionId);
    await call('Network.enable', {}, sessionId);
    await call('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]}, sessionId);
    await call('Network.setUserAgentOverride', {userAgent: input.user_agent, acceptLanguage: 'zh-CN,zh,zh-TW,en'}, sessionId);
    await call('Emulation.setTimezoneOverride', {timezoneId: input.timezone}, sessionId);
    await call('Emulation.setLocaleOverride', {locale: 'zh-CN'}, sessionId);
    await call('Network.setCookies', {cookies: [{name: 'oai-did', value: input.device_id, url: 'https://chatgpt.com/', secure: true}]}, sessionId);
    await call('Page.navigate', {url: 'https://chatgpt.com/'}, sessionId);
    for (let i = 0; i < 60; i++) {
      if (await evaluate(context, 'document.readyState') === 'complete') break;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    // 只执行镜像内经哈希核验的网页 SDK；客户端不能提交可执行 JavaScript。
    await evaluate(context, sdk);
    const p = await evaluate(context, 'SentinelSDK.__imageRuntime.pow.getRequirementsTokenBlocking()');
    if (typeof p !== 'string' || !p.startsWith('gAAAAAC') || p.length > 16384) throw safeError('invalid_sdk_result');
    context.p = p;
    context.html = '';
    return {ticket, p};
  } catch (error) {
    await dispose(ticket);
    throw error;
  } finally {creating--;}
}

async function solve(input) {
  if (!input || typeof input.ticket !== 'string' || input.ticket.length !== 36) throw safeError('invalid_input');
  const context = contexts.get(input.ticket);
  if (!context || !context.p) throw safeError('expired_ticket');
  if (context.busy) throw safeError('worker_busy');
  if (!input.challenge || typeof input.challenge !== 'object' || Array.isArray(input.challenge)) throw safeError('invalid_input');
  context.busy = true;
  try {
    const run = async ({challenge, p}) => {
      const api = SentinelSDK.__imageRuntime;
      api.bind(challenge, p);
      const proof = await api.pow.getEnforcementToken(challenge, {forceSync: true});
      const turnstile = challenge.turnstile?.required ? await api.solve(challenge, challenge.turnstile.dx) : '';
      return {proof: proof || '', turnstile: turnstile || ''};
    };
    const result = await evaluate(context, `(${run.toString()})(${JSON.stringify({challenge: input.challenge, p: context.p})})`);
    if (!result || !['proof', 'turnstile'].every(key => typeof result[key] === 'string' && result[key].length <= 131072 && !/[\x00-\x1f\x7f]/.test(result[key]))) throw safeError('invalid_sdk_result');
    return result;
  } finally {await dispose(input.ticket);}
}

try {
  const rawSDK = await fs.readFile(sdkPath, 'utf8');
  if (!/^[a-f\d]{64}$/.test(expectedHash || '') || createHash('sha256').update(rawSDK).digest('hex') !== expectedHash || !rawSDK.includes('t.init=Ee,')) throw safeError('unsupported_sdk');
  // 对已验证版本暴露内部计算入口；版本改变时显式失败，不猜测压缩后的符号。
  const sdk = rawSDK.replace('t.init=Ee,', 't.__imageRuntime={pow:E,bind:D,solve:Rn},t.init=Ee,');
  chrome = spawn(chromePath, ['--headless', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], {stdio: 'ignore'});
  chrome.on('exit', () => {if (!closing) process.exit(1);});
  let port;
  for (let i = 0; i < 100; i++) {
    try {port = Number((await fs.readFile(profile + '/DevToolsActivePort', 'utf8')).split('\n')[0]); break;}
    catch {await new Promise(resolve => setTimeout(resolve, 100));}
  }
  if (!port) throw safeError('browser_start_failed');
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {ws.onopen = resolve; ws.onerror = () => reject(safeError('browser_start_failed'));});
  ws.onmessage = async event => {
    const message = JSON.parse(event.data);
    if (pending.has(message.id)) {
      const task = pending.get(message.id); pending.delete(message.id); clearTimeout(task.timer);
      message.error ? task.reject(safeError('browser_command_failed')) : task.resolve(message.result);
    }
    if (message.method === 'Fetch.requestPaused') {
      const context = sessions.get(message.sessionId), params = message.params;
      try {
        if (context && params.resourceType === 'Document' && params.request.url === 'https://chatgpt.com/') {
          await call('Fetch.fulfillRequest', {requestId: params.requestId, responseCode: 200,
            responseHeaders: [{name: 'Content-Type', value: 'text/html'}, {name: 'Content-Security-Policy', value: "default-src 'none'; script-src 'none'"}],
            body: Buffer.from(context.html).toString('base64')}, message.sessionId);
        } else await call('Fetch.failRequest', {requestId: params.requestId, errorReason: 'BlockedByClient'}, message.sessionId);
      } catch {}
    }
  };
  server = http.createServer(async (request, response) => {
    const send = (status, body) => response.writeHead(status, {'Content-Type': 'application/json'}).end(JSON.stringify(body));
    try {
      if (request.method === 'GET' && request.url === '/health') return send(200, {ok: true, contexts: contexts.size});
      if (request.method !== 'POST' || !['/v1/start', '/v1/solve', '/v1/release'].includes(request.url)) return send(404, {code: 'not_found'});
      let bytes = 0; const chunks = [];
      for await (const chunk of request) {bytes += chunk.length; if (bytes > 4 * 1024 * 1024) throw safeError('invalid_input'); chunks.push(chunk);}
      let input;
      try {input = JSON.parse(Buffer.concat(chunks).toString('utf8'));} catch {throw safeError('invalid_input');}
      if (request.url === '/v1/start') return send(200, await begin(input, sdk));
      if (request.url === '/v1/solve') return send(200, await solve(input));
      if (!input || typeof input.ticket !== 'string' || input.ticket.length !== 36) throw safeError('invalid_input');
      await dispose(input.ticket); send(200, {ok: true});
    } catch (error) {
      const allowed = ['invalid_input', 'worker_busy', 'expired_ticket', 'browser_timeout', 'browser_command_failed', 'sdk_execution_failed', 'invalid_sdk_result'];
      const code = allowed.includes(error.code) ? error.code : 'worker_failed';
      send(code === 'worker_busy' ? 503 : code === 'invalid_input' ? 400 : 502, {code});
    }
  });
  server.requestTimeout = 30000;
  await fs.rm(socketPath, {force: true});
  await new Promise(resolve => server.listen(socketPath, resolve));
  await fs.chmod(socketPath, 0o600);
  setInterval(() => {for (const [ticket, context] of contexts) if (Date.now() - context.created > 60000) void dispose(ticket);}, 5000).unref();
  process.on('SIGTERM', () => shutdown().finally(() => process.exit(0)));
  process.on('SIGINT', () => shutdown().finally(() => process.exit(0)));
  console.log('sentinel worker ready');
} catch {
  console.error('sentinel worker startup failed');
  await shutdown(); process.exitCode = 1;
}
