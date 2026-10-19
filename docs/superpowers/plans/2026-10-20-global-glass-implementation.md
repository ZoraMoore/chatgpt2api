# 全站玻璃工作台实施计划

> **For agentic workers:** Implement this plan directly, task-by-task, in the current session. No separate execution sub-skill is required. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按已批准设计将六个页面统一到持久左侧导航，恢复可构建前端并保留测试站现有业务。

**Architecture:** frontend 保存 MIT 来源的可构建 Next/React 源码及本站兼容适配；AppShell 统一公共布局、外观偏好和任务入口。保留后端接口和已验证账号操作扩展，新增静态产物只更新测试站。

**Tech Stack:** React、Next 静态导出、TypeScript、Tailwind、Motion、Playwright。

## 任务 1 源码恢复和兼容基线

- [x] 创建 `/tmp/chatgpt2api-global-glass-20261020` 隔离 worktree。
- [x] 恢复 frontend 源码、来源和 MIT 许可，排除参考项目配置与凭据。
- [x] 固定 npm lock，检查构建配置不得忽略 TypeScript 错误。
- [x] 在 `frontend/src/lib/api.ts` 适配当前日志列表、账号字段、图片管理开关和设置传输模式。
- [x] 在 `frontend/src/app/accounts/components/account-import-parsers.ts` 恢复完整会话与 credentials 字段；先执行新语义测试证明缺失，再实施。

验证：`node --experimental-strip-types --test frontend/src/app/accounts/components/account-import-parsers.test.ts`；输入非法时不输出原凭据。

## 任务 2 公共布局与动态语言

- [x] 修改 `frontend/src/components/shell/{app-shell,app-sidebar,top-bar,nav-config}.tsx`，六入口直属左栏，使用真实 `/image-manager` 路由与现有角色权限。
- [x] 修改 `frontend/src/app/globals.css`、`frontend/src/lib/ease.ts`、`frontend/src/components/theme-toggle.tsx`，统一蓝白玻璃、深色、动态与透明度偏好。
- [x] 创建外观面板和真实任务中心；命令面板、折叠、手机抽屉均支持键盘和关闭焦点。
- [x] 主题切换即时；键盘操作即时；持续动态只反映真实未完成任务。

验证：`BASE_URL=http://127.0.0.1:18220 python tests/test_global_glass_ui.py`，六页均有 `aside[data-global-sidebar]`、`main[data-page-content]` 与 `body[data-global-glass]`，手机左抽屉，无顶部六项导航。

## 任务 3 业务页面兼容与交互

- [x] accounts：六统计卡、当前列序与安全事件桥，保留额外账号操作和图像空间清理。
- [x] logs：当前接口本地分页、北京时间、详情与可选查询，不调用不存在的 metrics。
- [x] settings：传输模式、保存草稿、未保存提示、原有 CPA/Sub2API/备份/用户密钥等。
- [x] image：参考图、历史、真实任务阶段、继续等待；确认本地会话 ID 与上游会话 ID 分离，不重放生成 POST。
- [x] image-manager：启用开关、网格/列表、标签、选择、预览、下载与删除。
- [x] debug：保留五子页，输出与安装代码默认脱敏，保留真实文件任务。

代理按页面职责独立写入；公共配置、依赖、API 类型、安全核心和部署仅由主 agent 修改。

## 任务 4 集成验证与发布

- [x] `npm run test`、`npx tsc --noEmit`、`npm run build`，生成完整 out 静态导出。
- [x] 用模拟接口完成六页直达与 SPA、响应式、权限、外观、账号/图片/日志/设置核心回归。
- [x] 记录六页桌面与手机截图，确认真实业务状态不由动画伪造。
- [x] 备份测试站前端资源，候选镜像只变更前端；保留运行容器 ID、配置、数据挂载及主站。
- [x] 原子更新测试站静态文件，公开资源哈希校验，使用实际站点静态资源重复集成回归。
- [x] 同步源码与构建产物至 image-development，记录完成范围和剩余后端能力边界。

不引入后端取消、配置回滚或回收站等未授权接口功能。删除、导入、刷新与保存验证仅使用隔离/模拟数据。
