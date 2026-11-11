"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Eye,
  EyeOff,
  Import,
  Layers,
  Link2,
  LoaderCircle,
  Mail,
  Pencil,
  Plus,
  RefreshCcw,
  Save,
  Search,
  ServerCog,
  Trash2,
  Unplug,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  createSub2APIServer,
  completeSub2APILogin,
  deleteSub2APIServer,
  fetchSub2APIServerGroups,
  fetchSub2APIServers,
  startSub2APIImport,
  syncSub2APIServer,
  updateSub2APIServer,
  type Sub2APIRemoteAccount,
  type Sub2APIRemoteGroup,
  type Sub2APIServer,
} from "@/lib/api";

const PAGE_SIZE_OPTIONS = ["50", "100", "200"] as const;

type AuthMode = "password" | "api_key";
const syncErrorLabels: Record<string, string> = {
  LOGIN_FAILED: "源站登录失败，请检查管理员邮箱和密码",
  LOGIN_2FA_INVALID: "二次验证失败，请检查 2FA 密钥及服务器时间",
  STEP_UP_INVALID: "凭据导出验证失败，将稍后重试",
  SESSION_EXPIRED: "源站会话已过期，将重新登录",
  SYNC_FAILED: "同步失败，请检查连接配置和 2FA 密钥",
  UPSTREAM_ERROR: "源站暂时无法响应，将稍后重试",
};

function normalizeAccounts(items: Sub2APIRemoteAccount[]) {
  const seen = new Set<string>();
  const accounts: Sub2APIRemoteAccount[] = [];
  for (const item of items) {
    const id = String(item.id || "").trim();
    if (!id || seen.has(id)) {
      continue;
    }
    seen.add(id);
    accounts.push({
      id,
      name: String(item.name || "").trim(),
      email: String(item.email || "").trim(),
      plan_type: String(item.plan_type || "").trim(),
      status: String(item.status || "").trim(),
      expires_at: String(item.expires_at || "").trim(),
      has_refresh_token: Boolean(item.has_refresh_token),
    });
  }
  return accounts;
}

