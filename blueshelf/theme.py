"""Light and dark themes, and the generated app icon.

Other modules read COLORS at paint time rather than hard-coding shades, so switching theme
repaints everything without restarting.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF

ACCENT = "#f2545b"

DARK = {
    "bg": "#141519", "panel": "#1b1c22", "panel_hover": "#2f3038", "input": "#1f2026",
    "border": "#2c2d35", "text": "#e8e8ec", "muted": "#8d8f98", "sidebar": "#111216",
    "header": "#0f1013", "chrome": "#0c0c0f", "card": "#1b1c22", "card_hover": "#25262d",
    "thumb_bg": "#0f1014", "title_text": "#f1f1f4", "sub_text": "#9a9ca5", "selected": "#2b1c20",
    "row_selected": "#3a2328", "row_alt": "#17181d", "scroll": "#34353d", "menu": "#1f2026",
    "tooltip": "#2a2b32", "button": "#26272e", "button_border": "#30313a", "star": "#f5c451",
    "placeholder": "#4b4d56", "grid_line": "#22232a", "disabled": "#6c6e77",
}

LIGHT = {
    "bg": "#f3f4f7", "panel": "#ffffff", "panel_hover": "#e6e7ec", "input": "#ffffff",
    "border": "#d2d4da", "text": "#1b1c20", "muted": "#64666f", "sidebar": "#eaebef",
    "header": "#e4e5ea", "chrome": "#e4e5ea", "card": "#ffffff", "card_hover": "#eceef2",
    "thumb_bg": "#d8dade", "title_text": "#15161a", "sub_text": "#5a5c64", "selected": "#fbdfe1",
    "row_selected": "#f8d3d5", "row_alt": "#f7f8fa", "scroll": "#c2c4cb", "menu": "#ffffff",
    "tooltip": "#ffffff", "button": "#ffffff", "button_border": "#cdcfd6", "star": "#c08a00",
    "placeholder": "#a8aab2", "grid_line": "#e2e3e8", "disabled": "#9a9ca4",
}

COLORS = dict(DARK)
MODES = {"system": "Match Windows", "dark": "Dark", "light": "Light"}

STYLE = """
QMainWindow, QDialog { background: %(bg)s; }
QSplitter::handle { background: %(border)s; width: 1px; }
QTreeWidget#sidebar { background: %(sidebar)s; border: none; }
QTreeWidget#sidebar::item { padding: 4px 2px; }
QTreeWidget#sidebar::item:hover { background: %(panel_hover)s; }
QTreeWidget#sidebar::item:selected { background: %(selected)s; color: %(text)s; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background: %(input)s; border: 1px solid %(border)s; border-radius: 6px; padding: 5px 8px;
    selection-background-color: %(accent)s; color: %(text)s;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: %(accent)s; }
QComboBox QAbstractItemView { background: %(menu)s; selection-background-color: %(accent)s; }
QPushButton { background: %(button)s; border: 1px solid %(button_border)s; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: %(panel_hover)s; }
QPushButton:disabled { color: %(disabled)s; }
QPushButton#primary { background: %(accent)s; border-color: %(accent)s; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #ff6a70; }
QPushButton#danger { color: #d6353c; }
QListView#grid { background: %(bg)s; border: none; }
QTableView { background: %(panel)s; border: none; gridline-color: %(grid_line)s;
             selection-background-color: %(row_selected)s; selection-color: %(text)s;
             alternate-background-color: %(row_alt)s; }
QHeaderView::section { background: %(header)s; color: %(muted)s; border: none;
                       border-bottom: 1px solid %(border)s; padding: 6px; }
