import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = ts.createSourceFile("page.tsx", readFileSync(new URL("./page.tsx", import.meta.url), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function sourceFunction(name) {
  let selected;
  const visit = (node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === name) selected = node.initializer.arguments[0].getText(source);
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) selected = node.getText(source);
    node.forEachChild(visit);
  };
  visit(source);
  assert.ok(selected, `缺少 ${name}`);
  return selected;
}
function runFunction(name, context) {
  return runInNewContext(ts.transpile(`const callback = ${sourceFunction(name)}; callback;`, { target: ts.ScriptTarget.ES2022 }), context);
}
const updateImage = (image, task) => ({ ...image, status: ["running", "queued"].includes(task.status) ? "loading" : task.status, error: task.error });
const deriveStatus = (turn) => ({ status: turn.images.some((image) => image.status === "loading") ? "generating" : turn.images.some((image) => image.status === "error") ? "error" : "success" });
const history = () => [{ id: "local-thread", turns: [{ id: "turn-1", mode: "generate", prompt: "原提示词", referenceImages: [], createdAt: "2026-10-20", status: "generating", images: [{ id: "image-1", taskId: "task-1", taskStatus: "running", status: "loading" }] }] }];

for (const mode of ["generate", "edit"]) for (const freshSubmission of [false, true]) test(`${mode}：${freshSubmission ? "首次显式提交只POST一次，后续missing不重放" : "普通已ack任务轮询missing时保持零生成POST"}`, async () => {
  let conversation = history()[0];
  conversation.turns[0].mode = mode;
  conversation.turns[0].referenceImages = [{ name: "reference.png", type: "image/png", dataUrl: "data:image/png;base64,AAAA" }];
  let reads = 0, writes = 0, missingReads = 0;
  const submit = async () => { writes++; return { id: "task-1", status: writes > 1 ? "success" : "running", data: [{ url: "/images/original.png" }] }; };
  const context = {
    linkedTaskId: null, linkedTaskDismissedRef: { current: false }, validLinkedTaskId: () => false,
    pendingSubmissionTaskIdsRef: { current: new Set(freshSubmission ? ["task-1"] : []) },
    activeConversationQueueIds: new Set(), resumingConversationIdsRef: { current: new Set() }, resumingTaskIdsRef: { current: new Set() },
    conversationsRef: { get current() { return [conversation]; } },
    updateConversation: async (_id, updater) => { conversation = updater(conversation); },
    fetchImageTasks: async () => { reads++; if (reads === 1 && !freshSubmission) return { items: [{ id: "task-1", status: "running" }], missing_ids: [] }; missingReads++; return { items: [], missing_ids: ["task-1"] }; },
    createImageGenerationTask: submit, createImageEditTask: submit, dataUrlToFile: () => ({ name: "reference.png" }),
    taskDataToStoredImage: updateImage, deriveTurnStatus: deriveStatus,
    loadQuota: async () => undefined, notifyTaskUpdate: () => undefined, setTimeoutRetry: () => undefined, sleep: async () => undefined, pollDelayMs: () => 0,
    toast: { error() {} }, Date, Map, Set,
  };
  await runFunction("runConversationQueue", context)("local-thread");
  assert.ok(missingReads > 0, "必须实际经过轮询missing分支");
  assert.equal(writes, freshSubmission ? 1 : 0, "只有用户刚授权的首次提交可以POST，任务丢失不构成再生成授权");
  assert.equal(conversation.turns[0].images[0].taskId, "task-1");
  assert.equal(conversation.turns[0].prompt, "原提示词");
  assert.equal(conversation.turns[0].images[0].status, "error");
  assert.match(conversation.turns[0].images[0].error, /不存在|过期|无法确认/);
});

for (const queryFails of [false, true]) test(queryFails ? "初次reconcile查询失败保留原任务ID并停止自动提交" : "初次reconcile记录已过期保留原任务ID并停止自动提交", async () => {
  const items = history();
  let persisted;
  const context = {
    fetchImageTasks: async () => { if (queryFails) throw new Error("网络暂时不可用"); return { items: [], missing_ids: ["task-1"] }; },
    taskDataToStoredImage: updateImage, deriveTurnStatus: deriveStatus,
    saveImageConversations: async (value) => { persisted = value; }, Date, Map, Set,
  };
  const result = await runFunction("syncConversationImageTasks", context)(items);
  const image = result[0].turns[0].images[0];
  assert.equal(image.taskId, "task-1");
  assert.equal(image.status, "error");
  assert.match(image.error, /不存在|过期|无法确认/);
  assert.equal(result[0].turns[0].status, "error");
  assert.ok(persisted, "无法确认状态应持久化，避免刷新后自动进入生成队列");
});
