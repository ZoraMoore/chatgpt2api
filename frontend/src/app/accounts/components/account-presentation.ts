import type { Account } from "@/lib/api";

export const MAX_IMAGE_PRIORITY = 1000000;

type ImagePriorityMode = Account["image_priority_mode"];
type ImagePriorityAccount = Pick<Account, "image_priority" | "image_priority_mode">;

export function sortAccountsByImagePriority<T extends { image_priority?: number }>(
  accounts: T[],
): T[] {
  // 套餐默认值由后端计算；旧接口缺字段时排在最后，不能把合法的 0 当作空值。
  return [...accounts].sort(
    (left, right) =>
      (left.image_priority ?? Number.MAX_SAFE_INTEGER) -
      (right.image_priority ?? Number.MAX_SAFE_INTEGER),
  );
}

export function parseImagePriority(value: string): number {
  const input = value.trim();
  const priority = Number(input);
  if (!/^\d+$/.test(input) || !Number.isSafeInteger(priority) || priority > MAX_IMAGE_PRIORITY) {
    throw new Error("生图优先级必须是 0 到 1000000 的整数");
  }
  return priority;
}

export function buildImagePriorityUpdate(
  account: ImagePriorityAccount,
  value: string,
  mode: ImagePriorityMode,
): { image_priority?: number; image_priority_mode?: ImagePriorityMode } {
  if (mode === "auto") {
    return account.image_priority_mode === "auto" ? {} : { image_priority_mode: "auto" };
  }
  const priority = parseImagePriority(value);
  return account.image_priority_mode === "manual" && account.image_priority === priority
    ? {}
    : { image_priority: priority, image_priority_mode: "manual" };
}

type ImageQuotaAccount = Pick<Account, "quota"> & Partial<Pick<Account,
  "image_quota_cap" | "image_quota_used" | "image_quota_reserved" |
  "image_quota_uncertain" | "image_quota_available" | "image_quota_baseline"
>>;

export function accountImageQuota(account: ImageQuotaAccount) {
  return {
    available: Math.max(0, account.image_quota_available ?? account.quota),
    capLabel: account.image_quota_cap == null ? "未知" : String(account.image_quota_cap),
    usedLabel: account.image_quota_baseline === "known" && account.image_quota_used != null
      ? String(account.image_quota_used)
      : "未知",
    reserved: account.image_quota_reserved ?? 0,
    uncertain: account.image_quota_uncertain ?? 0,
  };
}

type ImageRecoveryAccount = {
  image_limit_state?: string | null;
  image_limit_restore_at?: string | number | null;
  image_quota_reset_at?: string | null;
  restore_at?: string | null;
};

export function accountImageRecovery(account: ImageRecoveryAccount, now = Date.now()) {
  const at = account.image_limit_restore_at || account.image_quota_reset_at || account.restore_at || null;
  const state = account.image_limit_state;
  if (state === "cooldown") {
    const deadline = typeof at === "number" ? at * 1000 : Date.parse(at || "");
    return { label: Number.isFinite(deadline) && deadline <= now ? "冷却已到期，等待验证" : "冷却中", at };
  }
  const labels: Record<string, string> = {
    awaiting_check: "等待再次业务验证",
    probing: "恢复验证中",
    probe_ready: "等待恢复验证",
  };
  if (state && labels[state]) return { label: labels[state], at };
  return { label: at ? "周期重置" : "—", at };
}
