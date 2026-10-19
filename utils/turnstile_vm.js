"use strict";

// 只解释指令数据。上下文不暴露 Python 回调、文件或网络接口。
function sentinelBrowser(c) {
  const tag = (value, name) => Object.defineProperty(value, Symbol.toStringTag, {value: name});
  const width = c.screen_width || 1920, height = c.screen_height || 1080;
  const location = tag({href: "https://chatgpt.com/", origin: "https://chatgpt.com", protocol: "https:",
    hostname: "chatgpt.com", host: "chatgpt.com", pathname: "/", search: "", hash: "",
    toString() {return this.href;}}, "Location");
  const element = name => tag({tagName: String(name).toUpperCase(), style: {}, children: [],
    appendChild(child) {this.children.push(child); return child;},
    removeChild(child) {const i = this.children.indexOf(child); if (i >= 0) this.children.splice(i, 1); return child;},
    setAttribute(key, value) {this[key] = String(value);}, getAttribute(key) {return this[key] ?? null;},
    getBoundingClientRect() {return {x: 0, y: 0, top: 0, left: 0, right: width, bottom: height, width, height};},
    remove() {}, querySelector() {return null;}, querySelectorAll() {return [];}, getContext() {return null;},
    addEventListener() {}, removeEventListener() {}}, name === "body" ? "HTMLBodyElement" : "HTMLElement");
  const document = tag({location, URL: location.href, documentURI: location.href, referrer: "", cookie: "oai-did=" + (c.device_id || ""),
    readyState: "complete", visibilityState: "visible", hidden: false, compatMode: "CSS1Compat",
    characterSet: "UTF-8", contentType: "text/html", title: "ChatGPT", body: element("body"),
    head: element("head"), documentElement: element("html"), scripts: (c.script_sources || []).map(src => ({src})),
    createElement: element, querySelector() {return null;}, querySelectorAll() {return [];},
    getElementById() {return null;}, addEventListener() {}, removeEventListener() {}}, "Document");
  const storage = tag({"oai-did": c.device_id || ""}, "Storage");
  Object.defineProperties(storage, {getItem: {value: key => Object.hasOwn(storage, key) ? storage[key] : null},
    setItem: {value: (key, value) => {storage[String(key)] = String(value);}},
    removeItem: {value: key => {delete storage[key];}}, length: {get: () => Object.keys(storage).length},
    key: {value: index => Object.keys(storage)[index] ?? null}});
  const navigator = tag({userAgent: c.user_agent || "", appCodeName: "Mozilla", appName: "Netscape",
    appVersion: String(c.user_agent || "").replace(/^Mozilla\//, ""), platform: c.navigator_platform || "Win32", vendor: "Google Inc.",
    vendorSub: "", product: "Gecko", productSub: "20030107", language: c.language || "zh-CN",
    languages: c.languages || ["zh-CN", "zh", "en-US", "en"], hardwareConcurrency: c.hardware_concurrency || 8,
    deviceMemory: 8, maxTouchPoints: 0, cookieEnabled: true, onLine: true, webdriver: false,
    plugins: [], mimeTypes: [], pdfViewerEnabled: false,
    connection: {effectiveType: "4g", downlink: 10, rtt: 50, saveData: false},
    permissions: {query: () => Promise.resolve({state: "prompt"})}}, "Navigator");
  const major = String(c.user_agent || "").match(/Chrome\/(\d+)/)?.[1] || "146";
  navigator.userAgentData = {brands: [{brand: "Chromium", version: major}, {brand: "Google Chrome", version: major}, {brand: "Not_A Brand", version: "99"}],
    mobile: false, platform: c.platform || "Windows",
    getHighEntropyValues: () => Promise.resolve({architecture: "x86", bitness: "64", model: "", platform: c.platform || "Windows",
      platformVersion: "19.0.0", uaFullVersion: major + ".0.0.0", mobile: false})};
  const performance = tag({timeOrigin: c.time_origin_ms || Date.now(),
    now: () => Date.now() - (c.time_origin_ms || Date.now())}, "Performance");
  const window = tag({Object, Reflect, Math, JSON, Array, String, Number, Boolean, Date, Promise, Uint8Array,
    RegExp, Error, TypeError, Map, Set, WeakMap, WeakSet, Symbol, parseInt, parseFloat, isNaN, isFinite,
    NaN, Infinity, undefined, document, navigator, location, localStorage: storage, sessionStorage: tag({}, "Storage"),
    performance, history: tag({length: 1, state: null}, "History"), chrome: {},
    screen: tag({width, height, availWidth: width, availHeight: height - 40, colorDepth: 24, pixelDepth: 24,
      orientation: {type: "landscape-primary", angle: 0}}, "Screen"),
    innerWidth: width, innerHeight: height, outerWidth: width, outerHeight: height, screenX: 0, screenY: 0,
    devicePixelRatio: 1, scrollX: 0, scrollY: 0, addEventListener() {}, removeEventListener() {},
    matchMedia: () => ({matches: false, addListener() {}, removeListener() {}})}, "Window");
  // Date 使用隔离子进程的 TZ；这里提供挑战所需的 Intl 时区查询。
  window.Intl = {DateTimeFormat: function () {return {resolvedOptions: () => ({timeZone: c.timezone || "UTC", locale: "en-US"})};}};
  window.window = window; window.self = window; window.top = window; window.parent = window;
  return window;
}

function sentinelBtoa(text) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  text = String(text);
  if (text.length > 1048576) throw new Error("value_limit");
  let output = "";
  for (let i = 0; i < text.length; i += 3) {
    const a = text.charCodeAt(i), b = text.charCodeAt(i + 1), c = text.charCodeAt(i + 2);
    if (a > 255 || b > 255 || c > 255) throw new TypeError("Invalid character");
    output += alphabet[a >> 2] + alphabet[((a & 3) << 4) | (Number.isNaN(b) ? 0 : b >> 4)]
      + (Number.isNaN(b) ? "=" : alphabet[((b & 15) << 2) | (Number.isNaN(c) ? 0 : c >> 6)])
      + (Number.isNaN(c) ? "=" : alphabet[c & 63]);
  }
  return output;
}

