# 出口时区探测与缓存

2026-10-15 已部署到 `image.liunx.me`。本改动修复时区一致性；真实生图仍返回 `404: Model not found`，尚未恢复账号池模型调用。

## 行为

- 首次使用某个出口时，以与 ChatGPT 客户端相同的代理配置请求 `https://chatgpt.com/cdn-cgi/trace`，取得该路径的公网 IP；随后向三家接口并发查询这个指定 IP。
- 固定使用 ipwho.is → ipapi.co → FreeIPAPI 的优先级。接口对同一 IP 的地理判断可能不同，不能让响应速度决定时区来回变化。FreeIPAPI 当前入口为 `https://free.freeipapi.com/api/v1/json/{ip}`；其 `timeZones` 为多项时拒绝使用，因为这可能是国家全部时区。
- 以代理地址的 SHA-256 为缓存键；不同代理分别缓存，直连共用一项。内存和 `data/egress_timezones.json` 提供两层缓存，磁盘文件权限为 0600，不保存 IP、代理地址、用户名、密码或账号令牌。
- 首次无缓存最多等待约 4 秒；缓存有效期 10 分钟。到期后先返回已有缓存，再后台刷新，最长保留 24 小时旧值。失败后退避 60 秒，无可用缓存时统一使用 UTC。代理地址改变会使用新的缓存键；同一代理地址内部换 IP 的检测存在缓存周期延迟。
- 时区名、`timezone_offset_min` 和 Sentinel PoW 日期使用同一个客户端快照；偏移由 IANA 时区按请求时刻计算，支持夏令时和半小时、45 分钟时差。不修改服务器全局 `TZ` 或系统时间。

浏览器脚本中的 `GM_setValue`、`localStorage` 在 Python 服务端不存在，因此采用内存和磁盘缓存实现相应行为。缓存命中免去网络探测，但不承诺严格 0 毫秒。

## 安全与边界

地理接口使用全新的 HTTP 客户端，仅传入代理设置，不携带 ChatGPT Authorization、Cookie 或用户提示词。所有地址固定，公网 IP、返回的 IANA 时区、响应大小、缓存记录数和时间戳均经过校验；请求具有共享截止时间，禁止任意重定向。

IP 地理数据库不是权威位置证明。本次同一 IP 在 ipwho.is 和 ipapi.co 分别返回洛杉矶、纽约；FreeIPAPI 返回了美国 29 个时区。固定优先级避免缓存抖动，但不能保证数据库一定准确。对每个连接都更换出口的代理，也不能保证独立连接的出口完全一致。

## 验证

34 项相关测试通过，候选镜像中再次通过 22 项时区和 PoW 测试。覆盖协议字段、DST、缓存重启、代理隔离、并发备份、后台刷新、失败退避、输入校验和请求边界。

一次独立容器实测：冷探测 178.669 ms，内存命中 0.049 ms，磁盘缓存加载 0.319 ms。它们是单次观测值，不是延迟保证。

使用用户指定的 Plus 账号完成了两次隔离真实生图对照，各限制为一个生成 POST：

| 选择 | 请求时区 | 偏移 | PoW 日期偏移 | 生成结果 |
| --- | --- | ---: | --- | --- |
| 自动探测 | America/Los_Angeles | 420 | GMT-0700 | HTTP 404，图片 0 |
| ipapi.co 对照 | America/New_York | 240 | GMT-0400 | HTTP 404，图片 0 |

两次均完成 conversation init、Sentinel prepare/finalize、conversation prepare，生成请求仍返回 `Model not found`。线上安装后的指定账号真实生图复测约 2.371 秒返回相同错误，SSE 事件 0、图片 0。因此时区方案本身不足以消除当前故障，不能把前置接口 HTTP 200 当作恢复证明。

线上三个相关 Python 文件与候选构建逐字节一致；公网 `/version` 和 `/health?format=json` 均为 HTTP 200，健康状态为 `ok`。账号身份字段、3 个账号数量、业务密钥和 `config.json` 内容保持不变，旧站容器 ID、启动时间未变化。

## 部署与回滚

运行版本：`local/chatgpt2api-image:timezone-7v0zjp_j`，同时更新新站 `latest` 标签。候选版本基于当时线上镜像，仅覆盖 `services/openai_backend_api.py`、`services/egress_timezone.py` 和 `utils/pow.py`，保留线上已有的其他修复；它没有用存在既有差异的整个本地目录覆盖运行版本。

回滚镜像保留为 `local/chatgpt2api-image:before-timezone-7v0zjp_j`。如需回滚，在无正在执行的任务时，将该镜像重新标记为 `local/chatgpt2api-image:latest`，再仅重建 `chatgpt2api-image` 服务。不要删除数据卷，也不要重建旧站服务。

构建、探测和部署核验记录位于 `/tmp/image-timezone-repair-7v0zjp_j`；临时测试账号副本已清理，报告不含认证信息。后续仍需修复剩余调用差异并取得实际图片，才能关闭原始故障。
