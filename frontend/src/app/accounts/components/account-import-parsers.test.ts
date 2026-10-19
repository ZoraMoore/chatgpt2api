import assert from "node:assert/strict";
import { test } from "node:test";
import * as accountParsers from "./account-import-parsers.ts";

test("无效 JSON 的错误不会回显凭据片段且限制输入大小", () => {
  assert.equal(typeof accountParsers.parseImportJson, "function");
  for (const input of ["{fixture-private-session", "x".repeat(262145)]) {
    assert.throws(
      () => accountParsers.parseImportJson(input, 262144),
      (error) =>
        error instanceof Error &&
        !error.message.includes("fixture-private-session"),
    );
  }
});

import {
  describeSub2ApiSkips,
  mergeSub2ApiResults,
  parseSub2ApiExport,
  parseSub2ApiText,
} from "./account-import-parsers.ts";

/** The shape the Sub2API admin UI writes when you export an OpenAI OAuth account. */
const sub2apiExport = {
  accounts: [
    {
      auto_pause_on_expired: true,
      concurrency: 10,
      confirm_mixed_channel_risk: true,
      credentials: {
        access_token: "AT_REAL",
        chatgpt_user_id: "user-BjL72oLAu3udOTV8xQYE0UuS",
        client_id: "app_EMoamEEZ73f0CkXaXp7hrann",
        email: "qkduvp20912@outlook.com",
        expires_at: 1786540052,
        id_token: "IDT_REAL",
        organization_id: "org-yAOaPEVldApg2zfQCDJXBVF6",
        plan_type: "free",
        refresh_token: "rt.1.AADma2k",
      },
      name: "qkduvp20912@outlook.com",
      platform: "openai",
      priority: 1,
      rate_multiplier: 1,
      type: "oauth",
    },
  ],
  exported_at: "2026-08-02T17:40:11.601142715Z",
  proxies: [],
};

test("完整网页会话仅保留需要的会话凭据", () => {
  assert.equal(typeof accountParsers.getSessionAccountPayload, "function");
  const sessionToken = `${"a".repeat(32)}..${"b".repeat(16)}.${"c".repeat(16)}.${"d".repeat(16)}`;
  assert.deepEqual(
    accountParsers.getSessionAccountPayload({
      accessToken: "fixture-access",
      sessionToken,
      user: { name: "不应保存" },
    }),
    { access_token: "fixture-access", source_type: "web", sessionToken },
  );
  assert.deepEqual(
    accountParsers.getSessionAccountPayload({ accessToken: "fixture-access" }),
    { access_token: "fixture-access", source_type: "web" },
  );
  for (const value of [
    null,
    [],
    {},
    { accessToken: 42 },
    { accessToken: "bad\nheader" },
    { accessToken: "x".repeat(32769) },
    ...[
      42,
      {},
      "invalid",
      sessionToken + "\nheader",
      sessionToken + "x".repeat(16384),
    ].map((sessionToken) => ({ accessToken: "fixture-access", sessionToken })),
  ]) {
    assert.equal(accountParsers.getSessionAccountPayload(value), null);
  }
});

test("完整 Codex 导入保留嵌套轮换凭据并排除其他平台", () => {
  const raw = {
    platform: "openai",
    type: "oauth",
    name: "fixture@example.test",
    credentials: {
      access_token: "fixture-access",
      refresh_token: "fixture-refresh",
      id_token: "fixture-id",
      expires_at: 1234,
    },
  };
  const result = accountParsers.getAccountJsonAccount(raw);
  assert.deepEqual(result?.credentials, raw.credentials);
  assert.equal(result?.refresh_token, "fixture-refresh");
  assert.equal(result?.type, undefined);
  assert.equal(
    accountParsers.getAccountJsonAccount({ ...raw, platform: "anthropic" }),
    null,
  );
});

