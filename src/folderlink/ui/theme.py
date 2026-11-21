"""見た目（Windows 11 風、ライト／ダーク、角丸カード、青のアクセント）。

状態の色: 緑=同期完了、青=同期中、黄=注意（一時停止・競合）、赤=エラー、灰=オフライン・未設定。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class Palette:
    name: str
    window: str
    card: str
    card_alt: str
    border: str
    text: str
    subtext: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    button: str
    button_hover: str
    button_pressed: str
    button_border: str
    input: str
    disabled_text: str
    green: str
    blue: str
    yellow: str
    red: str
    gray: str
    warn_bg: str
    warn_border: str
    warn_text: str
    error_bg: str
    error_border: str
    info_bg: str
    info_border: str


LIGHT = Palette(
    name="light",
    window="#F3F3F3", card="#FFFFFF", card_alt="#FAFAFA", border="#E3E3E3",
    text="#1B1B1B", subtext="#5C5C5C",
    accent="#0067C0", accent_hover="#1975C5", accent_pressed="#005A9E", accent_text="#FFFFFF",
    button="#FBFBFB", button_hover="#F0F0F0", button_pressed="#E8E8E8", button_border="#D1D1D1",
    input="#FFFFFF", disabled_text="#A0A0A0",
    green="#107C10", blue="#0067C0", yellow="#B88700", red="#C42B1C", gray="#868686",
    warn_bg="#FFF4CE", warn_border="#EBD58A", warn_text="#4D3B00",
    error_bg="#FDE7E9", error_border="#F1BBBC",
    info_bg="#EFF6FC", info_border="#C7E0F4",
)

DARK = Palette(
    name="dark",
    window="#202020", card="#2B2B2B", card_alt="#323232", border="#3D3D3D",
    text="#FFFFFF", subtext="#C8C8C8",
    accent="#4CC2FF", accent_hover="#47B1E8", accent_pressed="#42A1D2", accent_text="#000000",
    button="#373737", button_hover="#404040", button_pressed="#2F2F2F", button_border="#4A4A4A",
    input="#1E1E1E", disabled_text="#7A7A7A",
    green="#6CCB5F", blue="#4CC2FF", yellow="#FCE100", red="#FF99A4", gray="#9D9D9D",
    warn_bg="#433519", warn_border="#6B5420", warn_text="#FFF1B8",
    error_bg="#442726", error_border="#7A3B3B",
    info_bg="#1F2F3D", info_border="#2F4F68",
)

_FONT_CANDIDATES = ("Yu Gothic UI", "Meiryo UI", "Segoe UI", "MS UI Gothic")


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        return False


def resolve(theme: str) -> Palette:
    """設定値（system / light / dark）から実際の配色を決める。"""
    if theme == "dark":
        return DARK
    if theme == "light":
        return LIGHT
    return DARK if system_prefers_dark() else LIGHT


def state_color(p: Palette, color_name: str) -> str:
    return {"green": p.green, "blue": p.blue, "yellow": p.yellow, "red": p.red}.get(color_name, p.gray)


def ui_font(point_size: float = 10.0) -> QFont:
    families = set(QFontDatabase.families())
    for name in _FONT_CANDIDATES:
        if name in families:
            font = QFont(name)
            break
    else:
        font = QFont()
    font.setPointSizeF(point_size)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def _check_image(p: Palette) -> str:
    """チェックボックスのチェック印の画像を作り、その場所（スタイルシート用の書き方）を返す。

    スタイルシートではチェック印を図形として描けないので、小さな PNG を一時フォルダに置く。
    作れなかった場合は空文字（その場合もチェック済みは色で分かる）。
    """
    from PySide6.QtCore import QDir, QPointF
    from PySide6.QtGui import QPainter, QPen, QPixmap

    try:
        folder = QDir(QDir.tempPath())
        folder.mkpath("FolderLink-ui")
        path = folder.filePath(f"FolderLink-ui/check_{p.name}.png")
        size = 32
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(p.accent_text), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.drawPolyline([QPointF(7, 17), QPointF(13.5, 23), QPointF(25, 10)])
        painter.end()
        if not pm.save(path, "PNG"):
            return ""
        return path.replace("\\", "/")
    except Exception:
        return ""


def stylesheet(p: Palette, check_image: str = "") -> str:
    check_rule = f"image: url({check_image});" if check_image else ""
    return f"""
    QCheckBox::indicator {{
        width: 16px; height: 16px; border: 1px solid {p.subtext}; border-radius: 4px; background: {p.input};
    }}
    QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
    QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; {check_rule} }}
    QCheckBox::indicator:disabled {{ border-color: {p.button_border}; background: {p.card_alt}; }}
    QCheckBox::indicator:checked:disabled {{ background: {p.disabled_text}; border-color: {p.disabled_text}; }}
    QCheckBox:disabled {{ color: {p.disabled_text}; }}
