import type { EditableFileTask } from "./components/types";

const validTaskId = (value: string | null | undefined): value is string => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);

export function parseDebugTaskLink(search: string) {
  const params = new URLSearchParams(search);
  const requestedTab = params.get("tab") || "skills";
  const tab = ["skills", "search", "ppt", "psd", "chat"].includes(requestedTab) ? requestedTab : "skills";
  const id = params.get("task_id");
  return { tab, taskId: ["ppt", "psd"].includes(tab) && validTaskId(id) ? id : null };
}

export async function resolveFileTaskLink(taskId: string, kind: string, read: (path: string) => Promise<{ items: EditableFileTask[] }>) {
  if (!validTaskId(taskId) || !["ppt", "psd"].includes(kind)) return null;
  const result = await read(`/v1/editable-file-tasks?ids=${encodeURIComponent(taskId)}`);
  return result.items.find((task) => (task.taskId || task.id) === taskId && task.kind === kind) || null;
}
