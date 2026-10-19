FROM node:22-bookworm-slim AS build
WORKDIR /work/frontend
ENV NEXT_TELEMETRY_DISABLED=1 NODE_OPTIONS=--max-old-space-size=1280
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY VERSION /work/VERSION
COPY frontend/ ./
RUN npm run test && npm run build

FROM scratch AS export
COPY --from=build /work/frontend/out /web_dist
