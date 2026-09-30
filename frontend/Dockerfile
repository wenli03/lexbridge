# =============================================================================
# LexBridge 前端镜像
# =============================================================================
# 多阶段：Node 构建产物 → nginx 提供静态文件并反代 /api。
#
# 构建工具只存在于第一阶段，最终镜像里没有 node_modules、没有源码、
# 没有构建工具链。一个只含静态文件与 nginx 的镜像，攻击面比带着
# 完整 Node 环境的镜像小一个数量级。
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1：构建
# ---------------------------------------------------------------------------
# node 24 而非 22：依赖树里有若干包要求 ^22.22.2 || ^24.15.0，
# 本地实测在 node 24 上 npm ci 稳定通过，故与本地保持一致。
# 构建镜像的 node 版本与开发机不一致，是"本地能装、容器装不上"的常见成因。
FROM node:24-alpine AS builder

# 可选：npm 镜像源。默认走官方 registry，保证仓库在任何环境下可构建；
# 网络受限时在 deploy/.env 里设 NPM_REGISTRY 即可。
#
# 这一项是实际踩到过的：本机 npm 配了国内镜像，容器里没有，
# 于是 `npm ci` 报出 "npm error Exit handler never called!" ——
# 这条错误信息完全指向 npm 自身的 bug，与网络环境看不出任何关联。
ARG NPM_REGISTRY=""

WORKDIR /build

# 先只拷贝依赖清单，让依赖层能被缓存复用
COPY package.json package-lock.json* ./
# npm ci 在 lock 文件缺失时失败；用 install 兜底并为首次构建生成 lock。
# 一旦 lock 文件入库（它应该入库），后续构建都会走 ci 的可复现路径。
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY" \
        && echo "已配置 npm 镜像：$NPM_REGISTRY"; \
    else echo "使用官方 npm registry"; fi \
    && if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
       else npm install --no-audit --no-fund; fi

COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
# 阶段 2：运行
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

# 去掉默认站点配置，避免它抢走 80 端口的默认 server
RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/conf.d/lexbridge.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

# 以非 root 运行需要额外的权限处理（监听 80、写缓存目录），
# 这里保持 root 但最小化镜像内容：nginx 官方镜像的默认配置
# 已经把 worker 进程降权到 nginx 用户。
# 用 127.0.0.1 而非 localhost —— 见 compose 里同一条健康检查的说明：
# Alpine 的 wget 会把 localhost 优先解析到 ::1。
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -qO- http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
