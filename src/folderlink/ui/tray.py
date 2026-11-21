"""通知領域（タスクトレイ）のアイコン。

- ウィンドウを閉じても、ここに残って同期を続ける。
- 「終了」を選んだときだけ、エンジンを止めてアプリを終える。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from folderlink import APP_NAME
from folderlink.core.sync_status import StatusSnapshot, SyncState
from folderlink.ui import icons, theme


class TrayController(QObject):
    openRequested = Signal()
    pauseToggleRequested = Signal(bool)
    quitRequested = Signal()

    def __init__(self, palette: theme.Palette, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._paused = False
        self._notified_hidden = False
        self._last_state: SyncState | None = None
        self.tray = QSystemTrayIcon(icons.app_icon(palette.gray), self)
        self.tray.setToolTip(f"{APP_NAME} — 起動中")

        self.menu = QMenu()
        self.open_action = QAction(f"{APP_NAME} を開く", self.menu)
        self.open_action.triggered.connect(self.openRequested)
        self.state_action = QAction("状態: 起動中", self.menu)
        self.state_action.setEnabled(False)
        self.pause_action = QAction("同期を一時停止", self.menu)
        self.pause_action.triggered.connect(lambda: self.pauseToggleRequested.emit(not self._paused))
        self.pause_action.setEnabled(False)
        self.quit_action = QAction("終了（同期も止まります）", self.menu)
        self.quit_action.triggered.connect(self.quitRequested)
        self.menu.addAction(self.open_action)
        self.menu.addSeparator()
        self.menu.addAction(self.state_action)
        self.menu.addAction(self.pause_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.openRequested.emit()

    def apply_snapshot(self, snap: StatusSnapshot) -> None:
        trip = getattr(snap, "guard_trip", None)
        if trip is not None and trip is not getattr(self, "_last_trip", None):
            from folderlink.core.safety_guard import trip_message

            self.tray.showMessage(f"{APP_NAME}: 大量の削除を検知しました", trip_message(trip),
                                  QSystemTrayIcon.MessageIcon.Warning, 15000)
        self._last_trip = trip
        self._paused = snap.state is SyncState.PAUSED
        self.pause_action.setText("同期を再開" if self._paused else "同期を一時停止")
        self.pause_action.setEnabled(snap.state not in (SyncState.STARTING, SyncState.NOT_CONFIGURED))
        self.state_action.setText(f"状態: {snap.state.label}")
        self.tray.setToolTip(f"{APP_NAME} — {snap.state.label}")
        if snap.state is not self._last_state:
            self.tray.setIcon(icons.app_icon(theme.state_color(self._palette, snap.state.color)))
            previous, self._last_state = self._last_state, snap.state
            # 目を離している間に起きた、対応が必要なことだけ知らせる
            if previous is not None and snap.state in (SyncState.CONFLICT, SyncState.ERROR):
                icon = (QSystemTrayIcon.MessageIcon.Critical if snap.state is SyncState.ERROR
                        else QSystemTrayIcon.MessageIcon.Warning)
                self.tray.showMessage(f"{APP_NAME}: {snap.state.label}", snap.message, icon, 8000)

    def notify_hidden_once(self) -> None:
        """ウィンドウを閉じたとき、最初の1回だけ「動き続けています」と知らせる。"""
        if self._notified_hidden:
            return
        self._notified_hidden = True
        self.tray.showMessage(
            APP_NAME,
            "FolderLink は通知領域で動き続け、同期を続けます。終了するには、アイコンを右クリックして「終了」を選んでください。",
            QSystemTrayIcon.MessageIcon.Information,
            6000,
        )

    def notify(self, title: str, message: str, critical: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Critical if critical else QSystemTrayIcon.MessageIcon.Warning
        self.tray.showMessage(title, message, icon, 10000)
