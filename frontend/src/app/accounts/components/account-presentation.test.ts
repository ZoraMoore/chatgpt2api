import assert from "node:assert/strict";
import { test } from "node:test";
import * as presentation from "./account-presentation.ts";

test("生图优先级按数字升序排列且保留同级顺序和原始数组", () => {
  assert.equal(typeof presentation.sortAccountsByImagePriority, "function");
  const accounts = [
    { id: "edu", image_priority: 1 },
    { id: "prolite", image_priority: 0 },
    { id: "manual", image_priority: 1 },
    { id: "last", image_priority: 1000000 },
  ];
  assert.deepEqual(
    presentation.sortAccountsByImagePriority(accounts).map((item) => item.id),
    ["prolite", "edu", "manual", "last"],
  );
  assert.equal(accounts[0].id, "edu");
});

test("优先级编辑保留0并拒绝空值、小数、负数和超出边界的输入", () => {
  assert.equal(typeof presentation.parseImagePriority, "function");
  assert.equal(presentation.parseImagePriority("0"), 0);
  assert.equal(presentation.parseImagePriority("1000000"), 1000000);
  for (const input of ["", " ", "-1", "0.1", "1e6", "1000001", "NaN", "true"]) {
    assert.throws(() => presentation.parseImagePriority(input), /0.*1000000.*整数/);
  }
});

test("人工优先级只在改动时发送，恢复默认仅发送auto模式", () => {
  assert.equal(typeof presentation.buildImagePriorityUpdate, "function");
  const manual = { image_priority: 0, image_priority_mode: "manual" as const };
  assert.deepEqual(presentation.buildImagePriorityUpdate(manual, "0", "manual"), {});
  assert.deepEqual(presentation.buildImagePriorityUpdate(manual, "1", "manual"), {
    image_priority: 1,
    image_priority_mode: "manual",
  });
  assert.deepEqual(presentation.buildImagePriorityUpdate(manual, "", "auto"), {
    image_priority_mode: "auto",
  });
  const automatic = { image_priority: 0, image_priority_mode: "auto" as const };
  assert.deepEqual(presentation.buildImagePriorityUpdate(automatic, "0", "auto"), {});
  assert.deepEqual(presentation.buildImagePriorityUpdate(automatic, "0", "manual"), {
    image_priority: 0,
    image_priority_mode: "manual",
  });
});

test("周期额度展示区分上限、已用未知、预占、不确定和实际可用", () => {
  assert.equal(typeof presentation.accountImageQuota, "function");
  const quota = presentation.accountImageQuota({
    quota: 120,
    image_quota_cap: 1000,
    image_quota_used: null,
    image_quota_reserved: 2,
    image_quota_uncertain: 3,
    image_quota_available: 0,
    image_quota_baseline: "unknown",
  });
  assert.equal(quota.available, 0);
  assert.equal(quota.capLabel, "1000");
  assert.equal(quota.usedLabel, "未知");
  assert.equal(quota.reserved, 2);
  assert.equal(quota.uncertain, 3);
});

test("已知本期用量保留0，旧API回退当前剩余额度且不伪造上限", () => {
  assert.equal(typeof presentation.accountImageQuota, "function");
  assert.equal(presentation.accountImageQuota({
    quota: 0, image_quota_cap: 1000, image_quota_used: 0,
    image_quota_baseline: "known",
  }).usedLabel, "0");
  assert.deepEqual(presentation.accountImageQuota({ quota: 117 }), {
    available: 117, capLabel: "未知", usedLabel: "未知", reserved: 0, uncertain: 0,
  });
});

test("冷却到期后仍提示待验证，不能显示成已恢复可用", () => {
  assert.equal(typeof presentation.accountImageRecovery, "function");
  const now = Date.parse("2026-11-07T00:00:00Z");
  assert.deepEqual(presentation.accountImageRecovery({
    image_limit_state: "cooldown",
    image_limit_restore_at: "2026-11-06T23:00:00Z",
  }, now), { label: "冷却已到期，等待验证", at: "2026-11-06T23:00:00Z" });
  assert.deepEqual(presentation.accountImageRecovery({
    image_limit_state: "cooldown",
    image_limit_restore_at: "2026-11-07T01:00:00Z",
  }, now), { label: "冷却中", at: "2026-11-07T01:00:00Z" });
  assert.equal(presentation.accountImageRecovery({ image_limit_state: "awaiting_check" }, now).label, "等待再次业务验证");
  assert.equal(presentation.accountImageRecovery({ image_limit_state: "probing" }, now).label, "恢复验证中");
  assert.equal(presentation.accountImageRecovery({ image_limit_state: "probe_ready" }, now).label, "等待恢复验证");
  assert.deepEqual(presentation.accountImageRecovery({
    image_quota_reset_at: "2026-11-08T00:00:00Z",
  }, now), { label: "周期重置", at: "2026-11-08T00:00:00Z" });
});
