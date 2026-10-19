#!/bin/bash
# 把 src/cfopt 同步到 fpk/app/src/cfopt，然后调用 fnpack 打包。
#
# 为什么要这个脚本：fpk 打包要求源码放在 fpk/app/src 下，但仓库里
# 只有一份源码（src/cfopt）。手动复制迟早会忘记，导致 fpk 里的代码
# 和 Docker 版的代码不一致。这个脚本保证每次打包都用最新源码。
#
# 用法：
#   ./build-fpk.sh                    # 用系统里的 fnpack
#   FNPACK=/path/to/fnpack ./build-fpk.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="${ROOT}/src/cfopt"
DEST="${ROOT}/fpk/app/src/cfopt"
FNPACK="${FNPACK:-fnpack}"

if [ ! -d "$SRC" ]; then
    echo "错误：找不到源码目录 $SRC" >&2
    exit 1
fi

if ! command -v "$FNPACK" >/dev/null 2>&1 && [ ! -x "$FNPACK" ]; then
    echo "错误：找不到 fnpack。请从 https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64 下载" >&2
    echo "     或设置 FNPACK=/path/to/fnpack 后重试" >&2
    exit 1
fi

echo "同步源码 $SRC -> $DEST"
rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -r "$SRC" "$DEST"
find "$DEST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name '*.pyc' -delete 2>/dev/null || true

echo "打包 fpk ..."
cd "${ROOT}/fpk"
"$FNPACK" build

echo "完成：${ROOT}/fpk/cfopt.fpk"
ls -lh "${ROOT}/fpk/cfopt.fpk"
