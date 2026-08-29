# ============================================================
# Frontend 多阶段构建
#   stage 1 (builder):  编译 React 静态资源
#   stage 2 (nginx):    用 Nginx 托管静态资源并反代后端 API
# ============================================================

# ---------- 阶段 1: builder ----------
FROM node:20-alpine AS builder
WORKDIR /app

# 国内网络:使用 npmmirror 镜像加速 npm,避免依赖代理
RUN npm config set registry https://registry.npmmirror.com

# 用 npm ci(依据 lock 文件)保证可复现构建;依赖层走缓存
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---------- 阶段 2: nginx ----------
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
