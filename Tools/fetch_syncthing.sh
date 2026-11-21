#!/usr/bin/env bash
# 使い方: ./Tools/fetch_syncthing.sh [--force]
# 固定バージョンの Syncthing（Windows amd64）を GitHub の syncthing/syncthing 公式リリースから
# 取得し、sha256 を2通りに照合してから vendor/syncthing/ に置く。
#   (1) 同じリリースにある公式の sha256sum.txt.asc の該当行
#   (2) このスクリプトに書いてある固定値（EXPECTED_SHA256）
# どちらかが合わなければ、何も配置せずに失敗する。
#
# バージョンを上げるとき: ST_VERSION と EXPECTED_SHA256 を、公式リリースの新しい安定版の値に書き換える
# （README.md「Syncthing の差し替え方」も参照）。
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# ---- 固定値（ここを変えるときは SYNCTHING_NOTES.md も更新する） ----
ST_VERSION="v2.1.7"
ST_ARCH="windows-amd64"
EXPECTED_SHA256="3ce7da770f546a8d02b65b2e7ee31f2a3b8b7c3f990ede045590cca4172ecfe1"
# ----------------------------------------------------------------------

ZIP_NAME="syncthing-${ST_ARCH}-${ST_VERSION}.zip"
BASE_URL="https://github.com/syncthing/syncthing/releases/download/${ST_VERSION}"
DEST="$ROOT/vendor/syncthing"
DL="$DEST/_download"
LOG="Logs/fetch_syncthing.log"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

