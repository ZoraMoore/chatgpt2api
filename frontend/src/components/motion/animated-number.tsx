"use client";
import { cn } from "@/lib/utils";
export interface AnimatedNumberProps {
  value: number;
  duration?: number;
  format?: (value: number) => string;
  className?: string;
  startOnView?: boolean;
}
/** 业务计数只显示已确认数值，避免零到目标值的计数动画制造不存在的中间状态。 */
export function AnimatedNumber({
  value,
  format = (number) => Math.round(number).toLocaleString(),
  className,
}: AnimatedNumberProps) {
  return <span className={cn("tabular-nums", className)}>{format(value)}</span>;
}
