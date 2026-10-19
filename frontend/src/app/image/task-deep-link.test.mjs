import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function implementation(path) {
  const file = new URL(path, import.meta.url);
  assert.ok(existsSync(file), "页面应实现只读任务深链解析");
  const exports = {};
  runInNewContext(ts.transpile(readFileSync(file, "utf8"), { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }), { exports, URLSearchParams });
  return exports;
}

test("图片深链优先定位本地任务，不读取或重放生成请求", async () => {
  const { resolveImageTaskLink } = implementation("./task-deep-link.ts");
  let reads = 0;
  const result = await resolveImageTaskLink("task-1", [{ id: "local-thread", turns: [{ images: [{ id: "image-1", taskId: "task-1" }] }] }], async () => { reads++; throw new Error("不应读取"); });
  assert.equal(result.kind, "local");
  assert.equal(result.conversationId, "local-thread");
  assert.equal(reads, 0);
});

test("本地不存在时只读原任务，保留服务端结果且不把上游会话ID变成本地ID", async () => {
  const { resolveImageTaskLink } = implementation("./task-deep-link.ts");
  const reads = [];
  const task = { id: "task-1", conversation_id: "upstream-thread", status: "success", data: [{ url: "/images/exact.png" }] };
  const result = await resolveImageTaskLink("task-1", [], async (ids) => { reads.push(...ids); return { items: [task], missing_ids: [] }; });
  assert.equal(result.kind, "remote");
  assert.equal(result.task, task);
  assert.equal(result.conversationId, undefined);
  assert.deepEqual(reads, ["task-1"]);
});

test("非法任务ID不发请求；合法但不存在的任务不创建新任务", async () => {
  const { resolveImageTaskLink } = implementation("./task-deep-link.ts");
  let reads = 0;
  const read = async () => { reads++; return { items: [], missing_ids: ["missing"] }; };
  assert.equal((await resolveImageTaskLink("bad,id", [], read)).kind, "invalid");
  assert.equal(reads, 0);
  assert.equal((await resolveImageTaskLink("missing", [], read)).kind, "missing");
  assert.equal(reads, 1);
});

test("文件深链仅选择合法的五页类型，按精确任务ID与类型读取，不自动生成", async () => {
  const { parseDebugTaskLink, resolveFileTaskLink } = implementation("../debug/task-deep-link.ts");
  const link = parseDebugTaskLink("?tab=psd&task_id=file-1");
  assert.equal(link.tab, "psd");
  assert.equal(link.taskId, "file-1");
  assert.equal(parseDebugTaskLink("?tab=unknown&task_id=bad,id").tab, "skills");
  assert.equal(parseDebugTaskLink("?tab=unknown&task_id=bad,id").taskId, null);
  const task = { id: "file-1", kind: "psd", status: "success" };
  const calls = [];
  assert.equal(await resolveFileTaskLink("file-1", "psd", async (path) => { calls.push(path); return { items: [task] }; }), task);
  assert.equal(calls.length, 1);
  assert.match(calls[0], /^\/v1\/editable-file-tasks\?ids=file-1$/);
  assert.equal(await resolveFileTaskLink("file-1", "ppt", async () => ({ items: [task] })), null);
});

test("带任务深链恢复本地运行中的图片时，普通队列也只能GET查询", async () => {
  const file = ts.createSourceFile("page.tsx", readFileSync(new URL("./page.tsx", import.meta.url), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback;
  const visit = (node) => { if (ts.isVariableDeclaration(node) && node.name.getText(file) === "runConversationQueue") callback = node.initializer.arguments[0]; node.forEachChild(visit); };
  visit(file);
  assert.ok(callback);
  let conversation = { id: "local-thread", turns: [{ id: "turn-1", mode: "generate", referenceImages: [], status: "generating", images: [{ id: "image-1", taskId: "task-1", status: "loading" }] }] };
  const calls = { query: 0, generation: 0 };
  const context = {
    linkedTaskId: "task-1", linkedTaskDismissedRef: { current: false }, validLinkedTaskId: (id) => /^[A-Za-z0-9_-]{1,128}$/.test(id),
    activeConversationQueueIds: new Set(), resumingConversationIdsRef: { current: new Set() }, resumingTaskIdsRef: { current: new Set() }, pendingSubmissionTaskIdsRef: { current: new Set() },
    conversationsRef: { get current() { return [conversation]; } },
    updateConversation: async (_id, updater) => { conversation = updater(conversation); },
    fetchImageTasks: async () => { calls.query++; return { items: [{ id: "task-1", status: "success", data: [{ url: "/images/exact.png" }] }], missing_ids: [] }; },
    createImageGenerationTask: async () => { calls.generation++; throw new Error("深链不能生成"); },
    taskDataToStoredImage: (image, task) => ({ ...image, status: task.status }), deriveTurnStatus: (turn) => ({ status: turn.images.some((image) => image.status === "loading") ? "generating" : "success" }),
    loadQuota: async () => undefined, notifyTaskUpdate: () => undefined, setTimeoutRetry: () => undefined, sleep: async () => undefined, pollDelayMs: () => 0,
    toast: { error() {} }, Date, Map, Set,
  };
  const handler = runInNewContext(ts.transpile(`const callback = ${callback.getText(file)}; callback;`, { target: ts.ScriptTarget.ES2022 }), context);
  await handler("local-thread");
  assert.equal(calls.generation, 0, "查看现有任务不应调用生成 POST");
  assert.ok(calls.query > 0);
});
