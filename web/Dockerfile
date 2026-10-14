# 前端容器：多阶段构建（Node 编译 → Nginx 托管）
#
# 第一阶段用 Node 把 web/ 打成静态产物 dist/；
# 第二阶段只把 dist/ 与 nginx.conf 拷进 Nginx 镜像，运行时不需要 Node。
# 后端地址由 nginx.conf 里的 `proxy_pass http://api:8000` 决定（compose 服务名）。
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
# 覆盖默认站点配置：静态托管 + /api 反代到 api:8000
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
