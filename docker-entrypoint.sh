#!/bin/sh
# 首次启动时把样例裸仓库种到数据卷，然后启动 Web 服务。
set -e
: "${REPO_PATH:=/srv/repo.git}"

if [ ! -d "$REPO_PATH" ]; then
  echo "初始化裸仓库 $REPO_PATH（来自 /samples/repo.git）"
  git clone --quiet --bare /samples/repo.git "$REPO_PATH"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
