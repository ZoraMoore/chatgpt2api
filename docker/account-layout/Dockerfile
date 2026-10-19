# 使用测试站当前镜像，只叠加本次三个前端资源，保留全部既有运行源码。
ARG BASE_IMAGE=local/chatgpt2api-image:quota-cooldown-20261016
FROM ${BASE_IMAGE}
COPY web/account-image-space.js /app/web/account-image-space.js
COPY web_dist/account-pool-layout.js web_dist/account-pool-layout.css /app/web_dist/
