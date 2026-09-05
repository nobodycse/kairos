# frontend 镜像：node 构建 → nginx 托管
# build context = 仓库根（compose 里指定），路径带 frontend/ 前缀
# 前端脚手架就位前该镜像无法构建——compose 中 frontend 服务挂 profiles: [frontend]
FROM node:20-alpine AS builder
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /build/dist /usr/share/nginx/html
COPY deploy/docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
