#!/usr/bin/env bash
# 使い方: ./Tools/build_exe.sh [--no-smoke]
# PyInstaller で dist/FolderLink/FolderLink.exe を作り、Syncthing（ライセンス文書つき）を
# dist/FolderLink/syncthing/ に入れる。最後に、画面を出さないスモークテスト
# （_sandbox/ の中の設定で、エンジンを起動→本人確認→停止）を行う。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
need_venv

SMOKE=1
[ "${1:-}" = "--no-smoke" ] && SMOKE=0

LOG="Logs/build_exe.log"
{
    echo "=== build_exe $(date '+%Y-%m-%d %H:%M:%S') ==="
    if [ ! -f vendor/syncthing/syncthing.exe ]; then
        echo "[エラー] vendor/syncthing/syncthing.exe がありません。先に ./Tools/fetch_syncthing.sh を実行してください。"
        exit 2
    fi

    echo "--- アイコン ---"
    QT_QPA_PLATFORM=offscreen PYTHONPATH="$ROOT/src" "$PY" -m folderlink.devtools.make_icon build/FolderLink.ico || exit 1

    echo "--- PyInstaller ---"
    "$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
        --name FolderLink --windowed \
        --icon "$ROOT/build/FolderLink.ico" \
        --paths "$ROOT/src" \
        --distpath dist --workpath build/pyinstaller --specpath build \
        --exclude-module tkinter --exclude-module pytest \
        --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets \
        --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.Qt3DCore \
        --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtCharts \
        --exclude-module PySide6.QtDataVisualization --exclude-module PySide6.QtPdf \
        --version-file "$ROOT/scripts/version_info.txt" \
        "$ROOT/src/folderlink/__main__.py" || exit 1

    echo "--- Syncthing を同梱（改変しない。ライセンス文書つき） ---"
    mkdir -p dist/FolderLink/syncthing
    for f in syncthing.exe LICENSE.txt AUTHORS.txt README.txt VERSION.txt; do
        if [ -f "vendor/syncthing/$f" ]; then cp "vendor/syncthing/$f" dist/FolderLink/syncthing/; fi
    done
    cp README.md dist/FolderLink/README.md 2>/dev/null
    [ -f LICENSE ] && cp LICENSE dist/FolderLink/LICENSE.txt
    [ -f THIRD_PARTY_NOTICES.md ] && cp THIRD_PARTY_NOTICES.md dist/FolderLink/THIRD_PARTY_NOTICES.md
    echo "--- 同梱するパッケージのライセンス文を licenses/ に集める ---"
    PYTHONPATH="$ROOT/src" "$PY" -m folderlink.devtools.collect_licenses dist/FolderLink/licenses || exit 1
    ls -la dist/FolderLink | head -20
    du -sh dist/FolderLink

    if [ "$SMOKE" = "1" ]; then
        echo "--- スモークテスト（画面は出さない。設定とエンジンのホームは _sandbox/ の中） ---"
        SMOKE_DIR="$ROOT/_sandbox/exe_smoke"
        rm -rf "$SMOKE_DIR"
        mkdir -p "$SMOKE_DIR"
        export QT_QPA_PLATFORM=offscreen
        start=$(date +%s)
        ./dist/FolderLink/FolderLink.exe --smoke-test --isolated --data-dir "$(cygpath -w "$SMOKE_DIR/data" 2>/dev/null || echo "$SMOKE_DIR/data")"
        code=$?
        echo "終了コード: $code（$(( $(date +%s) - start )) 秒）"
        if [ -f "$SMOKE_DIR/data/logs/folderlink.log" ]; then
            echo "--- アプリのログ（末尾） ---"
            tail -n 15 "$SMOKE_DIR/data/logs/folderlink.log"
        fi
        if tasklist //FI "IMAGENAME eq syncthing.exe" 2>/dev/null | grep -qi "syncthing.exe"; then
            echo "[注意] syncthing.exe のプロセスがあります（テスト前から動いていたものかもしれないので、触りません）。"
        fi
        if [ "$code" != "0" ]; then
            echo "[失敗] スモークテストが失敗しました。"
            exit 1
        fi
        echo "[成功] EXE がエンジンを起動→本人確認→停止できました。"
    fi
    echo "=== build_exe 完了: dist/FolderLink/FolderLink.exe ==="
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
