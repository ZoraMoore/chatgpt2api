# 同步主站运行版本

2026-10-16 13:40:43（北京时间），Image 站切换为主站当时实际运行的 WebImage 普通会话版本。部署采用本机已验证镜像，未从 Image 工作树重新构建。

- 主站镜像：`local/chatgpt2api:webimage-conversation-20261016`。
- Image 镜像：`local/chatgpt2api-image:sync-webimage-20261016`。
- 两站运行镜像摘要：`sha256:c2cc4db92af8bc750a2a315093d516e9287c704b7612d9c6a15f49b7aa59bea0`。
- 只替换 Image 运行容器与 Compose 镜像引用，主站容器 ID、镜像和启动时间未变。
- Image 的 2 个账号身份、密钥、配置原文件、数据挂载、CPU / 内存 / PID 限制均核对保留。
- 继续沿用 Image 原有默认语音模式和 4 个工作线程；新版本支持后台切换普通会话，普通会话使用 WebImage 协议。没有复制主站账号或配置。
- Image 本地实验源码未整体覆盖，运行版本以 Compose 引用的镜像为准。

回退镜像为 `local/chatgpt2api-image:before-sync-webimage-20261016`。部署记录位于 `/tmp/image-sync-webimage-20261016/deployment.json`；受保护的 `private` 子目录保存原 Compose、配置及旧容器日志，不得公开或提交。

初次核验因环境变量列表排列顺序不同触发自动回退；确认变量名和值一致后改用字典比较，重新部署通过。配置内容没有修改。

## 发布后真实验证

通过 `https://image.liunx.me/api/image-tasks/edits` 新建独立参考图编辑任务，57.746 秒完成；经公网下载并完整解码为 1254×1254 PNG（1,695,232 字节）。任务模式为 voice，执行记录 stream_complete=true。报告保存在 `/tmp/image-sync-webimage-20261016/public-verification/report.json`。这是单次真实交付证据，不代表所有账号或并发容量均已实测。
