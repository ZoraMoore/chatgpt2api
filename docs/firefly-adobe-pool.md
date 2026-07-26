# Adobe Firefly 接入号池

把 Adobe Firefly 账号作为 `source_type="adobe"` 纳入 chatgpt2api 号池，出图用 OpenAI 的
`/v1/images/generations`、`/v1/images/edits` 协议。**不改前端**：号池管理页原样展示 Adobe 账号的
来源、状态、剩余额度、成功/失败张数与在途并发数。

## 文件

| 路径 | 作用 |
|---|---|
| `services/firefly/constants.py` | 端点、client_id、scope（抓包确认） |
| `services/firefly/models.py` | 模型表、别名解析、OpenAI 参数映射 |
| `services/firefly/auth.py` | IMS token 解析、device_token 换票、Cookie 续期、profile 校验 |
| `services/firefly/client.py` | generate-async → 轮询 → 下载三步反代 + 参考图上传 |
| `services/firefly/provider.py` | 选号 → 出图 → 生图计数 |
| `services/firefly/pool.py` | 账号导入、编辑与分池统计 |
| `web/firefly-import.html` | Adobe 账号录入页（号池前端是预编译产物，这里补录入入口） |
| `scripts/test_firefly.py` | 离线自测（不联网、不写数据） |

接线点：`utils/helper.py`（前缀路由）、`services/protocol/conversation.py`（分流）、
`services/account_service.py`（Adobe 凭证续期与校验）、`services/protocol/openai_v1_models.py`
（模型发现）、`api/accounts.py`（导入与查询）。

## 导入账号

**界面**：http://127.0.0.1:8088/firefly-import 。填管理密钥后可新增账号、替换
`x-arp-session-id`、改额度与状态、删除账号，并直接看到每个账号的生图数量。

> **device_token 会自动桥接**：device_token 直接换出的是 FireflyAndroid 的 access_token
> （scope 只有 `creative_sdk`），**调不了生图端点**。导入时会自动跑 device → `/ims/jumptoken/v1`
> → GET jump 拿 SSO Cookie → `/ims/check/v6/token` 换成带 `firefly_api` 的 Web access（对应
> `services/firefly/auth.py:bridge_device_to_web_access`，移植自 Go 的 `bridge.go`），并保存桥接
> 产出的 Cookie 供续期。token 过期后续期同样走桥接。
>
> **x-arp-session-id 必填**：出图请求头 `x-arp-session-id`，导入时必须提供。

**接口**：凭证任选（`device_token` 会桥接，`session_cookies` / `access_token` 直接用），外加**必填**的 `arp_session_id`：

```bash
curl -X POST http://127.0.0.1:8088/api/accounts/firefly/import \
  -H "Authorization: Bearer $CHATGPT2API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"accounts":[
        {"device_token":"<FireflyAndroid device_token>","arp_session_id":"<x-arp-session-id>","quota":100},
        {"access_token":"<IMS access_token>","session_cookies":"<Adobe SSO Cookie 串>"}
      ]}'
```

改已有账号（换 arp、补 device_token、调额度）：

```bash
curl -X POST http://127.0.0.1:8088/api/accounts/firefly/update \
  -H "Authorization: Bearer $CHATGPT2API_AUTH_KEY" -H "Content-Type: application/json" \
  -d '{"access_token":"<该账号当前 access_token>","arp_session_id":"<新值>","quota":200}'
```

导入时会换票（如需）、调 `/ims/profile` 校验、写入 email / user_id / 账户类型，并检查
`firefly_api` / `creative_production` scope——缺 scope 的 token 出图必定 403，会直接落成「异常」。

`quota` 是本地生图次数配额（默认 100），每成功出图一张扣 1，归零转「限流」，可在号池管理页随时改。
它不等于 Adobe 的积分余额，只是号池调度用的闸门。

## x-arp-session-id

Adobe 的风控指纹包，base64 编码的 JSON：`sid`（会话 uuid）+ `ark` / `ftr`（Arkose 人机验证，
`arks-client.adobe.com`）+ `fpjs`（FingerprintJS 的 requestId / visitorId）。由 firefly.adobe.com
页面运行时生成，**带时间戳、会过期**，从浏览器出图请求头里抓。

- **导入必填**，存在账号的 `arp_session_id` 字段上，出图时由 `client.py` 注入（`services/firefly/client.py:_headers`）
- 失效后在 http://127.0.0.1:8088/firefly-import 的账号行点「编辑」贴新值即可（不能清空）
- 接口返回里只给 `has_arp_session_id` 和前 16 位预览，不回传完整值

查看分池情况：

