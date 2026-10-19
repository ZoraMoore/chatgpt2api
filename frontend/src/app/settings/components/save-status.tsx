"use client";
import { useUiReducedMotion as useReducedMotion } from "@/lib/hooks/use-ui-reduced-motion";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "motion/react";
import {
  Check,
  Circle,
  LoaderCircle,
  RotateCcw,
  Save,
  TriangleAlert,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useSettingsStore } from "../store";

/** 草稿仅保存在内存中，避免把含密钥的配置额外写入 localStorage。 */
export function SettingsSaveStatus() {
  const router = useRouter();
  const reducedMotion = useReducedMotion();
  const bypassNavigation = useRef(false);
  const pendingCommit = useRef<(() => void) | null>(null);
  const [pendingHref, setPendingHref] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const config = useSettingsStore((state) => state.config);
  const baseline = useSettingsStore((state) => state.savedConfig);
  const saving = useSettingsStore((state) => state.isSavingConfig);
  const lastSaved = useSettingsStore((state) => state.lastSavedAt);
  const error = useSettingsStore((state) => state.saveError);
  const save = useSettingsStore((state) => state.saveConfig);
  const discard = useSettingsStore((state) => state.discardConfigChanges);
  const dirty =
    config !== null &&
    baseline !== null &&
    JSON.stringify(config) !== JSON.stringify(baseline);

  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const interceptLink = (event: MouseEvent) => {
      if (
        bypassNavigation.current ||
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const anchor =
        event.target instanceof Element
          ? event.target.closest<HTMLAnchorElement>("a[href]")
          : null;
      if (
        !anchor ||
        anchor.target === "_blank" ||
        anchor.hasAttribute("download")
      )
        return;
      const target = new URL(anchor.href, location.href);
      if (
        target.origin !== location.origin ||
        target.pathname.replace(/\/$/, "") ===
          location.pathname.replace(/\/$/, "") ||
        target.pathname.startsWith("/api/") ||
        target.pathname.startsWith("/images/")
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      pendingCommit.current = null;
      setPendingHref(target.pathname + target.search + target.hash);
    };
    const beforeNavigate = (event: Event) => {
      if (bypassNavigation.current) return;
      const detail = (
        event as CustomEvent<{ href?: unknown; commit?: unknown }>
      ).detail;
      const href = detail?.href;
      if (typeof href !== "string" || href.length > 4096) return;
      try {
        const target = new URL(href, location.href);
        if (
          target.origin !== location.origin ||
          target.pathname.replace(/\/$/, "") ===
            location.pathname.replace(/\/$/, "")
        )
          return;
        event.preventDefault();
        pendingCommit.current =
          typeof detail?.commit === "function"
            ? (detail.commit as () => void)
            : null;
        setPendingHref(target.pathname + target.search + target.hash);
      } catch {
        /* 无效的程序导航不作为页面离开处理。 */
      }
    };
    window.addEventListener("chatgpt2api:before-navigate", beforeNavigate);
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", interceptLink, true);
    return () => {
      window.removeEventListener("chatgpt2api:before-navigate", beforeNavigate);
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", interceptLink, true);
    };
  }, [dirty]);

  const leave = () => {
    if (!pendingHref || saving) return;
    bypassNavigation.current = true;
    const commit = pendingCommit.current;
    pendingCommit.current = null;
    if (commit) commit();
    else router.push(pendingHref);
    setPendingHref(null);
  };
  const saveAndLeave = async () => {
    if (!(await save())) return;
    const state = useSettingsStore.getState();
    if (JSON.stringify(state.config) !== JSON.stringify(state.savedConfig))
      return;
    leave();
  };
  if (!config) return null;

  return (
    <>
      <div
        className="sticky top-0 z-20 -mx-1 rounded-2xl border border-border/70 bg-background/80 p-3 shadow-sm backdrop-blur-xl sm:p-3.5"
        role="region"
        aria-label="配置保存状态"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0 text-sm" role="status" aria-live="polite">
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={
                  saving
                    ? "saving"
                    : error
                      ? "error"
                      : dirty
                        ? "dirty"
                        : "saved"
                }
                initial={reducedMotion ? false : { opacity: 0, y: 3 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.13 }}
                className="flex items-center gap-2"
              >
                {saving ? (
                  <LoaderCircle className="size-4 animate-spin text-primary" />
                ) : error ? (
                  <TriangleAlert className="size-4 text-destructive" />
                ) : dirty ? (
                  <Circle className="size-2.5 fill-amber-500 text-amber-500" />
                ) : (
                  <Check className="size-4 text-emerald-500" />
                )}
                <span>
                  {saving
                    ? "正在保存系统配置…"
                    : error
                      ? "保存失败，草稿已保留"
                      : dirty
                        ? "有尚未保存的更改"
                        : lastSaved
                          ? "配置已保存"
                          : "已与服务器同步"}
                </span>
              </motion.div>
            </AnimatePresence>
            <p className="mt-1 pl-6 text-[11px] text-muted-foreground">
              {dirty
                ? "草稿保留在当前页面会话中，刷新页面前请保存。"
                : "修改配置后保存，新的请求将使用更新后的设置。"}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              size="sm"
              variant="ghost"
              disabled={!dirty || saving}
              onClick={() => setConfirmReset(true)}
            >
              <RotateCcw className="size-3.5" />
              撤销更改
            </Button>
            <Button
              size="sm"
              disabled={!dirty || saving}
              onClick={() => void save()}
            >
              {saving ? (
                <LoaderCircle className="size-3.5 animate-spin" />
              ) : (
                <Save className="size-3.5" />
              )}
              {error ? "重试保存" : "保存更改"}
            </Button>
          </div>
        </div>
      </div>
      <Dialog
        open={pendingHref !== null}
        onOpenChange={(open) => {
          if (!open && !saving) setPendingHref(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>还有尚未保存的更改</DialogTitle>
            <DialogDescription>
              保存后再离开，或将草稿留在当前会话中。刷新或关闭页面会丢失尚未保存的草稿。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              disabled={saving}
              onClick={() => setPendingHref(null)}
            >
              继续编辑
            </Button>
            <Button variant="outline" disabled={saving} onClick={leave}>
              保留草稿并离开
            </Button>
            <Button disabled={saving} onClick={() => void saveAndLeave()}>
              {saving ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : (
                <Save className="size-4" />
              )}
              保存并离开
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={confirmReset} onOpenChange={setConfirmReset}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>撤销未保存的更改？</DialogTitle>
            <DialogDescription>
              将恢复为最近一次从服务器读取或成功保存的配置。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmReset(false)}>
              继续编辑
            </Button>
            <Button
              onClick={() => {
                discard();
                setConfirmReset(false);
              }}
            >
              撤销更改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
