import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
const source = ts.createSourceFile("page.tsx", readFileSync(new URL("./page.tsx", import.meta.url), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function findNode(predicate) {
    let found;
    function visit(node) {
        if (!found && predicate(node))
            found = node;
        if (!found)
            node.forEachChild(visit);
    }
    visit(source);
    assert.ok(found, "目标页面中应保留被验证的恢复行为");
    return found;
}
test("超时恢复将任务绑定到本地会话，而不是上游 conversation_id", () => {
    const call = findNode((node) => ts.isCallExpression(node) && node.expression.getText(source) === "setTimeoutRetry" && ts.isObjectLiteralExpression(node.arguments[0]));
    const state = runInNewContext(`(${call.arguments[0].getText(source)})`, {
        conversationId: "local-conversation",
        timeoutTask: { id: "task-1", conversation_id: "upstream-conversation", error: "图片生成超时" },
    });
    assert.equal(state.conversationId, "local-conversation");
    assert.equal(state.taskId, "task-1");
});
for (const recoveredAfterReload of [false, true]) test(recoveredAfterReload ? "刷新后从图片任务 ID 恢复原任务，不依赖临时超时弹层状态" : "继续等待只恢复原任务并查询结果，不重新提交生成请求", async () => {
    const declaration = findNode((node) => ts.isVariableDeclaration(node) && node.name.getText(source) === "handleTimeoutRetryContinue");
    assert.ok(declaration.initializer && ts.isCallExpression(declaration.initializer));
    const callback = declaration.initializer.arguments[0];
    const javascript = ts.transpile(`const callback = ${callback.getText(source)}; callback;`, { target: ts.ScriptTarget.ES2022 });
    let localConversation = {
        id: "local-conversation", updatedAt: "", turns: [{ id: "turn-1", status: "error", images: [{ id: "image-1", taskId: "task-1", status: "error", error: "图片生成超时" }] }],
    };
    const calls = { resume: 0, query: 0, generation: 0 };
    const context = {
        timeoutRetry: recoveredAfterReload ? null : { conversationId: "local-conversation", taskId: "task-1" }, imageTimeoutRetrySecs: 30,
        conversationsRef: { get current() { return [localConversation]; } },
        loadCancelledRef: { current: false }, resumingConversationIdsRef: { current: new Set() }, resumingTaskIdsRef: { current: new Set() },
        resumeImagePoll: async () => { calls.resume++; return { id: "task-1", status: "running" }; },
        fetchImageTasks: async () => { calls.query++; return { items: [{ id: "task-1", status: "success", data: [{ url: "/images/exact-original.png" }] }], missing_ids: [] }; },
        updateConversation: async (_id, updater) => { localConversation = updater(localConversation); },
        taskDataToStoredImage: (image, task) => ({ ...image, status: task.status === "running" ? "loading" : task.status, data: task.data }),
        deriveTurnStatus: (turn) => ({ status: turn.images.some((image) => image.status === "loading") ? "generating" : turn.images.some((image) => image.status === "error") ? "error" : "success" }),
        setTimeoutRetry: () => undefined, loadQuota: async () => undefined, notifyTaskUpdate: () => undefined,
        runConversationQueue: () => { calls.generation++; }, sleep: async () => undefined, pollDelayMs: () => 0,
        toast: { info() { }, error() { } }, Date, Map, Set,
    };
    const handler = runInNewContext(javascript, context);
    await handler("task-1");
    assert.equal(calls.resume, 1);
    assert.ok(calls.query > 0, "恢复后应查询原任务的终态");
    assert.equal(calls.generation, 0, "恢复已有任务不应进入生成提交流程");
    assert.equal(localConversation.turns[0].images[0].status, "success");
});
