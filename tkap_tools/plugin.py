"""Host wiring: one TKAP Tools menu and toolbar button over the three tools.

Each tool is still a self-contained package under this one. What this module
does is own the menu, ask each tool for its actions, and keep the tools from
taking each other down.

That isolation is the point of the ``try`` around every tool. Before the merge
the three plugins loaded independently, so a broken import in Section Drawing
left Phasing working. A single plugin normally throws that away -- one bad
import aborts ``initGui`` and every tool disappears at once. Importing each
tool separately and catching its failure keeps the old blast radius: a tool
that cannot load loses its own menu entries and says so in the log, and the
rest still come up.
"""

from __future__ import annotations

import importlib
import os
import traceback
from dataclasses import dataclass

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QMenu, QToolButton

# QAction is in QtWidgets under Qt5 and QtGui under Qt6.
try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # pragma: no cover - depends on QGIS build
    from qgis.PyQt.QtGui import QAction

LOG_TAG = "TKAP Tools"
MENU = "&TKAP Tools"
PLUGIN_DIR = os.path.dirname(__file__)
DOCS_URL = "https://amcgallian.github.io/tkap-qgis/"


@dataclass(frozen=True)
class ToolSpec:
    """Where to find one tool's plugin class."""

    key: str
    label: str
    module: str
    attr: str


# Order here is the order in the menu: roughly the order of a season's work --
# get the survey points in, phase what has been dug, then draw the sections.
TOOLS = (
    ToolSpec("points", "Survey Points to Polygons", ".points.plugin", "SurveyPointsPlugin"),
    ToolSpec("phasing", "Stratigraphic Phasing", ".phasing.phase_blaster", "TkapPhasingPlugin"),
    ToolSpec("section", "Section Drawing", ".section.plugin", "TkapSectionPlugin"),
)


class TkapToolsPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.tools = {}
        self.actions = []
        self.menu = None
        self.toolbutton = None
        self.toolbar_action = None
        self.help_action = None

    # -- QGIS hooks --------------------------------------------------------

    def initGui(self):  # noqa: N802 -- required QGIS plugin hook
        icon = self._icon()
        self.menu = QMenu("TKAP Tools", self.iface.mainWindow())
        self.menu.setIcon(icon)

        failed = []
        for spec in TOOLS:
            actions = self._load(spec)
            if actions is None:
                failed.append(spec.label)
                continue
            if not actions:
                continue
            # A titled section, not a bare separator. Once all five actions sit
            # in one flat menu, "Draw a section..." and "Split by Phase..." give
            # no clue which tool they belong to; the heading names the tool and
            # draws the rule at the same time.
            self.menu.addSection(spec.label)
            for action in actions:
                self.menu.addAction(action)
                self.iface.addPluginToVectorMenu(MENU, action)
                self.actions.append(action)

        # Help goes last, under its own heading, so it reads as being about the
        # plugin rather than about whichever tool happens to sit above it.
        self.menu.addSection("Help")
        help_action = QAction("Tutorials and help...", self.iface.mainWindow())
        help_action.setToolTip("Open the TKAP Tools documentation in your browser")
        help_action.triggered.connect(self.open_docs)
        self.menu.addAction(help_action)
        self.iface.addPluginToVectorMenu(MENU, help_action)
        self.actions.append(help_action)
        self.help_action = help_action

        self.toolbutton = QToolButton()
        self.toolbutton.setMenu(self.menu)
        self.toolbutton.setIcon(icon)
        self.toolbutton.setPopupMode(QToolButton.InstantPopup)
        self.toolbutton.setToolTip("TKAP Tools")
        self.toolbar_action = self.iface.addToolBarWidget(self.toolbutton)

        if failed:
            # Worth interrupting for: the operator asked for these tools and
            # they are not there. The log has the traceback.
            self.iface.messageBar().pushMessage(
                LOG_TAG,
                "These tools could not be loaded: {}. See the TKAP Tools log "
                "for details.".format(", ".join(failed)),
                level=Qgis.Warning,
                duration=12,
            )

    def unload(self):
        for spec in TOOLS:
            tool = self.tools.get(spec.key)
            if tool is None:
                continue
            try:
                tool.unload()
            except Exception:
                # One tool refusing to tear down must not strand the others'
                # menu entries or leave a dangling toolbar widget.
                log(f"{spec.label} failed to unload:\n{traceback.format_exc()}")
        self.tools = {}

        for action in self.actions:
            self.iface.removePluginVectorMenu(MENU, action)
        self.actions = []

        if self.toolbar_action is not None:
            self.iface.removeToolBarIcon(self.toolbar_action)
            self.toolbar_action = None
        self.toolbutton = None

        self.help_action = None

        if self.menu is not None:
            self.menu.clear()
            self.menu = None

    # -- actions -----------------------------------------------------------

    def open_docs(self):
        """Open the documentation site in the operator's browser."""
        QDesktopServices.openUrl(QUrl(DOCS_URL))

    # -- internals ---------------------------------------------------------

    def _load(self, spec):
        """Import and start one tool. Returns its actions, or None if it failed."""
        try:
            # import_module, not __import__: the latter takes a relative name
            # WITHOUT its leading dot when given a level, so passing
            # ".points.plugin" there resolves to "tkap_tools." and fails.
            # import_module wants the dot and reads the package from the second
            # argument.
            module = importlib.import_module(spec.module, __package__)
            tool = getattr(module, spec.attr)(self.iface)
            actions = tool.actions(self.iface.mainWindow())
        except Exception:
            log(f"{spec.label} could not be loaded:\n{traceback.format_exc()}")
            return None
        self.tools[spec.key] = tool
        return actions

    def _icon(self):
        path = os.path.join(PLUGIN_DIR, "icon.svg")
        return QIcon(path) if os.path.exists(path) else QIcon()


def log(message, level=Qgis.Warning):
    QgsMessageLog.logMessage(str(message), LOG_TAG, level)
