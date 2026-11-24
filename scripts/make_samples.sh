#!/usr/bin/env bash
# 生成样例：一个裸仓库 + 若干相互依赖的 git bundle。
#
# 提交图（同一文件 a.txt 上的主线历史，feature 从 C2 岔出）：
#
#   C1 -- C2 -- C3 -- C4 -- C5        main 方向
#    \     \           \      \
#     \     \           \      \-- C6 -- C7   （C6 不随任何包发布，用于演示缺包）
#      \     \
#       \     \-- F1 -- F2            feature 方向
#        \
#         stable 指向 C1
#
# 产物：
#   samples/repo.git                          裸仓库：main=C2, stable=C1
#   samples/bundles/01-*.bundle               main→C4、feature→F1、标签 v1.0（前置 C2，仓库里已有）
#   samples/bundles/02-*.bundle               main→C5（前置 C4，由 01 提供）
#   samples/bundles/03-*.bundle               feature→F2（前置 F1，由 01 提供）
#   samples/bad/missing-prereq-*.bundle       main→C7（前置 C6，没有任何包提供 → 验包失败）
#   samples/bad/diverged-main-*.bundle        main→ALT（与 C4/C5 不在同一条链 → 分叉被拒）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLES="$ROOT/samples"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

rm -rf "$SAMPLES"
mkdir -p "$SAMPLES/bundles" "$SAMPLES/bad"

git init -q -b main "$WORK/repo"
cd "$WORK/repo"
git config user.email sample@example.com
git config user.name  "Sample Bot"
git config commit.gpgsign false
# 固定时间戳，产物可复现
export GIT_AUTHOR_DATE='2024-01-01T00:00:00+00:00'
export GIT_COMMITTER_DATE='2024-01-01T00:00:00+00:00'

commit() { # commit <msg> <lines-to-append...>
  local msg="$1"; shift
  local line
  for line in "$@"; do echo "$line" >> a.txt; done
  git add a.txt
  GIT_AUTHOR_DATE="${GIT_AUTHOR_DATE%+*}+00:00" git commit -q -m "$msg"
  git rev-parse HEAD
}

: > a.txt
C1=$(commit C1 one)
C2=$(commit C2 two)

# 此刻仓库状态即部署仓库的初始状态：main=C2，另有 stable=C1（演示无关分支不受影响）
git branch stable "$C1"
git clone -q --bare "$WORK/repo" "$SAMPLES/repo.git"

C3=$(commit C3 three)
C4=$(commit C4 four)

git checkout -q -b feature "$C2"
echo feat1 > f.txt
git add f.txt
git commit -q -m F1
F1=$(git rev-parse HEAD)
git tag -a -m "release 1.0" v1.0 "$C3"

# 包 01：main 推进到 C4、新建 feature=F1，并附带一个标签（应被导入器忽略）
git bundle create "$SAMPLES/bundles/01-main-c4-feature-f1.bundle" \
  "$C2..main" "$C2..feature" v1.0

git checkout -q main
C5=$(commit C5 five)
# 包 02：main 推进到 C5，前置 C4（由包 01 提供）
git bundle create "$SAMPLES/bundles/02-main-c5.bundle" "$C4..main"

git checkout -q feature
echo feat2 >> f.txt
git add f.txt
git commit -q -m F2
F2=$(git rev-parse HEAD)
# 包 03：feature 推进到 F2，前置 F1（由包 01 提供）
git bundle create "$SAMPLES/bundles/03-feature-f2.bundle" "$F1..feature"

git checkout -q main
C6=$(commit C6 six)
C7=$(commit C7 seven)
# 坏包 1：前置 C6 不在仓库、也不在任何已发布的包里
git bundle create "$SAMPLES/bad/missing-prereq-main-c7.bundle" "$C6..main"

git checkout -q -b altline "$C2"
echo alt >> a.txt
git add a.txt
git commit -q -m ALT
ALT=$(git rev-parse HEAD)
# 坏包 2：把分叉提交装进名为 main 的分支（与包 01/02 的 main 不在同一条快进链上）
git branch -f main "$ALT"
git bundle create "$SAMPLES/bad/diverged-main.bundle" "$C2..main"

cat > "$SAMPLES/README.txt" <<EOF
样例内容
========
repo.git/                       裸仓库：main=$C2，stable=$C1
bundles/01-main-c4-feature-f1   main->$C4, feature->$F1, 标签 v1.0（前置 $C2，仓库已有）
bundles/02-main-c5              main->$C5（前置 $C4，由 01 提供）
bundles/03-feature-f2           feature->$F2（前置 $F1，由 01 提供）
bad/missing-prereq-main-c7      main->$C7（前置 $C6，缺失 → 验包失败并指出该提交）
bad/diverged-main               main->$ALT（与 01/02 的 main 分叉 → 整批退回）

正常顺序：01 -> 02 -> 03（可故意乱序上传验证自动排序）。
导入 01+02+03 后：main=$C5，feature=$F2，stable 不变。
EOF

echo "== samples 生成完毕 =="
git -C "$SAMPLES/repo.git" for-each-ref
ls -1 "$SAMPLES/bundles" "$SAMPLES/bad"
