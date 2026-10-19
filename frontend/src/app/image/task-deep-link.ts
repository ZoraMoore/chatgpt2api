import type { ImageTask } from "@/lib/api";

type ConversationTasks = { id: string; turns: Array<{ images: Array<{ id: string; taskId?: string }> }> };
type TaskReader = (ids: string[]) => Promise<{ items: ImageTask[]; missing_ids: string[] }>;

export function validLinkedTaskId(value: string | null | undefined): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

/** 深链只定位已存在的任务，返回服务端任务时不构造本地生成队列。 */
export async function resolveImageTaskLink(taskId: string | null, conversations: ConversationTasks[], read: TaskReader) {
  if (!validLinkedTaskId(taskId)) return { kind: "invalid" } as const;
  const local = conversations.find((conversation) => conversation.turns.some((turn) => turn.images.some((image) => (image.taskId || image.id) === taskId)));
  if (local) return { kind: "local", conversationId: local.id } as const;
  const result = await read([taskId]);
  const task = result.items.find((item) => item.id === taskId);
  return task ? { kind: "remote", task } as const : { kind: "missing" } as const;
}
