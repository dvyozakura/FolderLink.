"""ダッシュボード以外のページの見本画像（初期設定の6ステップ、接続デバイス、設定、ログ、履歴）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from folderlink.devtools.ui_shots import NOW, SAMPLE_PEER, SAMPLE_SELF, _save, sample_settings, sample_snapshots


def _window(app, theme_name: str, configured: bool = True, tmp: Path | None = None):
    from folderlink.app import install_pages
    from folderlink.core.autostart import AutoStart, MemoryRegistry
    from folderlink.ui import theme
    from folderlink.ui.main_window import MainWindow

    palette = theme.apply_theme(app, theme_name)
    settings = sample_settings(configured)
    window = MainWindow(settings, None, palette)
    service = SimpleNamespace(settings=settings)
    store = SimpleNamespace(log_dir=tmp)
    # 自動起動は偽のレジストリで表示する（本物には触れない）
    autostart = AutoStart(MemoryRegistry(), r"C:\Program Files\FolderLink\FolderLink.exe", frozen=True)
    pages = install_pages(window, None, service, store, palette, autostart=autostart,
                          confirm=lambda *_a: False, choose_folder=lambda *_a: "")
    window.resize(820, 700)
    window.show()
    return window, pages


def shoot(app, out: Path, theme_name: str, suffix: str) -> None:
    from folderlink.core.engine_service import PreviewInfo
    from folderlink.core.pairing import PeerState, PendingDevice

    tag = f"{theme_name}{suffix}"
    full = theme_name == "light"

    # ---- 初期設定 ----
    window, pages = _window(app, theme_name, configured=False)
    wizard = pages["setup"]
    wizard.set_my_id(SAMPLE_SELF)
    window.show_page("setup")
    wizard.folder_edit.setText(r"C:\Users\taro\Desktop\共有フォルダ")
    wizard.preset_checks["unity"].setChecked(True)
    wizard.preset_checks["os_junk"].setChecked(True)
    _save(window, out / f"wizard_1_folder_{tag}.png")
    if full:
        wizard._show_step(1)
        wizard._on_copy()
        _save(window, out / f"wizard_2_my_id_{tag}.png")
        wizard._show_step(2)
        wizard.peer_edit.setPlainText(SAMPLE_PEER[:-1] + "A")  # わざと打ち間違えた ID
        _save(window, out / f"wizard_3_peer_id_error_{tag}.png")
        wizard.peer_edit.setPlainText(SAMPLE_PEER)
        wizard.peer_name_edit.setText("PC-B")
        wizard._show_pending([PendingDevice(SAMPLE_PEER, "PC-B", "203.0.113.5:22000", "2026-11-21T03:00:00Z")])
        _save(window, out / f"wizard_3_peer_id_{tag}.png")
        wizard._show_step(3)
        wizard._peer_registered = True
        wizard.approve_local.setText("登録しました")
        wizard._apply_peer_state(PeerState(True, True, False, "203.0.113.5:22000", False, "v2.1.7"))
        _save(window, out / f"wizard_4_approve_{tag}.png")
    wizard.peer_edit.setPlainText(SAMPLE_PEER)
    wizard.peer_name_edit.setText("PC-B")
    wizard._show_step(4)
    wizard.apply_preview(PreviewInfo(local_files=248, local_bytes=1_932_735_283, global_files=310,
                                     peer_connected=True, remote_ready=True, receive_items=62,
                                     receive_bytes=420_000_000, send_items=255, send_bytes=1_900_000_000,
                                     differing=4))
    _save(window, out / f"wizard_5_preview_both_{tag}.png")
    if full:
        wizard.apply_preview(PreviewInfo(local_files=248, local_bytes=1_932_735_283, peer_connected=True,
                                         remote_ready=False))
        _save(window, out / f"wizard_5_preview_waiting_{tag}.png")
        wizard._show_step(5)
        _save(window, out / f"wizard_6_start_{tag}.png")
    window.close()

    # ---- 接続デバイス ----
    window, pages = _window(app, theme_name)
    devices = pages["devices"]
    devices.set_my_id(SAMPLE_SELF)
    window.show_page("devices")
    window.apply_snapshot(sample_snapshots()["relay"])
    devices.values["version"].setText("v2.1.7")
    if full:
        from folderlink.core.pairing import normalize_device_id

        devices._show_pending([PendingDevice(normalize_device_id("B" * 52), "知らないPC", "198.51.100.7:22000",
                                             "2026-11-21T03:00:00Z")])
    _save(window, out / f"devices_{tag}.png")
    window.close()

    # ---- 設定 ----
    window, pages = _window(app, theme_name)
    window.show_page("settings")
    window.resize(820, 1500)
    _save(window, out / f"settings_{tag}.png")
    window.close()

    # ---- ログ ----
    if full:
        window, pages = _window(app, theme_name)
        log_view = pages["log"]
        window.show_page("log")
        log_view.set_app_log(
            "2026-11-21 12:30:01,120 INFO folderlink.app: FolderLink 0.1.0 を起動します。\n"
            "2026-11-21 12:30:03,410 INFO folderlink.core.syncthing_manager: 同期エンジンを確認しました（port=51234, version=v2.1.7）。\n"
            "2026-11-21 12:31:10,002 WARNING folderlink.ui.engine_controller: 例: X-API-Key: ***\n")
        log_view.set_engine_log("2026-11-21 12:30:02 INF syncthing v2.1.7 \"Hafnium Hornet\"\n"
                                "2026-11-21 12:30:03 INF My ID: MFZWI3D-…\n")
        _save(window, out / f"log_{tag}.png")
        window.close()

    # ---- 履歴（項目11で追加） ----
    try:
        from folderlink.devtools import ui_shots_history
    except ImportError:
        return
    ui_shots_history.shoot(app, out, theme_name, suffix, _window)


__all__ = ["shoot", "NOW"]
