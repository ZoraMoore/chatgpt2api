# 原生首轮生图模型字段测试

`scripts/test_image_protocol.py` 专门检测上游 `resolved_model_slug`，分别报告 SSE 和会话查询响应中的字段位置、值及空值状态。`model_slug`、`default_model_slug` 不会被误当成该字段。

## 修复后的实测

在新站仓库目录执行，提示词文件使用原始文本，不添加“必须校图”等要求：

```bash
python3 scripts/test_image_protocol.py live \
  --prompt-file /绝对路径/原提示词.txt \
  --output-dir /绝对路径/尚未存在的测试结果目录
```

需要复现图生图时增加 `--image-file /绝对路径/原图.png`；指定账号时增加 `--account-email 账号邮箱`；图片模型别名可通过 `--model gpt-image-2.5` 选择，默认 `gpt-image-2`。账号必须仍在新站当前号池中，否则测试停止，不使用历史保存的令牌。未指定账号时沿用当前网页账号池的选择规则。

后台导入按令牌去重，同一邮箱的新旧会话可能并存。明确指定邮箱时若匹配多个不同令牌，脚本会停止；用 `--token-fingerprint` 指定当前会话令牌 SHA-256 的 12 到 64 位前缀后再测，避免随机选号。报告只记录令牌指纹，不记录令牌原文；指纹不匹配时不会回退使用旧令牌。

实测会消耗一次生图额度，最多允许一个上游 conversation 生成 POST；不会为取得字段而重试生成或追加校图对话。原生协议自身的 GET 轮询继续执行，另在取得会话 ID 后读取一次会话响应以检查模型字段。配置中若已有全局系统提示词，沿用现有配置。

脚本读取 `chatgpt2api-image` 当前运行的镜像、代码、配置及账号副本，在独立容器调用 `/v1/images/generations` 或 `/v1/images/edits` 使用的同一协议处理函数。这样既覆盖部署后的实际协议代码，也隔离账号计数、日志、存储与后台任务。报告记录镜像 ID 和关键代码哈希；**此次测试不覆盖公网 HTTP 鉴权、Caddy 和页面**，`runtime.http_wrapper_tested=false` 明确标明该边界。

临时容器不挂载线上数据卷，不启动服务后台调度，不上传到线上 WebDAV；结束或超时后删除临时容器及凭据副本。只读查询仍会使用所选账号的真实上游服务，生成的会话及额度消耗是真实的。正常运行的 Ctrl+C 或错误退出也走清理分支；强制 kill、宿主机断电不属于该保证。

## 报告与判定

结果目录包含 `report.json`；真实返回图片经 Pillow 检查格式并完整解码后，保存为 `generated.png` 或实际格式扩展名。HTTP 200 或返回了一段 base64 均不会单独认定出图成功。

`sse.resolved_model_slug.presence` 和 `conversation_response.resolved_model_slug.presence` 分别为：

- `present`：确实观察到字段；`value_states` 区分有效模型名、`null`、空串及已脱敏的异常值。
- `absent`：该来源完整解析，且实测取得有效图片，但没有观察到字段。
- `inconclusive`：调用失败、断流、解析失败、分析超出限制或会话响应不可识别，不能判定缺失。

字段若已在失败前出现，仍报告 `present`，但 `generation.successful=false`、退出码 2 保留本次生图失败的事实。`model_fields` 给出 `assistant.metadata.resolved_model_slug`、`server_ste_metadata.model_slug`、`conversation.default_model_slug` 等位置；不会保存提示词、推理正文、工具代码、Cookie、令牌或上游错误原文。报告权限为 600，结果目录为 700。

退出码：0 = 实测成功且 SSE 有字段；1 = 实测成功且完整 SSE 无字段；2 = 测试失败或无法判断。离线模式只判定提供的捕获内容，不能证明新请求已修复或真实图片已生成。

生图场景缺少 `resolved_model_slug` 不能单独证明模型降级；公开图片别名、请求模型、模型标签及实际返回字段必须分别比较。

## 离线检查与脚本回归

```bash
python3 scripts/test_image_protocol.py sse /绝对路径/conversation-sse.txt \
  --report /绝对路径/sse-report.json
python3 scripts/test_image_protocol.py har /绝对路径/chatgpt.com.har \
  --report /绝对路径/har-report.json
python3 -m unittest discover -s tests -p test_image_protocol_probe.py
```

HAR 优先读取 `_eventSourceMessages` 中的实际 `data`；事件数组只有名称和时间时，尝试响应正文，二者均无载荷则报告无法判断。正文为空不会直接认定字段缺失。只选择 conversation 生成 POST，忽略 prepare 等辅助请求；存在多条生成 POST 时，必须通过 `--entry-index` 指定从 0 开始的条目索引，避免混合不同会话。

离线回归覆盖模型标签与真实字段区分、增量补丁、空值、HAR 事件数组、失败与断流、脱敏、临时数据隔离及超时清理；不联网、不消耗生图额度。

2026-10-14 初版验证记录：线上运行镜像的隔离容器在 `--network none` 下通过 26 项脚本测试，含 4 项 worker 验证，生成流程使用模拟响应。已有 `conversation-sse gma.txt` 离线回放检出 `resolved_model_slug=gpt-6-thinking`；已有 HAR 的事件数组未保存 `data`，报告无法判断。初版验证没有发起新生图请求，也没有据此声称反代已修复。
