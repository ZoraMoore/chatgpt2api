# 仅叠加静态页面及兼容扩展，不复制账号、配置、日志或运行时数据。
# 必须显式传入当前运行镜像，防止恢复前端时回退后端或安全更新。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY web_dist /app/web_dist
COPY web/account-image-space.js web/account-image-cleanup.js web/image-transport-settings.js /app/web/
RUN /app/.venv/bin/python -c "from pathlib import Path; root=Path('/app/web_dist'); routes=['image','accounts','image-manager','logs','debug','settings','settings/components-demo']; assert all('data-global-glass' in (root/route/'index.html').read_text() for route in routes), '缺少全站玻璃前端'; assert (root/'global-glass-compat.css').is_file(), '缺少账号弹窗主题样式'"
LABEL io.chatgpt2api.frontend="global-glass-v1"