```bash
curl http://127.0.0.1:8088/api/accounts/firefly -H "Authorization: Bearer $CHATGPT2API_AUTH_KEY"
# → {"items":[...脱敏后的账号...],"stats":{"total":3,"active":3,"total_quota":287,
#              "total_success":13,"total_fail":1,"inflight":0},"models":[...]}
```

## 出图

模型名统一 `firefly-` 前缀（前缀即路由依据，与自有的 `gpt-image-2` 不冲突）：

| 模型 | 上游 | 说明 |
|---|---|---|
| `firefly-image5` | image-v5 自有 | Firefly Image 5，3P 过载时的自动回退目标 |
| `firefly-gemini-flash` | firefly-3p | Nano Banana 3 |
| `firefly-nano-banana-2` | firefly-3p | Nano Banana Pro |
| `firefly-gpt-image-2` | firefly-3p | GPT Image 2 |
| `firefly-gpt-image-1.5` | firefly-3p | GPT Image 1.5 |
| `firefly-flux-pro-2` | firefly-3p | Flux Pro 2 |

别名 `firefly-nano-banana`、`firefly-gpt-image`、`firefly-flux`、`firefly-image5` 同样可用。
号池里有 Adobe 账号时，这些模型会自动出现在 `GET /v1/models`。

```bash
curl -X POST http://127.0.0.1:8088/v1/images/generations \
  -H "Authorization: Bearer $USER_KEY" -H "Content-Type: application/json" \
  -d '{"model":"firefly-gemini-flash","prompt":"一只在草地上奔跑的小狗",
       "n":2,"size":"1024x1024","quality":"high","response_format":"b64_json"}'
```

图生图（参考图会先上传到 Adobe，再作为 `referenceBlobs` 提交）：

```bash
curl -X POST http://127.0.0.1:8088/v1/images/edits \
  -H "Authorization: Bearer $USER_KEY" \
  -F model=firefly-gemini-flash -F prompt="把背景换成雪山" -F image=@cat.png
```

参数映射（OpenAI → Firefly）：

| OpenAI | Firefly | 规则 |
|---|---|---|
| `size: "1344x768"` | `aspect_ratio: "16:9"` | 折算成模型支持的最接近比例，再换成 Adobe 认可的像素尺寸 |
| `size: "auto"` | `aspect_ratio: "auto"` | gpt-image 走 `modelSpecificPayload.size=auto` |
| `quality: low/medium/high` | `resolution: 1K/2K/4K` | gpt-image 再换算成 `detailLevel` 1/2/3 |
| `n` | 并发张数 | 每张图独立选号、独立线程，与 OpenAI 通道行为一致 |
| `response_format` | `b64_json` / `url` | `url` 复用既有图片落盘与外链逻辑 |

## 号池管理页能看到什么

Adobe 账号与 OpenAI 账号在同一张表里，来源列显示 `adobe`：

- **状态**：正常 / 限流（额度耗尽）/ 异常（token 失效或缺 scope）/ 禁用
- **额度**：剩余可出图张数，可直接编辑
- **成功 / 失败**：累计生图数量，由 `mark_image_result` 在每次出图后写入
- **在途**：当前并发生图数，持续 > 0 说明并发槽泄漏
- **最后使用时间**：每次出图后刷新

刷新、禁用、改额度、改代理、删除、导出都可直接用现有按钮。

## 与 OpenAI 账号的行为差异

- **续期**：Firefly 没有 refresh_token。过期前 5 分钟自动用 `device_token`（`/ims/token/v4`）或
  SSO Cookie（`/ims/check/v6/token`）换新 token，轮换后的 token 通过既有的 token alias 机制无缝替换主键。
- **失效不删号**：OpenAI 账号 token 失效时可能被自动移除；Adobe 账号只标「异常」，因为
  `device_token` / Cookie 是拿回账号的唯一凭证，删掉就没了。
- **重新登录**：Adobe 账号不存密码，号池页的「重新登录」会跳过它们。
- **导出**：导出为 `{"type":"adobe", access_token, device_token, device_id, session_cookies, client_id}`，
  重新导入时会被自动识别回 adobe 分池。
- **3P 过载回退**：第三方端点返回 `system under load` 时，先按 2s/4s/8s 退避重试，仍失败则回退到
  `firefly-image5`，再失败才换号。

## 自测

```bash
python scripts/test_firefly.py
```

离线运行，覆盖模型路由、参数映射、四种请求体形态、IMS token 解析、号池闭环（选号 / 计数 / 额度扣减）
与导入导出对称性。缺 `git`、`pybase64` 等可选依赖时会自动用空壳顶替。
