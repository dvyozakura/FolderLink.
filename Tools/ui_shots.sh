#!/usr/bin/env bash
# 使い方: ./Tools/ui_shots.sh [出力先(既定: Docs/ があれば Docs/screenshots、無ければ build/screenshots)]
# 各画面をオフスクリーンで描いて PNG に保存する（画面には何も出さない。エンジンも起動しない）。
# 等倍の全画面と、高 DPI（150%）の確認用の数枚を保存する。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_venv

DEFAULT_OUT="build/screenshots"
[ -d Docs ] && DEFAULT_OUT="Docs/screenshots"
OUT="${1:-$DEFAULT_OUT}"
export QT_QPA_PLATFORM=offscreen
# テストと同じく、設定の保存先を実物（%LOCALAPPDATA%\FolderLink）に向けない
export FOLDERLINK_DATA_DIR="$ROOT/_sandbox/ui_shots_data"
mkdir -p "$OUT" "$FOLDERLINK_DATA_DIR"
LOG="Logs/ui_shots.log"
{
    echo "=== ui_shots $(date '+%Y-%m-%d %H:%M:%S') ==="
    "$PY" -m folderlink.devtools.ui_shots --out "$OUT" || exit 1
    "$PY" -m folderlink.devtools.ui_shots --out "$OUT" --scale 1.5 --quick || exit 1
    echo "=== ui_shots 完了 ==="
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
