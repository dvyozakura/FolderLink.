"""画面で共通に使う部品。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def repolish(widget: QWidget) -> None:
    """動的プロパティを変えたあと、スタイルを当て直す。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def make_button(text: str, *, accent: bool = False, flat: bool = False, danger: bool = False) -> QPushButton:
    button = QPushButton(text)
    if accent:
        button.setProperty("accent", True)
    if flat:
        button.setProperty("flat", True)
    if danger:
        button.setProperty("danger", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def label(text: str = "", name: str | None = None, *, wrap: bool = False, selectable: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


class Card(QFrame):
    """角丸のカード。title を渡すと、上に小さな見出しが付く。"""

    def __init__(self, title: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 14, 18, 14)
        self.body.setSpacing(8)
        if title:
            self.body.addWidget(label(title, "SectionTitle"))

    def add(self, item) -> None:
        if isinstance(item, QWidget):
            self.body.addWidget(item)
        else:
            self.body.addLayout(item)


class Banner(QFrame):
    """注意書きの帯（warn / error / info）。右側にボタンを1つ置ける。"""

    def __init__(self, kind: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setProperty("kind", kind)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        self.text = label("", wrap=True)
        self.text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.text, 1)
        self.button = make_button("")
        self.button.hide()
        row.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_kind(self, kind: str) -> None:
        self.setProperty("kind", kind)
        repolish(self)

    def set_text(self, text: str) -> None:
        self.text.setText(text)

    def set_button(self, text: str | None) -> None:
        if text:
            self.button.setText(text)
            self.button.show()
        else:
            self.button.hide()


class ElidedLabel(QLabel):
    """幅に収まらないとき「…」で省略するラベル（パス用に中ほどを省略）。全文はツールチップで見られる。"""

    def __init__(self, text: str = "", mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(60)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        self.update()

    def full_text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        return QSize(min(fm.horizontalAdvance(self._full) + 4, 600), fm.height() + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        return QSize(60, fm.height() + 4)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        fm = QFontMetrics(self.font())
        text = fm.elidedText(self._full, self._mode, max(0, self.width() - 2))
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.end()


def metric(title: str) -> tuple[QWidget, QLabel]:
    """小さな見出し＋値の組。値のラベルを返すので、あとで書き換える。"""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(label(title, "Small"))
    value = label("—", "MetricValue")
    lay.addWidget(value)
    return box, value
