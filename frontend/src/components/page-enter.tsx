"use client";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
/** 公共框架负责用户导航后的短过渡；页面首次加载直接可用，不逐项飞入。 */
export function PageEnter({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn("min-h-0", className)}>{children}</div>;
}
