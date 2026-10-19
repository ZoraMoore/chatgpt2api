"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { usePathname, useRouter } from "next/navigation";
import { LogOut, Moon, PanelLeft, Frame } from "lucide-react";
import {
  CommandPalette,
  type CommandItem,
} from "@/components/motion/command-palette";
import { AppSidebar } from "@/components/shell/app-sidebar";
import { TopBar } from "@/components/shell/top-bar";
import {
  AppearanceProvider,
  useAppearance,
} from "@/components/shell/appearance";
import { flattenNav, getNav } from "@/components/shell/nav-config";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { setSidebarCollapsed, useSidebarCollapsed } from "@/hooks/use-sidebar";
import {
  getValidatedAuthSession,
  invalidateValidatedAuthSession,
} from "@/lib/auth-session";
import { requestNavigation } from "@/lib/navigation";
import { fetchThirdPartyApps } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  clearStoredAuthSession,
  AUTH_EPOCH_STORAGE_KEY,
  type StoredAuthSession,
} from "@/store/auth";

function Shell({ children }: { children: ReactNode }) {
  const { reduced } = useAppearance();
  const animateNavigation = useRef(false),
    pageAnimation = useRef<Animation | null>(null);
  const pathname = usePathname(),
    router = useRouter(),
    collapsed = useSidebarCollapsed();
  const [session, setSession] = useState<StoredAuthSession | null>(null);
  const [canvasUrl, setCanvasUrl] = useState("");
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false),
    [mobileOpen, setMobileOpen] = useState(false),
    [paletteOpen, setPaletteOpen] = useState(false);
  const focusOnRoute = useRef(false),
    menuTrigger = useRef<HTMLButtonElement | null>(null);
  const contentRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    let current = true;
    if (pathname.replace(/\/$/, "") === "/login") {
      setSession(null);
      return;
    }
    const load = () =>
      void getValidatedAuthSession().then((value) => {
        if (current) setSession(value);
      });
    load();
    const storage = (event: StorageEvent) => {
      if (event.key === AUTH_EPOCH_STORAGE_KEY) {
        setSession(null);
        load();
      }
    };
    window.addEventListener("storage", storage);
    return () => {
      current = false;
      window.removeEventListener("storage", storage);
    };
  }, [pathname]);
  useEffect(() => {
    let current = true;
    const load = () => {
      if (!session) {
        setCanvasUrl("");
        return;
      }
      void fetchThirdPartyApps()
        .then((data) => {
          if (!current) return;
          const app = data.third_party_apps?.infinite_canvas;
          try {
            const target = new URL(app?.url || "");
            setCanvasUrl(
              app?.enabled && ["https:", "http:"].includes(target.protocol)
                ? target.toString()
                : "",
            );
          } catch {
            setCanvasUrl("");
          }
        })
        .catch(() => {
          if (current) setCanvasUrl("");
        });
    };
    load();
    window.addEventListener("third-party-apps-updated", load);
    return () => {
      current = false;
      window.removeEventListener("third-party-apps-updated", load);
    };
  }, [session?.key]);
  useEffect(() => {
    const query = matchMedia("(max-width: 767px)");
    const update = () => {
      setIsMobile(query.matches);
      if (!query.matches) setMobileOpen(false);
    };
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!focusOnRoute.current) return;
    focusOnRoute.current = false;
    requestAnimationFrame(() => {
      const heading = contentRef.current?.querySelector<HTMLElement>("h1");
      if (heading) {
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
      }
    });
  }, [pathname]);
  const nav = useMemo(
    () => (session ? getNav(session.role) : []),
    [session?.role],
  );
  const navigate = useCallback(
    (href: string) => {
      if (isMobile) {
        focusOnRoute.current = true;
        setMobileOpen(false);
      }
      requestNavigation(href, () => {
        animateNavigation.current = !reduced;
        const target = new URL(href, window.location.href);
        if (
          target.origin === window.location.origin &&
          target.pathname.replace(/\/$/, "") ===
            window.location.pathname.replace(/\/$/, "")
        ) {
          // Next 的原生 History 补丁会同步 searchParams；避免静态 RSC 缓存恢复旧查询。
          // 传 null 而非 history.state，后者会绕过 Next 的同步补丁。
          window.history.pushState(
            null,
            "",
            target.pathname + target.search + target.hash,
          );
        } else router.push(href);
      });
    },
    [router, isMobile, reduced],
  );
  useEffect(() => {
    const shouldAnimate = animateNavigation.current && !reduced;
    animateNavigation.current = false;
    pageAnimation.current?.cancel();
    if (shouldAnimate && contentRef.current)
      pageAnimation.current = contentRef.current.animate(
        [
          { opacity: 0.82, transform: "translateY(6px)" },
          { opacity: 1, transform: "translateY(0)" },
        ],
        { duration: 160, easing: "cubic-bezier(0.22,1,0.36,1)" },
      );
    return () => pageAnimation.current?.cancel();
  }, [pathname, reduced]);
  const logout = useCallback(() => {
    setMobileOpen(false);
    requestNavigation("/login", () => {
      void clearStoredAuthSession().then(() => {
        invalidateValidatedAuthSession();
        setSession(null);
        router.replace("/login");
      });
    });
  }, [router]);
  const toggle = useCallback(() => {
    if (isMobile) setMobileOpen((v) => !v);
    else setSidebarCollapsed(!collapsed);
  }, [isMobile, collapsed]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "b") {
        event.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [toggle]);
  const commands = useMemo<CommandItem[]>(
    () => [
      ...flattenNav(nav).map((item) => ({
        id: item.href,
        label: item.label,
        icon: item.icon,
        group: "页面",
        keywords: item.keywords,
        onSelect: () => navigate(item.href),
      })),
      {
        id: "sidebar",
        label: "展开 / 收起侧栏",
        icon: PanelLeft,
        group: "工作台",
        onSelect: toggle,
      },
      {
        id: "theme",
        label: "切换深色 / 浅色",
        icon: Moon,
        group: "工作台",
        onSelect: () =>
          document
            .querySelector<HTMLButtonElement>('button[aria-label="切换主题"]')
            ?.click(),
      },
      {
        id: "logout",
        label: "退出登录",
        icon: LogOut,
        group: "账户",
        onSelect: logout,
      },
      ...(canvasUrl
        ? [
            {
              id: "canvas",
              label: "打开无限画布",
              icon: Frame,
              group: "工作台",
              onSelect: () => setCanvasOpen(true),
            },
          ]
        : []),
    ],
    [nav, navigate, toggle, logout, canvasUrl],
  );
  useEffect(() => {
    const openPreview = () => {
      if (session?.role === "admin") navigate("/settings/components-demo");
    };
    window.addEventListener("chatgpt2api:component-preview", openPreview);
    return () =>
      window.removeEventListener("chatgpt2api:component-preview", openPreview);
  }, [session?.role, navigate]);
  const active = Boolean(session) && pathname.replace(/\/$/, "") !== "/login";
  const sidebar = session ? (
    <AppSidebar
      nav={nav}
      pathname={pathname}
      session={session}
      collapsed={collapsed}
      mobile={isMobile}
      onNavigate={navigate}
      onCollapse={() => setSidebarCollapsed(!collapsed)}
      onClose={() => setMobileOpen(false)}
      onLogout={logout}
    />
  ) : null;
  return (
    <div
      className={active ? "glass-layout" : "glass-public-layout"}
      data-shell-collapsed={collapsed}
    >
      {active && !isMobile ? sidebar : null}
      <div className="glass-workspace">
        {active && session ? (
          <TopBar
            nav={nav}
            pathname={pathname}
            session={session}
            isMobile={isMobile}
            onToggle={toggle}
            onOpenSearch={() => setPaletteOpen(true)}
            onNavigate={navigate}
            onOpenCanvas={canvasUrl ? () => setCanvasOpen(true) : undefined}
          />
        ) : null}
        <main
          data-page-content=""
          ref={contentRef}
          className={active ? "glass-page-content" : "glass-public-content"}
          id="main-content"
        >
          {children}
        </main>
        {active ? (
          <span aria-live="polite" className="sr-only">
            {
              flattenNav(nav).find(
                (item) => pathname.replace(/\/$/, "") === item.href,
              )?.label
            }
          </span>
        ) : null}
      </div>
      {active && isMobile ? (
        <Dialog open={mobileOpen} onOpenChange={setMobileOpen}>
          <DialogContent
            className="glass-mobile-sidebar"
            showCloseButton={false}
            onCloseAutoFocus={(event) => {
              if (focusOnRoute.current) {
                event.preventDefault();
                return;
              }
              menuTrigger.current = document.querySelector(
                'button[aria-label="打开导航"]',
              );
              menuTrigger.current?.focus();
            }}
          >
            <DialogTitle className="sr-only">主导航</DialogTitle>
            <DialogDescription className="sr-only">
              选择工作台板块
            </DialogDescription>
            {sidebar}
          </DialogContent>
        </Dialog>
      ) : null}
      {active ? (
        <CommandPalette
          items={commands}
          open={paletteOpen}
          onOpenChange={setPaletteOpen}
          placeholder="搜索页面或操作…"
          emptyMessage="没有找到匹配的页面或操作"
        />
      ) : null}
      <Dialog open={canvasOpen} onOpenChange={setCanvasOpen}>
        <DialogContent>
          <DialogTitle>打开无限画布</DialogTitle>
          <DialogDescription>
            继续后会在新页面打开已配置的画布，并传入当前服务地址和接入密钥。请确认该画布是你信任的服务。
          </DialogDescription>
          <p className="break-all text-xs text-muted-foreground">
            {canvasUrl ? new URL(canvasUrl).origin : ""}
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setCanvasOpen(false)}>
              取消
            </Button>
            <Button
              onClick={() => {
                if (canvasUrl && session) {
                  const target = new URL(canvasUrl);
                  target.searchParams.set("apiKey", session.key);
                  target.searchParams.set("baseUrl", window.location.origin);
                  window.open(
                    target.toString(),
                    "_blank",
                    "noopener,noreferrer",
                  );
                }
                setCanvasOpen(false);
              }}
            >
              继续打开
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      <a className="glass-skip-link" href="#main-content">
        跳到主内容
      </a>
    </div>
  );
}
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <AppearanceProvider>
      <Shell>{children}</Shell>
    </AppearanceProvider>
  );
}
