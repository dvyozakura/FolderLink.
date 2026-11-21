"""メインウィンドウ（ダッシュボード）と、ページの切り替え。

仕様 3章の画面:
    ● 同期完了                    最終確認 12:34:56
    同期フォルダ   C:\\Users\\...\\Desktop\\共有フォルダ   [変更]
    接続デバイス   PC-B  ● オンライン                  [詳細]
    ファイル 248 件 ・ 1.8 GB                         100%
    [同期を一時停止] [履歴を開く] [ログを見る]

- 表示の更新は apply_snapshot() に StatusSnapshot を渡すだけ。取得できなかった値は「—」。
- エンジンへの操作は EngineController 経由（別スレッド）。このクラスの中で待つ処理は無い。
- ウィンドウを閉じても終了しない（通知領域に残る）。終了は「終了」を選んだときだけ。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from folderlink import APP_NAME
from folderlink.core import formatting as fm
from folderlink.core.config_store import Settings
from folderlink.core.sync_status import StatusSnapshot, SyncState
from folderlink.ui import icons, theme
from folderlink.ui.widgets import Banner, Card, ElidedLabel, label, make_button, metric, repolish

BACKUP_NOTICE = (
    "同期はバックアップではありません。片方で消したり壊したりしたファイルは、もう片方にも反映されます。"
    "大切なデータは、別のドライブやクラウドにも独立したバックアップを取ってください。"
)


class DashboardPage(QWidget):
    changeFolderRequested = Signal()
    deviceDetailsRequested = Signal()
    pauseToggleRequested = Signal(bool)   # True = 一時停止したい
    historyRequested = Signal()
    logRequested = Signal()
    setupRequested = Signal()
    retryRequested = Signal()

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._palette = palette
        self._paused = False
        self._configured = False
        self._engine_error = ""
        self._last_state: SyncState | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # --- 注意の帯（未設定・エラーのときだけ出る） ---
        self.banner = Banner("warn")
        self.banner.hide()
        self.banner.button.clicked.connect(self._on_banner_button)
        self._banner_action = ""
        outer.addWidget(self.banner)

        # --- 状態 ---
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        self.state_dot = icons.StatusDot(16, palette.gray)
        self.state_label = label("起動中", "StateLabel")
        row.addWidget(self.state_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        row.addWidget(label("最終確認", "Small"), 0, Qt.AlignmentFlag.AlignVCenter)
        self.checked_label = label("—", "Sub")
        row.addWidget(self.checked_label, 0, Qt.AlignmentFlag.AlignVCenter)
        card.add(row)
        self.message_label = label("同期エンジンを起動しています…", "Sub", wrap=True)
        card.add(self.message_label)
        outer.addWidget(card)

        # --- 同期フォルダ ---
        card = Card("同期フォルダ")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.folder_icon = QLabel()
        self.folder_icon.setPixmap(icons.folder_pixmap(22, palette.accent, self.devicePixelRatioF()))
        row.addWidget(self.folder_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.folder_label = ElidedLabel("—")
        row.addWidget(self.folder_label, 1)
        self.change_button = make_button("変更")
        self.change_button.clicked.connect(self.changeFolderRequested)
        row.addWidget(self.change_button)
        card.add(row)
        outer.addWidget(card)

        # --- 接続デバイス ---
        card = Card("接続デバイス")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.pc_icon = QLabel()
        self.pc_icon.setPixmap(icons.pc_pixmap(22, palette.text, self.devicePixelRatioF()))
        row.addWidget(self.pc_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.peer_name_label = label("—")
        row.addWidget(self.peer_name_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(8)
        self.peer_dot = icons.StatusDot(10, palette.gray)
        row.addWidget(self.peer_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.peer_state_label = label("—")
        row.addWidget(self.peer_state_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.peer_route_label = label("", "Small")
        row.addWidget(self.peer_route_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        self.details_button = make_button("詳細")
        self.details_button.clicked.connect(self.deviceDetailsRequested)
        row.addWidget(self.details_button)
        card.add(row)
        outer.addWidget(card)

        # --- 進み具合 ---
        card = Card()
        row = QHBoxLayout()
        self.files_label = label("ファイル —")
        row.addWidget(self.files_label)
        row.addStretch(1)
        self.percent_label = label("—", "MetricValue")
        row.addWidget(self.percent_label)
        card.add(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        card.add(self.progress)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setContentsMargins(0, 6, 0, 0)
        box, self.rate_value = metric("転送速度")
        grid.addWidget(box, 0, 0)
        box, self.need_value = metric("未同期")
        grid.addWidget(box, 0, 1)
        box, self.last_sync_value = metric("最終同期")
        grid.addWidget(box, 0, 2)
        box, self.conflict_value = metric("競合")
        grid.addWidget(box, 0, 3)
        for col in range(4):
            grid.setColumnStretch(col, 1)
        card.add(grid)
        outer.addWidget(card)

        # --- 操作 ---
        row = QHBoxLayout()
        row.setSpacing(10)
        self.pause_button = make_button("同期を一時停止")
        self.pause_button.clicked.connect(self._on_pause_clicked)
        self.history_button = make_button("履歴を開く")
        self.history_button.clicked.connect(self.historyRequested)
        self.log_button = make_button("ログを見る")
        self.log_button.clicked.connect(self.logRequested)
        row.addWidget(self.pause_button)
        row.addWidget(self.history_button)
        row.addWidget(self.log_button)
        row.addStretch(1)
        outer.addLayout(row)

        # --- 注意書き ---
        self.notice_label = label(BACKUP_NOTICE, "Small", wrap=True)
        outer.addWidget(self.notice_label)
        self.guard_label = label("", "Small", wrap=True)
        self.guard_label.hide()
        outer.addWidget(self.guard_label)
        outer.addStretch(1)

        self._update_buttons()

    # ---- 表示の更新 ----
    def apply_settings(self, settings: Settings) -> None:
        self._configured = bool(settings.setup_completed and settings.folder_id and settings.peer_device_id)
        self.folder_label.set_full_text(settings.folder_path or "—")
        name = settings.peer_name or (settings.peer_device_id or "").split("-")[0]
        self.peer_name_label.setText(name or "—")
        if self._configured and settings.mass_delete_guard_enabled:
            self.set_guard_text(f"大量削除の検知: オン（相手のPCで {settings.mass_delete_threshold} 件以上削除されたら"
                                "同期を一時停止して知らせます。確実に防ぐ機能ではありません）")
        elif self._configured:
            self.set_guard_text("大量削除の検知: オフ")
        else:
            self.set_guard_text("")
        self._update_buttons()
        self._update_banner()

    def set_guard_text(self, text: str) -> None:
        self.guard_label.setText(text)
        self.guard_label.setVisible(bool(text))

    def apply_snapshot(self, snap: StatusSnapshot) -> None:
        p = self._palette
        self._last_state = snap.state
        color = theme.state_color(p, snap.state.color)
        self.state_dot.set_color(color)
        self.state_label.setText(snap.state.label)
        self.message_label.setText(snap.message or "")
        self.checked_label.setText(fm.fmt_time(snap.checked_at, with_date=False))

        # 接続デバイス
        if snap.peer_paused:
            self.peer_dot.set_color(p.yellow)
            self.peer_state_label.setText("一時停止")
            self.peer_route_label.setText("")
        elif snap.peer_connected is True:
            self.peer_dot.set_color(p.green)
            self.peer_state_label.setText("オンライン")
            if snap.peer_via_relay is True:
                self.peer_route_label.setText("（中継サーバー経由。速度は保証されません）")
            elif snap.peer_via_relay is False:
                self.peer_route_label.setText("（直接接続）")
            else:
                self.peer_route_label.setText("")
        elif snap.peer_connected is False:
            self.peer_dot.set_color(p.gray)
            self.peer_state_label.setText("オフライン")
            self.peer_route_label.setText("")
        else:
            self.peer_dot.set_color(p.gray)
            self.peer_state_label.setText("—")
            self.peer_route_label.setText("")

        # 進み具合
        if snap.global_files is None and snap.global_bytes is None:
            self.files_label.setText("ファイル —")
        else:
            self.files_label.setText(
                f"ファイル {fm.fmt_count(snap.global_files)} ・ {fm.fmt_bytes(snap.global_bytes)}"
            )
        self.percent_label.setText(fm.fmt_percent(snap.completion))
        self.progress.setValue(int(round((snap.completion or 0.0) * 10)) if snap.completion is not None else 0)
        self.progress.setProperty("state", snap.state.color)
        repolish(self.progress)
        if snap.in_bps is None and snap.out_bps is None:
            self.rate_value.setText("—")
        else:
            self.rate_value.setText(f"↓ {fm.fmt_rate(snap.in_bps)}  ↑ {fm.fmt_rate(snap.out_bps)}")
        self.need_value.setText(fm.fmt_count(snap.need_items))
        self.last_sync_value.setText(fm.fmt_time(snap.last_sync))
        self.conflict_value.setText(fm.fmt_count(snap.conflicts))
        self.conflict_value.setStyleSheet(f"color: {p.yellow};" if snap.conflicts else "")

        self._paused = snap.state is SyncState.PAUSED
        self._update_buttons()
        self._update_banner(snap)

    def _update_buttons(self) -> None:
        self.pause_button.setText("同期を再開" if self._paused else "同期を一時停止")
        self.pause_button.setProperty("accent", bool(self._paused))
        repolish(self.pause_button)
        usable = self._configured and self._last_state not in (None, SyncState.STARTING, SyncState.NOT_CONFIGURED)
        self.pause_button.setEnabled(bool(usable))
        self.history_button.setEnabled(self._configured)
        self.change_button.setText("変更" if self._configured else "設定")

    def show_engine_error(self, message: str) -> None:
        """エンジンを起動できなかったときの帯（「再試行」つき）。状態が回復したら自動で消える。"""
        self._engine_error = message or "同期エンジンを起動できませんでした。"
        self._update_banner()

    def _update_banner(self, snap: StatusSnapshot | None = None) -> None:
        if snap is not None and snap.state not in (SyncState.ERROR, SyncState.STARTING):
            self._engine_error = ""  # エンジンが動いて状態を返すようになったら、帯を消す
        if self._engine_error:
            self.banner.set_kind("error")
            self.banner.set_text(self._engine_error)
            self.banner.set_button("再試行")
            self._banner_action = "retry"
            self.banner.show()
        elif not self._configured:
            self.banner.set_kind("warn")
            self.banner.set_text("初期設定がまだ終わっていません。同期するフォルダと相手のPCを設定してください。")
            self.banner.set_button("設定を始める")
            self._banner_action = "setup"
            self.banner.show()
        elif snap is not None and getattr(snap, "guard_trip", None) is not None:
            from folderlink.core.safety_guard import trip_message

            self.banner.set_kind("error" if snap.guard_trip.applied_deletes or not snap.guard_trip.paused else "warn")
            self.banner.set_text(trip_message(snap.guard_trip))
            self.banner.set_button("確認する")
            self._banner_action = "history"
            self.banner.show()
        elif snap is not None and snap.state is SyncState.CONFLICT:
            self.banner.set_kind("warn")
            self.banner.set_text(snap.message)
            self.banner.set_button("履歴を開く")
            self._banner_action = "history"
            self.banner.show()
        else:
            self.banner.hide()

    def _on_banner_button(self) -> None:
        {"retry": self.retryRequested, "setup": self.setupRequested,
         "history": self.historyRequested}.get(self._banner_action, self.setupRequested).emit()

    def _on_pause_clicked(self) -> None:
        self.pauseToggleRequested.emit(not self._paused)


class MainWindow(QMainWindow):
    quitRequested = Signal()
    hiddenToTray = Signal()
    pageChanged = Signal(str, str)   # 前のページ, 新しいページ

    def __init__(self, settings: Settings, controller=None, palette: theme.Palette | None = None,
                 close_to_tray: bool = False) -> None:
        super().__init__()
        self.settings = settings
        self.controller = controller
        self.palette_ = palette or theme.LIGHT
        self.close_to_tray = close_to_tray
        self._really_quit = False
        self._pages: dict[str, tuple[QWidget, str]] = {}
        self.last_snapshot: StatusSnapshot | None = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.setMinimumSize(720, 600)
        self.resize(820, 700)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 16, 22, 18)
        outer.setSpacing(12)

        # --- 見出しの行 ---
        header = QHBoxLayout()
        header.setSpacing(10)
        self.back_button = make_button("戻る", flat=True)
        self.back_button.setIcon(QIcon(icons.back_pixmap(16, self.palette_.text, self.devicePixelRatioF())))
        self.back_button.clicked.connect(lambda: self.show_page("dashboard"))
        self.back_button.hide()
        header.addWidget(self.back_button)
        self.app_icon_label = QLabel()
        self.app_icon_label.setPixmap(icons.app_pixmap(26, dpr=self.devicePixelRatioF()))
        header.addWidget(self.app_icon_label)
        self.title_label = label(APP_NAME, "AppTitle")
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.settings_button = make_button("設定", flat=True)
        self.settings_button.setIcon(QIcon(icons.gear_pixmap(16, self.palette_.text, self.devicePixelRatioF())))
        self.settings_button.clicked.connect(lambda: self.show_page("settings"))
        header.addWidget(self.settings_button)
        outer.addLayout(header)

        # --- ページ ---
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.dashboard = DashboardPage(self.palette_)
        self.add_page("dashboard", self.dashboard, APP_NAME, scroll=True)
        self.dashboard.apply_settings(settings)
        self.dashboard.historyRequested.connect(lambda: self.show_page("history"))
        self.dashboard.logRequested.connect(lambda: self.show_page("log"))
        self.dashboard.deviceDetailsRequested.connect(lambda: self.show_page("devices"))
        self.dashboard.pauseToggleRequested.connect(self._toggle_pause)
        self.dashboard.retryRequested.connect(self._retry)

        if controller is not None:
            controller.statusChanged.connect(self.apply_snapshot)
            controller.engineFailed.connect(self._on_engine_failed)
            controller.actionFinished.connect(self._on_action_finished)

    # ---- ページ ----
    def add_page(self, name: str, widget: QWidget, title: str, scroll: bool = False) -> None:
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QScrollArea.Shape.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            area.setWidget(widget)
            container: QWidget = area
        else:
            container = widget
        self._pages[name] = (container, title)
        self.stack.addWidget(container)

    def has_page(self, name: str) -> bool:
        return name in self._pages

    def current_page(self) -> str:
        current = self.stack.currentWidget()
        for name, (widget, _title) in self._pages.items():
            if widget is current:
                return name
        return ""

    def page(self, name: str) -> QWidget | None:
        entry = self._pages.get(name)
        if entry is None:
            return None
        container = entry[0]
        return container.widget() if isinstance(container, QScrollArea) else container

    def show_page(self, name: str) -> None:
        if name not in self._pages:
            return
        previous = self.current_page()
        if previous and previous != name:
            old = self.page(previous)
            if hasattr(old, "on_hidden"):
                old.on_hidden()
        container, title = self._pages[name]
        self.stack.setCurrentWidget(container)
        if previous != name:
            self.pageChanged.emit(previous, name)
        is_home = name == "dashboard"
        self.back_button.setVisible(not is_home)
        self.app_icon_label.setVisible(is_home)
        self.settings_button.setVisible(is_home)
        self.title_label.setText(title)
        self.title_label.setObjectName("AppTitle" if is_home else "PageTitle")
        repolish(self.title_label)
        inner = self.page(name)
        if hasattr(inner, "on_shown"):
            inner.on_shown()

    # ---- 表示の更新 ----
    def apply_snapshot(self, snap: StatusSnapshot) -> None:
        self.last_snapshot = snap
        self.dashboard.apply_snapshot(snap)
        for name in self._pages:
            inner = self.page(name)
            if inner is not self.dashboard and hasattr(inner, "apply_snapshot"):
                inner.apply_snapshot(snap)

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.dashboard.apply_settings(settings)

    def _on_engine_failed(self, message: str) -> None:
        from datetime import timezone

        self.apply_snapshot(StatusSnapshot(SyncState.ERROR, message, datetime.now(timezone.utc)))
        self.dashboard.show_engine_error(message)

    def _on_action_finished(self, name: str, ok: bool, result) -> None:
        if name == "pause" and not ok:
            self.dashboard.banner.set_kind("error")
            self.dashboard.banner.set_text(str(result))
            self.dashboard.banner.set_button(None)
            self.dashboard.banner.show()

    # ---- 操作 ----
    def _toggle_pause(self, pause: bool) -> None:
        if self.controller is not None:
            self.dashboard.pause_button.setEnabled(False)  # 結果が出るまで連打させない
            self.controller.run("pause", lambda service: service.set_paused(pause))

    def _retry(self) -> None:
        if self.controller is not None:
            self.controller.start()

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_quit(self) -> None:
        """「終了」が選ばれた。エンジンの停止は呼び出し側（app.py）が行う。"""
        self._really_quit = True
        self.quitRequested.emit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._really_quit or not self.close_to_tray:
            event.accept()
            if not self._really_quit:
                self._really_quit = True
                self.quitRequested.emit()
            return
        # ×で閉じても終了しない。通知領域に残って同期を続ける。
        event.ignore()
        self.hide()
        self.hiddenToTray.emit()
