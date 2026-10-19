# 号池管理布局优化实施计划

> **For agentic workers:** Implement this plan directly, task-by-task, in the current session. No separate execution sub-skill is required. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依据用户提供的截图，将测试站号池管理页调整为浅蓝玻璃质感布局，并保留现有账号操作契约。

**Architecture:** 使用页面专用 CSS 和轻量 DOM 标记扩展，不重建或移动 React 管理的节点。复用现有全页账号扩展作为加载入口，依据路由启停样式，兼容 Next 客户端跳转。

**Tech Stack:** 原生 CSS、JavaScript、现有 Next 静态产物、Playwright。

---

- [x] 定位当前前端资源、运行容器及既有事件桥，确认隔离工作目录。
- [x] 创建页面样式和装饰脚本：蓝白背景、侧栏、统计卡、模型条、账号工具栏与表格；页面在窄屏下无全局横向溢出，表格自身横向滚动。
- [x] 在 web/account-image-space.js 加载扩展；保留 h1 层级、tr[data-account-ref] 及账号列顺序。
- [x] 使用脱敏模拟数据运行 Playwright：搜索、过滤、分页、选择、账号弹窗、跨页跳转、窄屏及空数据。执行既有 Node 导入/事件桥测试和 node --check。
- [x] 检查静态资源引用，保存桌面与窄屏截图，发布测试站前备份对应文件，并仅更新前端资源。

验收：视觉接近参考图，真实数量由现有页面计算，不将截图数字写成固定内容；支持键盘焦点与减少动态效果偏好。生产主站、账号池、管理员密钥、数据和后端业务保持原状。

## 验证与发布方式

- `node --check web_dist/account-pool-layout.js` 与 `node --check web/account-image-space.js`：检查扩展脚本语法。
- `node tests/test_session_import.mjs` 与 `node tests/test_account_operations_ui.mjs`：检查会话导入和账号操作事件桥。
- 浏览器环境需安装 Playwright，设置 `CHROMIUM_PATH` 指向已安装的 Chromium；通过 `BASE_URL=http://127.0.0.1:18219 SCREENSHOT_DIR=/tmp/account-pool-screenshots python tests/test_account_pool_layout.py` 运行 6 组场景。目标服务器须已提供本次静态资源；所有 API 请求在浏览器内使用模拟数据，不写入真实账号。未显式设置 BASE_URL 或未安装 Playwright 时跳过浏览器测试。
- `docker build -f docker/account-layout/Dockerfile -t local/chatgpt2api-image:account-layout-20261019 .`：在当前测试站镜像上只叠加三个前端文件。比较候选镜像与运行容器的源文件哈希，确认差异仅为本次三个资源。
- 运行容器仅对三个前端文件做同目录临时文件重命名更新，不重启服务。Compose 指向已构建的新镜像，使下次重建容器仍保留页面改动；本次运行容器继续使用原容器身份和挂载。
- 页面更新回退：恢复备份的 `web/account-image-space.js`，删除新增的 `web_dist/account-pool-layout.js`、`web_dist/account-pool-layout.css`，恢复 Compose 原镜像引用。新资源失效不会影响既有账号操作。

## 最终结果

- 六组浏览器回归通过，覆盖 1672 / 768 / 375 像素视口；无账号写入。
- 修复 Next 共享导航在离开号池后遗留装饰图标的问题，加入退出和返回场景断言。
- 两项既有 Node 回归和脚本语法检查通过。
- 三个前端资源已更新至测试站，公开 URL 返回的文件哈希与本次产物一致。
- 测试站与主站的容器 ID、启动时间、数据挂载均未变化。本次没有重启容器。
