# 最新 HAR 与线上生图接口对照

下文保留修复前的诊断快照，依据用户最新上传的 `chatgpt.com2 10 15.har`（79 条请求）和当时运行容器导出的源码。本地候选修复及后续实测见 [修复状态](sentinel-repair-status-20261015.md)；候选修复尚未部署，Plus 实际出图仍未恢复。

## 已经能够确认的结论

1. HAR 的 ChatGPT 账号标识与当前号池的 Plus 账号匹配；生成响应的 `server_ste_metadata.plan_type=plus`。
2. 第 18 条 `POST /backend-api/f/conversation` 返回 200；请求和 SSE 的 `resolved_model_slug`、`model_slug` 均为 `gpt-6-thinking`。正文包含 61 个 SSE data 记录并以 `[DONE]` 结束，完整 HTTP 请求约 355.870 秒。
3. 第 33 条响应包含一张可完整解码的 PNG：987 × 1594，1,336,265 字节。服务端元数据为 `tool_invoked=true`、`turn_use_case=image gen`、`tool_name=ImageGenToolTemporal`。这是实际图片证据，不只是模型目录或健康接口返回 200。
4. 用户已明确确认同账号首轮也能生图。因此，本 HAR 使用已有会话只是样本结构，**排除“必须续聊才能生图”作为故障原因**。不应以复用用户旧会话作为必要修复。
5. 当前网关失败记录是生成 POST 返回 `404: Model not found`，发生在 SSE 和图片工具开始前。现有证据不能把该文案等同于 Plus 无权限、模型已删除，或某项校验失败的唯一特征。

## 主要未对齐项

### 1. Turnstile 解析确定失败，且失败后仍继续请求

成功主请求携带非空 `OpenAI-Sentinel-Turnstile-Token`，长度为 7160。当前线上 `solve_turnstile_token` 对本次 HAR 第 20 条捕获的挑战进行离线回放时，输出为空；89 条初始指令中复现了 6 次 `KeyError`。异常在解析循环中被吞掉，部分不支持的指令被忽略。

源码位置：

- `utils/turnstile.py#solve_turnstile_token:177`：遍历固定初始队列，异常后直接继续。
- 线上 `openai_backend_api.py#_get_chat_requirements:2919`：解析失败通过 `or ""` 变成空字符串，仍执行 finalize。
- 线上 `openai_backend_api.py#_image_headers:619`：空结果导致最终生成请求不发送 Turnstile 头。

这是可复现的协议缺陷，应优先修复。它是否就是当前 404 的直接原因，仍需通过修正后的同账号实际调用验证，不能仅凭错误文案定因。

第 20/22 条 prepare/finalize 是主请求开始后的另一组校验，其最终 requirements token 与第 18 条主请求头不同。因此不能把这组挑战的预期结果认作主请求那份 7160 字符 token，也不能把不同轮次的 token 拼接回放。

### 2. 浏览器身份不一致，设备信息未持久化

HAR 为 Chrome 156，Client Hints 的主版本、完整版本与之对应。当前指定 Plus 账号实际使用默认 Edge/Chrome 143 的 UA、Edge 143 Client Hints，TLS 模拟配置却为 `chrome110`。

账号内没有持久化设备指纹；同一账号创建两个后端客户端会生成不同设备 ID。当前 prepare 与生成在单次客户端内已经复用设备和追踪 ID，但不同请求之间没有对应网页浏览器那样的稳定设备上下文。

源码位置：线上 `openai_backend_api.py#__init__:183`、`__init__:199`、`_build_fp:265`。以上差异已通过实例化实际线上客户端确认，但它们引起何种上游判定仍属待验证因果关系。

### 3. 参考图上传信息仍有遗漏

HAR 创建文件时携带 `entry_surface`、`client_resolved_mime_type`、`mime_resolution_source`、`mime_type`、`supports_direct_azure_multipart`；当前 `_upload_image` 未发送这些字段。

HAR 的 `process_upload_stream.metadata.library_file_info` 包含 `origination_thread_id`、`origination_message_id`；当前实现只发送 `store_in_library` 和 `is_project_thread`。

不过，当前代码已实现创建文件、PUT 上传、uploaded 确认、图库处理、`library_file_id` 提取；主请求中的 `sediment://` 指针、附件 ID 和附件维度结构也已基本对齐。现有证据不足以把上述遗漏认定为当前所有 404 的原因，尤其纯文生图不经过参考图上传。

### 4. prepare 和流传输还有差异，优先级较低

HAR 附件选择触发的 prepare 使用 `client_prepare_dispatch=immediate`、`client_prepare_source=file_picker`；当前参考图 prepare 使用 `debounced`、`composer_editor_state`。HAR 的 prepare 不带 Sentinel 头，当前会携带。两边都能取得 conduit token，现无证据表明这些调度或附加头差异造成 404。

HAR 请求 `X-OpenAI-Web-SSE-Compression: gzip`；当前未主动请求该扩展。需要连同响应解析能力核对，不能只补请求头。这类流格式差异不能解释当前尚未进入 SSE 就出现的 404。

## 已对齐或不应继续作为当前根因的项目

- 图片公开别名映射为 `gpt-6-thinking`，与 HAR 及真实 resolved model 一致；不能仅因公开别名叫 `gpt-image-2.5` 就认定模型映射错误。
- `thinking_effort=extended`、`local.continue_in_work`、Codex Browser 相关标识和主消息附件结构基本一致；当前线上参考图请求没有强加 `picture_v2`。
- PoW 已改为当前 FNV 混合哈希和 `~S` 后缀，不能继续沿用“线上仍为旧 SHA3”的结论。
- HAR 仍使用 `Asia/Shanghai/-480` 并成功出图；此前服务端洛杉矶、纽约两组时区对照都仍返回 404。时区一致性修正已部署，但没有证据说明继续改时区可恢复此次调用。
- 当前参考图流预算为 600 秒，覆盖这份 HAR 的约 356 秒；纯文生图默认 120 秒存在独立的长耗时兼容风险，但它不解释几秒内返回的 404。也不能为当前问题直接把所有超时一律调大。
- HAR 隐去了 Authorization 和 Cookie。账号标识相同可以确认账号对应关系，不能证明成功网页与服务器请求的完整认证上下文逐字相同。

## 修复优先级

先修复并验证 Turnstile 解析与失败处理，再统一并持久化每个账号的 UA、Client Hints、TLS 模拟和设备上下文。验证时使用同一 Plus 账号、同一出口和独立的新测试会话，最多一次生成 POST，保留阶段状态与脱敏校验结果。参考图上传缺项随后补齐。

只有取得实际图片，才能证明账号池调用已经恢复。在此之前，结论是“账号有生图能力，当前网关请求与成功网页请求仍有明确差异”，不能宣布单一根因已经坐实。

## 证据文件

线上源码只读快照及校验结果位于 `/tmp/har-alignment-20261015-_m3b5gh5`。`manifest.json` 记录导出文件 SHA-256；`summary.json` 记录图片校验和已排除的续聊假设。快照只包含代码和脱敏汇总，不含用户凭据。

当前线上 `openai_backend_api.py` 与本地工作目录不是整文件一致，所以涉及线上行为的行号以上述导出快照为准；`utils/turnstile.py`、`utils/pow.py` 已核验与本地对应文件一致。