QWidget#playerControls, QWidget#playerHeader { background: %(chrome)s; }
QLabel#playerTitle { font-size: 14px; font-weight: 600; }
QToolButton#sideArrow { background: %(chrome)s; border: none; border-radius: 0; }
QToolButton#sideArrow:hover { background: %(panel_hover)s; }
QToolButton#sideArrow:disabled { background: %(chrome)s; }
QSlider::groove:horizontal { height: 4px; background: %(scroll)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider::handle:horizontal { background: %(text)s; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
QToolButton { border: none; padding: 4px 8px; border-radius: 4px; color: %(text)s; }
QToolButton:hover { background: %(panel_hover)s; }
QToolButton:checked { background: %(selected)s; }
QStatusBar { background: %(header)s; color: %(muted)s; }
QStatusBar QLabel { color: %(muted)s; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 5px; min-height: 30px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: %(scroll)s; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QLabel#muted { color: %(muted)s; }
QLabel#heading { font-size: 18px; font-weight: 600; }
QMenu { background: %(menu)s; border: 1px solid %(border)s; padding: 4px; }
QMenu::item { padding: 5px 20px; border-radius: 4px; }
QMenu::item:selected { background: %(accent)s; color: #ffffff; }
QMenuBar { background: %(header)s; }
QMenuBar::item:selected { background: %(panel_hover)s; }
QProgressBar { background: %(panel_hover)s; border: none; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }
QFrame#card { background: %(card)s; border-radius: 10px; }
QScrollArea { border: none; background: transparent; }
QTabWidget::pane { border: 1px solid %(border)s; border-radius: 6px; }
QTabBar::tab { background: transparent; padding: 7px 14px; color: %(muted)s; }
QTabBar::tab:selected { color: %(text)s; border-bottom: 2px solid %(accent)s; }
QToolTip { background: %(tooltip)s; color: %(text)s; border: 1px solid %(border)s; }
QCheckBox, QRadioButton, QLabel { color: %(text)s; }
"""


def windows_prefers_light() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        return False


def resolve_mode(mode: str) -> str:
    if mode in ("light", "dark"):
        return mode
    return "light" if windows_prefers_light() else "dark"


def apply_theme(app, mode: str = "system") -> str:
    """Paint the app light or dark. Returns the mode actually used."""
    resolved = resolve_mode(mode)
    COLORS.clear()
    COLORS.update(LIGHT if resolved == "light" else DARK)
    COLORS["accent"] = ACCENT
    app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.Window: COLORS["bg"], QPalette.WindowText: COLORS["text"], QPalette.Base: COLORS["panel"],
        QPalette.AlternateBase: COLORS["row_alt"], QPalette.ToolTipBase: COLORS["tooltip"],
        QPalette.ToolTipText: COLORS["text"], QPalette.Text: COLORS["text"],
        QPalette.PlaceholderText: COLORS["muted"], QPalette.Button: COLORS["button"],
        QPalette.ButtonText: COLORS["text"], QPalette.BrightText: "#ffffff", QPalette.Highlight: ACCENT,
        QPalette.HighlightedText: "#ffffff", QPalette.Link: ACCENT,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        palette.setColor(QPalette.Disabled, role, QColor(COLORS["disabled"]))
    app.setPalette(palette)
    app.setStyleSheet(STYLE % COLORS)
    return resolved


def make_icon(size: int = 256) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    body = QPainterPath()
    body.addRoundedRect(QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.88), size * 0.22, size * 0.22)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, QColor("#ff7a6b"))
    gradient.setColorAt(1, QColor("#b8325a"))
    painter.fillPath(body, gradient)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("white"))
    painter.drawPolygon(QPolygonF([QPointF(size * 0.40, size * 0.29), QPointF(size * 0.40, size * 0.71),
                                   QPointF(size * 0.73, size * 0.50)]))
    painter.end()
    return pixmap


def draw_icon(name: str, size: int = 20, color: str = "") -> QIcon:
    """Line icons for the player controls, drawn in the current theme's text colour."""
    scale = 3
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color or COLORS.get("text", "#e8e8ec"))
    u = size * scale / 20  # icons are designed on a 20x20 grid

    def pt(x, y):
        return QPointF(x * u, y * u)

    def fill_poly(*points):
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QPolygonF([pt(x, y) for x, y in points]))

    def bar(x, y, w, h):
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(x * u, y * u, w * u, h * u), 0.6 * u, 0.6 * u)

    def stroke():
        p.setPen(QPen(c, 1.8 * u, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)

    if name == "play":
        fill_poly((6, 4), (6, 16), (16, 10))
    elif name == "pause":
        bar(5, 4, 3.5, 12)
        bar(11.5, 4, 3.5, 12)
    elif name == "prev":
        bar(4, 5, 2.2, 10)
        fill_poly((16, 5), (16, 15), (7.5, 10))
    elif name == "next":
        bar(13.8, 5, 2.2, 10)
        fill_poly((4, 5), (4, 15), (12.5, 10))
    elif name in ("volume", "muted"):
        fill_poly((2.5, 7.5), (6, 7.5), (10.5, 4), (10.5, 16), (6, 12.5), (2.5, 12.5))
        stroke()
        if name == "volume":
            p.drawArc(QRectF(8 * u, 6.5 * u, 6 * u, 7 * u), -55 * 16, 110 * 16)
            p.drawArc(QRectF(6 * u, 3.5 * u, 11 * u, 13 * u), -55 * 16, 110 * 16)
        else:
            p.drawLine(pt(13, 7.5), pt(18, 12.5))
            p.drawLine(pt(18, 7.5), pt(13, 12.5))
    elif name in ("fullscreen", "exit_fullscreen"):
        stroke()
        if name == "fullscreen":
            corners = [((3, 7), (3, 3), (7, 3)), ((13, 3), (17, 3), (17, 7)),
                       ((17, 13), (17, 17), (13, 17)), ((7, 17), (3, 17), (3, 13))]
        else:
            corners = [((7, 3), (7, 7), (3, 7)), ((17, 7), (13, 7), (13, 3)),
                       ((13, 17), (13, 13), (17, 13)), ((3, 13), (7, 13), (7, 17))]
        for corner in corners:
            p.drawPolyline(QPolygonF([pt(x, y) for x, y in corner]))
    elif name == "back":
        stroke()
        p.drawPolyline(QPolygonF([pt(9, 4), pt(3, 10), pt(9, 16)]))
        p.drawLine(pt(3.5, 10), pt(17, 10))
    elif name == "close":
        stroke()
        p.drawLine(pt(5, 5), pt(15, 15))
        p.drawLine(pt(15, 5), pt(5, 15))
    elif name == "rewind":
        fill_poly((9.5, 5), (9.5, 15), (3.5, 10))
        fill_poly((16.5, 5), (16.5, 15), (10.5, 10))
    elif name == "forward":
        fill_poly((3.5, 5), (3.5, 15), (9.5, 10))
        fill_poly((10.5, 5), (10.5, 15), (16.5, 10))
    elif name in ("chevron_left", "chevron_right"):
        p.setPen(QPen(c, 2.2 * u, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        if name == "chevron_left":
            p.drawPolyline(QPolygonF([pt(13, 3.5), pt(6.5, 10), pt(13, 16.5)]))
        else:
            p.drawPolyline(QPolygonF([pt(7, 3.5), pt(13.5, 10), pt(7, 16.5)]))
    elif name in ("heart", "heart_outline"):
        heart = QPainterPath()
        heart.moveTo(pt(10, 16.5))
        heart.cubicTo(pt(1.5, 10.5), pt(4, 3.5), pt(10, 7))
        heart.cubicTo(pt(16, 3.5), pt(18.5, 10.5), pt(10, 16.5))
        if name == "heart":
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPath(heart)
        else:
            stroke()
            p.drawPath(heart)
    elif name == "trash":
        stroke()
        p.drawLine(pt(3.5, 5.5), pt(16.5, 5.5))
        p.drawPolyline(QPolygonF([pt(5.5, 5.5), pt(6.5, 17), pt(13.5, 17), pt(14.5, 5.5)]))
        p.drawPolyline(QPolygonF([pt(8, 5.5), pt(8, 3.5), pt(12, 3.5), pt(12, 5.5)]))
    p.end()
    pixmap.setDevicePixelRatio(scale)
    return QIcon(pixmap)
