"""Adobe Firefly 反代 provider。

把 Adobe Firefly 账号作为 source_type="adobe" 纳入 chatgpt2api 号池，
出图请求沿用 OpenAI 的 /v1/images/generations 与 /v1/images/edits 协议。

模块划分：
- constants: 端点与客户端常量（来自 firefly.adobe.com / Android App 抓包）
- models:    模型表、别名解析、生成参数校验
- auth:      IMS token 解析、device_token 换票、Cookie 续期、profile 校验
- client:    generate-async → 轮询 → 下载 三步反代
- provider:  号池选号 + 出图 + 计数，产出 conversation.ImageOutput
"""

SOURCE_TYPE = "adobe"