function sentinelAtob(text) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  text = String(text).replace(/[\t\n\f\r ]/g, "");
  if (text.length > 1048576 || text.length % 4 === 1 || !/^[A-Za-z0-9+/]*={0,2}$/.test(text)) throw new TypeError("Invalid base64");
  let bits = 0, value = 0, output = "";
  for (const char of text.replace(/=+$/, "")) {
    value = (value << 6) | alphabet.indexOf(char); bits += 6;
    if (bits >= 8) {bits -= 8; output += String.fromCharCode((value >> bits) & 255);}
  }
  return output;
}

function startSentinel(programJSON, contextJSON, key) {
  globalThis.__sentinelResult = null;
  const context = JSON.parse(contextJSON), r = new Map(), window = sentinelBrowser(context);
  let steps = 0, depth = 0, result = null, rejected = false, fatal = null;
  const started = Date.now();
  const xor = (a, b) => {a = String(a); b = String(b); let out = "";
    for (let i = 0; i < a.length; i++) out += String.fromCharCode(a.charCodeAt(i) ^ b.charCodeAt(i % b.length));
    return b.length ? out : a;};
  window.atob = sentinelAtob; window.btoa = sentinelBtoa;
  const put = (op, fn) => r.set(op, fn);
  const stop = reason => {fatal = reason; throw new Error(reason);};
  async function execute() {
    while (r.get(9)?.length && result === null && !rejected) {
      if (++steps > 20000 || Date.now() - started > 750) stop("execution_limit");
      const queue = r.get(9);
      if (!Array.isArray(queue) || queue.length > 10000) stop("queue_limit");
      const row = queue.shift();
      if (!Array.isArray(row) || row.length > 64) stop("invalid_instruction");
      const [op, ...args] = row, fn = r.get(op);
      if (typeof fn !== "function") stop("unsupported_opcode");
      await fn(...args);
    }
  }
  async function nested(code) {
    if (++depth > 16 || !Array.isArray(code) || code.length > 10000) stop("call_limit");
    const saved = r.get(9);
    try {r.set(9, [...code]); await execute();} finally {r.set(9, saved); depth--;}
  }
  put(1, (a, b) => r.set(a, xor(r.get(a), r.get(b))));
  put(2, (a, b) => r.set(a, b));
  put(3, value => {result = sentinelBtoa(String(value));});
  put(4, () => {rejected = true;});
  put(5, (a, b) => {const value = r.get(a); Array.isArray(value) ? value.push(r.get(b)) : r.set(a, value + r.get(b));});
  put(6, (a, b, c) => r.set(a, r.get(b)[r.get(c)]));
  put(7, (a, ...args) => r.get(a)(...args.map(value => r.get(value))));
  put(8, (a, b) => r.set(a, r.get(b)));
  r.set(10, window);
  put(11, (a, b) => r.set(a, window.document.scripts.map(script => script.src.match(r.get(b))).find(match => match?.length)?.[0] ?? null));
  put(12, a => r.set(a, r));
  put(13, (a, b, ...args) => {try {r.get(b)(...args);} catch (error) {r.set(a, String(error));}});
  put(14, (a, b) => r.set(a, JSON.parse(String(r.get(b)))));
  put(15, (a, b) => r.set(a, JSON.stringify(r.get(b))));
  r.set(16, key);
  put(17, async (a, b, ...args) => {try {r.set(a, await r.get(b)(...args.map(value => r.get(value))));} catch (error) {r.set(a, String(error));}});
  put(18, a => r.set(a, sentinelAtob(r.get(a))));
  put(19, a => r.set(a, sentinelBtoa(r.get(a))));
  put(20, (a, b, c, ...args) => r.get(a) === r.get(b) ? r.get(c)(...args) : null);
  put(21, (a, b, c, d, ...args) => Math.abs(r.get(a) - r.get(b)) > r.get(c) ? r.get(d)(...args) : null);
  put(22, async (a, code) => {try {await nested(code);} catch (error) {r.set(a, String(error));}});
  put(23, (a, b, ...args) => r.get(a) !== undefined ? r.get(b)(...args) : null);
  put(24, (a, b, c) => r.set(a, r.get(b)[r.get(c)].bind(r.get(b))));
  for (const op of [25, 26, 28]) put(op, () => {});
  put(27, (a, b) => {const value = r.get(a); Array.isArray(value) ? value.splice(value.indexOf(r.get(b)), 1) : r.set(a, value - r.get(b));});
  put(29, (a, b, c) => r.set(a, r.get(b) < r.get(c)));
  put(30, (a, target, slots, code) => {const hasArgs = Array.isArray(code), body = hasArgs ? code : slots;
    r.set(a, async (...args) => {if (hasArgs) slots.forEach((slot, index) => r.set(slot, args[index]));
      try {await nested(body || []); return r.get(target);} catch (error) {return String(error);}});});
  put(33, (a, b, c) => r.set(a, Number(r.get(b)) * Number(r.get(c))));
  put(34, async (a, b) => r.set(a, await r.get(b)));
  put(35, (a, b, c) => r.set(a, Number(r.get(c)) === 0 ? 0 : Number(r.get(b)) / Number(r.get(c))));
  r.set(9, JSON.parse(programJSON));
  execute().then(() => {
    const token = !fatal && !rejected && typeof result === "string" && result.length > 0 && result.length <= 131072 ? result : null;
    globalThis.__sentinelResult = JSON.stringify({token, steps, reason: token ? null : fatal || (rejected ? "rejected" : "no_result")});
  }).catch(error => {
    const known = ["execution_limit", "queue_limit", "invalid_instruction", "unsupported_opcode", "call_limit"];
    globalThis.__sentinelResult = JSON.stringify({token: null, steps, reason: known.includes(error.message) ? error.message : "execution_failed"});
  });
}
