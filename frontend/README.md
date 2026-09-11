# KAIROS Frontend

Vue 3 + TypeScript + Vite + Pinia + Element Plus + ECharts。接口契约见 `docs/design.md` §1/§3/§4。

## 本地开发

```bash
npm install
npm run dev        # http://localhost:5173，/api 代理到 http://127.0.0.1:8000
```

先在仓库根起后端 stub（Python 3.10+）：

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8000
```

Phase 0 演示凭据：`admin / admin123`。

## 构建与部署

```bash
npm run build      # vue-tsc 类型检查 + vite build → dist/
```

生产部署由 `deploy/docker/frontend.Dockerfile` 在容器内构建（node:20-alpine，`npm ci` → `npm run build`），nginx 托管 dist 并反代 `/api`。**服务器上 frontend 服务挂 `profiles: [frontend]`，`deploy.sh` / `setup_server.sh` 检测到本文件存在即自动启用**。

## 结构（architecture.md §13）

```
src/
├── views/        # login / dashboard / resources / lab / diagnosis / history 六页
├── components/   # AppLayout（侧边栏布局）、EChart（echarts 封装）
├── services/     # API 封装（axios）+ SSE 客户端；类型定义随文件
├── stores/       # Pinia（auth：token + 过期时间，localStorage 持久化）
├── router/       # history 模式 + 登录守卫
└── utils/        # 格式化 / 状态中文标签
```

## 与后端对齐的约定

- Base URL `/api/v1`，`Authorization: Bearer <JWT>`；SSE 走 `?token=` query（EventSource 不能设请求头）
- 分页 `?page=1&page_size=20`，响应 `{items, total, page, page_size}`
- 错误统一 `{"detail": "..."}`；401（非登录接口）→ 清 token 跳登录
- SSE 四个命名事件 `snapshot / agent_step / status_changed / verification_progress`，心跳为 `: ping` 注释帧
- 路由避开 `/health`、`/api`、`/grafana`（nginx 占用）
