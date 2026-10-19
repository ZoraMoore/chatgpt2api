"use client";
import Link from "next/link";
import { motion } from "motion/react";
import { ChevronLeft, ChevronRight, LogOut, X } from "lucide-react";
import {
  flattenNav,
  normalizePath,
  type NavEntry,
} from "@/components/shell/nav-config";
import { useAppearance } from "@/components/shell/appearance";
import type { StoredAuthSession } from "@/store/auth";

type Props = {
  nav: NavEntry[];
  pathname: string;
  session: StoredAuthSession;
  collapsed: boolean;
  mobile?: boolean;
  onNavigate: (href: string) => void;
  onCollapse: () => void;
  onClose: () => void;
  onLogout: () => void;
};
export function AppSidebar({
  nav,
  pathname,
  session,
  collapsed,
  mobile = false,
  onNavigate,
  onCollapse,
  onClose,
  onLogout,
}: Props) {
  const { reduced } = useAppearance();
  return (
    <aside
      data-global-sidebar=""
      data-collapsed={collapsed && !mobile}
      className={mobile ? "glass-sidebar mobile" : "glass-sidebar"}
    >
      <div className="glass-sidebar-brand">
        <Link
          href="/image"
          onClick={(e) => {
            e.preventDefault();
            onNavigate("/image");
          }}
          aria-label="chatgpt2api 首页"
        >
          <span className="glass-brand-mark" aria-hidden="true" />
          <span className="glass-brand-text">chatgpt2api</span>
        </Link>
        {mobile ? (
          <button
            className="glass-icon-button"
            onClick={onClose}
            aria-label="关闭导航"
          >
            <X size={19} />
          </button>
        ) : null}
      </div>
      <div className="glass-sidebar-label">WORKSPACE</div>
      <nav aria-label="主导航" className="glass-nav">
        {flattenNav(nav).map((item) => {
          const active =
              normalizePath(pathname) === item.href ||
              normalizePath(pathname).startsWith(item.href + "/"),
            Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              title={collapsed && !mobile ? item.label : undefined}
              onClick={(e) => {
                e.preventDefault();
                onNavigate(item.href);
              }}
            >
              {active ? (
                <motion.span
                  className="glass-nav-selection"
                  layoutId={mobile ? "mobile-active-nav" : "active-nav"}
                  transition={
                    reduced
                      ? { duration: 0 }
                      : { duration: 0.18, ease: [0.22, 1, 0.36, 1] }
                  }
                />
              ) : null}
              <Icon aria-hidden="true" className="glass-nav-icon" />
              <span className="glass-nav-label">{item.label}</span>
              {active ? (
                <ChevronRight
                  aria-hidden="true"
                  className="glass-nav-chevron"
                  size={16}
                />
              ) : null}
            </Link>
          );
        })}
      </nav>
      <div className="glass-sidebar-bottom">
        <div className="glass-sidebar-note">
          <span className="glass-note-orb" aria-hidden="true" />
          <strong>专注每一次创作</strong>
          <span>让 AI 能力触手可及</span>
        </div>
        <div className="glass-user-row">
          <span className="glass-avatar" aria-hidden="true">
            {(session.name || "管").slice(0, 1)}
          </span>
          <span className="glass-user-label">
            <strong>{session.name || "管理员"}</strong>
            <small>{session.role === "admin" ? "管理员" : "普通用户"}</small>
          </span>
          <button
            type="button"
            className="glass-icon-button"
            aria-label="退出登录"
            onClick={onLogout}
          >
            <LogOut size={17} />
          </button>
        </div>
        {!mobile ? (
          <button
            className="glass-collapse-button"
            type="button"
            onClick={onCollapse}
            aria-label={collapsed ? "展开侧栏" : "折叠侧栏"}
          >
            {collapsed ? (
              <ChevronRight size={18} />
            ) : (
              <>
                <ChevronLeft size={18} />
                <span>收起侧栏</span>
                <kbd>⌘ B</kbd>
              </>
            )}
          </button>
        ) : null}
      </div>
    </aside>
  );
}
