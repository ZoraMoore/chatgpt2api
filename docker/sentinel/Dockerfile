FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 \
    libdrm2 libxkbcommon0 libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libcairo2 libpango-1.0-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /run/sentinel && chown node:node /run/sentinel
COPY chromium/ /opt/chromium/
COPY sentinel-sdk.js sentinel_worker.mjs /app/
ARG SENTINEL_SDK_SHA256
ENV SENTINEL_SDK_SHA256=$SENTINEL_SDK_SHA256
ENV XDG_CONFIG_HOME=/tmp/chrome-config XDG_CACHE_HOME=/tmp/chrome-cache
USER node
WORKDIR /app
ENTRYPOINT ["node", "sentinel_worker.mjs"]
