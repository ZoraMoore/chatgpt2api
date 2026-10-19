"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Clock3,
  ExternalLink,
  Layers3,
  LoaderCircle,
  RefreshCw,
  TriangleAlert,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { httpRequest } from "@/lib/request";
import { getAuthEpoch, type StoredAuthSession } from "@/store/auth";

type Entry = {
  id: string;
  kind: string;
  status: string;
  progress?: string;
  updated_at?: string;
};
type Job = {
  id: string;
  kind: "account-refresh" | "account-cleanup";
  created: number;
};
const pending = (status: string) =>
  [
    "queued",
    "running",
    "pending",
    "scanning",
    "executing",
    "verifying",
  ].includes(status);
const label = (status: string) =>
  ({
    queued: "排队中",
    pending: "等待处理",
    running: "处理中",
    scanning: "预览中",
    ready: "等待确认",
    success: "已完成",
    completed: "已完成",
    error: "未完成",
    failed: "未完成",
    unknown: "无法读取",
    partial: "部分完成",
  })[status] || status;
const kindLabel = (kind: string) =>
  ({
    image: "图片生成",
    ppt: "PPT 生成",
    psd: "PSD 生成",
    "account-refresh": "账号刷新",
    "account-cleanup": "图像空间清理",
  })[kind] || "文件任务";

