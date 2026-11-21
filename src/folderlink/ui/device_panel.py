"""接続先デバイスの画面: このPCの ID、相手のPCの状態、接続を求めているPCの承認・拒否。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QMessageBox, QVBoxLayout, QWidget

from folderlink.core.config_store import Settings
from folderlink.core.sync_status import StatusSnapshot
from folderlink.ui import icons, theme
from folderlink.ui.setup_wizard import format_id_two_lines
from folderlink.ui.widgets import Card, label, make_button


def default_confirm(parent: QWidget, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


class DevicePanel(QWidget):
    changePeerRequested = Signal()   # 相手を変える（同期設定を解除して、初期設定からやり直す）

    def __init__(self, palette: theme.Palette, controller=None, settings: Settings | None = None,
                 confirm: Callable[[QWidget, str, str], bool] = default_confirm,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._palette = palette
        self.controller = controller
        self.settings = settings or Settings()
        self._confirm = confirm
        self.my_id = getattr(controller, "device_id", None) or ""
        self._pending: list = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        card = Card("このPC")
        self.my_id_label = label("準備中…", "Mono", selectable=True)
        card.add(self.my_id_label)
        row = QHBoxLayout()
        self.copy_button = make_button("ID をコピー")
        self.copy_button.clicked.connect(self._copy)
        self.copy_status = label("", "OkText")
        row.addWidget(self.copy_button)
        row.addWidget(self.copy_status)
        row.addStretch(1)
        card.add(row)
        outer.addWidget(card)

        card = Card("相手のPC")
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        self.values: dict[str, object] = {}
        rows = [("name", "呼び名"), ("id", "デバイス ID"), ("state", "状態"), ("route", "接続の経路"),
                ("address", "アドレス"), ("version", "同期エンジン")]
        for i, (key, title) in enumerate(rows):
            grid.addWidget(label(title, "Sub"), i, 0)
            if key == "state":
                cell = QHBoxLayout()
                cell.setSpacing(6)
                self.state_dot = icons.StatusDot(10, palette.gray)
                value = label("—")
                cell.addWidget(self.state_dot)
                cell.addWidget(value)
                cell.addStretch(1)
                grid.addLayout(cell, i, 1)
            else:
                value = label("—", selectable=True, wrap=(key == "id"))
                grid.addWidget(value, i, 1)
            self.values[key] = value
        grid.setColumnStretch(1, 1)
        card.add(grid)
        self.route_note = label("", "Small", wrap=True)
        card.add(self.route_note)
        row = QHBoxLayout()
        self.change_button = make_button("相手のPCを変更…", danger=True)
        self.change_button.clicked.connect(self._change_peer)
        row.addWidget(self.change_button)
        row.addStretch(1)
        card.add(row)
        outer.addWidget(card)

        self.pending_card = Card("接続を求めているPC")
        self.pending_text = label("", wrap=True, selectable=True)
        self.pending_card.add(self.pending_text)
        self.pending_card.add(label(
            "心当たりのあるPCだけ承認してください。承認すると、そのPCとこのPCが接続できるようになります"
            "（フォルダの共有は、別に設定するまで始まりません）。", "Small", wrap=True))
        row = QHBoxLayout()
        self.accept_button = make_button("承認", accent=True)
        self.accept_button.clicked.connect(self._accept)
        self.reject_button = make_button("拒否")
        self.reject_button.clicked.connect(self._reject)
        row.addWidget(self.accept_button)
        row.addWidget(self.reject_button)
        row.addStretch(1)
        self.pending_card.add(row)
        self.pending_card.hide()
        outer.addWidget(self.pending_card)

        self.message = label("", "ErrorText", wrap=True)
        outer.addWidget(self.message)
        outer.addStretch(1)

        if controller is not None:
            controller.engineStarted.connect(self.set_my_id)
            controller.actionFinished.connect(self._on_action)
        self.set_my_id(self.my_id)
        self.apply_settings(self.settings)

    # ---- 表示 ----
    def set_my_id(self, device_id: str) -> None:
        self.my_id = device_id or ""
        self.my_id_label.setText(format_id_two_lines(self.my_id) if self.my_id else "準備中…")
        self.copy_button.setEnabled(bool(self.my_id))

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        peer = settings.peer_device_id or ""
        self.values["name"].setText(settings.peer_name or (peer.split("-")[0] if peer else "—"))
        self.values["id"].setText(format_id_two_lines(peer) if peer else "未登録")
        self.change_button.setEnabled(bool(peer))

    def apply_snapshot(self, snap: StatusSnapshot) -> None:
        p = self._palette
        state, route = self.values["state"], self.values["route"]
        self.route_note.setText("")
        if not self.settings.peer_device_id:
            self.state_dot.set_color(p.gray)
            state.setText("未登録")
            route.setText("—")
            self.values["address"].setText("—")
            return
        if snap.peer_paused:
            self.state_dot.set_color(p.yellow)
            state.setText("一時停止")
        elif snap.peer_connected is True:
            self.state_dot.set_color(p.green)
            state.setText("オンライン")
        elif snap.peer_connected is False:
            self.state_dot.set_color(p.gray)
            state.setText("オフライン")
        else:
            self.state_dot.set_color(p.gray)
            state.setText("—")
        if snap.peer_connected and snap.peer_via_relay is True:
            route.setText("中継サーバー経由")
            self.route_note.setText(
                "2台が直接つながらないため、Syncthing の中継サーバーを通しています。内容は暗号化されていますが、"
                "速度は遅く、いつでも使えるとは限りません。")
        elif snap.peer_connected and snap.peer_via_relay is False:
            route.setText("直接接続")
        else:
            route.setText("—")
        self.values["address"].setText(snap.peer_address or "—")

    def on_shown(self) -> None:
        self.copy_status.setText("")
        self.message.setText("")
        self.refresh()

    def refresh(self) -> None:
        if self.controller is not None:
            self.controller.run("devices:info", lambda s: (s.pending_devices(), s.peer_state()))

    def _show_pending(self, pending: list) -> None:
        self._pending = [p for p in pending if p.device_id not in (self.my_id, self.settings.peer_device_id)]
        if not self._pending:
            self.pending_card.hide()
            return
        first = self._pending[0]
        name = f"{first.name}\n" if first.name else ""
        more = f"\n（ほかに {len(self._pending) - 1} 台）" if len(self._pending) > 1 else ""
        # 相手は1台だけ。既に相手が登録済みなら、ここからの承認はできない（先に「相手のPCを変更」）
        can_accept = not self.settings.peer_device_id
        hint = "" if can_accept else "\n※ 同期できる相手は1台です。このPCを相手にするには、先に「相手のPCを変更…」を押してください。"
        self.pending_text.setText(f"{name}{first.device_id}\n接続元: {first.address or '—'}{more}{hint}")
        self.accept_button.setEnabled(can_accept)
        self.pending_card.show()

    # ---- 操作 ----
    def _copy(self) -> None:
        if self.my_id:
            QGuiApplication.clipboard().setText(self.my_id)
            self.copy_status.setText("コピーしました")

    def _accept(self) -> None:
        if not self._pending or self.controller is None:
            return
        first = self._pending[0]
        self.controller.run("devices:accept", lambda s: s.register_peer(first.device_id, first.name))

    def _reject(self) -> None:
        if not self._pending or self.controller is None:
            return
        device_id = self._pending[0].device_id
        self.controller.run("devices:reject", lambda s: s.pairing.reject_pending_device(device_id))

    def _change_peer(self) -> None:
        if not self._confirm(self, "相手のPCを変更",
                             "今の相手との同期設定を解除して、初期設定からやり直します。\n\n"
                             "同期フォルダの中のファイルは、このPCにも相手のPCにも残ります（消えません）。\n"
                             "続けますか？"):
            return
        self.changePeerRequested.emit()

    def _on_action(self, name: str, ok: bool, result) -> None:
        if not name.startswith("devices:"):
            return
        if not ok:
            self.message.setText(str(result))
            return
        self.message.setText("")
        if name == "devices:info":
            pending, peer_state = result
            self._show_pending(pending or [])
            if peer_state is not None:
                self.values["version"].setText(peer_state.client_version or "—")
        elif name in ("devices:accept", "devices:reject"):
            if name == "devices:accept" and self.controller is not None:
                self.apply_settings(self.controller.service.settings)
            self.refresh()
