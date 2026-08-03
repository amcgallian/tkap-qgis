"""Tool class for TKAP Stratigraphic Phasing.

Builds its actions and hands them to the TKAP Tools host, which owns the menu
and the toolbar button. Nothing here registers itself with ``iface`` -- that is
the host's job, so all three tools land in one menu rather than three.
"""

from __future__ import annotations

import os

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtGui import QIcon

# QAction is in QtWidgets under Qt5 and QtGui under Qt6.
try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # pragma: no cover - depends on QGIS build
    from qgis.PyQt.QtGui import QAction

LOG_TAG = "TKAP Phasing"


class TkapPhasingPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self._actions = []
        self.dialog = None

    # -- host interface ----------------------------------------------------

    def actions(self, parent):
        """Return this tool's menu actions. The host does the registering."""
        icon_path = os.path.join(self.plugin_dir, "icon.svg")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        return [
            self._action(
                icon,
                "Split by Phase...",
                "Split SUs into one layer per phase for a single field",
                self.run_split,
                parent,
            ),
            self._action(
                icon,
                "Export Phase Plans...",
                "Export one plan per phase using a print layout you designed",
                self.run_export,
                parent,
            ),
        ]

    def unload(self):
        self._actions = []
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

    def _action(self, icon, text, tooltip, slot, parent):
        action = QAction(icon, text, parent)
        action.setToolTip(tooltip)
        action.triggered.connect(slot)
        self._actions.append(action)
        return action

    # -- entry points ------------------------------------------------------

    def run_split(self):
        from .phase_blaster_dialog import PhasingDialog

        # Rebuilt each time so the layer list and unique values are current.
        self.dialog = PhasingDialog(self.iface, self.iface.mainWindow())
        self.dialog.exec_()
        # Warnings are logged by the dialog itself, so they survive a run the
        # operator then cancels out of.

    def run_export(self):
        from .export_dialog import ExportPlansDialog

        self.dialog = ExportPlansDialog(self.iface, self.iface.mainWindow())
        self.dialog.exec_()


def log(message, level=Qgis.Info):
    """Convenience logger used by the runner and dialogs."""
    QgsMessageLog.logMessage(str(message), LOG_TAG, level)
