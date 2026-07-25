# chatgpt2api GitHub 导出设计

## 目标

将当前运行中的 chatgpt2api Docker 镜像导出为可复现部署的 Git 仓库，并推送到私有仓库 `ZoraMoore/chatgpt2api`。

## 范围

- 导出容器 `/app` 下的应用源码、依赖锁定文件和已构建的 Web 静态资源。
- 提供 `Dockerfile`、`docker-compose.yml`、`.gitignore`、`config.example.json` 与中文部署说明。
- 在本机完成镜像构建和最小启动验证后，创建 GitHub 私有仓库并推送首个提交。

## 安全边界

以下内容绝不进入 Git 历史：

- `data/` 中的 OAuth 账号池、认证密钥、Sub2API 配置、任务、日志和图片索引。
- 当前生产 `config.json`，其可能包含访问密钥、代理或存储凭据。
- Python 虚拟环境、缓存、编译产物中的临时文件。

仓库只包含不带凭据的 `config.example.json`；首次部署时，操作者自行复制为 `config.json` 并设置管理员密钥。

## 部署结构

`docker compose up -d --build` 启动单个应用服务，使用命名卷保存 `/app/data`，并将宿主机的本地 `config.json` 只读挂载至容器。默认仅映射到本机回环地址，避免未经反向代理和 TLS 配置直接暴露。

## 验证与交付

导出后执行敏感内容扫描、Python 语法编译检查、Docker 构建、容器健康请求，并检查首个 Git 提交不包含运行时数据。最后确认远端为私有仓库且 `main` 分支可访问。
