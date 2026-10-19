# 固定线上基线，保留号池、恢复轮询与传输重试等已部署修复。
ARG BASE_IMAGE=local/chatgpt2api-image:voice-base-c6feb98e6bcd
FROM ${BASE_IMAGE}
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY services/openai_backend_api.py services/account_service.py services/image_execution.py services/voice_image_transport.py services/voice_protocol.py services/voice_delta.py /app/services/
COPY services/protocol/conversation.py services/protocol/final_image.py /app/services/protocol/
# 账号导入修复曾以静态文件热更新部署，重建时必须一并保留。
COPY web_dist/accounts/__next._full.txt web_dist/accounts/__next.accounts.__PAGE__.txt web_dist/accounts/index.html web_dist/accounts/index.txt /app/web_dist/accounts/
COPY web_dist/_next/static/chunks/session-import-ca69a8b4e99c.js /app/web_dist/_next/static/chunks/