export function Sub2APIConnections() {
  const didLoadRef = useRef(false);
  const pollTimerRef = useRef<number | null>(null);

  const [servers, setServers] = useState<Sub2APIServer[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingServer, setEditingServer] = useState<Sub2APIServer | null>(null);
  const [formName, setFormName] = useState("");
  const [formBaseUrl, setFormBaseUrl] = useState("");
  const [formEmail, setFormEmail] = useState("");
  const [formPassword, setFormPassword] = useState("");
  const [formApiKey, setFormApiKey] = useState("");
  const [formGroupId, setFormGroupId] = useState("");
  const [formTotpSecret, setFormTotpSecret] = useState("");
  const [formClearTotp, setFormClearTotp] = useState(false);
  const [formAutoSync, setFormAutoSync] = useState(false);
  const [formSyncInterval, setFormSyncInterval] = useState(5);
  const [authMode, setAuthMode] = useState<AuthMode>("password");
  const [showSecret, setShowSecret] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const [remoteGroups, setRemoteGroups] = useState<Sub2APIRemoteGroup[] | null>(null);
  const [isLoadingGroups, setIsLoadingGroups] = useState(false);

  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [loadingAccountsId, setLoadingAccountsId] = useState<string | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginServer, setLoginServer] = useState<Sub2APIServer | null>(null);
  const [loginEmail, setLoginEmail] = useState("");
  const [loginCode, setLoginCode] = useState("");
  const [isVerifyingLogin, setIsVerifyingLogin] = useState(false);

  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserServer, setBrowserServer] = useState<Sub2APIServer | null>(null);
  const [remoteAccounts, setRemoteAccounts] = useState<Sub2APIRemoteAccount[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [accountQuery, setAccountQuery] = useState("");
  const [accountPage, setAccountPage] = useState(1);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZE_OPTIONS)[number]>("100");
  const [isStartingImport, setIsStartingImport] = useState(false);
  const [importStepUpCode, setImportStepUpCode] = useState("");

  const loadServers = async () => {
    setIsLoading(true);
    try {
      const data = await fetchSub2APIServers();
      setServers(data.servers);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载 Sub2API 连接失败");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void loadServers();
  }, []);

  useEffect(() => {
    const hasRunningJobs = servers.some(
      (server) => server.import_job?.status === "pending" || server.import_job?.status === "running",
    );
    const hasAutoSync = servers.some((server) => server.auto_sync_enabled);
    if (!hasRunningJobs && !hasAutoSync) {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }

    pollTimerRef.current = window.setInterval(() => {
      void fetchSub2APIServers()
        .then((data) => {
          setServers(data.servers);
        })
        .catch((error) => {
          if (pollTimerRef.current !== null) {
            window.clearInterval(pollTimerRef.current);
            pollTimerRef.current = null;
          }
          toast.error(error instanceof Error ? error.message : "查询导入进度失败");
        });
    }, hasRunningJobs ? 1500 : 10000);

    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [servers]);

  const openAddDialog = () => {
    setFormTotpSecret("");
    setFormClearTotp(false);
    setFormAutoSync(false);
    setFormSyncInterval(5);
    setEditingServer(null);
    setFormName("");
    setFormBaseUrl("");
    setFormEmail("");
    setFormPassword("");
    setFormApiKey("");
    setFormGroupId("");
    setAuthMode("password");
    setShowSecret(false);
    setRemoteGroups(null);
    setDialogOpen(true);
  };

  const openEditDialog = (server: Sub2APIServer) => {
    setFormTotpSecret("");
    setFormClearTotp(false);
    setFormAutoSync(server.auto_sync_enabled === true);
    setFormSyncInterval(server.sync_interval_minutes || 5);
    setEditingServer(server);
    setFormName(server.name);
    setFormBaseUrl(server.base_url);
    setFormEmail(server.email);
    setFormPassword("");
    setFormApiKey("");
    setFormGroupId(server.group_id || "");
    setAuthMode(server.has_api_key ? "api_key" : "password");
    setShowSecret(false);
    setRemoteGroups(null);
    setDialogOpen(true);
  };

  const handleFetchGroups = async () => {
    if (!editingServer) {
      toast.error("请先保存连接后再拉取分组");
      return;
    }
    setIsLoadingGroups(true);
    try {
      const data = await fetchSub2APIServerGroups(editingServer.id);
      setRemoteGroups(data.groups);
      if (data.groups.length === 0) {
        toast.message("远端没有配置分组");
      } else {
        toast.success(`读取到 ${data.groups.length} 个分组`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "拉取分组失败");
    } finally {
      setIsLoadingGroups(false);
    }
  };

  const handleSave = async () => {
    if (!Number.isInteger(formSyncInterval) || formSyncInterval < 1 || formSyncInterval > 1440) {
      toast.error("同步间隔需为 1 至 1440 分钟");
      return;
    }
    if (formAutoSync && (authMode !== "password" || (!formTotpSecret.trim() && (!editingServer?.has_totp_secret || formClearTotp)))) {
      toast.error("自动同步需要管理员邮箱、密码和 2FA 密钥");
      return;
    }
    if (!formBaseUrl.trim()) {
      toast.error("请输入 Sub2API 地址");
      return;
    }
    if (authMode === "password") {
      if (!formEmail.trim()) {
        toast.error("请输入管理员邮箱");
        return;
      }
      if (!editingServer && !formPassword.trim()) {
        toast.error("请输入管理员密码");
        return;
      }
    } else if (!editingServer && !formApiKey.trim()) {
      toast.error("请输入 Admin API Key");
      return;
    }

    setIsSaving(true);
    try {
      if (editingServer) {
        const updates: Parameters<typeof updateSub2APIServer>[1] = {
          name: formName.trim(),
          base_url: formBaseUrl.trim(),
          group_id: formGroupId.trim(),
          auto_sync_enabled: formAutoSync,
          sync_interval_minutes: formSyncInterval,
          clear_totp_secret: formClearTotp,
        };
        if (formTotpSecret.trim()) updates.totp_secret = formTotpSecret.trim();
        if (authMode === "password") {
          updates.email = formEmail.trim();
          if (formPassword.trim()) {
            updates.password = formPassword.trim();
          }
          updates.api_key = "";
        } else {
          if (formApiKey.trim()) {
            updates.api_key = formApiKey.trim();
          }
          updates.email = "";
          updates.password = "";
        }
        const data = await updateSub2APIServer(editingServer.id, updates);
        setServers(data.servers);
        toast.success("连接已更新");
      } else {
        const data = await createSub2APIServer({
          name: formName.trim(),
          base_url: formBaseUrl.trim(),
          email: authMode === "password" ? formEmail.trim() : "",
          password: authMode === "password" ? formPassword.trim() : "",
          api_key: authMode === "api_key" ? formApiKey.trim() : "",
          group_id: formGroupId.trim(),
          totp_secret: formTotpSecret.trim(),
          auto_sync_enabled: formAutoSync,
          sync_interval_minutes: formSyncInterval,
        });
        setServers(data.servers);
        toast.success("连接已添加");
      }
      setDialogOpen(false);
      setFormTotpSecret("");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (server: Sub2APIServer) => {
    setDeletingId(server.id);
    try {
      const data = await deleteSub2APIServer(server.id);
      setServers(data.servers);
      toast.success("连接已删除");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setDeletingId(null);
    }
  };

  const openAccountBrowser = (server: Sub2APIServer, items: Sub2APIRemoteAccount[]) => {
    const accounts = normalizeAccounts(items);
    setBrowserServer(server);
    setRemoteAccounts(accounts);
    setSelectedIds([]);
    setAccountQuery("");
    setAccountPage(1);
    setImportStepUpCode("");
    setBrowserOpen(true);
    toast.success(`读取成功，共 ${accounts.length} 个 OpenAI 账号`);
  };

  const handleBrowseAccounts = async (server: Sub2APIServer) => {
    setLoadingAccountsId(server.id);
    try {
      // 远端会话仅保存在服务端内存中；同步必须先登录，不能直接请求账号列表。
      const data = await syncSub2APIServer(server.id);
      if (data.status === "login_2fa_required") {
        setLoginServer(server);
        setLoginEmail(data.user_email_masked || server.email);
        setLoginCode("");
        setLoginOpen(true);
        return;
      }
      openAccountBrowser(server, data.accounts);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取 Sub2API 账号失败");
    } finally {
      setLoadingAccountsId(null);
    }
  };

  const handleCompleteLogin = async () => {
    if (!loginServer || !/^\d{6}$/.test(loginCode)) {
      toast.error("请输入六位数字验证码");
      return;
    }
    setIsVerifyingLogin(true);
    try {
      const data = await completeSub2APILogin(loginServer.id, loginCode);
      setLoginCode("");
      setLoginOpen(false);
      openAccountBrowser(loginServer, data.accounts);
    } catch (error) {
      setLoginCode("");
      toast.error(error instanceof Error ? error.message : "二次验证失败，请重试");
    } finally {
      setIsVerifyingLogin(false);
    }
  };

  const filteredAccounts = useMemo(() => {
    const query = accountQuery.trim().toLowerCase();
    if (!query) {
      return remoteAccounts;
    }
    return remoteAccounts.filter((item) => {
      return (
        item.email.toLowerCase().includes(query) ||
        item.name.toLowerCase().includes(query) ||
        item.plan_type.toLowerCase().includes(query) ||
        item.id.toLowerCase().includes(query)
      );
    });
  }, [accountQuery, remoteAccounts]);

  const currentPageSize = Number(pageSize);
  const accountPageCount = Math.max(1, Math.ceil(filteredAccounts.length / currentPageSize));
  const safeAccountPage = Math.min(accountPage, accountPageCount);
  const pagedAccounts = filteredAccounts.slice(
    (safeAccountPage - 1) * currentPageSize,
    safeAccountPage * currentPageSize,
  );
  // 用 Set 判断是否已选：全选后 selectedIds 与 filteredAccounts 都是上千条，includes 会是 O(n²)，每次输入搜索词都要算一遍
  const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const allFilteredSelected =
    filteredAccounts.length > 0 && filteredAccounts.every((item) => selectedIdSet.has(item.id));

  const toggleAccount = (id: string, checked: boolean) => {
    setSelectedIds((prev) => {
      if (checked) {
        return Array.from(new Set([...prev, id]));
      }
      return prev.filter((item) => item !== id);
    });
  };

  const handleToggleSelectAllFiltered = (checked: boolean) => {
    if (checked) {
      setSelectedIds(Array.from(new Set([...selectedIds, ...filteredAccounts.map((item) => item.id)])));
      return;
    }
    const filteredSet = new Set(filteredAccounts.map((item) => item.id));
    setSelectedIds((prev) => prev.filter((id) => !filteredSet.has(id)));
  };

  const handleStartImport = async () => {
    if (!browserServer) {
      return;
    }
    if (selectedIds.length === 0) {
      toast.error("请先选择要导入的账号");
      return;
    }
    // 登录验证只允许浏览；导出完整凭据还需独立验证，由用户再次提供当前验证码。
    if (!browserServer.has_api_key && !browserServer.has_totp_secret && !/^\d{6}$/.test(importStepUpCode)) {
      toast.error("请输入六位数字的导入二次验证码");
      return;
    }

    setIsStartingImport(true);
    try {
      const result = await startSub2APIImport(browserServer.id, selectedIds, importStepUpCode);
      setServers((prev) =>
        prev.map((server) =>
          server.id === browserServer.id ? { ...server, import_job: result.import_job } : server,
        ),
      );
      setBrowserOpen(false);
      toast.success("导入任务已启动");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动导入失败");
    } finally {
      setImportStepUpCode("");
      setIsStartingImport(false);
    }
  };

  return (
    <>
      <Card>
        <CardHeader className="flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary/15 text-primary">
              <ServerCog className="size-4" />
            </div>
            <div className="min-w-0 space-y-1">
              <CardTitle>Sub2API 连接管理</CardTitle>
              <CardDescription>
                配置 Sub2API 服务器后，可查询其中的 OpenAI OAuth 账号并批量导入本地号池。
              </CardDescription>
            </div>
          </div>
          <div className="flex w-fit shrink-0 items-center gap-2">
            {servers.length > 0 ? (
              <Badge variant="secondary">
                {servers.length} 个连接
              </Badge>
            ) : null}
            <Button onClick={openAddDialog}>
              <Plus className="size-4" />
              添加连接
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-5 p-5">
          {isLoading ? (
            <div className="flex items-center justify-center py-10">
              <LoaderCircle className="size-5 animate-spin text-muted-foreground" />
            </div>
          ) : servers.length === 0 ? (
            <EmptyState
              icon={<ServerCog />}
              title="暂无 Sub2API 连接"
              description="点击「添加连接」保存你的 Sub2API 信息。"
            />
          ) : (
            <div className="space-y-3">
              {servers.map((server) => {
                const isBusy = deletingId === server.id || loadingAccountsId === server.id;
                const importJob = server.import_job ?? null;
                return (
                  <div
                    key={server.id}
                    className="flex flex-col gap-3 rounded-2xl border border-border bg-field/50 p-4"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-foreground">{server.name || server.base_url}</div>
                        <div className="truncate text-xs text-muted-foreground">
                          {server.base_url}
                          {server.email ? ` · ${server.email}` : server.has_api_key ? " · API Key" : ""}
                          {server.group_id ? ` · 分组 ${server.group_id}` : " · 全部分组"}
                        </div>
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          className="text-muted-foreground"
                          onClick={() => openEditDialog(server)}
                          disabled={isBusy}
                          title="编辑"
                          aria-label="编辑"
                        >
                          <Pencil />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          className="text-destructive hover:text-destructive"
                          onClick={() => void handleDelete(server)}
                          disabled={isBusy}
                          title="删除"
                          aria-label="删除"
                        >
                          {deletingId === server.id ? (
                            <LoaderCircle className="animate-spin" />
                          ) : (
                            <Trash2 />
                          )}
                        </Button>
                      </div>
                    </div>

                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        onClick={() => void handleBrowseAccounts(server)}
                        disabled={isBusy}
                      >
                        {loadingAccountsId === server.id ? (
                          <LoaderCircle className="size-4 animate-spin" />
                        ) : (
                          <Import className="size-4" />
                        )}
                        同步
                      </Button>
                    </div>

                    <div className="text-xs leading-6 text-muted-foreground">
                      {server.auto_sync_enabled ? `自动同步已开启 · 每 ${server.sync_interval_minutes || 5} 分钟 · 分组 ${server.group_id || "全部"}` : "自动同步未开启"}
                      {server.last_sync_at ? ` · 上次 ${new Date(server.last_sync_at).toLocaleString("zh-CN")} · ${server.sync_count || 0} 个账号` : ""}
                      {server.last_sync_error ? <p className="text-destructive">同步未完成：{syncErrorLabels[server.last_sync_error] || server.last_sync_error}</p> : null}
                    </div>

                    {importJob ? (
                      <div className="space-y-3 rounded-xl border border-border bg-field px-4 py-3">
                        {(() => {
                          const progress =
                            importJob.total > 0
                              ? Math.round((importJob.completed / importJob.total) * 100)
                              : 0;
                          return (
                            <>
                              <div className="flex items-center justify-between gap-3">
                                <div className="min-w-0">
                                  <div className="text-sm font-medium text-foreground/80">
                                    状态 {importJob.status}，已处理 {importJob.completed}/{importJob.total}
                                  </div>
                                  <div className="truncate text-xs text-muted-foreground">
                                    任务 {importJob.job_id.slice(0, 8)} · {importJob.created_at}
                                  </div>
                                </div>
                                <Badge
                                  variant={
                                    importJob.status === "completed"
                                      ? "success"
                                      : importJob.status === "failed"
                                        ? "danger"
                                        : "info"
                                  }
                                  className="shrink-0"
                                >
                                  {progress}%
                                </Badge>
                              </div>
                              <div className="h-1.5 rounded-full bg-secondary">
                                <div
                                  className="h-full rounded-full bg-primary transition-[width]"
                                  style={{ width: `${progress}%` }}
                                />
                              </div>
                              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                                <span>新增 {importJob.added}</span>
                                <span>跳过 {importJob.skipped}</span>
                                <span>刷新 {importJob.refreshed}</span>
                                <span>失败 {importJob.failed}</span>
                              </div>
                            </>
                          );
                        })()}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}

          <div className="rounded-xl border border-border bg-field/50 px-4 py-3 text-sm leading-6 text-foreground/80">
            <p className="font-medium">使用说明</p>
            <ul className="mt-1 list-inside list-disc space-y-0.5">
              <li>输入 Sub2API 地址和管理员账户（或 Admin API Key），保存为一个连接。</li>
              <li>点击某个连接的「同步」会拉取其中 platform=openai 且 type=oauth 的账号列表。</li>
              <li>配置管理员 2FA 密钥后，服务器会自动完成登录和导出验证；也可临时输入六位验证码。</li>
              <li>本地只保存账号 access_token，由 Sub2API 统一续期；自动同步会更新当前分组已有账号并导入新增账号。</li>
            </ul>
          </div>
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent showCloseButton={false} className="max-h-[90vh] overflow-y-auto">
          <DialogHeader className="gap-2">
            <DialogTitle>{editingServer ? "编辑连接" : "添加连接"}</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              {editingServer ? "修改 Sub2API 连接信息" : "添加一个新的 Sub2API 连接"}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-[13px] font-medium text-foreground/80">名称（可选）</label>
              <Input
                value={formName}
                onChange={(event) => setFormName(event.target.value)}
                placeholder="例如：自建 sub2api"
              />
            </div>
            <div className="space-y-2">
              <label className="flex items-center gap-1.5 text-[13px] font-medium text-foreground/80">
                <Link2 className="size-3.5" />
                Sub2API 地址
              </label>
              <Input
                value={formBaseUrl}
                onChange={(event) => setFormBaseUrl(event.target.value)}
                placeholder="http://your-sub2api-host:8080"
              />
            </div>
            <div className="space-y-2">
              <label className="text-[13px] font-medium text-foreground/80">认证方式</label>
              <Select value={authMode} onValueChange={(value) => {
                setAuthMode(value as AuthMode);
                if (value === "api_key") {setFormAutoSync(false); setFormTotpSecret(""); setFormClearTotp(true);}
              }}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="password">管理员邮箱 + 密码</SelectItem>
                  <SelectItem value="api_key">Admin API Key</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {authMode === "password" ? (
              <>
                <div className="space-y-2">
                  <label htmlFor="sub2api-totp-secret" className="text-[13px] font-medium text-foreground/80">管理员 2FA 密钥</label>
                  <Input id="sub2api-totp-secret" type="password" autoComplete="off" maxLength={2048}
                    value={formTotpSecret} onChange={(event) => {setFormTotpSecret(event.target.value); setFormClearTotp(false);}}
                    placeholder={editingServer?.has_totp_secret && !formClearTotp ? "已配置，留空保留" : "填写 TOTP 密钥或 otpauth 链接"} />
                  <p className="text-xs text-muted-foreground">填写验证器的长期密钥，不是六位验证码。服务器加密保存，页面不回显。</p>
                  {editingServer?.has_totp_secret ? <Button type="button" variant="outline" size="sm"
                    onClick={() => {setFormClearTotp(true); setFormTotpSecret(""); setFormAutoSync(false);}}>
                    {formClearTotp ? "保存后清除密钥" : "清除已保存密钥"}
                  </Button> : null}
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox checked={formAutoSync} onCheckedChange={(value) => setFormAutoSync(Boolean(value))} />
                    定时同步当前分组的账号和最新 AT
                  </label>
                  <label htmlFor="sub2api-sync-interval" className="text-xs text-muted-foreground">同步间隔（分钟）</label>
                  <Input id="sub2api-sync-interval" type="number" min={1} max={1440} value={formSyncInterval}
                    onChange={(event) => setFormSyncInterval(Number(event.target.value))} />
                </div>
                <div className="space-y-2">
                  <label className="flex items-center gap-1.5 text-[13px] font-medium text-foreground/80">
                    <Mail className="size-3.5" />
                    管理员邮箱
                  </label>
                  <Input
                    value={formEmail}
                    onChange={(event) => setFormEmail(event.target.value)}
                    placeholder="admin@example.com"
                  />
                </div>
                <div className="space-y-2">
                  <label className="flex items-center gap-1.5 text-[13px] font-medium text-foreground/80">
                    <Unplug className="size-3.5" />
                    管理员密码
                  </label>
                  <div className="relative">
                    <Input
                      type={showSecret ? "text" : "password"}
                      value={formPassword}
                      onChange={(event) => setFormPassword(event.target.value)}
                      placeholder={editingServer ? "留空则不修改密码" : "管理员密码"}
                      className="pr-10"
                    />
                    <button
                      type="button"
                      className="absolute top-1/2 right-3 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground/80"
                      onClick={() => setShowSecret((prev) => !prev)}
                    >
                      {showSecret ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <div className="space-y-2">
                <label className="flex items-center gap-1.5 text-[13px] font-medium text-foreground/80">
                  <Unplug className="size-3.5" />
                  Admin API Key
                </label>
                <div className="relative">
                  <Input
                    type={showSecret ? "text" : "password"}
                    value={formApiKey}
                    onChange={(event) => setFormApiKey(event.target.value)}
                    placeholder={editingServer ? "留空则不修改密钥" : "Sub2API Admin API Key"}
                    className="pr-10"
                  />
                  <button
                    type="button"
                    className="absolute top-1/2 right-3 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground/80"
                    onClick={() => setShowSecret((prev) => !prev)}
                  >
                    {showSecret ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
              </div>
            )}
            <div className="space-y-2">
              <label className="flex items-center gap-1.5 text-[13px] font-medium text-foreground/80">
                <Layers className="size-3.5" />
                分组（可选）
              </label>
              {remoteGroups && remoteGroups.length > 0 ? (
                <Select value={formGroupId || "__all__"} onValueChange={(value) => setFormGroupId(value === "__all__" ? "" : value)}>
                  <SelectTrigger>
                    <SelectValue placeholder="选择分组" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">全部分组（不限制）</SelectItem>
                    <SelectItem value="ungrouped">未分组</SelectItem>
                    {remoteGroups.map((group) => (
                      <SelectItem key={group.id} value={group.id}>
                        {group.name || `Group ${group.id}`}
                        {group.platform ? `（${group.platform}）` : ""}
                        {group.account_count
                          ? ` · ${group.active_account_count}/${group.account_count}`
                          : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  value={formGroupId}
                  onChange={(event) => setFormGroupId(event.target.value)}
                  placeholder="留空则同步所有分组；或填写分组 ID / ungrouped"
                />
              )}
              {editingServer ? (
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>同步时会用分组 ID 过滤，留空 = 同步所有 OpenAI OAuth 账号。</span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void handleFetchGroups()}
                    disabled={isLoadingGroups}
                  >
                    {isLoadingGroups ? (
                      <LoaderCircle className="animate-spin" />
                    ) : (
                      <RefreshCcw />
                    )}
                    {remoteGroups ? "重新拉取" : "拉取分组"}
                  </Button>
                </div>
              ) : (
                <div className="text-xs text-muted-foreground">
                  添加完连接后可在编辑对话框里点「拉取分组」选择具体分组。
                </div>
              )}
            </div>
          </div>
          <DialogFooter className="pt-2">
            <Button
              variant="secondary"
              onClick={() => setDialogOpen(false)}
              disabled={isSaving}
            >
              取消
            </Button>
            <Button
              onClick={() => void handleSave()}
              disabled={isSaving}
            >
              {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
              {editingServer ? "保存修改" : "添加"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={loginOpen} onOpenChange={(open) => {
        if (isVerifyingLogin) return;
        setLoginOpen(open);
        if (!open) {
          setLoginCode("");
          setLoginServer(null);
        }
      }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Sub2API 二次验证</DialogTitle>
            <DialogDescription>
              {loginEmail} 已通过密码验证，请输入验证器中的六位验证码以继续同步。
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={(event) => {
            event.preventDefault();
            if (!isVerifyingLogin) void handleCompleteLogin();
          }}>
            <Input
              aria-label="二次验证码"
              autoFocus
              autoComplete="one-time-code"
              inputMode="numeric"
              pattern="[0-9]{6}"
              maxLength={6}
              placeholder="六位验证码"
              value={loginCode}
              disabled={isVerifyingLogin}
              onChange={(event) => setLoginCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
            />
            <DialogFooter className="pt-4">
              <Button type="submit" disabled={isVerifyingLogin || !/^\d{6}$/.test(loginCode)}>
                {isVerifyingLogin ? <LoaderCircle className="size-4 animate-spin" /> : null}
                验证并同步
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={browserOpen} onOpenChange={(open) => {
        if (isStartingImport) return;
        setBrowserOpen(open);
        if (!open) setImportStepUpCode("");
      }}>
        <DialogContent showCloseButton={false} className="flex max-h-[85vh] max-w-5xl flex-col overflow-hidden">
          <DialogHeader className="shrink-0 gap-2">
            <DialogTitle>选择要导入的账号</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              {browserServer ? `来自 ${browserServer.name || browserServer.base_url}` : "Sub2API 上的 OpenAI OAuth 账号"}
            </DialogDescription>
          </DialogHeader>

          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto pr-1">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="relative min-w-[260px]">
                <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={accountQuery}
                  onChange={(event) => {
                    setAccountQuery(event.target.value);
                    setAccountPage(1);
                  }}
                  placeholder="搜索邮箱、套餐或名称"
                  className="pl-10"
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  value={pageSize}
                  onValueChange={(value) => {
                    setPageSize(value as (typeof PAGE_SIZE_OPTIONS)[number]);
                    setAccountPage(1);
                  }}
                >
                  <SelectTrigger className="w-[120px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PAGE_SIZE_OPTIONS.map((item) => (
                      <SelectItem key={item} value={item}>
                        {item} / 页
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  variant="outline"
                  onClick={() => handleToggleSelectAllFiltered(!allFilteredSelected)}
                >
                  {allFilteredSelected ? "取消全选" : "全选筛选结果"}
                </Button>
              </div>
            </div>

            <div className="rounded-2xl border border-border">
              <div className="flex items-center justify-between border-b border-border px-4 py-3 text-sm text-muted-foreground">
                <div className="flex items-center gap-3">
                  <Checkbox
                    checked={allFilteredSelected}
                    onCheckedChange={(checked) => handleToggleSelectAllFiltered(Boolean(checked))}
                  />
                  <span>筛选结果 {filteredAccounts.length} 个</span>
                </div>
                <span>已选 {selectedIds.length} 个</span>
              </div>
              <div className="max-h-[420px] overflow-auto">
                {pagedAccounts.length === 0 ? (
                  <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">没有匹配的账号</div>
                ) : (
                  <div className="divide-y divide-border">
                    {pagedAccounts.map((item) => (
                      <label
                        key={item.id}
                        className="flex cursor-pointer items-center gap-3 px-4 py-3 transition-colors hover:bg-accent/60"
                      >
                        <Checkbox
                          checked={selectedIdSet.has(item.id)}
                          onCheckedChange={(checked) => toggleAccount(item.id, Boolean(checked))}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="truncate text-sm font-medium text-foreground/80">
                              {item.name || item.email || item.id}
                            </span>
                            {item.plan_type ? (
                              <Badge variant="secondary">{item.plan_type}</Badge>
                            ) : null}
                            {item.status ? (
                              <Badge
                                variant={item.status === "active" ? "success" : "info"}
                              >
                                {item.status}
                              </Badge>
                            ) : null}
                          </div>
                          <div className="truncate text-xs text-muted-foreground">
                            id {item.id}
                            {item.expires_at ? ` · 过期 ${item.expires_at}` : ""}
                          </div>
                        </div>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="flex flex-col gap-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
              <span>
                第 {filteredAccounts.length === 0 ? 0 : (safeAccountPage - 1) * currentPageSize + 1} -{" "}
                {Math.min(safeAccountPage * currentPageSize, filteredAccounts.length)} 条，共 {filteredAccounts.length} 条
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  onClick={() => setAccountPage((prev) => Math.max(1, prev - 1))}
                  disabled={safeAccountPage <= 1}
                >
                  上一页
                </Button>
                <span>
                  {safeAccountPage}/{accountPageCount}
                </span>
                <Button
                  variant="outline"
                  onClick={() => setAccountPage((prev) => Math.min(accountPageCount, prev + 1))}
                  disabled={safeAccountPage >= accountPageCount}
                >
                  下一页
                </Button>
              </div>
            </div>
          </div>

          {browserServer?.has_totp_secret ? <p className="text-xs text-muted-foreground">已配置自动二次验证，服务器会完成凭据导出验证，本地仅保存 AT。</p> : null}
          {browserServer && !browserServer.has_api_key && !browserServer.has_totp_secret ? (
            <div className="shrink-0 space-y-2 rounded-xl border border-border bg-field p-3">
              <label htmlFor="sub2api-import-step-up" className="text-sm font-medium">
                导入二次验证码
              </label>
              <Input
                id="sub2api-import-step-up"
                className="max-w-xs"
                type="password"
                autoComplete="one-time-code"
                inputMode="numeric"
                pattern="[0-9]{6}"
                maxLength={6}
                placeholder="验证器中的六位验证码"
                value={importStepUpCode}
                disabled={isStartingImport}
                onChange={(event) => setImportStepUpCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                aria-describedby="sub2api-import-step-up-help"
              />
              <p id="sub2api-import-step-up-help" className="text-xs text-muted-foreground">
                导出账号凭据需要再次验证。若刚使用过当前验证码，请等待下一组再导入。
              </p>
            </div>
          ) : null}

          <DialogFooter className="shrink-0 pt-2">
            <Button
              variant="secondary"
              onClick={() => {
                setBrowserOpen(false);
                setImportStepUpCode("");
              }}
              disabled={isStartingImport}
            >
              取消
            </Button>
            <Button
              onClick={() => void handleStartImport()}
              disabled={isStartingImport || selectedIds.length === 0 || (!browserServer?.has_api_key && !browserServer?.has_totp_secret && !/^\d{6}$/.test(importStepUpCode))}
            >
              {isStartingImport ? <LoaderCircle className="size-4 animate-spin" /> : <Import className="size-4" />}
              导入选中账号
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
