#!/bin/bash
# NAS 端更新脚本（由 deploy-nas.command 上传，sudo 运行）
# 用法: sudo bash ~/panwatch-remote-update.sh <版本标签>
# 构建新镜像 → 替换容器 → 健康检查；失败自动回滚到旧镜像。数据卷永不触碰。
set -u
TAG="${1:-}"
LOG=/home/jinghui.hu/panwatch-build.log
RESULT=/home/jinghui.hu/panwatch-update.result
SRC=/home/jinghui.hu/panwatch-build
DATA=/volume1/docker/panwatch/data
IMAGE="panwatch-local:$TAG"
rm -f "$RESULT"
exec > >(tee "$LOG") 2>&1
echo "=== PanWatch update $TAG $(date '+%F %T') ==="
finish() { echo "$1" > "$RESULT"; chmod 644 "$RESULT"; }

if [ "$(id -u)" != "0" ]; then echo "FAIL: 需要 sudo"; finish "FAIL not-root"; exit 1; fi
if [ -z "$TAG" ]; then echo "FAIL: 缺少版本标签"; finish "FAIL no-tag"; exit 1; fi
if [ ! -d "$DATA" ]; then echo "FAIL: 数据目录 $DATA 不存在，请先完成首次部署"; finish "FAIL no-data-dir"; exit 1; fi

cd "$SRC" || { finish "FAIL no-source"; exit 1; }
echo "--- 构建镜像 $IMAGE ---"
if ! timeout 2400 docker build --build-arg VERSION="$TAG" -t "$IMAGE" .; then
  echo "=== BUILD FAILED $(date '+%F %T') ==="
  finish "FAIL build（容器和数据未受影响，旧版本仍在运行）"
  exit 1
fi

OLD_IMAGE=""
if docker ps -a --format '{{.Names}}' | grep -qx panwatch; then
  OLD_IMAGE="$(docker inspect panwatch --format '{{.Config.Image}}')"
  echo "--- 停止旧容器（当前镜像 $OLD_IMAGE）---"
  docker stop panwatch >/dev/null
  docker rm panwatch >/dev/null
fi

start_container() {
  docker run -d --name panwatch \
    --restart unless-stopped \
    -e TZ=Asia/Shanghai \
    -p 8000:8000 \
    -v "$DATA":/app/data \
    "$1" >/dev/null
}
wait_health() {
  local tries="$1" i code
  for i in $(seq 1 "$tries"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:8000/api/health || true)"
    [ "$code" = "200" ] && return 0
    sleep 3
  done
  return 1
}

echo "--- 启动新版本 ---"
if ! start_container "$IMAGE"; then
  echo "FAIL: docker run 失败"
  if [ -n "$OLD_IMAGE" ]; then start_container "$OLD_IMAGE"; fi
  finish "FAIL run"
  exit 1
fi
if wait_health 60; then
  echo "=== UPDATE OK $TAG $(date '+%F %T') ==="
  finish "OK $TAG"
  docker logs --tail 10 panwatch 2>&1
  exit 0
fi

echo "--- 新版本健康检查失败，最近日志 ---"
docker logs --tail 30 panwatch 2>&1
if [ -n "$OLD_IMAGE" ] && [ "$OLD_IMAGE" != "$IMAGE" ]; then
  echo "--- 自动回滚到 $OLD_IMAGE ---"
  docker stop panwatch >/dev/null 2>&1; docker rm panwatch >/dev/null 2>&1
  if start_container "$OLD_IMAGE" && wait_health 40; then
    echo "=== ROLLED BACK $(date '+%F %T') ==="
    finish "FAIL new-version（已回滚到 $OLD_IMAGE，服务正常）"
    exit 1
  fi
fi
finish "FAIL health（请人工检查 docker logs panwatch）"
exit 1