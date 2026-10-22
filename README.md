# chatgpt2api

基于 FastAPI 的 ChatGPT API 兼容服务，包含账号池管理、OpenAI 兼容接口、图片任务和 Sub2API 账号导入能力。

## 项目结构

- `api/`、`services/`、`utils/`：服务端接口和业务实现。
- `web_dist/`：服务端提供的静态前端资源。
- `web/`：账号操作等兼容扩展。
- `docs/`：功能说明、构建方法和通用验证流程。
- `image-development` 分支另含 `frontend/`，用于构建统一左侧导航前端。

## 配置与安全

- 新环境从 `.env.example` 和 `config.example.json` 创建自己的配置；已有部署不要覆盖现有文件。
- `CHATGPT2API_AUTH_KEY` 为管理员密钥，应使用随机长字符串并保存在环境变量或受保护配置中。
- 不要提交真实 `.env`、`config.json`、`data/`、账号导出、Cookie、HAR、日志或备份。
- 示例域名使用 `image.example.com`；请替换为自己的域名，通过反向代理提供 HTTPS。
- 多实例应使用独立配置与数据卷，不复制其他实例的账号和客户密钥。

## 构建与运行

先核对所用分支的 Dockerfile 和 Compose 配置。Compose 中的本地镜像需要自行构建或替换为已经验证的镜像，仓库不分发个人部署镜像。

```bash
docker build -t chatgpt2api:local .
docker compose config --quiet
```

在自己的 Compose 配置中指定已构建镜像、端口、密钥和数据挂载后启动服务：

```bash
docker compose up -d --no-build
docker compose ps
```

服务提供 `/login`、`/image`、`/accounts` 等页面。`/version` 用于检查进程可用性；空账号池可能使 `/health` 返回 `degraded`。健康检查成功不能替代真实图片生成验证。

可选资源参数：

- `CHATGPT2API_LOG_MAX_BYTES`：单个调用日志文件上限。
- `CHATGPT2API_LOG_BACKUP_COUNT`：保留的轮转日志数量。
- `CHATGPT2API_IMAGE_WORKER_LIMIT`：独立生图工作线程上限。

## 更新与数据保留

更新前备份配置和数据卷，记录实际运行镜像，确认在途任务的处理方式。只更新目标实例，保留账号、密钥、数据和无关服务。不要使用 `docker compose down -v` 代替代码更新。

发布验收应包括实际前端版本、认证、核心接口与必要的真实请求。公开文档只保留通用方法；具体域名、服务器路径、镜像摘要和个人部署记录应留在本地受保护的运维记录中。

更多功能边界见 [最终图片流程](docs/final-image-output.md) 和 [图片协议验证](docs/image-protocol-test.md)。
