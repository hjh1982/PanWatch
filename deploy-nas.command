#!/bin/bash
# PanWatch 一键部署到 NAS（双击运行）
# 流程：打包本机代码 → 传到 NAS → 构建新镜像 → 替换容器（数据不动，失败自动回滚）
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAS="jinghui.hu@192.168.18.29"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o ControlMaster=auto -o ControlPath=/tmp/panwatch-nas-ssh-%C -o ControlPersist=120)
PREPARE_ONLY=0
[ "${1:-}" = "--prepare-only" ] && PREPARE_ONLY=1

cd "$PROJECT_DIR"
fail() { echo ""; echo "❌ 部署中止：$1"; echo "按任意键关闭窗口..."; read -r -n 1; exit 1; }

echo "▶ [1/5] 检查 NAS 连接..."
ssh "${SSH_OPTS[@]}" "$NAS" 'echo ok' >/dev/null 2>&1 || fail "连不上 NAS（192.168.18.29），检查 NAS 是否开机、SSH 是否开启"

echo "▶ [2/5] 打包代码..."
HASH="$(git rev-parse --short HEAD)"
DIRTY="$(git status --porcelain | grep -v '^?? .DS_Store$' || true)"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP_TAR="$(mktemp /tmp/panwatch-deploy.XXXXXX.tar.gz)"
trap 'rm -f "$TMP_TAR"' EXIT
if [ -z "$DIRTY" ]; then
  TAG="$HASH"
  git archive --format=tar.gz --prefix=panwatch/ HEAD -o "$TMP_TAR"
  echo "  部署已提交版本：$TAG"
else
  TAG="${HASH}-dirty-${STAMP}"
  echo "  ⚠️ 有未提交改动，将部署当前工作区代码：$TAG"
  tar --format ustar -czf "$TMP_TAR" \
    --exclude='./.git' --exclude='./venv' --exclude='./.venv' \
    --exclude='./data' --exclude='./node_modules' --exclude='*/node_modules' \
    --exclude='./frontend/dist' --exclude='./static' \
    --exclude='*/__pycache__' --exclude='*.pyc' --exclude='*/.pytest_cache' \
    --exclude='*.egg-info' --exclude='.DS_Store' --exclude='*.log' \
    --exclude='.env' --exclude='.env.*' --exclude='*.db' --exclude='*.sqlite' \
    --exclude='./.qoder' --exclude='./.claude' --exclude='./.docs' \
    -s '|^\./|panwatch/|' -C "$PROJECT_DIR" .
fi

echo "▶ [3/5] 传输到 NAS 并校验..."
cat "$TMP_TAR" | ssh "${SSH_OPTS[@]}" "$NAS" 'cat > ~/panwatch-migrate/panwatch-src.tar.gz'
LOCAL_MD5="$(md5 -q "$TMP_TAR")"
REMOTE_MD5="$(ssh "${SSH_OPTS[@]}" "$NAS" 'md5sum ~/panwatch-migrate/panwatch-src.tar.gz | cut -d" " -f1')"
[ "$LOCAL_MD5" = "$REMOTE_MD5" ] || fail "传输校验不一致，请重试"
echo "  MD5 一致：$LOCAL_MD5"

echo "▶ [4/5] 在 NAS 准备构建环境..."
ssh "${SSH_OPTS[@]}" "$NAS" 'rm -rf ~/panwatch-build && mkdir -p ~/panwatch-build && tar -xzf ~/panwatch-migrate/panwatch-src.tar.gz -C ~/panwatch-build --strip-components=1'
cat "$PROJECT_DIR/scripts/nas/patch-dockerfile.py" | ssh "${SSH_OPTS[@]}" "$NAS" 'python3 -'
cat "$PROJECT_DIR/scripts/nas/remote-update.sh" | ssh "${SSH_OPTS[@]}" "$NAS" 'cat > ~/panwatch-remote-update.sh'

if [ "$PREPARE_ONLY" = "1" ]; then
  echo ""
  echo "✅ 准备完成（--prepare-only：未触发构建）。标签：$TAG"
  exit 0
fi

echo "▶ [5/5] 构建并更新容器（约 3-15 分钟，需要输入一次 NAS 密码）..."
echo "  提示：输入密码时屏幕不显示字符，输完回车即可"
ssh -t -o ControlPath=/tmp/panwatch-nas-ssh-%C "$NAS" "sudo bash ~/panwatch-remote-update.sh '$TAG'" || fail "NAS 端更新未成功，详见上方输出（数据不受影响）"

echo ""
echo "✅ 部署完成：http://192.168.18.29:8000（版本 $TAG）"
open "http://192.168.18.29:8000" 2>/dev/null || true
echo "按任意键关闭窗口..."
read -r -n 1