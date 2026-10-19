# 前端源码来源

构建源码基线来自 MIT 许可的 239573049/WebImage，提交 c414463（本机只读参考快照），原许可见 THIRD_PARTY_LICENSE。

本目录用于恢复可构建前端，并适配 chatgpt2api 当前接口和已部署的会话导入、账号操作、图像清理及传输设置。参考源码中的额外代理池与运行健康管理不作为本站接口能力。

构建：在 frontend 目录执行 npm ci、npm run build；静态产物为 out，发布时复制到项目 web_dist。现有 web 目录保留服务端加载的页面扩展。
