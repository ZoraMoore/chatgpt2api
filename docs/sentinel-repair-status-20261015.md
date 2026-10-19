# Plus 生图修复状态（2026-10-15）

> 本文保留当时未部署候选的历史结果；后续部署、真实出图与导入修复见 [网页会话生图与导入修复](browser-session-image-repair-20261015.md)。

**状态：候选代码已完成针对性回归，真实 Plus 生图仍失败，未部署。** 本文依据隔离测试的运行输出，不将前置接口 HTTP 200 或非空校验结果当作出图成功。

## 已修复的可复现问题

- `utils/turnstile.py`、`utils/turnstile_vm.js`、`utils/turnstile_worker.py`：以 JavaScript 类型和异步语义处理动态指令队列，修正旧解析器对最新 HAR 挑战返回空结果的问题。要求校验时，失败立即停止，禁止将空值继续提交。使用独立子进程，并限制执行时间、内存、输入规模及输出长度；不向子进程传递服务凭据或文件、网络回调。
- `utils/browser_fingerprint.py`：统一所支持的 Chrome 配置、User-Agent 和 Client Hints；按账号及站点稳定生成设备标识，保留已配置的合法设备 UUID，无需重写号池文件。
- `services/openai_backend_api.py`：设备 Cookie 与请求头一致；首页导航移除 API 专用认证头并在异常时恢复；Sentinel 阶段包含对应的账号和客户端上下文；prepare 不携带生成专用 Sentinel 头；对 prepare/finalize 字段进行类型、长度及控制字符校验。
- `utils/pow.py`：PoW 与挑战执行共用时区、语言、屏幕、CPU 和页面时间信息。保留已部署的 FNV 计算与 `~S` 后缀。
- 网页生图请求补齐 SSE 压缩声明；参考图 prepare 使用最新 HAR 的 `immediate/file_picker`。参考图请求体使用脱敏 HAR 夹具校验，创建新的测试会话，不复用用户已有会话。
- `pyproject.toml`、`uv.lock`：固定 `quickjs-ng==0.17.0.1`，保留其余依赖版本及平台包信息。

本地工作目录与运行源码在修复开始前已有差异。候选镜像从当前运行镜像派生，只覆盖本次修改的源码与依赖；保留线上已有的文件传输重试等实现。本地图片请求构造方法同步到相同基线，普通对话不额外强制 `picture_v2`。

## 验证结果

| 检查 | 实际结果 |
| --- | --- |
| 工作目录针对性回归 | 61 项通过 |
| 生产基线候选镜像针对性回归 | 同一套 61 项通过 |
| JavaScript 语法、差异空白检查 | 通过 |
| 同账号 `/api/auth/session` | 200；读回 Access Token 与号池一致 |
| 同账号模型目录 | 200；包含 `gpt-6-thinking`，列出 `image_gen_tool_enabled` |
| conversation init | 200 |
| Sentinel prepare / finalize | 均为 200；提交非空挑战结果 |
| conversation prepare | 200，响应 `status=ok`，返回 conduit token |
| 最终生成 POST | **404，`Model not found`** |
| 实际返回图片 | **0 张** |

最终候选对同一 Plus 账号、最新 HAR 对应的既有参考图进行了一次生成 POST；测试走独立配置与号池副本，完成后删除副本。上游仍在约 4.4 秒内返回 404，尚未进入图片结果流。

已做的对照包括：登录 Cookie、HAR 的设备与时区、普通网页客户端标识、服务器已有代理、新会话随机 parent UUID，以及在真实 Chromium 中执行挑战指令。这些对照均未得到图片，不能据此确认唯一根因。Chromium 指令对照不等同于完整登录网页端到端验证；完整网页捕获未能取得可用于生成的完整原生上下文，未发送生成请求。

用户确认其电脑通过普通 Chrome/Edge 直连，且同账号首轮可以生图。不得继续将“需要续聊”“必须使用代理”“Plus 不支持该模型”作为已确认原因。HAR 中的 Codex Browser 标识也不能直接证明用户使用了 Codex 内置浏览器。

## 当前限制与下一步证据

QuickJS 执行环境提供受限的浏览器对象，并不等同于完整 Chrome 页面。非空结果和 finalize 200 不能证明生成请求通过了上游所有校验。

最新 HAR 中没有成功生成请求的 `Authorization` 和 `Cookie`。用户提供的会话 JSON 已验证有效，但它不足以证明实际成功生成请求使用完全相同的 Bearer 和全部 Cookie。继续逐字段对齐需要成功的 `POST /backend-api/f/conversation` 完整请求副本，例如将该请求的 **Copy as cURL** 结果作为私密文本附件提供；无需再次提供整份会话 JSON。

参考图文件创建及图库来源元数据还有此前记录的差异，尚未改动。它们不能解释不经过上传的纯文生图同样在生成阶段返回 404。

## 运行与复现信息

- 候选镜像：`local/chatgpt2api-image:sentinel-w0tggv85`。
- 候选镜像标识：`sha256:5b9f82b114320dc88f6766a4f8077a6ad1e8319f36b8ebd5664c5633f0dc9591`。
- 构建目录及脱敏探测报告：`/tmp/image-sentinel-repair-w0tggv85`。其中 `probe-reference/report.json` 对应最后一次真实调用。
- 测试模块：`tests.test_turnstile`、`tests.test_pow_protocol`、`tests.test_egress_timezone`、`tests.test_web_image_protocol`、`tests.test_native_image_flow`。测试在隔离容器内运行，不连接生产数据目录。
- `chatgpt2api-image` 容器仍为 `f4c0e754a48b`，旧站 `chatgpt2api` 仍为 `b1d434f9838b`；本轮均未重建或重启，两站健康接口均返回 200。
- 本轮临时认证副本和原生捕获文件已清理；用户原附件保留。
