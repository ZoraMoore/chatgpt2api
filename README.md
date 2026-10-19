# chatgpt2api

基于 FastAPI 的 ChatGPT API 兼容服务，包含账号池管理、OpenAI 兼容接口、图片任务和 Sub2API 账号导入能力。

## image.liunx.me 独立实例

- 访问入口：https://image.liunx.me/login；生图页面：https://image.liunx.me/image。
- 源码分支：`image-development`；包含部署时原工作区的二开文件，尚未推送远程。
- Compose 项目和容器：`chatgpt2api-image`；独立镜像：`local/chatgpt2api-image:latest`。
- 数据卷：`chatgpt2api-image_image-data`；独立配置：本目录 `config.json`。
- 管理员密钥保存在本目录 `.env` 的 `CHATGPT2API_AUTH_KEY`，文件权限为 `0600`，不要粘贴进日志或提交 Git。
- 初始为空账号池，不复制老站账号、客户密钥、日志和图片。
- CPU 上限 1.5 核、内存上限 1 GiB，生图线程默认 4。
- Caddy 在 `/etc/caddy/Caddyfile` 中将新域名转发到 `127.0.0.1:8089`，自动维护 HTTPS 证书。
- 老站 `api.liunx.me` 继续使用原目录、容器和 `127.0.0.1:8088`。后续二开请在本目录操作。

## 安全说明

本仓库不包含任何生产账号、OAuth Token、API 密钥、日志、图片或 Sub2API 服务器配置。

- 不要提交 `config.json`、`.env` 或 `data/`。
- 生产环境请通过反向代理提供 HTTPS；Compose 默认仅监听 `127.0.0.1:8089`。
- `CHATGPT2API_AUTH_KEY` 是管理员访问密钥，必须使用随机长字符串。

## Docker Compose 部署

```bash
cd '/home/ubuntu/project/chatgpt 2api/chatgpt2api-image'
```

此目录已初始化，请勿覆盖现有 `.env` 或 `config.json`。新环境才需从示例复制配置。

编辑 `.env`，将 `CHATGPT2API_AUTH_KEY` 替换为随机密钥，然后启动：

可选的资源保护参数：

- `CHATGPT2API_LOG_MAX_BYTES`：单个日志文件上限，默认 64 MiB。
- `CHATGPT2API_LOG_BACKUP_COUNT`：保留的轮转日志数量，默认 2 个。
- `CHATGPT2API_IMAGE_WORKER_LIMIT`：独立生图工作线程上限，默认 4；不会占用 Web/管理接口的默认线程配额。

```bash
docker compose up -d --build
docker compose ps
curl 'http://127.0.0.1:8089/health?format=json'
```

首次启动没有导入账号时，健康接口会返回 `degraded`，这是预期行为。登录管理界面后导入可用账号即可。

## 更新

```bash
cd '/home/ubuntu/project/chatgpt 2api/chatgpt2api-image'
sudo docker compose up -d --build chatgpt2api-image
```

更新前请备份 Docker 卷 `chatgpt2api-image_image-data` 与本地 `config.json`。这些数据不应上传到 GitHub。

部署验证已覆盖公网页面与静态资源、管理员认证、空账号池、配置写入和重启后持久化。首次部署时未导入账号或执行真实生图；后续真实请求对照与修复边界见 [最终图片流程说明](docs/final-image-output.md)。空账号池的 `/health` 返回 `degraded`，容器健康检查使用 `/version`。

只停止新实例可在本目录执行 `sudo docker compose stop chatgpt2api-image`；不要使用 `down -v`，以免删除数据卷。共享 Caddy 配置需先校验再平滑重载，不要停止 Caddy。
