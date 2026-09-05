#!/usr/bin/env bash
# KAIROS CI/CD 部署脚本（webhook_server.py 触发，也可手动执行）
# 流程：pull → 按 git sha 打 tag build → up → /health 健康检查 → 失败回退上一版本
set -euo pipefail

KAIROS_HOME=/opt/kairos
REPO_DIR=$KAIROS_HOME/repo
COMPOSE_FILE=$REPO_DIR/deploy/compose/docker-compose.yml
STATE_FILE=$KAIROS_HOME/current_tag
HEALTH_URL=http://127.0.0.1/health

log() { echo "[deploy] $(date '+%F %T') $*"; }

# 跨进程互斥：webhook 的进程内锁之外，防止手动执行与自动部署并发
exec 9>/tmp/kairos-deploy.lock
flock -n 9 || { log "已有部署在进行，退出"; exit 0; }

cd "$REPO_DIR"
REMOTE=$(git remote get-url gitee >/dev/null 2>&1 && echo gitee || echo origin)
git fetch "$REMOTE" main
git reset --hard "$REMOTE"/main

NEW_TAG=$(git rev-parse --short HEAD)
OLD_TAG=$(cat "$STATE_FILE" 2>/dev/null || echo "")

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# 前端脚手架就位前不参与构建（frontend 服务挂 profiles: [frontend]）
PROFILE_ARGS=()
[[ -f $REPO_DIR/frontend/package.json ]] && PROFILE_ARGS=(--profile frontend)

log "构建镜像 tag=$NEW_TAG（当前运行：${OLD_TAG:-无}）"
export TAG="$NEW_TAG"
compose build
compose up -d "${PROFILE_ARGS[@]}"

# 健康检查：最多 30 次 x 4s = 120s
for i in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    echo "$NEW_TAG" > "$STATE_FILE"
    log "部署成功：$NEW_TAG 已上线"
    exit 0
  fi
  sleep 4
done

log "健康检查失败，回退到 ${OLD_TAG:-<无历史版本>}"
compose logs --tail=50 backend || true
if [[ -n "$OLD_TAG" && "$OLD_TAG" != "$NEW_TAG" ]]; then
  export TAG="$OLD_TAG"
  compose up -d "${PROFILE_ARGS[@]}"
  curl -fsS "$HEALTH_URL" >/dev/null 2>&1 \
    && log "已回退到 $OLD_TAG" \
    || log "回退后健康检查仍未通过，请人工介入"
fi
exit 1
