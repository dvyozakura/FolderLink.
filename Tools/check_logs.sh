#!/usr/bin/env bash
# 使い方: ./Tools/check_logs.sh [行数(既定 40)]
# 直近のテスト・ビルドのログ（Logs/*.log）からエラーらしい行を抜き出す。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

N="${1:-40}"
found=0
for f in Logs/setup_env.log Logs/test_latest.log Logs/integration_latest.log \
         Logs/ui_shots.log Logs/build_exe.log Logs/build_installer.log Logs/fetch_syncthing.log; do
    [ -f "$f" ] || continue
    hits="$(grep -n -i -E "error|エラー|failed|traceback|exception|fatal|\[NG\]" "$f" \
            | grep -v -i -E "0 failed|0 errors|no error" | tail -n "$N")"
    if [ -n "$hits" ]; then
        found=1
        echo "----- $f -----"
        echo "$hits"
    fi
    echo "[$f] 最終行: $(tail -n 1 "$f")"
done
[ "$found" = 0 ] && echo "エラー行は見つかりませんでした。"
exit 0
