# 仅叠加静态页面及兼容扩展，不复制账号、配置、日志或运行时数据。
ARG BASE_IMAGE=local/chatgpt2api-image:account-layout-20261019
FROM ${BASE_IMAGE}
COPY web_dist /app/web_dist
COPY web/account-image-space.js web/account-image-cleanup.js web/image-transport-settings.js /app/web/
