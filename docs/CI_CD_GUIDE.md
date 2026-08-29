# CI/CD 上线指南(基于 Docker + GitHub Actions)

本文档从零解释如何用 Docker 搭建"方便上线"的 CI/CD,并给出本仓库
已经写好的全套文件和使用方法。建议按顺序阅读,先理解概念,再动手。

## 1. 整体架构:一条流水线的四个环节

```text
代码 push 到 GitHub
      │
      ▼
┌─────────────┐   ┌──────────────┐   ┌───────────────┐   ┌───────────────┐
│  1. CI 检查  │ → │  2. 构建镜像  │ → │ 3. 推送镜像仓库 │ → │  4. 自动部署   │
│ lint / test │   │  docker build │   │ push GHCR     │   │ 本机 Docker    │
│ build       │   │  (可复现)     │   │ (唯一可信产物) │   │ Watchtower    │
└─────────────┘   └──────────────┘   └───────────────┘   └───────────────┘
```

核心思想:**"构建一次,到处运行"**。CI 负责保证代码质量并产出 Docker
镜像;镜像被推送到镜像仓库(GHCR);部署侧只负责拉取镜像并运行,不再
在服务器上编译任何东西。

## 2. 为什么这样设计

| 环节 | 解决的问题 | 本仓库实现 |
| --- | --- | --- |
| 多阶段构建 | 运行镜像不含编译工具,体积小、更安全 | `backend/Dockerfile`、`frontend/Dockerfile` |
| `.dockerignore` | 避免把 `node_modules`(2.9 万个文件)等塞进构建上下文 | `frontend/.dockerignore`、`backend/.dockerignore` |
| CI 自动测试 | 每次 push/PR 自动跑 lint、类型检查、单测、构建 | `.github/workflows/ci.yml` |
| 镜像仓库 GHCR | 镜像有版本、可回滚、部署机可拉取 | CI 里 `docker/build-push-action` |
| 本机自动部署 | 不用手动 SSH 上去 `docker compose up` | `deploy/docker-compose.prod.yml` 内置 Watchtower |
| 密钥分离 | 生产密码不写进仓库 | `deploy/.env.prod`(已 gitignore) |

## 3. 需要理解的三个核心概念

### 3.1 多阶段构建(Multi-stage build)

```dockerfile
FROM python:3.12-slim AS builder   # 阶段 1: 装编译工具
RUN apt-get install -y build-essential libpq-dev
RUN pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim               # 阶段 2: 精简运行环境
COPY --from=builder /wheels /wheels
RUN pip install /wheels/*            # 只安装预编译好的 wheel
```

阶段 1 产生的编译工具(几个 GB)不会进入最终镜像,最终镜像只有运行
需要的部分。这就是"镜像小、攻击面小、启动快"的关键。

### 3.2 镜像仓库与版本(GHCR)

CI 推送到 `ghcr.io/<owner>/<repo>/backend:latest`,同时打一个
`:<git-sha>` 的不可变版本标签:

```yaml
tags: |
  ghcr.io/OWNER/REPO/backend:latest
  ghcr.io/OWNER/REPO/backend:${{ github.sha }}
```

- `latest` 让部署侧(Watchtower)知道"有新版本了"
- `git-sha` 让你随时能回滚到历史某个提交的镜像

### 3.3 自动部署的两种路线

**路线 A(推荐,本仓库默认):Watchtower 拉模式**
生产 Compose 里有一个 `watchtower` 容器,它每 5 分钟检查 GHCR 上
带 `com.centurylinklabs.watchtower.enable=true` 标签的容器镜像,
发现 `latest` 变了就自动 `pull` + 重建容器。

- 优点:本机不需要公网 IP,只要能访问 `ghcr.io` 即可;无需任何 SSH 密钥
- 本仓库里 `deploy/docker-compose.prod.yml` 的 `watchtower` 服务就是干这个的

