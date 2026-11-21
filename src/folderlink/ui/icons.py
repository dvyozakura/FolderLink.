"""アイコン。絵文字フォントや画像ファイルに頼らず、その場で描く（高 DPI でもぼやけない）。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

APP_BLUE = "#0067C0"


def _canvas(size: int, dpr: float = 1.0) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pm, painter


def _arrow(painter: QPainter, x1: float, x2: float, y: float, head: float, width: float, color: QColor) -> None:
    """(x1,y) から (x2,y) への矢印。"""
    pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    direction = 1 if x2 > x1 else -1
    painter.drawLine(QPointF(x1, y), QPointF(x2 - direction * head * 0.5, y))
    painter.setBrush(QBrush(color))
    painter.setPen(QPen(color, width * 0.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.drawPolygon(QPolygonF([
        QPointF(x2, y),
        QPointF(x2 - direction * head, y - head * 0.75),
        QPointF(x2 - direction * head, y + head * 0.75),
    ]))


def app_pixmap(size: int = 64, color: str = APP_BLUE, dpr: float = 1.0, badge: str | None = None) -> QPixmap:
    """アプリのアイコン: 角丸の四角に、行き来する2本の矢印。badge は右下の状態色（トレイ用）。"""
    pm, p = _canvas(size, dpr)
    s = float(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.2, s * 0.2)
    white = QColor("#FFFFFF")
    _arrow(p, s * 0.24, s * 0.76, s * 0.38, s * 0.16, s * 0.085, white)
    _arrow(p, s * 0.76, s * 0.24, s * 0.62, s * 0.16, s * 0.085, white)
    if badge:
        r = s * 0.2
        p.setPen(QPen(QColor("#FFFFFF"), s * 0.05))
        p.setBrush(QColor(badge))
        p.drawEllipse(QPointF(s - r - s * 0.03, s - r - s * 0.03), r, r)
    p.end()
    return pm


def app_icon(badge: str | None = None) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(size, badge=badge))
    return icon


def folder_pixmap(size: int, color: str, dpr: float = 1.0) -> QPixmap:
    pm, p = _canvas(size, dpr)
    s = float(size)
    c = QColor(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    tab = QPainterPath()
    tab.addRoundedRect(QRectF(s * 0.08, s * 0.2, s * 0.4, s * 0.25), s * 0.06, s * 0.06)
    p.drawPath(tab)
    body = QPainterPath()
    body.addRoundedRect(QRectF(s * 0.08, s * 0.3, s * 0.84, s * 0.5), s * 0.08, s * 0.08)
    p.drawPath(body)
    p.end()
    return pm


def pc_pixmap(size: int, color: str, dpr: float = 1.0) -> QPixmap:
    pm, p = _canvas(size, dpr)
    s = float(size)
    c = QColor(color)
    p.setPen(QPen(c, s * 0.08, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(s * 0.1, s * 0.18, s * 0.8, s * 0.48), s * 0.06, s * 0.06)
    p.drawLine(QPointF(s * 0.5, s * 0.66), QPointF(s * 0.5, s * 0.82))
    p.drawLine(QPointF(s * 0.3, s * 0.84), QPointF(s * 0.7, s * 0.84))
    p.end()
    return pm


def gear_pixmap(size: int, color: str, dpr: float = 1.0) -> QPixmap:
    pm, p = _canvas(size, dpr)
    s = float(size)
    c = QColor(color)
    p.translate(s / 2, s / 2)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    for i in range(8):
        p.save()
        p.rotate(i * 45)
        p.drawRoundedRect(QRectF(-s * 0.07, -s * 0.44, s * 0.14, s * 0.2), s * 0.03, s * 0.03)
        p.restore()
    p.drawEllipse(QPointF(0, 0), s * 0.3, s * 0.3)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.drawEllipse(QPointF(0, 0), s * 0.13, s * 0.13)
    p.end()
    return pm


def back_pixmap(size: int, color: str, dpr: float = 1.0) -> QPixmap:
    pm, p = _canvas(size, dpr)
    s = float(size)
    p.setPen(QPen(QColor(color), s * 0.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawLine(QPointF(s * 0.2, s * 0.5), QPointF(s * 0.8, s * 0.5))
    p.drawLine(QPointF(s * 0.2, s * 0.5), QPointF(s * 0.45, s * 0.25))
    p.drawLine(QPointF(s * 0.2, s * 0.5), QPointF(s * 0.45, s * 0.75))
    p.end()
    return pm


class StatusDot(QWidget):
    """状態の色を示す丸。"""

    def __init__(self, diameter: int = 14, color: str = "#868686", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._diameter = diameter
        self._color = QColor(color)
        self.setFixedSize(QSize(diameter + 2, diameter + 2))

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def color(self) -> str:
        return self._color.name().upper()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        r = self._diameter / 2
        p.drawEllipse(QPointF(self.width() / 2, self.height() / 2), r, r)
        p.end()
