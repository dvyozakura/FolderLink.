"""ログの画面: FolderLink 自身のログと、同期エンジン（Syncthing）のログ。

表示する前に、必ず機密（API キーなど）を消す（ログのファイル自体にも書いていないが、念のため）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget

from folderlink.core.logging_setup import read_log_tail
from folderlink.core.redact import redact
from folderlink.ui import theme
from folderlink.ui.widgets import label, make_button


class LogView(QWidget):
    def __init__(self, palette: theme.Palette, controller=None, log_dir: Path | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.controller = controller
        self.log_dir = log_dir

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        outer.addWidget(label("問題が起きたときは、このログが原因を調べる手がかりになります。"
                              "API キーなどの機密は表示されません。", "Sub", wrap=True))
        self.tabs = QTabWidget()
        self.app_text = self._make_text()
        self.engine_text = self._make_text()
        self.tabs.addTab(self.app_text, "FolderLink のログ")
        self.tabs.addTab(self.engine_text, "同期エンジンのログ")
        outer.addWidget(self.tabs, 1)

        row = QHBoxLayout()
        self.refresh_button = make_button("更新")
        self.refresh_button.clicked.connect(self.refresh)
        self.copy_button = make_button("表示中のログをコピー")
        self.copy_button.clicked.connect(self._copy)
        self.open_button = make_button("ログのフォルダを開く")
        self.open_button.clicked.connect(self._open_folder)
        self.status = label("", "Small")
        row.addWidget(self.refresh_button)
        row.addWidget(self.copy_button)
        row.addWidget(self.open_button)
        row.addWidget(self.status)
        row.addStretch(1)
        outer.addLayout(row)

        if controller is not None:
            controller.actionFinished.connect(self._on_action)

    @staticmethod
    def _make_text() -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSizeF(9.5)
        text.setFont(font)
        text.setMaximumBlockCount(5000)
        return text

    def on_shown(self) -> None:
        self.refresh()

    def set_app_log(self, text: str) -> None:
        self._set(self.app_text, text, "（まだログがありません）")

    def set_engine_log(self, text: str) -> None:
        self._set(self.engine_text, text, "（同期エンジンのログはまだありません）")

    @staticmethod
    def _set(widget: QPlainTextEdit, text: str, empty: str) -> None:
        widget.setPlainText(redact(text) if text.strip() else empty)
        bar = widget.verticalScrollBar()
        bar.setValue(bar.maximum())

    def refresh(self) -> None:
        self.status.setText("")
        if self.log_dir is not None:
            self.set_app_log(read_log_tail(self.log_dir, 500))
        if self.controller is not None:
            self.controller.run("log:engine", lambda s: s.engine_log_tail(500))

    def _on_action(self, name: str, ok: bool, result) -> None:
        if name == "log:engine":
            self.set_engine_log(str(result) if ok else f"（取得できませんでした: {result}）")

    def _copy(self) -> None:
        widget = self.tabs.currentWidget()
        if isinstance(widget, QPlainTextEdit):
            QGuiApplication.clipboard().setText(widget.toPlainText())
            self.status.setText("コピーしました")

    def _open_folder(self) -> None:
        if self.log_dir is not None and self.log_dir.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_dir)))