**路线 B(可选):SSH 推模式**
如果本机有公网 IP 或内网穿透,`.github/workflows/deploy.yml`
会 SSH 到本机执行 `deploy-local.sh update`。需要配置三个
GitHub Secrets:`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`。
未配置时该工作流自动跳过(默认走路线 A)。

## 4. 本仓库文件清单

| 文件 | 作用 |
| --- | --- |
| `backend/Dockerfile` | 后端多阶段构建(已优化) |
| `backend/.dockerignore` | 排除虚拟环境、测试、密钥进镜像 |
| `frontend/Dockerfile` | 前端 builder + nginx 两阶段构建 |
| `frontend/.dockerignore` | 排除 node_modules 等 |
| `.github/workflows/ci.yml` | CI:后端 lint/typecheck/test + 前端 build + 推镜像 |
| `.github/workflows/deploy.yml` | CD:SSH 部署(可选,未配密钥时跳过) |
| `deploy/docker-compose.prod.yml` | 生产 Compose:拉 GHCR 镜像 + Watchtower 自动更新 |
| `deploy/.env.prod.example` | 生产密钥模板(复制为 `.env.prod`) |
| `deploy/deploy-local.sh` | 本机部署脚本(start/update/status/logs/stop/down) |
| `deploy/docker-compose.yml` | 原有的开发版 Compose(在服务器上构建) |

## 5. 从零到上线的操作步骤

### 第 1 步:推送代码到 GitHub

```bash
git add .
git commit -m "feat: add CI/CD pipeline"
git push origin main
```

推送后进入 GitHub 仓库 → **Actions** 标签页,可以看到 `CI` 工作流
自动运行。第一次跑会自动测试并构建镜像。

### 第 2 步:让 CI 能推送镜像到 GHCR

CI 的 `docker` job 用 `GITHUB_TOKEN` 推送,只需在仓库
**Settings → Actions → General → Workflow permissions** 确认
"Read and write permissions" 已开启。仓库若是私有,镜像默认也是
私有的,拉取时需要登录(见第 4 步)。

### 第 3 步:本机准备部署目录

在你自己本机上克隆仓库(和 GitHub 上是同一个仓库):

```bash
mkdir -p ~/apps && cd ~/apps
git clone git@github.com:<你的用户名>/MyAgentDemo1.git
cd MyAgentDemo1
```

### 第 4 步:本机配置密钥并首次部署

```bash
cp deploy/.env.prod.example deploy/.env.prod
vim deploy/.env.prod      # 填写 GHCR_OWNER / POSTGRES_PASSWORD / LLM_API_KEY / MCP_SECRET_KEY 等

# 首次需要登录 GHCR(拉取私有镜像)
docker login ghcr.io -u <你的GitHub用户名>
# 密码填一个 PAT:GitHub → Settings → Developer settings → Personal access tokens
# scope 勾选 read:packages(拉取)或 write:packages(推送)

./deploy/deploy-local.sh start
```

访问 `http://localhost` 即是前端工作台,`http://localhost:8000` 是后端。

### 第 5 步:验证自动部署闭环

1. 本地改一行代码,`git push origin main`
2. 观察 GitHub Actions:CI 跑完 lint/test → 构建镜像 → 推送到 GHCR
3. Watchtower 最多 5 分钟后检测到 `latest` 更新 → 自动拉取重建
4. 刷新 `http://localhost`,新代码已上线 —— 全程没有手动 SSH

### 第 6 步(可选):启用 SSH 直推部署

如果本机有公网 IP/内网穿透,可以在 GitHub Secrets 中配置:

| Secret | 值 |
| --- | --- |
| `DEPLOY_HOST` | 本机公网 IP 或域名 |
| `DEPLOY_USER` | 本机 SSH 用户名 |
| `DEPLOY_SSH_KEY` | 本机 SSH 私钥 |
| `DEPLOY_PATH` | 仓库在服务器上的绝对路径 |

配置后 `deploy.yml` 会在每次 push 后 SSH 部署,替代 Watchtower。

## 6. 常见问题排查

