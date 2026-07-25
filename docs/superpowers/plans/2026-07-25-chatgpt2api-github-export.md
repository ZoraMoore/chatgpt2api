# chatgpt2api GitHub 导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将运行中的 chatgpt2api 容器安全导出为可复现部署的私有 GitHub 仓库。

**Architecture:** 以容器内 `/app` 作为源码快照，排除运行数据和生产配置后放入当前工作目录。仓库通过 Python 3.13、`uv` 和 Docker Compose 重建服务；持久化数据与真实配置仅在部署机本地保存。

**Tech Stack:** Python 3.13、FastAPI、uv、Docker、Docker Compose、Git、GitHub CLI。

---

### Task 1: 导出并隔离运行时数据

**Files:**
- Create: `api/`, `services/`, `utils/`, `scripts/`, `web_dist/`, `main.py`, `pyproject.toml`, `uv.lock`, `VERSION`
- Create: `.gitignore`
- Create: `config.example.json`
- Preserve: `docs/superpowers/specs/2026-07-25-chatgpt2api-github-export-design.md`

- [ ] **Step 1: 在临时目录导出容器应用目录**

Run: `export_dir=$(mktemp -d); docker cp chatgpt2api:/app/. "$export_dir/"`

Expected: 临时目录包含应用源码、依赖锁文件、Web 静态资源与运行时目录。

- [ ] **Step 2: 仅复制可提交的应用文件到仓库工作目录**

Run:

```bash
rsync -a \
  --exclude '.venv/' --exclude 'data/' --exclude 'config.json' \
  --exclude '__pycache__/' --exclude '*.pyc' \
  "$export_dir/"/ /root/chatgpt-share-server/
```

Expected: 工作目录不出现 `data/`、生产 `config.json` 或 Python 虚拟环境。

- [ ] **Step 3: 写入忽略规则和无凭据配置样例**

Create `.gitignore`:

```gitignore
config.json
data/
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.DS_Store
```

Create `config.example.json`:

```json
{
  "storage_backend": "json",
  "base_url": "",
  "image_account_concurrency": 1,
  "image_parallel_generation": false
}
```

- [ ] **Step 4: 执行敏感内容检查**

Run: `grep -RInEI --exclude='config.example.json' --exclude-dir='docs' --exclude-dir='.venv' --exclude-dir='data' '(access_token|refresh_token|api[_-]?key|password|secret)' .`

Expected: 不出现生产凭据；命中源码字段名时仅人工确认其为实现代码，不含真实值。

### Task 2: 加入可复现 Docker 部署配置

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `README.md`

- [ ] **Step 1: 创建 Dockerfile**

```dockerfile
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY web_dist ./web_dist
COPY main.py VERSION ./
RUN mkdir -p /app/data

EXPOSE 80
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]
```

- [ ] **Step 2: 创建默认仅本机暴露的 Compose 文件**

```yaml
services:
  chatgpt2api:
    build: .
    container_name: chatgpt2api
    restart: unless-stopped
    ports:
      - "127.0.0.1:8088:80"
    environment:
      STORAGE_BACKEND: json
      CHATGPT2API_AUTH_KEY: ${CHATGPT2API_AUTH_KEY:?请在 .env 中设置管理员密钥}
    volumes:
      - ./config.json:/app/config.json:ro
      - chatgpt2api-data:/app/data

volumes:
  chatgpt2api-data:
```

- [ ] **Step 3: 写入中文 README 部署步骤**

README 必须说明：复制 `config.example.json` 为 `config.json`；创建仅本机可读的 `.env` 并设置 `CHATGPT2API_AUTH_KEY`；运行 `docker compose up -d --build`；默认访问地址为 `http://127.0.0.1:8088`；反向代理与 TLS 由部署者在外层配置；严禁提交 `config.json`、`.env` 和 `data/`。

### Task 3: 验证源码快照和容器构建

**Files:**
- Verify: `main.py`
- Verify: `api/`, `services/`, `utils/`
- Verify: `Dockerfile`, `docker-compose.yml`

- [ ] **Step 1: 做 Python 语法检查**

Run: `uv run python -m compileall -q api services utils main.py`

Expected: 命令退出码为 0。

- [ ] **Step 2: 构建导出的镜像**

Run: `docker build -t chatgpt2api:github-export-check .`

Expected: 构建成功，镜像可用于启动 FastAPI 服务。

- [ ] **Step 3: 以临时配置启动验证容器并探测健康接口**

Run:

```bash
verify_dir=$(mktemp -d)
printf '{"storage_backend":"json"}\n' > "$verify_dir/config.json"
mkdir -p "$verify_dir/data"
docker run -d --rm --name chatgpt2api-export-check \
  -p 127.0.0.1:18088:80 \
  -e CHATGPT2API_AUTH_KEY=verification-only-key \
  -v "$verify_dir/config.json:/app/config.json:ro" \
  -v "$verify_dir/data:/app/data" \
  chatgpt2api:github-export-check
curl -fsS 'http://127.0.0.1:18088/health?format=json'
docker stop chatgpt2api-export-check
```

Expected: 健康接口返回 JSON；没有账号时可显示 `degraded`，但服务必须成功响应。

- [ ] **Step 4: 核对 Git 暂存内容**

Run: `find . -path './.git' -prune -o -type f -print | grep -E '(^\./data/|^\./config.json$|^\./\.env$|accounts\.json|auth_keys\.json|sub2api_config\.json|logs\.jsonl)'`

Expected: 命令没有输出，证明工作目录没有生产运行数据。

### Task 4: 创建私有 GitHub 仓库并推送

**Files:**
- Modify: `.git/`（本地 Git 元数据）
- Remote: `https://github.com/ZoraMoore/chatgpt2api`

- [ ] **Step 1: 初始化 Git 并创建首个提交**

Run:

```bash
git init -b main
git add .
git diff --cached --name-only | grep -E '(^data/|^config.json$|^\.env$|accounts\.json|auth_keys\.json|sub2api_config\.json|logs\.jsonl)' && exit 1 || true
git commit -m "feat: 导出 chatgpt2api 可部署源码"
```

Expected: `main` 分支存在，首个提交只包含源码、部署文件、README 与设计文档。

- [ ] **Step 2: 创建私有仓库并推送**

Run: `gh repo create ZoraMoore/chatgpt2api --private --source=. --remote=origin --push`

Expected: `origin` 指向 GitHub 私有仓库，`main` 已推送。

- [ ] **Step 3: 验证远端仓库与敏感文件排除**

Run:

```bash
gh repo view ZoraMoore/chatgpt2api --json nameWithOwner,visibility,url
git ls-tree -r --name-only origin/main | grep -E '(^data/|^config.json$|^\.env$|accounts\.json|auth_keys\.json|sub2api_config\.json|logs\.jsonl)'
```

Expected: 输出显示 `PRIVATE` 和仓库 URL；第二个命令没有输出。
