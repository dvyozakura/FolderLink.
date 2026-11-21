#!/usr/bin/env bash
# 使い方: ./Tools/run_integration.sh [pytest の引数]
# 1台の中で2つの Syncthing（別ホーム・別ポート・_sandbox/ 内）を立てる結合テスト。
# - 同期するフォルダもホームも _sandbox/ の下だけ
# - 既定ポート（8384/22000）は使わず、空きポートを自動で選ぶ
# - 終了時に、自分が起動したプロセスだけを止める
# これは「1台の中での確認」。実機2台・別ネットワークの受入テストの代わりではない。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_venv

if [ ! -f "$ROOT/vendor/syncthing/syncthing.exe" ]; then
    echo "[エラー] vendor/syncthing/syncthing.exe がありません。先に ./Tools/fetch_syncthing.sh を実行してください。" >&2
    exit 2
fi
if [ ! -d "$ROOT/tests/integration" ]; then
    echo "[未実装] tests/integration/ がまだありません（項目8で作成）。"
    exit 3
fi

export QT_QPA_PLATFORM=offscreen
LOG="Logs/integration_latest.log"
{
    echo "=== run_integration $(date '+%Y-%m-%d %H:%M:%S') args: $* ==="
    "$PY" -m pytest tests/integration -o addopts="" -m integration -v -rA "$@"
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
