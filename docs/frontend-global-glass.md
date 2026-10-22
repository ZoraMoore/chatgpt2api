# 全站玻璃前端的构建与验证

六个业务页面共用持久左侧导航。手机使用左侧抽屉，顶部仅保留工具操作。外观菜单提供动态档位、透明度、密度和 48 项组件预览，组件预览位于 `/settings/components-demo`，仅管理员可见。

## 源码与产物

- `frontend/`：可构建 React/Next 源码、锁文件和上游 MIT 许可。
- `frontend/out/`：本地构建产物，不提交。
- `web_dist/`：部署与提交的完整静态资源。
- `web/`：保留的账号操作、图片空间清理扩展；旧传输模式脚本遇到新前端会退出，设置页使用原生受控控件。
- `docker/global-glass/Dockerfile`：在已验证服务镜像上叠加静态页面与三个兼容脚本，不复制账号或配置。

## 构建

在项目根目录使用 Node 22 容器执行。项目保留现有后端 Python 运行环境，不要求在宿主机安装 npm。

```bash
docker run --rm --memory 2g --cpus 2 \
  -e NEXT_TELEMETRY_DISABLED=1 -e NODE_OPTIONS=--max-old-space-size=1280 \
  -v "$PWD":/work -w /work/frontend node:22-bookworm-slim \
  sh -c 'npm ci --ignore-scripts --no-audit --no-fund && npm run test && npm run build'
```

也可用独立导出镜像生成 `./local-verification`：

```bash
docker build -f frontend/Dockerfile --target export \
  --output type=local,dest=./local-verification .
```

构建会执行 TypeScript 检查，不忽略类型错误。前端来源、适配范围和许可见 `frontend/SOURCE.md` 与 `frontend/THIRD_PARTY_LICENSE`。

## 验证

- 容器内 `npm run test`：会话导入、缓存隔离、本地会话保存、生图恢复与任务深链等源码回归。
- 安装前端依赖后，项目根目录执行 `node tests/test_session_import.mjs` 与 `node tests/test_account_operations_ui.mjs`：完整凭据导入和安全事件桥。
- 浏览器环境安装 Playwright 与 Chromium，设置 `BASE_URL`、`CHROMIUM_PATH` 后执行 `python tests/test_global_glass_ui.py`；未显式设置 BASE_URL 或缺少 Playwright 时跳过。
- `test_account_pool_layout.py` 保留为旧命令入口，直接运行时转交全站浏览器测试。

浏览器测试对所有业务请求使用模拟响应，未知读写自动失败；不会向真实账号池导入、保存、清理或生成。截图使用合成账号、日志、图片与任务。

## 发布与回退

前端叠加镜像必须显式使用当前运行镜像作为基础，避免撤销后续后端和安全更新。在项目根目录执行：

```bash
frontend_base_image=$(docker inspect chatgpt2api-image --format '{{.Config.Image}}')
docker build --build-arg BASE_IMAGE="$frontend_base_image" \
  -f docker/global-glass/Dockerfile \
  -t chatgpt2api:example .
```

构建会检查七个页面入口的玻璃前端标记及兼容样式。更换基础镜像后，仍必须使用已有浏览器回归确认六页布局；`/version` 或健康检查成功只能说明服务可用，不能证明前端版本正确。

先完成构建、源码检查与整站浏览器验证，再备份测试站的 `web_dist`、三个兼容脚本和 Compose 镜像引用。先放置新资源，再切换页面文件，保留旧 `_next` 资源以兼容已打开的浏览器；配置、账号、卷和后端源码不随前端更新替换。

运行容器可以仅更新静态资源而不重启。与此同时构建并保存前端叠加镜像，Compose 引用该镜像，使后续重建容器保留相同页面。回退时恢复前端备份和原镜像引用，不回滚账号或业务数据。

发布后核对六个公开页面及关键资源哈希，并使用公开站点的真实静态页面再运行模拟接口浏览器回归。此验证证明前端链路，不替代实际上游生成验证。

## 发布验收与版本保护

公开说明不提供个人实例的运行镜像、部署时间、账号统计或容器信息。以当前运行镜像为基础叠加前端，并在构建时检查七个页面的标记及兼容样式；随后核对六页实际布局、前端资源哈希和核心交互。

若健康检查正常但前端退回旧布局，先比较运行容器与待发布包的静态资源，不根据宿主机文件存在就认定线上版本一致。修复只覆盖选定前端资源，保留后端安全更新、配置和数据。具体发布和回退记录保存在受保护的本地运维台账。
