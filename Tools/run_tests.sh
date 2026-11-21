#!/usr/bin/env bash
# 使い方: ./Tools/run_tests.sh [pytest の引数]
# 単体テスト（HTTP・レジストリなどはモック）。画面はオフスクリーンで描く。
# 結合テスト（本物の Syncthing を起動）は ./Tools/run_integration.sh。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_venv

export QT_QPA_PLATFORM=offscreen
LOG="Logs/test_latest.log"
{
    echo "=== run_tests $(date '+%Y-%m-%d %H:%M:%S') args: $* ==="
    "$PY" -m pytest "$@"
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
