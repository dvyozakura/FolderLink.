"""アプリ本体の起動処理。

- 画面（MainWindow）、通知領域（TrayController）、エンジンの窓口（EngineController）を組み立てる。
- ウィンドウを閉じても終了しない。通知領域の「終了」を選んだときだけ、エンジンを止めて終える。
- 二重起動したときは、先に動いているほうのウィンドウを前に出して、あとから起動したほうは終わる。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys

from folderlink import APP_NAME, __version__

log = logging.getLogger(__name__)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=True)
    parser.add_argument("--version", action="store_true", help="バージョンを表示して終了")
    parser.add_argument("--minimized", action="store_true", help="ウィンドウを出さず、通知領域で起動（自動起動用）")
    parser.add_argument("--data-dir", default=None, help="設定の保存先（開発・テスト用）")
    parser.add_argument("--isolated", action="store_true", help="外部の探索・リレーを使わない（開発・テスト用）")
    parser.add_argument("--listen-address", default=None, help="同期の待ち受けアドレス（開発・テスト用）")
    parser.add_argument("--quit", action="store_true",
                        help="動いている FolderLink に、エンジンを止めて終了するよう伝える（アンインストール用）")
    parser.add_argument("--smoke-test", action="store_true",
                        help="画面を出さずにエンジンを起動→確認→停止して終了（ビルドの確認用）")
    args, _unknown = parser.parse_known_args(argv)
    return args


def _say(text: str) -> None:
    """コンソールが無い（ウィンドウ用 EXE の）ときでも落ちないように出力する。"""
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass


def run(argv: list[str]) -> int:
    args = _parse(argv)
    if args.version:
        _say(f"{APP_NAME} {__version__}")
        return 0

    from folderlink.core.config_store import ENV_DATA_DIR, ConfigStore
    from folderlink.core.logging_setup import install_excepthook, setup_logging

    if args.data_dir:
        os.environ[ENV_DATA_DIR] = args.data_dir
    store = ConfigStore()
    store.ensure_dirs()
    setup_logging(store.log_dir)
    install_excepthook()
    log.info("%s %s を起動します（smoke_test=%s, minimized=%s）。", APP_NAME, __version__, args.smoke_test,
             args.minimized)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication([APP_NAME])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    from folderlink.core.engine_service import EngineService
    from folderlink.ui import icons, theme
    from folderlink.ui.engine_controller import EngineController

    app.setWindowIcon(icons.app_icon())

    if args.quit:
        sent = SingleInstance(str(store.base_dir)).send(b"quit")
        log.info("終了の依頼: %s", "動いている FolderLink に伝えました" if sent else "動いている FolderLink はありません")
        _say("sent" if sent else "not running")
        return 0

    guard = None
    if not args.smoke_test:
        guard = SingleInstance(str(store.base_dir))
        if not guard.acquire():
            log.info("既に起動しているので、そちらのウィンドウを前に出して終了します。")
            return 0

    service = EngineService(store, isolated=args.isolated, listen_address=args.listen_address)
    palette = theme.apply_theme(app, service.settings.theme)
    controller = EngineController(service)

    if args.smoke_test:
        return _smoke_test(app, controller, service, palette)

    from folderlink.ui.main_window import MainWindow
    from folderlink.ui.tray import TrayController

    tray_ok = TrayController.is_available()
    window = MainWindow(service.settings, controller, palette, close_to_tray=tray_ok)
    install_pages(window, controller, service, store, palette)

    tray = None
    if tray_ok:
        tray = TrayController(palette)
        tray.openRequested.connect(window.show_and_raise)
        tray.pauseToggleRequested.connect(window._toggle_pause)
        tray.quitRequested.connect(window.request_quit)
        controller.statusChanged.connect(tray.apply_snapshot)
        window.hiddenToTray.connect(tray.notify_hidden_once)
        tray.show()
    if guard is not None:
        guard.activated.connect(window.show_and_raise)
        guard.quitRequested.connect(window.request_quit)  # `FolderLink.exe --quit`（アンインストール時など）

    def quit_app() -> None:
        log.info("終了が選ばれました。エンジンを止めます。")
        window.hide()
        if tray is not None:
            tray.hide()
        controller.shutdown()
        app.quit()

    window.quitRequested.connect(quit_app)
    for note in store.recovery_notes:
        log.warning(note)

    controller.start()
    if not (args.minimized and tray_ok) and not service.settings.start_minimized:
        window.show()
    elif not tray_ok:
        window.showMinimized()

    code = app.exec()
    controller.shutdown()
    if guard is not None:
        guard.release()
    log.info("%s を終了しました（code=%s）。", APP_NAME, code)
    return int(code)


def install_pages(window, controller, service, store, palette, *, autostart=None, confirm=None,
                  choose_folder=None) -> dict:
    """ダッシュボード以外のページ（初期設定・接続デバイス・設定・ログ・履歴）を取り付けて、つなぐ。

    autostart / confirm / choose_folder は、テストや画面の見本で偽物に差し替えるためのもの。
    """
    from folderlink.ui import theme
    from folderlink.ui.device_panel import DevicePanel, default_confirm
    from folderlink.ui.history_view import HistoryView
    from folderlink.ui.log_view import LogView
    from folderlink.ui.settings_view import SettingsView
    from folderlink.ui.setup_wizard import SetupWizard

    confirm = confirm or default_confirm
    wizard = SetupWizard(palette, controller, service.settings, choose_folder)
    devices = DevicePanel(palette, controller, service.settings, confirm)
    settings_view = SettingsView(palette, controller, service.settings, autostart, confirm)
    log_view = LogView(palette, controller, store.log_dir)
    history_view = HistoryView(palette, controller, service.settings, confirm)
    window.add_page("setup", wizard, "初期設定", scroll=True)
    window.add_page("devices", devices, "接続先デバイス", scroll=True)
    window.add_page("settings", settings_view, "設定", scroll=True)
    window.add_page("log", log_view, "ログ")
    window.add_page("history", history_view, "同期履歴・競合")
    pages = {"setup": wizard, "devices": devices, "settings": settings_view, "log": log_view,
             "history": history_view}

    def sync_settings() -> None:
        window.apply_settings(service.settings)
        devices.apply_settings(service.settings)
        settings_view.apply_settings(service.settings)
        history_view.apply_settings(service.settings)

    def open_wizard() -> None:
        wizard.restart(service.settings)
        window.show_page("setup")

    def change_folder() -> None:
        if service.settings.setup_completed:
            if not confirm(window, "同期フォルダの変更",
                           "同期フォルダを変えるには、今の同期設定をいったん解除して、設定し直します。\n\n"
                           "・今のフォルダの中のファイルは残ります（消えません）。\n"
                           "・相手のPCのファイルも残ります。\n"
                           "・新しいフォルダと相手のPCのフォルダの中身は、同期を始めると合わさります。\n\n続けますか？"):
                return
            if controller is not None:
                controller.run("app:reset_for_wizard", lambda s: s.reset_sync(forget_peer=False))
        else:
            open_wizard()

    def change_peer() -> None:
        if controller is not None:
            controller.run("app:reset_for_wizard", lambda s: s.reset_sync(forget_peer=True))

    def on_action(name: str, ok: bool, result) -> None:
        if name == "app:reset_for_wizard":
            sync_settings()
            if ok:
                open_wizard()
        elif name.startswith("wizard:") and ok and name.split(":", 1)[1] in ("register_peer", "start_sync"):
            sync_settings()
        elif name == "devices:accept" and ok:
            sync_settings()

    def on_page_changed(previous: str, _new: str) -> None:
        # 初期設定を途中でやめたら、プレビュー用に登録したフォルダ（送信のみ）を外す。ファイルには触れない。
        if previous == "setup" and controller is not None and not wizard.started:
            controller.run("app:abort_preview", lambda s: (
                s.reset_sync(forget_peer=False)
                if s.settings.folder_id and not s.settings.setup_completed else None))

    def on_wizard_finished() -> None:
        sync_settings()
        window.show_page("dashboard")

    def on_theme_changed(key: str) -> None:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, key)
        settings_view.message.setText("テーマを変更しました。アイコンなど一部の色は、FolderLink を起動し直すと反映されます。")

    window.dashboard.setupRequested.connect(open_wizard)
    window.dashboard.changeFolderRequested.connect(change_folder)
    wizard.finished.connect(on_wizard_finished)
    wizard.cancelled.connect(lambda: window.show_page("dashboard"))
    devices.changePeerRequested.connect(change_peer)
    settings_view.resetRequested.connect(lambda: (sync_settings(), window.show_page("dashboard")))
    settings_view.settingsChanged.connect(lambda _s: sync_settings())
    settings_view.themeChanged.connect(on_theme_changed)
    window.pageChanged.connect(on_page_changed)
    if controller is not None:
        controller.actionFinished.connect(on_action)
    return pages


def _smoke_test(app, controller, service, palette, timeout_s: int = 120) -> int:
    """画面を出さずに、エンジンの起動→本人確認→停止までを通す。終了コード 0 が成功。"""
    from PySide6.QtCore import QTimer

    from folderlink.ui.main_window import MainWindow

    result = {"code": 2, "detail": "時間切れ"}
    window = MainWindow(service.settings, controller, palette, close_to_tray=False)
    install_pages(window, controller, service, service.store, palette)

    def on_started(device_id: str) -> None:
        # 画面を（表示せずに）描けることも確かめる
        pixmap = window.grab()
        ok = not pixmap.isNull() and bool(device_id)
        result.update(code=0 if ok else 3, detail="起動と描画に成功" if ok else "描画に失敗")
        app.quit()

    def on_failed(message: str) -> None:
        result.update(code=1, detail=message)
        app.quit()

    controller.engineStarted.connect(on_started)
    controller.engineFailed.connect(on_failed)
    QTimer.singleShot(timeout_s * 1000, app.quit)
    controller.start()
    app.exec()
    stopped = controller.shutdown()
    if result["code"] == 0 and not stopped:
        result.update(code=4, detail="エンジンの停止が時間内に終わりませんでした")
    log.info("スモークテストの結果: code=%s %s", result["code"], result["detail"])
    try:
        (service.store.base_dir / "smoke_result.txt").write_text(
            f"code={result['code']}\ndetail={result['detail']}\nversion={__version__}\n", encoding="utf-8")
    except OSError:
        pass
    _say(f"smoke-test: code={result['code']} {result['detail']}")
    return int(result["code"])


class SingleInstance:
    """二重起動の防止。2つ目は、1つ目に「前に出て」と伝えて終わる。"""

    def __init__(self, key: str) -> None:
        from PySide6.QtCore import QObject, Signal

        digest = hashlib.sha256(os.path.normcase(os.path.abspath(key)).encode("utf-8")).hexdigest()[:16]
        self._name = f"FolderLink-{digest}"
        self._server = None

        class _Notifier(QObject):
            activated = Signal()
            quitRequested = Signal()

        self._notifier = _Notifier()
        self.activated = self._notifier.activated
        self.quitRequested = self._notifier.quitRequested

    def send(self, message: bytes) -> bool:
        """動いている1つ目に伝える。1つ目がいなければ False。"""
        from PySide6.QtNetwork import QLocalSocket

        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(500):
            return False
        socket.write(message)
        socket.flush()
        socket.waitForBytesWritten(500)
        # 先に切ると、相手が読む前にデータが消える（Windows の名前付きパイプ）。相手が閉じるのを待つ
        if not socket.waitForDisconnected(2000):
            socket.disconnectFromServer()
        return True

    def acquire(self) -> bool:
        from PySide6.QtNetwork import QLocalServer

        if self.send(b"show"):
            return False
        QLocalServer.removeServer(self._name)  # 前回の異常終了で残った名前を片づける
        server = QLocalServer()
        if not server.listen(self._name):
            return True  # 名前を取れなくても、起動は続ける
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def _on_connection(self) -> None:
        conn = self._server.nextPendingConnection() if self._server else None
        if conn is None:
            return
        handled = {"done": False}

        def handle() -> None:
            if handled["done"]:
                return
            data = bytes(conn.readAll().data())
            if not data and conn.state() == conn.LocalSocketState.ConnectedState:
                return  # まだ届いていない
            handled["done"] = True
            if data.strip() == b"quit":
                self._notifier.quitRequested.emit()
            else:
                self._notifier.activated.emit()
            conn.close()

        conn.readyRead.connect(handle)
        conn.disconnected.connect(handle)
        if conn.bytesAvailable():
            handle()

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
