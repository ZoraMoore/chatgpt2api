import {
  Database,
  ImageIcon,
  Images,
  Code2,
  ScrollText,
  Settings,
  type LucideIcon,
} from "lucide-react";
import type { AuthRole } from "@/store/auth";
export type NavLink = {
  kind: "link";
  href: string;
  label: string;
  icon: LucideIcon;
  keywords?: string[];
};
export type NavGroup = {
  kind: "group";
  id: string;
  label: string;
  icon: LucideIcon;
  children: NavLink[];
};
export type NavEntry = NavLink | NavGroup;
const adminNav: NavLink[] = [
  {
    kind: "link",
    href: "/image",
    label: "生图",
    icon: ImageIcon,
    keywords: ["画图", "image"],
  },
  {
    kind: "link",
    href: "/accounts",
    label: "号池管理",
    icon: Database,
    keywords: ["账号", "额度", "account"],
  },
  {
    kind: "link",
    href: "/image-manager",
    label: "图片管理",
    icon: Images,
    keywords: ["存储", "标签", "gallery"],
  },
  {
    kind: "link",
    href: "/logs",
    label: "日志管理",
    icon: ScrollText,
    keywords: ["调用", "日志", "logs"],
  },
  {
    kind: "link",
    href: "/debug",
    label: "调试",
    icon: Code2,
    keywords: ["skills", "搜索", "PPT", "PSD", "对话"],
  },
  {
    kind: "link",
    href: "/settings",
    label: "设置",
    icon: Settings,
    keywords: ["配置", "备份", "密钥", "CPA", "Sub2API"],
  },
];
export const getNav = (role: AuthRole): NavEntry[] =>
  role === "admin" ? adminNav : [adminNav[0]];
export const flattenNav = (entries: NavEntry[]): NavLink[] =>
  entries.flatMap((item) => (item.kind === "group" ? item.children : [item]));
export const normalizePath = (path: string) => path.replace(/\/$/, "") || "/";
export const findNavLink = (entries: NavEntry[], pathname: string) =>
  flattenNav(entries).find(
    (item) =>
      item.href === normalizePath(pathname) ||
      normalizePath(pathname).startsWith(item.href + "/"),
  );
