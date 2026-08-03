"""Keep the dialogs usable on a laptop screen.

Qt will not shrink a window below the minimum height its contents demand. Both
dialogs have grown enough that on a 1080p screen -- once the QGIS title bar and
taskbar are accounted for -- the window opens taller than the desktop, and the
buttons along the bottom end up off-screen with no way to drag them back.

Putting the body in a scroll area drops that floor, so the window can be resized
to anything. The button box stays *outside* the scroll area, pinned to the
bottom, so Export/Cancel are reachable however small the window gets.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import QScrollArea, QVBoxLayout, QWidget


def scrollable_body(dialog, margin: int = 9):
    """Give ``dialog`` a scrolling body and a fixed footer.

    Returns ``(body, footer)``: add the form content to ``body``, and anything
    that must stay visible -- progress bars, button boxes -- to ``footer``.
    """
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    content = QWidget()
    body = QVBoxLayout(content)
    body.setContentsMargins(margin, margin, margin, margin)

    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    # Never scroll sideways: the body tracks the dialog's width, so only the
    # vertical bar should ever appear.
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setWidget(content)
    outer.addWidget(area, 1)

    footer = QVBoxLayout()
    footer.setContentsMargins(margin, margin, margin, margin)
    outer.addLayout(footer)

    return body, footer


def fit_to_screen(dialog, width: int, height: int, margin: int = 120) -> None:
    """Open at ``width`` x ``height``, or as close as the screen allows.

    ``margin`` leaves room for the title bar and taskbar, which are not part of
    the reported available geometry on every platform.
    """
    screen = None
    try:
        screen = dialog.screen()
    except AttributeError:  # pragma: no cover - Qt < 5.14
        pass
    if screen is None:
        screen = QGuiApplication.primaryScreen()

    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, max(available.width() - margin, 320))
        height = min(height, max(available.height() - margin, 240))

    dialog.resize(width, height)