| 现象 | 原因与处理 |
| --- | --- |
| 后端启动失败 `COALESCE could not convert type jsonb to json` | 历史 bug 已修复:`knowledge_bases.graph_meta / settings` 列类型从 `JSON` 改为 `JSONB`(`backend/app/storage/models.py`),并在 `backend/app/storage/db.py` 增加旧库幂等迁移,与启动统计 SQL 的 jsonb 运算符一致 |
| 镜像名 `must be lowercase` | GHCR 要求仓库名全小写;CI 已用 `${{ lower(github.repository) }}` 转小写,`.env.prod` 的 `GHCR_REPO=myagentdemo1` |
| 本地 `docker build` 在 `apt-get update` 失败(exit 100) | 本机 Docker Desktop 配置了代理(`127.0.0.1:1082`)但代理软件未启动。Dockerfile 已改用国内镜像源(阿里云 apt / 清华 PyPI / npmmirror),构建时再清空代理:`docker build --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg http_proxy= --build-arg https_proxy= -f backend/Dockerfile backend` |
| CI 里 `pytest` 失败 | 检查测试是否依赖外部 LLM/Embedding 服务;CI 已提供 PostgreSQL+pgvector 服务,`DATABASE_URL` 已指向它 |
| 镜像推送失败 `denied` | 确认仓库 Workflow permissions 为 Read and write;私有仓库首次需在 Packages 设置里把镜像可见性改为 public 或授权 |
| 本机 `docker compose up` 拉不到镜像 | 未登录 GHCR 或 token 缺少 `read:packages` 权限;执行 `docker login ghcr.io -u zneghao777`(token 勾选 `read:packages`) |
| GHCR 镜像还没推送 | 首次部署前必须先 `git push origin main` 触发 CI 构建推送;若只想先本地跑,可暂时把 `docker-compose.prod.yml` 的 `image:` 改为本地构建(见第 8 节) |
| Watchtower 不更新 | 确认容器有 `com.centurylinklabs.watchtower.enable=true` 标签;看 `docker logs harness-watchtower` |
| 部署后 502 | 后端未就绪(健康检查没过),看 `./deploy/deploy-local.sh logs` |

## 7. 后续学习方向

这套流程跑通后,可按需进阶:

1. **回滚**:保留 `git-sha` 镜像标签,出错时 `docker compose up` 指定旧 tag
2. **安全扫描**:CI 加 `trivy` 扫描镜像漏洞、`npm audit` 检查依赖
3. **蓝绿/金丝雀**:镜像不变,通过 Compose 复制服务并切换流量
4. **数据库迁移**:把 `alembic` 迁移放进部署流程,避免手动执行 SQL
5. **多环境**:`staging` / `prod` 两套 Compose override,共用同一套镜像
6. **更多测试**:接入 Playwright 端到端测试,部署后自动冒烟

## 8. 关键命令速查

```bash
# 本地验证 Dockerfile 可构建(不依赖 CI)
# 注意:若本机 Docker 配置了代理但代理未启动,需追加
#   --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg http_proxy= --build-arg https_proxy=
docker build -t harness-backend:local backend
docker build -t harness-frontend:local frontend

# 仅用本地构建的镜像启动(不依赖 GHCR,适合先本地验证)
#   1. 把 deploy/docker-compose.prod.yml 中 backend/frontend 的
#      image: ghcr.io/... 改为 harness-backend:local / harness-frontend:local
#   2. 再启动:
docker compose --env-file deploy/.env.prod -f deploy/docker-compose.prod.yml up -d postgres redis
docker compose --env-file deploy/.env.prod -f deploy/docker-compose.prod.yml up -d backend frontend

# 本机生产部署
./deploy/deploy-local.sh start     # 首次部署
./deploy/deploy-local.sh update    # 手动更新(Watchtower 也会自动做)
./deploy/deploy-local.sh status    # 查看容器状态
./deploy/deploy-local.sh logs      # 跟踪后端日志

# 原有开发环境
docker compose -f deploy/docker-compose.yml up --build
```
