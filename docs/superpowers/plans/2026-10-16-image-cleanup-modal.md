# 图像空间清理弹窗实施计划

> **For agentic workers:** Implement this plan directly, task-by-task, in the current session. No separate execution sub-skill is required.

**Goal:** 清理全流程在弹窗内完成，支持跨分页全选账号。

**Architecture:** 沿用 image-space API；增加全选请求选项及明确选择范围，独立前端扩展维护原任务状态，账号列表事件桥增加全选指令。

**Tech Stack:** Python/FastAPI/unittest、原生 JS、现有 Next 构建产物、Playwright、Docker。

- [x] 从测试站实际运行镜像复制隔离基线，确认当前跳转与 20 个账号限制。
- [x] 编写后端全选、超 20 个选择、账号快照与旧链接重定向用例，先验证旧实现失败。
- [x] 修改 `api/image_space.py` 和 `services/image_space_service.py`，全选固定受理时账号，继续要求显式执行确认。
- [x] 前端弹窗接入 preview / jobs / execute，增加全选和查看任务入口；避免重发、错绑账号和过期确认。
- [x] 对账号页事件桥做最小修改，更新 chunk 哈希引用；旧页面链接跳回弹窗。
- [x] 验证后台与浏览器完整流程、恢复和错误分支；使用合成任务验证删除，不执行真实删除。
- [x] 仅部署测试站，保持账号/配置/数据，验证真实全选预览和主站未变。
