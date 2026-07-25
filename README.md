# chatgpt2api

基于 FastAPI 的 ChatGPT API 兼容服务，包含账号池管理、OpenAI 兼容接口、图片任务和 Sub2API 账号导入能力。

## 安全说明

本仓库不包含任何生产账号、OAuth Token、API 密钥、日志、图片或 Sub2API 服务器配置。

- 不要提交 `config.json`、`.env` 或 `data/`。
- 生产环境请通过反向代理提供 HTTPS；Compose 默认仅监听 `127.0.0.1:8088`。
- `CHATGPT2API_AUTH_KEY` 是管理员访问密钥，必须使用随机长字符串。

## Docker Compose 部署

```bash
git clone https://github.com/ZoraMoore/chatgpt2api.git
cd chatgpt2api
cp config.example.json config.json
cp .env.example .env
chmod 600 .env config.json
```

编辑 `.env`，将 `CHATGPT2API_AUTH_KEY` 替换为随机密钥，然后启动：

```bash
docker compose up -d --build
docker compose ps
curl 'http://127.0.0.1:8088/health?format=json'
```

首次启动没有导入账号时，健康接口会返回 `degraded`，这是预期行为。登录管理界面后导入可用账号即可。

## 更新

```bash
git pull
docker compose up -d --build
```

更新前请备份 Docker 卷 `chatgpt2api-data` 与本地 `config.json`。这些数据不应上传到 GitHub。