main() {
    echo "=== fetch_syncthing $(date '+%Y-%m-%d %H:%M:%S') ${ST_VERSION} ${ST_ARCH} ==="
    mkdir -p "$DEST" "$DL"

    if [ "$FORCE" = 0 ] && [ -f "$DEST/syncthing.exe" ] && [ -f "$DEST/VERSION.txt" ] \
       && grep -q "^version=${ST_VERSION}\$" "$DEST/VERSION.txt" \
       && grep -q "^zip_sha256=${EXPECTED_SHA256}\$" "$DEST/VERSION.txt"; then
        exe_now="$(sha256sum "$DEST/syncthing.exe" | cut -d' ' -f1)"
        if grep -q "^exe_sha256=${exe_now}\$" "$DEST/VERSION.txt"; then
            echo "既に配置済みです（exe のハッシュも記録と一致）。再取得するには --force。"
            show_version
            return 0
        fi
        echo "[注意] syncthing.exe のハッシュが記録と違います。取り直します。"
    fi

    echo "取得: $BASE_URL/$ZIP_NAME"
    curl -fsSL --retry 3 -o "$DL/$ZIP_NAME" "$BASE_URL/$ZIP_NAME" || { echo "[エラー] ZIP の取得に失敗"; return 1; }
    curl -fsSL --retry 3 -o "$DL/sha256sum.txt.asc" "$BASE_URL/sha256sum.txt.asc" || { echo "[エラー] sha256sum.txt.asc の取得に失敗"; return 1; }

    actual="$(sha256sum "$DL/$ZIP_NAME" | cut -d' ' -f1)"
    official="$(tr -d '\r' < "$DL/sha256sum.txt.asc" | grep -E "^[0-9a-f]{64}[ *]+${ZIP_NAME}\$" | head -n 1 | cut -d' ' -f1)"
    echo "実際の sha256        : $actual"
    echo "公式 sha256sum の値  : ${official:-（該当行なし）}"
    echo "スクリプトの固定値   : $EXPECTED_SHA256"

    if [ -z "$official" ]; then
        echo "[エラー] 公式の sha256sum.txt.asc に $ZIP_NAME の行がありません。"; return 1
    fi
    if [ "$actual" != "$official" ]; then
        echo "[エラー] 公式の sha256sum と一致しません。配置しません。"; return 1
    fi
    if [ "$actual" != "$EXPECTED_SHA256" ]; then
        echo "[エラー] スクリプトの固定値と一致しません。配置しません。"
        echo "  （バージョンを上げた直後なら、公式の値を確かめてから EXPECTED_SHA256 を更新する）"; return 1
    fi
    echo "[OK] sha256 一致（公式 sha256sum・固定値の両方）"

    # PGP 署名（sha256sum.txt.asc は Syncthing Release Management の鍵で署名されている）
    PGP_STATE="未検証（gpg が無い、または公開鍵が鍵束に無い）"
    if grep -q "BEGIN PGP SIGNED MESSAGE" "$DL/sha256sum.txt.asc" && command -v gpg >/dev/null 2>&1; then
        if gpg --verify "$DL/sha256sum.txt.asc" >/dev/null 2>&1; then
            PGP_STATE="検証済み（gpg --verify 成功）"
        fi
    fi
    echo "PGP 署名: $PGP_STATE"

    # 展開は専用の空フォルダへ。中身は実行ファイルとして信頼する前にハッシュ照合済み。
    EX="$DL/extract"
    if [ -d "$EX" ]; then
        find "$EX" -mindepth 1 -delete 2>/dev/null
    fi
    mkdir -p "$EX"
    if command -v unzip >/dev/null 2>&1; then
        unzip -q -o "$DL/$ZIP_NAME" -d "$EX" || { echo "[エラー] 展開に失敗"; return 1; }
    elif [ -n "$PY" ]; then
        "$PY" -I -m zipfile -e "$DL/$ZIP_NAME" "$EX" || { echo "[エラー] 展開に失敗"; return 1; }
    else
        echo "[エラー] unzip も .venv の python もありません。"; return 1
    fi
    SRC="$EX/syncthing-${ST_ARCH}-${ST_VERSION}"
    if [ ! -f "$SRC/syncthing.exe" ]; then
        echo "[エラー] ZIP の中に syncthing.exe が見つかりません: $SRC"; ls -la "$EX"; return 1
    fi
    cp -f "$SRC/syncthing.exe" "$DEST/syncthing.exe" || return 1
    for f in LICENSE.txt AUTHORS.txt README.txt; do
        [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$DEST/$f"
    done
    {
        echo "version=${ST_VERSION}"
        echo "arch=${ST_ARCH}"
        echo "zip=${ZIP_NAME}"
        echo "zip_sha256=${actual}"
        echo "exe_sha256=$(sha256sum "$DEST/syncthing.exe" | cut -d' ' -f1)"
        echo "source=${BASE_URL}/${ZIP_NAME}"
        echo "pgp=${PGP_STATE}"
        echo "fetched=$(date '+%Y-%m-%d %H:%M:%S')"
    } > "$DEST/VERSION.txt"
    echo "配置: $DEST"
    ls -la "$DEST"
    show_version
}

show_version() {
    # --version / --help は設定を読み書きしないが、念のためホームを _sandbox/ に向けておく
    # （%LOCALAPPDATA%\Syncthing の実物に絶対に触れないため）。
    mkdir -p "$ROOT/_sandbox/st_probe_home"
    export STHOMEDIR="$ROOT/_sandbox/st_probe_home"
    export STNOUPGRADE=1
    echo "--- syncthing --version ---"
    out="$("$DEST/syncthing.exe" --version 2>&1)" || { echo "$out"; echo "[エラー] syncthing --version が失敗"; return 1; }
    echo "$out"
    case "$out" in
        *"syncthing ${ST_VERSION} "*) echo "[OK] バージョン一致: ${ST_VERSION}" ;;
        *) echo "[エラー] バージョンが想定（${ST_VERSION}）と違います"; return 1 ;;
    esac
    # コマンドラインオプションの控え（v1 と名前が違うものがあるので、実物の出力を正とする）
    # 開発用の記録フォルダ（Docs/）があればそこへ、無ければ vendor/syncthing/ へ保存する
    local help_out="$ROOT/vendor/syncthing/cli_help.txt"
    [ -d "$ROOT/Docs" ] && help_out="$ROOT/Docs/syncthing_cli_help.txt"
    {
        echo "# syncthing ${ST_VERSION} のヘルプ出力（fetch_syncthing.sh が自動で保存）"
        for sub in "" serve generate device-id; do
            echo
            echo "## syncthing ${sub} --help"
            # shellcheck disable=SC2086
            "$DEST/syncthing.exe" $sub --help 2>&1
        done
    } > "$help_out"
    echo "ヘルプ出力を ${help_out#$ROOT/} に保存しました（新しい版にしたとき、オプションが変わっていないかを比べる用）。"
}

main 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
