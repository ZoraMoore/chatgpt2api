# 测试站源码快照

本次快照来自 image.liunx.me 对应的 chatgpt2api-image 源码目录，并使用运行容器 /app 中的文件补齐已部署差异。

- 保存源码、前端静态资源、现有测试及部署说明。
- 不包含 .env、config.json、账号池、运行日志、生成图片或浏览器运行时。
- docker-compose.yml 保留服务器的本地镜像引用；其他机器部署时需要先构建镜像并创建独立配置，不包含本地 Docker 镜像本体。
- 本次仅备份源码到 GitHub，不修改或重启线上服务。

## 从运行容器补齐的文件

- `api/ai.py`
- `api/image_tasks.py`
- `services/config.py`
- `services/sub2api_session_service.py`
- `services/webimage_conversation_transport.py`
- `services/protocol/openai_v1_image_edit.py`
- `services/protocol/openai_v1_image_generations.py`
- `services/protocol/openai_v1_response.py`
- `services/protocol/openai_v1_chat_complete.py`
- `services/protocol/terminal_image_delivery.py`
- `web/image-transport-settings.js`
- `web_dist/_next/static/chunks/03bl833~.m285-image-transport-7b4f49b79dd1.js`
- `web_dist/_next/static/chunks/10p1zjrwfyqvw-image-transport-97ae6cd0d4a2.js`
- `web_dist/image/__next.image.__PAGE__.txt`
- `web_dist/image/index.html`
- `web_dist/image/__next._full.txt`
- `web_dist/image/index.txt`
- `web_dist/settings/__next.settings.__PAGE__.txt`
- `web_dist/settings/index.html`
- `web_dist/settings/__next._full.txt`
- `web_dist/settings/index.txt`
