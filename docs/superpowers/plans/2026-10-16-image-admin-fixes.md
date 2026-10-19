# Image 管理修复实施计划

> **For agentic workers:** Implement this plan directly, task-by-task, in the current session. No separate execution sub-skill is required.

**Goal:** 修复北京时间、账号操作、完整 Sub2API 导入与 Token 刷新。

**Architecture:** 基于运行镜像隔离实现；日志读取做时区转换；规范化完整账号；现有刷新机制增加到期时间回退和账号开关；图像清理添加按账号每日调度；独立 JS 扩展通过窄事件与现有列表同步。

**Tech Stack:** Python/FastAPI/unittest、静态 Next 产物与原生 JS、Playwright、Docker。

- [x] 核对运行版本和调用链，确认用户自动清理规则为每天清理 7 天前图片。
- [x] `tests/test_admin_log_time.py` 先复现 UTC 显示和日界线筛选；修改 `services/log_service.py`，保留旧 ID，新增时区标签。
- [x] `tests/test_sub2api_full_import.py` 先复现凭据丢失；修改 `services/sub2api_service.py` 和 `services/account_service.py`，导出对象替代 token 列表，兼容本地 JSON 导入、身份合并、到期元数据与 refresh token 轮换。
- [x] `tests/test_account_operations.py` 覆盖管理员鉴权、单账号/批量手动刷新和设置保存；新增 `services/account_operations.py`、`api/account_operations.py`，`api/app.py` 注册。
- [x] `tests/test_image_auto_cleanup.py` 覆盖截止时间与调度去重；`services/image_space_service.py` 只在显式开启账号执行每日清理，添加生命周期线程。
- [x] `web/account-image-space.js` 改为账号行与批量操作扩展，展示北京时间到期与状态；账号 chunk 添加稳定行标识和选中项事件桥，更新 hash 文件名和页面引用；日志列标注北京时间。
- [x] 隔离单测、浏览器交互和真实测试站只读验证，记录测试账号没有 refresh token 时的证据边界。
- [x] 构建最小覆盖镜像，只发布 Image；验证主站状态不变、账号/密钥/配置保留及真实生图。
