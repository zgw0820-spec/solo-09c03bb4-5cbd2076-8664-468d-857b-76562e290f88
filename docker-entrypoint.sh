#!/bin/sh
# 首次启动时把样例裸仓库种到数据卷，然后启动 Web 服务。
set -e
: "${REPO_PATH:=/srv/repo.git}"

# 隔离验包目录放在与正式库同一数据卷/文件系统上（TMPDIR），
# 这样对象迁入可以用硬链接复用，而不必写第二份 pack。
: "${TMPDIR:=/srv/tmp}"
mkdir -p "$TMPDIR"
export TMPDIR

if [ ! -d "$REPO_PATH" ]; then
  echo "初始化裸仓库 $REPO_PATH（来自 /samples/repo.git）"
  git clone --quiet --bare /samples/repo.git "$REPO_PATH"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