""" + f"""
    QWidget {{ color: {p.text}; }}
    QWidget#Root, QDialog, QStackedWidget, QWidget#Page {{ background: {p.window}; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QFrame#Card {{ background: {p.card}; border: 1px solid {p.border}; border-radius: 8px; }}
    QFrame#Banner[kind="warn"] {{ background: {p.warn_bg}; border: 1px solid {p.warn_border}; border-radius: 8px; }}
    QFrame#Banner[kind="error"] {{ background: {p.error_bg}; border: 1px solid {p.error_border}; border-radius: 8px; }}
    QFrame#Banner[kind="info"] {{ background: {p.info_bg}; border: 1px solid {p.info_border}; border-radius: 8px; }}
    QFrame#Banner QLabel {{ background: transparent; }}
    QLabel {{ background: transparent; }}
    QLabel#AppTitle {{ font-size: 15pt; font-weight: 600; }}
    QLabel#PageTitle {{ font-size: 13pt; font-weight: 600; }}
    QLabel#StateLabel {{ font-size: 15pt; font-weight: 600; }}
    QLabel#SectionTitle {{ color: {p.subtext}; font-size: 9pt; font-weight: 600; }}
    QLabel#Sub, QLabel[sub="true"] {{ color: {p.subtext}; }}
    QLabel#Small {{ color: {p.subtext}; font-size: 9pt; }}
    QLabel#MetricValue {{ font-size: 11pt; font-weight: 600; }}
    QLabel#Mono {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: 11pt; }}
    QLabel#ErrorText {{ color: {p.red}; }}
    QLabel#OkText {{ color: {p.green}; }}
    QLabel#StepActive {{ color: {p.accent}; font-weight: 600; }}
    QLabel#StepDone {{ color: {p.green}; }}
    QLabel#StepTodo {{ color: {p.disabled_text}; }}

    QPushButton {{
        background: {p.button}; border: 1px solid {p.button_border}; border-radius: 5px;
        padding: 6px 14px; min-height: 20px;
    }}
    QPushButton:hover {{ background: {p.button_hover}; }}
    QPushButton:pressed {{ background: {p.button_pressed}; }}
    QPushButton:disabled {{ color: {p.disabled_text}; }}
    QPushButton[accent="true"] {{
        background: {p.accent}; color: {p.accent_text}; border: 1px solid {p.accent}; font-weight: 600;
    }}
    QPushButton[accent="true"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton[accent="true"]:pressed {{ background: {p.accent_pressed}; border-color: {p.accent_pressed}; }}
    QPushButton[accent="true"]:disabled {{ background: {p.button_border}; border-color: {p.button_border}; color: {p.disabled_text}; }}
    QPushButton[flat="true"] {{ background: transparent; border: 1px solid transparent; }}
    QPushButton[flat="true"]:hover {{ background: {p.button_hover}; }}
    QPushButton[danger="true"] {{ color: {p.red}; }}

    QProgressBar {{
        background: {p.border}; border: none; border-radius: 4px; min-height: 8px; max-height: 8px;
    }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
    QProgressBar[state="green"]::chunk {{ background: {p.green}; }}
    QProgressBar[state="yellow"]::chunk {{ background: {p.yellow}; }}
    QProgressBar[state="red"]::chunk {{ background: {p.red}; }}
    QProgressBar[state="gray"]::chunk {{ background: {p.gray}; }}

    QLineEdit, QPlainTextEdit, QTextEdit {{
        background: {p.input}; border: 1px solid {p.button_border}; border-radius: 5px; padding: 5px 8px;
        selection-background-color: {p.accent}; selection-color: {p.accent_text};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
        border: 1px solid {p.accent};
    }}
    QSpinBox, QComboBox {{ min-height: 26px; padding-left: 6px; }}
    QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"] {{ border: 1px solid {p.red}; }}
    QLineEdit:read-only, QPlainTextEdit:read-only {{ background: {p.card_alt}; }}
    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}

    QTableWidget, QListWidget, QTreeWidget {{
        background: {p.card}; alternate-background-color: {p.card_alt}; border: 1px solid {p.border};
        border-radius: 6px; gridline-color: {p.border};
    }}
    QTableWidget::item, QListWidget::item, QTreeWidget::item {{ padding: 4px 6px; }}
    QTableWidget::item:selected, QListWidget::item:selected, QTreeWidget::item:selected {{
        background: {p.accent}; color: {p.accent_text};
    }}
    QHeaderView::section {{
        background: {p.card_alt}; color: {p.subtext}; border: none; border-bottom: 1px solid {p.border};
        padding: 6px 8px; font-weight: 600;
    }}
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{
        background: transparent; padding: 7px 16px; border: none; border-bottom: 2px solid transparent;
        color: {p.subtext};
    }}
    QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; font-weight: 600; }}
    QMenu {{ background: {p.card}; border: 1px solid {p.border}; padding: 4px; }}
    QMenu::item {{ padding: 6px 24px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {p.button_hover}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}
    QToolTip {{ background: {p.card}; color: {p.text}; border: 1px solid {p.border}; padding: 4px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {p.button_border}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {p.button_border}; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    """


def apply_theme(app: QApplication, theme: str) -> Palette:
    """アプリ全体に配色を適用し、使った配色を返す。"""
    p = resolve(theme)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(p.window))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
    pal.setColor(QPalette.ColorRole.Base, QColor(p.input))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(p.card_alt))
    pal.setColor(QPalette.ColorRole.Text, QColor(p.text))
    pal.setColor(QPalette.ColorRole.Button, QColor(p.button))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(p.text))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(p.accent_text))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.card))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(p.text))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.disabled_text))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.disabled_text))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.disabled_text))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(p.disabled_text))
    app.setPalette(pal)
    app.setFont(ui_font())
    app.setStyleSheet(stylesheet(p, _check_image(p)))
    app.setProperty("flPalette", p.name)
    return p
