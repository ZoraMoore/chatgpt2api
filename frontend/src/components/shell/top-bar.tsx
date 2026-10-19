"use client";
import { PanelLeft, Search, ChevronRight, Frame } from "lucide-react";
import { AppearanceControl } from "@/components/shell/appearance";
import { ThemeToggle } from "@/components/theme-toggle";
import { TaskCenter } from "@/components/shell/task-center";
import { VersionReleaseDialog } from "@/components/version-release-dialog";
import { findNavLink, type NavEntry } from "@/components/shell/nav-config";
import { getAuthEpoch, type StoredAuthSession } from "@/store/auth";
type Props = {
  nav: NavEntry[];
  pathname: string;
  session: StoredAuthSession;
  isMobile: boolean;
  onToggle: () => void;
  onOpenSearch: () => void;
  onNavigate: (href: string) => void;
  onOpenCanvas?: () => void;
};
export function TopBar({
  nav,
  pathname,
  session,
  isMobile,
  onToggle,
  onOpenSearch,
  onNavigate,
  onOpenCanvas,
}: Props) {
  const current = findNavLink(nav, pathname);
  return (
    <header className="glass-topbar" data-global-topbar="">
      <div className="glass-topbar-location">
        <button
          type="button"
          className="glass-icon-button"
          aria-label={isMobile ? "打开导航" : "展开或收起侧栏"}
          onClick={onToggle}
        >
          <PanelLeft size={20} />
        </button>
        <span className="glass-breadcrumb-root">工作台</span>
        <ChevronRight className="glass-breadcrumb-arrow" size={14} />
        <strong>{current?.label || "工作台"}</strong>
      </div>
      <div className="glass-topbar-tools">
        <button
          type="button"
          className="glass-command-button"
          aria-label="打开命令面板"
          onClick={onOpenSearch}
        >
          <Search size={16} />
          <span>搜索页面与操作</span>
          <kbd>⌘ K</kbd>
        </button>
        <TaskCenter
          key={`${session.role}:${session.subjectId}:${getAuthEpoch()}`}
          session={session}
          onNavigate={onNavigate}
        />
        <AppearanceControl allowGallery={session.role === "admin"} />
        {onOpenCanvas ? (
          <button
            type="button"
            className="glass-icon-button glass-canvas"
            aria-label="打开无限画布"
            onClick={onOpenCanvas}
          >
            <Frame size={18} />
          </button>
        ) : null}
        <ThemeToggle className="glass-icon-button" />
        <VersionReleaseDialog className="glass-version-button hidden xl:inline-flex" />
        <a
          className="glass-icon-button glass-github"
          href="https://github.com/ZoraMoore/chatgpt2api"
          target="_blank"
          rel="noreferrer"
          aria-label="GitHub 仓库"
        >
          <img
            src="/github.svg"
            alt=""
            width={18}
            height={18}
            className="dark:invert"
          />
        </a>
      </div>
    </header>
  );
}
