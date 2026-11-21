#!/usr/bin/env bash
# Tools/*.sh の共通部分。source して使う。
# - プロジェクトのルートへ移動する
# - .venv の python の場所を PY に入れる
# - Logs/ を用意する

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

mkdir -p Logs

if [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
    PY="$ROOT/.venv/Scripts/python.exe"
elif [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
else
    PY=""
fi

# 日本語の出力が文字化けしないように
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
# 作業フォルダに .pyc を散らかさない
export PYTHONDONTWRITEBYTECODE=1

need_venv() {
    if [ -z "$PY" ]; then
        echo "[エラー] .venv がありません。先に ./Tools/setup_env.sh を実行してください。" >&2
        exit 2
    fi
}

# 安全ルール: テストが使ってよい場所は _sandbox/ の下だけ
export FOLDERLINK_SANDBOX="$ROOT/_sandbox"
