#!/usr/bin/env bash
# 使い方: ./Tools/setup_env.sh
# .venv を作り、PySide6・requests・pytest・pyinstaller を入れる。
# ダウンロードするのは pip のパッケージだけ。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="Logs/setup_env.log"
{
    echo "=== setup_env $(date '+%Y-%m-%d %H:%M:%S') ==="
    if [ -z "$PY" ]; then
        SYSPY=""
        for c in python python3 py; do
            if command -v "$c" >/dev/null 2>&1; then SYSPY="$c"; break; fi
        done
        if [ -z "$SYSPY" ]; then
            echo "[エラー] Python が見つかりません。Python 3.11 以上を入れてください。"
            exit 2
        fi
        echo "仮想環境を作成: $SYSPY -m venv .venv"
        "$SYSPY" -m venv .venv || exit 1
        PY="$ROOT/.venv/Scripts/python.exe"
        [ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
    fi
    "$PY" --version
    "$PY" -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 以上が必要です'" || exit 1
    "$PY" -m pip install --upgrade pip --quiet || echo "[注意] pip の更新に失敗（続行）"
    "$PY" -m pip install -e ".[dev]" --quiet || exit 1
    "$PY" -m pip list 2>/dev/null | grep -i -E "^(pyside6|requests|pytest|pyinstaller|folderlink) "
    echo "=== setup_env 完了 ==="
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
