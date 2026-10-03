# syntax=docker/dockerfile:1
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
ENV AUTH_UPSTREAM=auth:8000 \
    VERIFY_UPSTREAM=auth:8000 \
    BOARD_UPSTREAM=board-api:8000 \
    REAL_IP_FROM=10.0.0.0/8 \
    RATE_READ=100r/s \
    BURST_READ=200 \
    RATE_WRITE=2r/s \
    BURST_WRITE=5 \
    RATE_LOGIN=30r/m \
    BURST_LOGIN=10
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/snippets/ /etc/nginx/snippets/
COPY --chmod=755 frontend/docker/15-real-ip.sh /docker-entrypoint.d/15-real-ip.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
