# -*- coding: utf-8 -*-
"""
plugin.py

Tool glue: builds the action that opens the dialog and hands it to the TKAP
Tools host, which owns the menu and the toolbar button. Keeps a single dialog
instance alive between runs so the last-used parameters stay put during a
session.
"""

import os

from qgis.PyQt.QtGui import QIcon

# QAction is in QtWidgets under Qt5 and QtGui under Qt6.
try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # pragma: no cover - depends on QGIS build
    from qgis.PyQt.QtGui import QAction

PLUGIN_DIR = os.path.dirname(__file__)


class SurveyPointsPlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def actions(self, parent):
        """Return this tool's menu actions. The host does the registering."""
        icon_path = os.path.join(PLUGIN_DIR, "icon.svg")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "Survey Points to Polygons…", parent)
        self.action.setObjectName("SurveyPointsToPolygonsAction")
        self.action.triggered.connect(self.run)
        return [self.action]

    def unload(self):
        self.action = None
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

    def run(self):
        # Imported here, not at module scope, so a problem in the dialog or in
        # core costs this one action rather than the whole plugin's menu.
        from .dialog import SurveyPointsDialog

        # Reuse one dialog so field/layer selections persist during the session.
        if self.dialog is None:
            self.dialog = SurveyPointsDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
