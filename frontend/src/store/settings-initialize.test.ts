import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import type { useSettingsStore } from "../app/settings/store.ts";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../app/settings/store.ts", import.meta.url),
  "utf8",
);
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

type Config = {
  proxy: string;
  base_url: string;
  image_transport: "voice" | "conversation";
};
const initialConfig: Config = {
  proxy: "",
  base_url: "https://before.example",
  image_transport: "voice",
};

function harness() {
  let server = { ...initialConfig };
  let calls = 0;
  const pending: Array<(value: { config: Config }) => void> = [];
  let deferLoads = false;
  const api = {
    fetchSettingsConfig: async () => {
      calls++;
      if (deferLoads)
        return new Promise<{ config: Config }>((resolve) =>
          pending.push(resolve),
        );
      return { config: { ...server } };
    },
    fetchCPAPools: async () => ({ items: [] }),
    updateSettingsConfig: async (config: Config) => ({ config: { ...config } }),
  };
  const exports = {} as { useSettingsStore: typeof useSettingsStore };
  runInNewContext(compiled, {
    exports,
    require: (name: string) =>
      name === "@/lib/api"
        ? api
        : name === "sonner"
          ? { toast: { error() {}, success() {} } }
          : require(name),
    window: { dispatchEvent() {} },
    Event: class Event {},
  });
  return {
    store: exports.useSettingsStore,
    calls: () => calls,
    server: (value: Config) => {
      server = value;
    },
    defer: () => {
      deferLoads = true;
    },
    resolve: (index: number, value: Config) =>
      pending[index]({ config: value }),
  };
}

test("干净配置重入设置时重新读取服务器更新", async () => {
  const h = harness();
  await h.store.getState().initialize();
  h.server({ ...initialConfig, base_url: "https://after.example" });
  await h.store.getState().initialize();
  assert.equal(h.calls(), 2);
  assert.equal(h.store.getState().config?.base_url, "https://after.example");
  assert.equal(h.store.getState().config, h.store.getState().savedConfig);
});

test("有未保存草稿时重入设置保留编辑且不重新读取", async () => {
  const h = harness();
  await h.store.getState().initialize();
  h.store.getState().setBaseUrl("https://draft.example");
  h.server({ ...initialConfig, base_url: "https://external.example" });
  await h.store.getState().initialize();
  assert.equal(h.calls(), 1);
  assert.equal(h.store.getState().config?.base_url, "https://draft.example");
  assert.equal(
    h.store.getState().savedConfig?.base_url,
    initialConfig.base_url,
  );
});

test("配置加载期间的新编辑不会被慢响应覆盖", async () => {
  const h = harness();
  await h.store.getState().initialize();
  h.defer();
  const loading = h.store.getState().loadConfig();
  h.store.getState().setBaseUrl("https://draft.example");
  h.resolve(0, { ...initialConfig, base_url: "https://remote.example" });
  await loading;
  assert.equal(h.store.getState().config?.base_url, "https://draft.example");
  assert.equal(
    h.store.getState().savedConfig?.base_url,
    "https://remote.example",
  );
  assert.equal(h.store.getState().isLoadingConfig, false);
});

test("并发加载乱序完成时仅采用较新的请求结果", async () => {
  const h = harness();
  await h.store.getState().initialize();
  h.defer();
  const oldRequest = h.store.getState().loadConfig();
  const newRequest = h.store.getState().loadConfig();
  h.resolve(1, { ...initialConfig, base_url: "https://newest.example" });
  await newRequest;
  h.resolve(0, { ...initialConfig, base_url: "https://older.example" });
  await oldRequest;
  assert.equal(h.store.getState().config?.base_url, "https://newest.example");
  assert.equal(
    h.store.getState().savedConfig?.base_url,
    "https://newest.example",
  );
});

test("保存成功后不接纳保存前发出的配置读取", async () => {
  const h = harness();
  await h.store.getState().initialize();
  h.defer();
  const loading = h.store.getState().loadConfig();
  h.store.getState().setBaseUrl("https://saved.example");
  assert.equal(await h.store.getState().saveConfig(), true);
  h.resolve(0, initialConfig);
  await loading;
  assert.equal(h.store.getState().config?.base_url, "https://saved.example");
  assert.equal(
    h.store.getState().savedConfig?.base_url,
    "https://saved.example",
  );
  assert.equal(h.store.getState().isLoadingConfig, false);
});