export function TaskCenter({
  session,
  onNavigate,
}: {
  session: StoredAuthSession;
  onNavigate: (href: string) => void;
}) {
  const [open, setOpen] = useState(false),
    [entries, setEntries] = useState<Entry[]>([]),
    [loading, setLoading] = useState(false),
    [connectionError, setConnectionError] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(40);
  const jobs = useRef<Job[]>([]),
    active = useRef(true),
    busy = useRef(false);
  const owner = `${session.role}:${session.subjectId}:${getAuthEpoch()}`;
  const storageKey = `chatgpt2api:tasks:${owner}`;
  const load = useCallback(async () => {
    if (busy.current || document.visibilityState === "hidden") return;
    busy.current = true;
    setLoading(true);
    try {
      const sources = await Promise.allSettled([
        httpRequest<{ items: Entry[] }>("/api/image-tasks"),
        httpRequest<{ items: Entry[] }>("/v1/editable-file-tasks"),
        ...jobs.current.map(async (job) => {
          const path =
            job.kind === "account-refresh"
              ? "/api/accounts/operations/jobs/"
              : "/api/accounts/image-space/jobs/";
          const value = await httpRequest<{ status: string }>(
            path + encodeURIComponent(job.id),
          );
          return {
            items: [
              {
                id: job.id,
                kind: job.kind,
                status: value.status,
                updated_at: new Date(job.created).toISOString(),
              },
            ],
          };
        }),
      ]);
      if (!active.current) return;
      const items: Entry[] = [];
      sources.forEach((result, index) => {
        if (
          result.status === "fulfilled" &&
          Array.isArray(result.value.items)
        ) {
          for (const item of result.value.items) {
            if (
              !item ||
              typeof item.id !== "string" ||
              typeof item.status !== "string"
            )
              continue;
            items.push({
              ...item,
              kind: index === 0 ? "image" : item.kind || "file",
            });
          }
        }
      });
      // 查询失败保留上次确认的任务，不能把连接错误写成业务失败。
      if (sources.some((result) => result.status === "rejected")) {
        setConnectionError(true);
        setEntries((previous) => {
          const seen = new Set(items.map((item) => `${item.kind}:${item.id}`));
          return [
            ...items,
            ...previous.filter((item) => !seen.has(`${item.kind}:${item.id}`)),
          ];
        });
      } else {
        setConnectionError(false);
        setEntries(items);
      }
    } finally {
      busy.current = false;
      if (active.current) setLoading(false);
    }
  }, [owner]);
  useEffect(() => {
    active.current = true;
    setEntries([]);
    jobs.current = [];
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
      if (Array.isArray(saved))
        jobs.current = saved
          .filter(
            (item: Job) =>
              item &&
              /^[a-f0-9]{32}$/.test(item.id) &&
              ["account-refresh", "account-cleanup"].includes(item.kind) &&
              Date.now() - item.created < 86400000,
          )
          .slice(-30);
    } catch {}
    const changed = () => void load();
    const register = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (
        session.role !== "admin" ||
        !detail ||
        typeof detail.id !== "string" ||
        !/^[a-f0-9]{32}$/.test(detail.id) ||
        !["account-refresh", "account-cleanup"].includes(detail.kind)
      )
        return;
      jobs.current = [
        ...jobs.current.filter((job) => job.id !== detail.id),
        { id: detail.id, kind: detail.kind, created: Date.now() },
      ].slice(-30);
      try {
        sessionStorage.setItem(storageKey, JSON.stringify(jobs.current));
      } catch {}
      void load();
    };
    changed();
    window.addEventListener("chatgpt2api:tasks-updated", changed);
    window.addEventListener("chatgpt2api:job-created", register);
    document.addEventListener("visibilitychange", changed);
    return () => {
      active.current = false;
      try {
        sessionStorage.removeItem(storageKey);
      } catch {}
      window.removeEventListener("chatgpt2api:tasks-updated", changed);
      window.removeEventListener("chatgpt2api:job-created", register);
      document.removeEventListener("visibilitychange", changed);
    };
  }, [storageKey, load, session.role]);
  const count = entries.filter((entry) => pending(entry.status)).length;
  useEffect(() => {
    const timer = setInterval(
      () => void load(),
      open || count > 0 ? 5000 : 30000,
    );
    return () => clearInterval(timer);
  }, [load, open, count]);
  return (
    <>
      <button
        type="button"
        className="glass-task-capsule"
        aria-label="打开任务中心"
        onClick={() => {
          setOpen(true);
          void load();
        }}
      >
        {count ? (
          <LoaderCircle size={16} className="animate-spin" />
        ) : (
          <Layers3 size={17} />
        )}
        <span>任务{count ? ` ${count}` : ""}</span>
        {count ? <i aria-hidden="true" /> : null}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="glass-task-dialog">
          <DialogHeader>
            <div className="glass-dialog-symbol">
              <Layers3 size={24} />
            </div>
            <DialogTitle>任务中心</DialogTitle>
            <DialogDescription>
              查看当前账户的真实任务。离开页面不会取消已提交任务。
            </DialogDescription>
          </DialogHeader>
          <div className="flex items-center justify-between gap-3">
            <span className="glass-helper">
              {count} 个进行中 · {entries.length} 条已知任务
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={loading}
              onClick={() => void load()}
            >
              <RefreshCw className={loading ? "animate-spin" : ""} size={14} />
              刷新任务
            </Button>
          </div>
          {connectionError ? (
            <p role="status" className="glass-connection-note">
              暂时无法更新部分任务，保留最后确认的状态。
            </p>
          ) : null}
          <div className="glass-task-list">
            {entries.length === 0 ? (
              <div className="glass-task-empty">
                <Clock3 size={30} />
                <strong>当前没有任务</strong>
                <span>生图、文件生成和已登记的账号任务会在这里显示。</span>
              </div>
            ) : (
              [...entries]
                .sort(
                  (a, b) =>
                    Number(pending(b.status)) - Number(pending(a.status)) ||
                    (Date.parse(b.updated_at || "") || 0) -
                      (Date.parse(a.updated_at || "") || 0),
                )
                .slice(0, visibleLimit)
                .map((entry) => (
                  <div
                    className="glass-task-entry"
                    key={`${entry.kind}:${entry.id}`}
                  >
                    <span
                      className={`glass-task-state ${pending(entry.status) ? "pending" : entry.status === "partial" ? "warning" : entry.status === "ready" ? "ready" : ["error", "failed", "unknown"].includes(entry.status) ? "error" : "done"}`}
                    >
                      {pending(entry.status) || entry.status === "ready" ? (
                        <Clock3 size={18} />
                      ) : ["error", "failed", "unknown", "partial"].includes(
                          entry.status,
                        ) ? (
                        <TriangleAlert size={18} />
                      ) : (
                        <CheckCircle2 size={18} />
                      )}
                    </span>
                    <div>
                      <strong>{kindLabel(entry.kind)}</strong>
                      <span>{label(entry.status)}</span>
                      <code>{entry.id.slice(0, 12)}…</code>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setOpen(false);
                        onNavigate(
                          entry.kind === "image"
                            ? `/image?task_id=${encodeURIComponent(entry.id)}`
                            : entry.kind.startsWith("account-")
                              ? "/accounts"
                              : `/debug?tab=${entry.kind === "psd" ? "psd" : "ppt"}&task_id=${encodeURIComponent(entry.id)}`,
                        );
                      }}
                    >
                      查看
                      <ExternalLink size={13} />
                    </Button>
                  </div>
                ))
            )}
          </div>
          {entries.length > visibleLimit ? (
            <Button
              variant="outline"
              onClick={() => setVisibleLimit((value) => value + 40)}
            >
              查看更多任务（剩余 {entries.length - visibleLimit} 条）
            </Button>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
