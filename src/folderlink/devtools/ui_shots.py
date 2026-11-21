"""各画面をオフスクリーンで描いて PNG に保存する（画面には何も出さない。エンジンも起動しない）。

使い方: ./Tools/ui_shots.sh   （= python -m folderlink.devtools.ui_shots --out <出力先>）
表示するデータは見本（ダミー）。実際の同期は行わない。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SAMPLE_PEER = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"
SAMPLE_SELF = "MFZWI3D-BONSGYC-YLTMRWG-C43ENR5-QXGZDMM-FZWI3DP-BONSGYY-LTMRWAD"
NOW = datetime(2026, 11, 21, 12, 34, 56, tzinfo=timezone(timedelta(hours=9)))


def sample_settings(configured: bool = True):
    from folderlink.core.config_store import Settings

    if not configured:
        return Settings()
    return Settings(setup_completed=True, folder_path=r"C:\Users\taro\Desktop\共有フォルダ",
                    folder_id="fl-abcde-fghij", peer_device_id=SAMPLE_PEER, peer_name="PC-B",
                    ignore_presets=["unity", "os_junk"])


def sample_snapshots() -> dict:
    from folderlink.core.sync_status import StatusSnapshot, SyncState

    base = dict(checked_at=NOW, global_files=248, global_bytes=1_932_735_283, local_files=248,
                local_bytes=1_932_735_283, peer_connected=True, peer_via_relay=False, conflicts=0,
                last_sync=NOW - timedelta(minutes=3), in_bps=0.0, out_bps=0.0, need_items=0, need_bytes=0)

    def snap(state, message, **over):
        data = dict(base)
        data.update(over)
        return StatusSnapshot(state=state, message=message, **data)

    return {
        "synced": snap(SyncState.SYNCED, "すべて同期されています。", completion=100.0),
        "syncing": snap(SyncState.SYNCING, "同期しています。", completion=62.4, need_items=37,
                        need_bytes=726_000_000, in_bps=8_650_000.0, out_bps=120_000.0, local_files=211),
        "offline": snap(SyncState.OFFLINE, "相手のPCに接続できていません。接続できしだい続きを同期します。",
                        completion=None, peer_connected=False, peer_via_relay=None, need_items=3),
        "paused": snap(SyncState.PAUSED, "同期を一時停止しています。", completion=None, need_items=None,
                       need_bytes=None, in_bps=None, out_bps=None, global_files=None, global_bytes=None),
        "conflict": snap(SyncState.CONFLICT, "競合したファイルが 2 件あります。履歴から確認してください。",
                         completion=100.0, conflicts=2),
        "error": snap(SyncState.ERROR, "ディスクの空き容量が足りません。", completion=41.0, need_items=120,
                      need_bytes=3_200_000_000),
        "relay": snap(SyncState.SYNCING, "同期しています。", completion=12.0, need_items=180,
                      need_bytes=1_700_000_000, in_bps=310_000.0, out_bps=4_000.0, peer_via_relay=True),
        "starting": StatusSnapshot(SyncState.STARTING, "同期エンジンを起動しています…", NOW),
    }


def load_offscreen_fonts() -> list[str]:
    """オフスクリーン描画は Windows のフォントを自動では読まない（文字が□になる）ので、
    画面で使うフォントのファイルを読み込む（読むだけ）。読み込めた書体名を返す。"""
    from PySide6.QtGui import QFontDatabase

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    loaded: list[str] = []
    for name in ("YuGothR.ttc", "YuGothM.ttc", "YuGothB.ttc", "meiryo.ttc", "meiryob.ttc",
                 "segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "consola.ttf", "msgothic.ttc"):
        path = fonts_dir / name
        if path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id >= 0:
                loaded.extend(QFontDatabase.applicationFontFamilies(font_id))
    return sorted(set(loaded))


def _save(widget, path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    widget.ensurePolished()
    for _ in range(3):
        QApplication.processEvents()
    pixmap = widget.grab()
    if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"保存できませんでした: {path}")
    print(f"  {path.name}  {pixmap.width()}x{pixmap.height()}")


def shoot_dashboard(app, out: Path, theme_name: str, suffix: str, only: list[str] | None = None) -> None:
    from folderlink.ui import theme
    from folderlink.ui.main_window import MainWindow

    palette = theme.apply_theme(app, theme_name)
    snaps = sample_snapshots()
    for name, snap in snaps.items():
        if only and name not in only:
            continue
        window = MainWindow(sample_settings(), None, palette)
        window.resize(820, 680)
        window.show()
        window.apply_snapshot(snap)
        _save(window, out / f"dashboard_{name}_{theme_name}{suffix}.png")
        window.close()
        window.deleteLater()
    if not only:
        from folderlink.core.sync_status import StatusSnapshot, SyncState

        window = MainWindow(sample_settings(configured=False), None, palette)
        window.resize(820, 680)
        window.show()
        window.apply_snapshot(StatusSnapshot(SyncState.NOT_CONFIGURED, "初期設定がまだ終わっていません。", NOW))
        _save(window, out / f"dashboard_not_configured_{theme_name}{suffix}.png")
        window.close()
        # 最小の幅（720px）でも崩れないか
        window = MainWindow(sample_settings(), None, palette)
        window.resize(720, 560)
        window.show()
        window.apply_snapshot(snaps["syncing"])
        _save(window, out / f"dashboard_syncing_min_{theme_name}{suffix}.png")
        window.close()


def shoot_pages(app, out: Path, theme_name: str, suffix: str) -> None:
    """ダッシュボード以外のページ（項目10・11で追加）。"""
    try:
        from folderlink.devtools import ui_shots_pages
    except ImportError:
        return
    ui_shots_pages.shoot(app, out, theme_name, suffix)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="build/screenshots")
    parser.add_argument("--scale", default="1")
    parser.add_argument("--quick", action="store_true", help="高 DPI 確認用に、少数の画面だけ")
    args = parser.parse_args(argv)
    if args.scale != "1":
        os.environ["QT_SCALE_FACTOR"] = args.scale
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(["ui_shots"])
    suffix = "" if args.scale == "1" else f"_x{args.scale.replace('.', '')}"
    families = load_offscreen_fonts()
    print(f"保存先: {out}（scale={args.scale}）")
    print(f"読み込んだフォント: {', '.join(families) or '（なし。文字が□になります）'}")
    if args.quick:
        shoot_dashboard(app, out, "light", suffix, only=["syncing", "error"])
    else:
        shoot_dashboard(app, out, "light", suffix)
        shoot_dashboard(app, out, "dark", suffix, only=["synced", "syncing", "error", "paused"])
        shoot_pages(app, out, "light", suffix)
        shoot_pages(app, out, "dark", suffix)
    print("完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
