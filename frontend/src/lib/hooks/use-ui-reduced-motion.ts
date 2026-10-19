"use client";
import { useReducedMotion } from "motion/react";
import { useAppearance } from "@/components/shell/appearance";
/** 自定义控件同时遵守系统、用户偏好与键盘即时操作，不能仅检查系统设置。 */
export function useUiReducedMotion() {
  const system = useReducedMotion();
  const { reduced } = useAppearance();
  return Boolean(system || reduced);
}
