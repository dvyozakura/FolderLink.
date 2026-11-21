#!/usr/bin/env bash
# 使い方: ./Tools/build_installer.sh
# Inno Setup (ISCC.exe) があれば installer/FolderLink.iss からセットアップを作る。
# 無ければその旨を出力して終了する。作ったセットアップは実行しない。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="Logs/build_installer.log"
{
    echo "=== build_installer $(date '+%Y-%m-%d %H:%M:%S') ==="
    ISCC=""
    if command -v ISCC >/dev/null 2>&1; then
        ISCC="$(command -v ISCC)"
    else
        for c in "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
                 "/c/Program Files/Inno Setup 6/ISCC.exe" \
                 "$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe"; do
            if [ -f "$c" ]; then ISCC="$c"; break; fi
        done
    fi
    if [ -z "$ISCC" ]; then
        echo "[スキップ] Inno Setup (ISCC.exe) が見つかりません。"
        echo "  入れ方: https://jrsoftware.org/isdl.php から Inno Setup 6 を入れてから、もう一度実行してください。"
        exit 4
    fi
    if [ ! -f installer/FolderLink.iss ]; then
        echo "[未実装] installer/FolderLink.iss がまだありません（項目13で作成）。"
        exit 3
    fi
    if [ ! -f dist/FolderLink/FolderLink.exe ]; then
        echo "[エラー] dist/FolderLink/FolderLink.exe がありません。先に ./Tools/build_exe.sh を実行してください。"
        exit 2
    fi
    echo "ISCC: $ISCC"
    "$ISCC" installer/FolderLink.iss || exit 1
    ls -la installer/Output/
    echo "=== build_installer 完了（作成したセットアップは実行していません） ==="
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
