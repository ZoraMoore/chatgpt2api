"use client";
import { useUiReducedMotion as useReducedMotion } from "@/lib/hooks/use-ui-reduced-motion";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion} from "motion/react";
import {
  Ban,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Database,
  Download,
  LoaderCircle,
  LogIn,
  Pencil,
  RefreshCw,
  Trash2,
  TriangleAlert,
  Users,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { AnimatedNumber } from "@/components/motion/animated-number";
import { PageEnter } from "@/components/page-enter";
import { PageLoader } from "@/components/page-loader";
import { SearchInput } from "@/components/search-input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteAccounts,
  fetchAccounts,
  fetchModels,
  fetchRefreshProgress,
  fetchReLoginProgress,
  reLoginAccounts,
  refreshAccounts,
  testProxy,
  updateAccount,
  type Account,
  type AccountStatus,
  type Model,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";
import { AccountImportDialog } from "./components/account-import-dialog";

const statuses: AccountStatus[] = ["正常", "限流", "异常", "禁用"];
const operationPrefix = "chatgpt2api:account-operations:";
const statusTone: Record<AccountStatus, string> = {
  正常: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  限流: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  异常: "bg-rose-500/10 text-rose-600 dark:text-rose-400",
  禁用: "bg-slate-500/10 text-slate-500 dark:text-slate-400",
};

function compact(value: number) {
  return value >= 1000
    ? `${(value / 1000).toFixed(1)}k`
    : String(Math.round(value));
}
function maskToken(value: string) {
  return value.length > 24
    ? `${value.slice(0, 16)}…${value.slice(-7)}`
    : "••••••••";
}
function parseTimestamp(value?: string | number | null) {
  if (!value) return null;
  const raw =
    typeof value === "number"
      ? value * 1000
      : /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
        ? value
        : `${value.replace(" ", "T")}Z`;
  const date = new Date(raw);
  return Number.isFinite(date.getTime()) ? date : null;
}
function beijingTime(value?: string | number | null, short = false) {
  const date = parseTimestamp(value);
  if (!date) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    ...(short ? {} : { year: "numeric" as const }),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}
function restoreTime(value?: string | null) {
  const date = parseTimestamp(value);
  if (!date) return { relative: "—", absolute: "" };
  const hours = Math.max(0, Math.ceil((date.getTime() - Date.now()) / 3600000));
  return {
    relative: hours
      ? `剩余 ${Math.floor(hours / 24)}d ${hours % 24}h`
      : "已到期",
    absolute: beijingTime(value),
  };
}
function downloadTokens(accounts: Account[]) {
  const url = URL.createObjectURL(
    new Blob([accounts.map((item) => item.access_token).join("\n")], {
      type: "text/plain;charset=utf-8",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `accounts-${Date.now()}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function AccountsContent() {
  const reducedMotion = useReducedMotion();
  const mounted = useRef(true);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [type, setType] = useState("all");
  const [status, setStatus] = useState<AccountStatus | "all">("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState("10");
  const [working, setWorking] = useState(false);
  const [progress, setProgress] = useState<{
    processed: number;
    total: number;
    label: string;
  } | null>(null);
  const [editing, setEditing] = useState<Account | null>(null);
  const [editStatus, setEditStatus] = useState<AccountStatus>("正常");
  const [editProxy, setEditProxy] = useState("");
  const [testingProxy, setTestingProxy] = useState(false);
  const [deleting, setDeleting] = useState<string[]>([]);

  const applyAccounts = (items: Account[]) => {
    setAccounts(items);
    setSelected((current) =>
      current.filter((token) =>
        items.some((item) => item.access_token === token),
      ),
    );
  };
  const load = async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      applyAccounts((await fetchAccounts()).items);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载账号失败");
    } finally {
      if (mounted.current) setLoading(false);
    }
  };

  useEffect(() => {
    mounted.current = true;
    void load();
    void fetchModels()
      .then((data) => setModels(Array.isArray(data.data) ? data.data : []))
      .catch(() => toast.error("加载模型失败"));
    return () => {
      mounted.current = false;
    };
  }, []);

  // 扩展只接收不含凭据的账号引用；选择仍留在 React 内，避免把 Token 传给页面事件。
  useEffect(() => {
    const publish = () =>
      window.dispatchEvent(
        new CustomEvent(operationPrefix + "state", {
          detail: {
            loading,
            items: accounts.map((item) => ({
              id: item.account_ref,
              label: item.email || item.name || "未命名账号",
              expires_at: item.access_token_expires_at,
              has_refresh_token: item.has_refresh_token,
              auto_refresh_token: item.auto_refresh_token !== false,
              image_cleanup_enabled: item.image_cleanup_enabled === true,
              cleanup_next_at: item.image_cleanup_next_at,
              cleanup_last_at: item.image_cleanup_last_at,
              cleanup_last_status: item.image_cleanup_last_status,
              cleanup_last_error: item.image_cleanup_last_error || "",
              status: item.status,
              quota_state: item.image_limit_state,
              quota_reason: item.image_limit_reason,
              quota_restore_at: item.image_limit_restore_at,
              quota_occurred_at: item.image_limit_occurred_at,
              quota_next_check_at: item.image_limit_next_check_at,
              supported: item.source_type !== "adobe",
            })),
            selected: accounts
              .filter((item) => selected.includes(item.access_token))
              .map((item) => item.account_ref),
          },
        }),
      );
    publish();
    window.addEventListener(operationPrefix + "request", publish);
    return () =>
      window.removeEventListener(operationPrefix + "request", publish);
  }, [accounts, selected, loading]);
  useEffect(() => {
    const reload = () => {
      void load(true);
    };
    window.addEventListener(operationPrefix + "reload", reload);
    return () => window.removeEventListener(operationPrefix + "reload", reload);
  }, []);
  useEffect(() => {
    const selectAll = (event: Event) => {
      const all = (event as CustomEvent<{ all?: boolean }>).detail?.all;
      if (all === true)
        setSelected(
          accounts
            .filter((item) => item.source_type !== "adobe")
            .map((item) => item.access_token),
        );
      else if (all === false) setSelected([]);
    };
    window.addEventListener(operationPrefix + "select-all", selectAll);
    return () =>
      window.removeEventListener(operationPrefix + "select-all", selectAll);
  }, [accounts]);

  const summary = useMemo(
    () => ({
      total: accounts.length,
      active: accounts.filter((item) => item.status === "正常").length,
      limited: accounts.filter((item) => item.status === "限流").length,
      abnormal: accounts.filter((item) => item.status === "异常").length,
      disabled: accounts.filter((item) => item.status === "禁用").length,
      quota: accounts
        .filter((item) => item.status === "正常")
        .reduce((sum, item) => sum + Math.max(0, item.quota), 0),
    }),
    [accounts],
  );
  const filtered = useMemo(() => {
    const text = query.trim().toLowerCase();
    return accounts.filter(
      (item) =>
        (!text ||
          [item.email, item.name, item.user_id, item.access_token].some(
            (value) => value?.toLowerCase().includes(text),
          )) &&
        (type === "all" || item.type === type) &&
        (status === "all" || item.status === status),
    );
  }, [accounts, query, type, status]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / Number(pageSize)));
  const safePage = Math.min(page, pageCount);
  const currentRows = filtered.slice(
    (safePage - 1) * Number(pageSize),
    safePage * Number(pageSize),
  );
  const allSelected =
    currentRows.length > 0 &&
    currentRows.every((item) => selected.includes(item.access_token));
  const types = Array.from(new Set(accounts.map((item) => item.type)))
    .filter(Boolean)
    .sort();
  const cards = [
    {
      label: "账户总数",
      value: summary.total,
      icon: Users,
      color: "text-sky-500",
      filter: "all" as const,
    },
    {
      label: "正常账户",
      value: summary.active,
      icon: Check,
      color: "text-emerald-500",
      filter: "正常" as const,
    },
    {
      label: "限流账户",
      value: summary.limited,
      icon: Clock3,
      color: "text-amber-500",
      filter: "限流" as const,
    },
    {
      label: "异常账户",
      value: summary.abnormal,
      icon: TriangleAlert,
      color: "text-rose-500",
      filter: "异常" as const,
    },
    {
      label: "禁用账户",
      value: summary.disabled,
      icon: Ban,
      color: "text-slate-400",
      filter: "禁用" as const,
    },
    {
      label: "剩余额度",
      value: summary.quota,
      icon: Database,
      color: "text-blue-500",
      filter: "all" as const,
    },
  ];

  const runAccountJob = async (tokens: string[], recover = false) => {
    if (!tokens.length || working) return;
    setWorking(true);
    setProgress({
      processed: 0,
      total: tokens.length,
      label: recover ? "正在尝试恢复账号" : "正在刷新信息和额度",
    });
    try {
      const job = await (recover
        ? reLoginAccounts(tokens)
        : refreshAccounts(tokens));
      const deadline = Date.now() + 20 * 60 * 1000;
      while (mounted.current && Date.now() < deadline) {
        const state = await (recover
          ? fetchReLoginProgress(job.progress_id)
          : fetchRefreshProgress(job.progress_id));
        if (!mounted.current) return;
        setProgress({
          processed: state.processed,
          total: state.total,
          label: recover ? "正在尝试恢复账号" : "正在刷新信息和额度",
        });
        if (state.done) {
          if (state.error) throw new Error(state.error);
          if (state.result?.items) applyAccounts(state.result.items);
          else await load(true);
          const errors = state.result?.errors?.length ?? 0;
          toast.success(
            `${recover ? "恢复" : "刷新"}完成${errors ? `，${errors} 个账号未完成，请查看日志` : ""}`,
          );
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      if (mounted.current)
        toast.message("任务仍在后台处理，可稍后刷新列表查看结果");
    } catch (error) {
      if (mounted.current)
        toast.error(error instanceof Error ? error.message : "账号操作失败");
    } finally {
      if (mounted.current) {
        setWorking(false);
        setProgress(null);
      }
    }
  };
  const remove = async () => {
    if (!deleting.length || working) return;
    setWorking(true);
    try {
      const data = await deleteAccounts(deleting);
      applyAccounts(data.items);
      setDeleting([]);
      toast.success(`已删除 ${data.removed ?? 0} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setWorking(false);
    }
  };
  const save = async () => {
    if (!editing || working) return;
    setWorking(true);
    try {
      const data = await updateAccount(editing.access_token, {
        status: editStatus,
        ...(editProxy.trim() !== (editing.proxy ?? "").trim()
          ? { proxy: editProxy.trim() }
          : {}),
      });
      applyAccounts(data.items);
      setEditing(null);
      toast.success("账号信息已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setWorking(false);
    }
  };
  const testAccountProxy = async () => {
    if (!editProxy.trim()) {
      toast.error("请先填写代理地址");
      return;
    }
    setTestingProxy(true);
    try {
      const data = await testProxy(editProxy.trim());
      data.result.ok
        ? toast.success(`代理可用 · ${data.result.latency_ms} ms`)
        : toast.error(data.result.error || "代理不可用");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试失败");
    } finally {
      setTestingProxy(false);
    }
  };

  return (
    <PageEnter className="space-y-5">
      <header className="flex flex-col justify-between gap-4 xl:flex-row xl:items-end">
        <div>
          <p className="mb-2 text-[11px] font-medium tracking-[.19em] text-muted-foreground">
            ACCOUNT POOL MANAGEMENT
          </p>
          <h1 className="text-3xl font-semibold tracking-tight sm:text-[34px]">
            号池管理
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            管理和维护 AI 账户资源，监控账户状态与使用情况
          </p>
        </div>
        <div
          id="account-page-actions"
          className="flex flex-wrap items-center gap-2"
        >
          <Button
            variant="outline"
            size="sm"
            onClick={() => void load()}
            disabled={loading || working}
          >
            <RefreshCw className={cn("size-4", loading && "animate-spin")} />
            重新加载
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => downloadTokens(accounts)}
            disabled={!accounts.length}
          >
            <Download className="size-4" />
            导出 Token
          </Button>
          <AccountImportDialog
            size="sm"
            disabled={working}
            onImported={(items) => {
              applyAccounts(items);
              setSelected([]);
              setPage(1);
            }}
          />
        </div>
      </header>

      <div className="pool-stats grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {cards.map((card, index) => (
          <motion.button
            type="button"
            key={card.label}
            aria-label={`${card.label} ${compact(card.value)}，显示${card.filter === "all" ? "全部" : card.filter}账号`}
            onClick={() => {
              setStatus(card.filter);
              setPage(1);
            }}
            initial={reducedMotion ? false : { opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{
              duration: 0.24,
              delay: reducedMotion ? 0 : index * 0.035,
            }}
            className="group rounded-2xl text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Card className="relative h-full overflow-hidden p-4 transition-transform duration-200 motion-safe:group-hover:-translate-y-0.5 sm:p-5">
              <div className="flex items-center gap-3">
                <span
                  className={cn(
                    "grid size-9 place-items-center rounded-xl bg-field/60",
                    card.color,
                  )}
                >
                  <card.icon className="size-6" />
                </span>
                <span className="text-[13px] text-muted-foreground">
                  {card.label}
                </span>
              </div>
              <div
                className={cn(
                  "mt-3 text-[29px] font-semibold tracking-tight tabular-nums",
                  card.color,
                )}
              >
                <AnimatedNumber
                  value={card.value}
                  format={card.label === "剩余额度" ? compact : undefined}
                  duration={reducedMotion ? 0 : 0.65}
                />
              </div>
              <svg
                aria-hidden="true"
                viewBox="0 0 160 45"
                className={cn(
                  "pointer-events-none absolute right-0 bottom-0 h-10 w-36 opacity-[0.08]",
                  card.color,
                )}
              >
                <path
                  d="M0 45C30 45 44 26 65 28S112-7 160 13V45Z"
                  fill="currentColor"
                />
              </svg>
            </Card>
          </motion.button>
        ))}
      </div>

      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-2 text-sm font-medium">
            系统可用模型{" "}
            <span className="ml-1 text-xs text-muted-foreground">
              ({models.length})
            </span>
          </span>
          {models.length ? (
            models.map((model) => (
              <button
                key={model.id}
                type="button"
                onClick={() => {
                  void navigator.clipboard
                    .writeText(model.id)
                    .then(() => toast.success("模型名已复制"))
                    .catch(() => toast.error("复制失败，请手动复制"));
                }}
                className="rounded-full border border-border bg-field/40 px-3 py-1.5 text-xs transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring"
                title="点击复制模型名"
              >
                {model.id}
              </button>
            ))
          ) : (
            <span className="text-xs text-muted-foreground">暂无模型</span>
          )}
        </div>
      </Card>

      {progress ? (
        <Card className="p-4" role="status" aria-live="polite">
          <div className="mb-3 flex items-center justify-between gap-3 text-sm">
            <span className="flex items-center gap-2">
              <LoaderCircle className="size-4 animate-spin text-primary" />
              {progress.label}
            </span>
            <span className="text-xs tabular-nums text-muted-foreground">
              {progress.processed} / {progress.total}
            </span>
          </div>
          <div
            role="progressbar"
            aria-label={progress.label}
            aria-valuenow={progress.processed}
            aria-valuemin={0}
            aria-valuemax={progress.total}
            className="h-1.5 overflow-hidden rounded-full bg-primary/10"
          >
            <div
              className="h-full origin-left rounded-full bg-primary transition-transform duration-300"
              style={{
                transform: `scaleX(${progress.total ? progress.processed / progress.total : 0})`,
              }}
            />
          </div>
        </Card>
      ) : null}

      <Card className="pool-list overflow-hidden">
        <CardContent className="p-0">
          <div className="flex flex-col justify-between gap-4 p-5 xl:flex-row xl:items-center">
            <div className="flex items-center gap-2.5">
              <Database className="size-5 text-primary" />
              <h2 className="text-lg font-semibold">账户列表</h2>
              <Badge variant="secondary">{filtered.length}</Badge>
            </div>
            <div className="flex flex-wrap gap-2">
              <SearchInput
                value={query}
                onValueChange={(value) => {
                  setQuery(value);
                  setPage(1);
                }}
                placeholder="搜索邮箱 / TOKEN / 账号信息…"
                className="w-full sm:w-72"
              />
              <Select
                value={type}
                onValueChange={(value) => {
                  setType(value);
                  setPage(1);
                }}
              >
                <SelectTrigger className="w-32" aria-label="账号类型">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部类型</SelectItem>
                  {types.map((value) => (
                    <SelectItem value={value} key={value}>
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select
                value={status}
                onValueChange={(value) => {
                  setStatus(value as AccountStatus | "all");
                  setPage(1);
                }}
              >
                <SelectTrigger className="w-32" aria-label="账号状态">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部状态</SelectItem>
                  {statuses.map((value) => (
                    <SelectItem value={value} key={value}>
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="flex flex-wrap gap-2 px-5 pb-4">
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                void runAccountJob(accounts.map((item) => item.access_token))
              }
              disabled={working || !accounts.length}
            >
              <RefreshCw className="size-3.5" />
              刷新信息和额度
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-amber-600"
              onClick={() =>
                void runAccountJob(
                  accounts
                    .filter((item) => item.status === "异常")
                    .map((item) => item.access_token),
                  true,
                )
              }
              disabled={working || !summary.abnormal}
            >
              <LogIn className="size-3.5" />
              尝试恢复异常账号
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-destructive"
              onClick={() =>
                setDeleting(
                  accounts
                    .filter((item) => item.status === "异常")
                    .map((item) => item.access_token),
                )
              }
              disabled={working || !summary.abnormal}
            >
              <Trash2 className="size-3.5" />
              移除异常账号
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-destructive"
              onClick={() => setDeleting(selected)}
              disabled={working || !selected.length}
            >
              <Trash2 className="size-3.5" />
              删除所选
            </Button>
          </div>
          <div className="overflow-x-auto px-3">
            <table className="w-full min-w-[1260px] text-left text-xs">
              <thead className="bg-field/70 text-muted-foreground">
                <tr className="[&>th]:px-3 [&>th]:py-3 [&>th]:font-medium">
                  <th className="w-10">
                    <Checkbox
                      aria-label="选择当前页全部账号"
                      checked={allSelected}
                      onCheckedChange={(checked) =>
                        setSelected((current) =>
                          checked
                            ? Array.from(
                                new Set([
                                  ...current,
                                  ...currentRows.map(
                                    (item) => item.access_token,
                                  ),
                                ]),
                              )
                            : current.filter(
                                (token) =>
                                  !currentRows.some(
                                    (item) => item.access_token === token,
                                  ),
                              ),
                        )
                      }
                    />
                  </th>
                  <th>TOKEN</th>
                  <th>类型</th>
                  <th>来源</th>
                  <th>状态</th>
                  <th>账号信息</th>
                  <th>创建时间</th>
                  <th>额度</th>
                  <th>恢复时间</th>
                  <th>用途</th>
                  <th>成功</th>
                  <th>失败</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {currentRows.map((account) => {
                  const restore = restoreTime(account.restore_at);
                  return (
                    <tr
                      data-account-ref={account.account_ref}
                      data-selected={
                        selected.includes(account.access_token) || undefined
                      }
                      key={account.access_token}
                      className="border-b border-border/50 transition-colors hover:bg-accent/35 data-[selected]:bg-primary/5 [&>td]:px-3 [&>td]:py-3.5"
                    >
                      <td>
                        <Checkbox
                          aria-label={`选择 ${account.email || "账号"}`}
                          checked={selected.includes(account.access_token)}
                          onCheckedChange={(checked) =>
                            setSelected((current) =>
                              checked
                                ? [
                                    ...new Set([
                                      ...current,
                                      account.access_token,
                                    ]),
                                  ]
                                : current.filter(
                                    (value) => value !== account.access_token,
                                  ),
                            )
                          }
                        />
                      </td>
                      <td>
                        <div className="flex items-center gap-1.5">
                          <span className="whitespace-nowrap font-mono text-[11px]">
                            {maskToken(account.access_token)}
                          </span>
                          <button
                            type="button"
                            aria-label="复制 Token"
                            className="rounded-md p-1 text-muted-foreground hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring"
                            onClick={() => {
                              void navigator.clipboard
                                .writeText(account.access_token)
                                .then(() => toast.success("Token 已复制"))
                                .catch(() => toast.error("复制失败"));
                            }}
                          >
                            <Copy className="size-3.5" />
                          </button>
                        </div>
                      </td>
                      <td>
                        <Badge variant="secondary">
                          {account.type || "Free"}
                        </Badge>
                      </td>
                      <td>
                        <Badge variant="outline">
                          {account.source_type || "web"}
                        </Badge>
                      </td>
                      <td>
                        <span
                          className={cn(
                            "inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg px-2 py-1",
                            statusTone[account.status],
                          )}
                        >
                          <span className="size-1.5 rounded-full bg-current" />
                          {account.status}
                        </span>
                      </td>
                      <td data-account-info>
                        <span
                          className="block max-w-[235px] truncate"
                          title={account.email || account.name || undefined}
                        >
                          {account.email || account.name || "—"}
                        </span>
                      </td>
                      <td
                        className="whitespace-nowrap text-muted-foreground"
                        title="北京时间"
                      >
                        {beijingTime(account.created_at, true)}
                      </td>
                      <td>
                        <span className="rounded-md border border-blue-500/15 bg-blue-500/5 px-2 py-1 text-blue-600 dark:text-blue-400">
                          {Math.max(0, account.quota)}
                        </span>
                      </td>
                      <td>
                        <span className="block whitespace-nowrap">
                          {restore.relative}
                        </span>
                        <span
                          className="mt-1 block whitespace-nowrap text-[10px] text-muted-foreground"
                          title="北京时间"
                        >
                          {restore.absolute}
                        </span>
                      </td>
                      <td className="tabular-nums">
                        {account.image_inflight ?? 0}
                      </td>
                      <td className="tabular-nums">{account.success}</td>
                      <td className="tabular-nums">{account.fail}</td>
                      <td className="whitespace-nowrap">
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`编辑 ${account.email || "账号"}`}
                          onClick={() => {
                            setEditing(account);
                            setEditStatus(account.status);
                            setEditProxy(account.proxy || "");
                          }}
                          disabled={working}
                        >
                          <Pencil className="size-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`刷新 ${account.email || "账号"}`}
                          onClick={() =>
                            void runAccountJob([account.access_token])
                          }
                          disabled={working}
                        >
                          <RefreshCw className="size-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`删除 ${account.email || "账号"}`}
                          onClick={() => setDeleting([account.access_token])}
                          disabled={working}
                        >
                          <Trash2 className="size-3.5" />
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!currentRows.length ? (
              <div className="flex min-h-44 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
                {loading ? (
                  <>
                    <LoaderCircle className="size-6 animate-spin text-primary" />
                    正在加载账号…
                  </>
                ) : (
                  <>
                    <Users className="size-8 opacity-40" />
                    {accounts.length
                      ? "没有符合筛选条件的账号"
                      : "还没有账号，从右上角导入开始"}
                    {accounts.length ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setQuery("");
                          setType("all");
                          setStatus("all");
                        }}
                      >
                        清除筛选
                      </Button>
                    ) : null}
                  </>
                )}
              </div>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 p-5 text-xs text-muted-foreground">
            <span>
              已选择 {selected.length} 项 · 共 {filtered.length} 个账号
            </span>
            <div className="flex items-center gap-2">
              <Select
                value={pageSize}
                onValueChange={(value) => {
                  setPageSize(value);
                  setPage(1);
                }}
              >
                <SelectTrigger className="h-8 w-28" aria-label="每页账号数">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {["10", "20", "50", "100"].map((value) => (
                    <SelectItem key={value} value={value}>
                      {value} 条/页
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="icon-sm"
                aria-label="上一页"
                disabled={safePage <= 1}
                onClick={() => setPage(safePage - 1)}
              >
                <ChevronLeft className="size-4" />
              </Button>
              <span className="min-w-12 text-center tabular-nums">
                {safePage} / {pageCount}
              </span>
              <Button
                variant="outline"
                size="icon-sm"
                aria-label="下一页"
                disabled={safePage >= pageCount}
                onClick={() => setPage(safePage + 1)}
              >
                <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <AnimatePresence>
        {selected.length > 0 ? (
          <motion.div
            initial={reducedMotion ? false : { opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: reducedMotion ? 0 : 6 }}
            transition={{ duration: 0.16 }}
            className="sticky bottom-5 z-30 mx-auto flex w-fit max-w-full flex-wrap items-center justify-center gap-2 rounded-2xl border border-border bg-background/85 p-2 shadow-xl backdrop-blur-xl"
            role="toolbar"
            aria-label="选中账号操作"
          >
            <span className="px-2 text-xs text-muted-foreground">
              已选 {selected.length} 项
            </span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => void runAccountJob(selected)}
              disabled={working}
            >
              <RefreshCw className="size-4" />
              刷新额度
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => void runAccountJob(selected, true)}
              disabled={working}
            >
              <LogIn className="size-4" />
              尝试恢复
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-destructive"
              onClick={() => setDeleting(selected)}
              disabled={working}
            >
              <Trash2 className="size-4" />
              删除
            </Button>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label="取消选择"
              onClick={() => setSelected([])}
            >
              <X className="size-4" />
            </Button>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <Dialog
        open={Boolean(editing)}
        onOpenChange={(open) => {
          if (!open && !working) setEditing(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>编辑账号</DialogTitle>
            <DialogDescription>
              {editing?.email || "管理账号状态与专属代理"}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm">状态</label>
              <Select
                value={editStatus}
                onValueChange={(value) => setEditStatus(value as AccountStatus)}
              >
                <SelectTrigger aria-label="编辑账号状态">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {statuses.map((value) => (
                    <SelectItem value={value} key={value}>
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label htmlFor="account-proxy" className="text-sm">
                账号代理
              </label>
              <Input
                id="account-proxy"
                value={editProxy}
                onChange={(event) => setEditProxy(event.target.value)}
                maxLength={4096}
                placeholder="留空使用全局代理"
              />
              <div className="flex items-center justify-between gap-3">
                <p className="text-xs text-muted-foreground">
                  支持 HTTP、HTTPS 和 SOCKS 代理。
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void testAccountProxy()}
                  disabled={testingProxy || !editProxy.trim()}
                >
                  {testingProxy ? (
                    <LoaderCircle className="size-3.5 animate-spin" />
                  ) : null}
                  测试代理
                </Button>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={working}
              onClick={() => setEditing(null)}
            >
              取消
            </Button>
            <Button disabled={working} onClick={() => void save()}>
              {working ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : null}
              保存修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={deleting.length > 0}
        onOpenChange={(open) => {
          if (!open && !working) setDeleting([]);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除 {deleting.length} 个账号？</DialogTitle>
            <DialogDescription>
              账号将从号池中移除。请确认选择范围，删除后需要重新导入。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={working}
              onClick={() => setDeleting([])}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={working}
              onClick={() => void remove()}
            >
              {working ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Trash2 className="size-4" />
              )}
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageEnter>
  );
}

export default function AccountsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  return isCheckingAuth || !session || session.role !== "admin" ? (
    <PageLoader />
  ) : (
    <AccountsContent />
  );
}