test("keeps the OAuth credentials from a Sub2API export", () => {
  const result = parseSub2ApiExport(sub2apiExport);
  assert.equal(result.accounts.length, 1);
  assert.equal(result.skippedPlatform, 0);
  assert.equal(result.skippedNoToken, 0);

  const account = result.accounts[0];
  assert.equal(account.access_token, "AT_REAL");
  assert.equal(account.refresh_token, "rt.1.AADma2k");
  assert.equal(account.id_token, "IDT_REAL");
  assert.equal(account.email, "qkduvp20912@outlook.com");
  assert.equal(account.plan_type, "free");
  assert.equal(account.expires_at, 1786540052);
  assert.equal(account.source_type, "codex");
  assert.equal(account.export_type, "codex");
  assert.equal(account.sub2api_platform, "openai");
  assert.equal(account.sub2api_auth_type, "oauth");
  assert.equal(account.concurrency, 10);
  assert.equal(account.priority, 1);
});

test("does not let the wrapper's type=oauth become a plan type", () => {
  assert.equal(parseSub2ApiExport(sub2apiExport).accounts[0].type, undefined);
});

test("skips accounts from other platforms", () => {
  const result = parseSub2ApiExport({
    accounts: [
      sub2apiExport.accounts[0],
      {
        platform: "anthropic",
        type: "oauth",
        credentials: { access_token: "AT_CLAUDE" },
      },
      {
        platform: "gemini",
        type: "oauth",
        credentials: { access_token: "AT_GEMINI" },
      },
    ],
  });
  assert.deepEqual(
    result.accounts.map((item) => item.access_token),
    ["AT_REAL"],
  );
  assert.equal(result.skippedPlatform, 2);
  assert.equal(describeSub2ApiSkips(result), "跳过 2 个非 OpenAI 平台账号");
});

test("reports api_key accounts as unusable instead of importing them", () => {
  const result = parseSub2ApiExport({
    accounts: [
      {
        platform: "openai",
        type: "api_key",
        credentials: { api_key: "sk-xxx" },
      },
    ],
  });
  assert.equal(result.accounts.length, 0);
  assert.equal(result.skippedNoToken, 1);
  assert.equal(
    describeSub2ApiSkips(result),
    "跳过 1 个缺少 access_token 的账号",
  );
});

test("accepts the admin API envelope, a bare array and a single account", () => {
  const enveloped = parseSub2ApiExport({
    code: 0,
    message: "ok",
    data: { accounts: sub2apiExport.accounts, total: 1 },
  });
  assert.equal(enveloped.accounts.length, 1);
  assert.equal(parseSub2ApiExport(sub2apiExport.accounts).accounts.length, 1);
  assert.equal(
    parseSub2ApiExport(sub2apiExport.accounts[0]).accounts.length,
    1,
  );
});

test("merging files dedupes by access_token and sums the skip counters", () => {
  const merged = mergeSub2ApiResults([
    parseSub2ApiExport(sub2apiExport),
    parseSub2ApiExport(sub2apiExport),
    parseSub2ApiExport({
      accounts: [
        {
          platform: "openai",
          type: "oauth",
          credentials: { access_token: "AT_SECOND" },
        },
      ],
    }),
    parseSub2ApiExport({
      accounts: [
        { platform: "anthropic", credentials: { access_token: "AT_CLAUDE" } },
      ],
    }),
  ]);
  assert.deepEqual(
    merged.accounts.map((item) => item.access_token),
    ["AT_REAL", "AT_SECOND"],
  );
  assert.equal(merged.skippedPlatform, 1);
});

test("tolerates blank, malformed and irrelevant input", () => {
  assert.equal(parseSub2ApiText("").accounts.length, 0);
  assert.equal(parseSub2ApiText("   ").accounts.length, 0);
  assert.equal(parseSub2ApiText("{not json").accounts.length, 0);
  assert.equal(
    parseSub2ApiText(JSON.stringify(sub2apiExport)).accounts.length,
    1,
  );
  assert.equal(
    parseSub2ApiExport({ accounts: [], proxies: [] }).accounts.length,
    0,
  );
  assert.equal(parseSub2ApiExport(null).accounts.length, 0);
  assert.equal(parseSub2ApiExport("nope").accounts.length, 0);
  assert.equal(
    describeSub2ApiSkips({ skippedPlatform: 0, skippedNoToken: 0 }),
    "",
  );
});
