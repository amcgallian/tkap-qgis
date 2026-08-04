"""Tool wiring: menu action -> trace tool -> setup dialog -> drawing session.

Deliberately thin. All the decisions live in the modules this imports; what is
here is the state machine that gets from "user clicked the button" to "user is
editing polygons", and the guarantee that whatever happens, the project is put
back the way it was found.

Actions are built and handed to the TKAP Tools host, which owns the menu and
the toolbar button.
"""

from __future__ import annotations

from pathlib import Path

from qgis.core import Qgis, QgsMessageLog, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox

from .export_dialog import ExportDialog
from .figure import DIGITIZED, KIND_LABELS, WIREFRAME, export_figure
from .panel import SectionPanel
from .section_geom import SectionLine
from .session import SectionSession
from .setup_dialog import SectionSetupDialog
from .store import (
    line_from_metadata,
    load_polygons,
    read_metadata,
    save_session,
)
from .su_source import candidates_from_features
from .trace_tool import SectionTraceTool

PLUGIN_NAME = "TKAP Section Drawing"
LOG_TAG = "TKAP Section"


class TkapSectionPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.action: QAction | None = None
        self.open_action: QAction | None = None
        self.tool: SectionTraceTool | None = None
        self.session: SectionSession | None = None
        self.panel: SectionPanel | None = None
        self._source_layer = None

    # ------------------------------------------------------------ lifecycle --

    def actions(self, parent) -> list[QAction]:
        """Return this tool's menu actions. The host does the registering."""
        icon_path = Path(__file__).parent / "icon.png"
        icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
        self.action = QAction(icon, "Draw a section...", parent)
        self.action.setToolTip(
            "Draw a line across the wall you want to record, then trace its "
            "units over a photo of it."
        )
        self.action.triggered.connect(self.start)

        self.open_action = QAction("Open a saved section...", parent)
        self.open_action.setToolTip(
            "Reopen a section saved as a GeoPackage and carry on editing it."
        )
        self.open_action.triggered.connect(self.open_section)

        return [self.action, self.open_action]

    def unload(self) -> None:
        self._end_session(restore=True)
        self.action = None
        self.open_action = None
        if self.tool is not None:
            self.iface.mapCanvas().unsetMapTool(self.tool)
            self.tool = None

    # ---------------------------------------------------------------- start --

    def start(self) -> None:
        if self.session is not None:
            answer = QMessageBox.question(
                self.iface.mainWindow(), "A section is already open",
                "Finish the current section before starting another?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
            self._end_session(restore=True)

        self.iface.messageBar().pushMessage(
            PLUGIN_NAME,
            "Click one end of the wall, then the other. The shaded band shows "
            "the side you are looking at - press SPACE to swap sides. "
            "Right-click to cancel.",
            level=Qgis.Info, duration=15,
        )
        self.tool = SectionTraceTool(self.iface.mapCanvas())
        self.tool.traceDrawn.connect(self._on_trace)
        self.tool.cancelled.connect(self._on_trace_cancelled)
        self.tool.sideChanged.connect(self._on_side_changed)
        self.iface.mapCanvas().setMapTool(self.tool)

    def _on_trace_cancelled(self) -> None:
        self.iface.mapCanvas().unsetMapTool(self.tool)
        self.tool = None

    def _on_side_changed(self, flipped: bool) -> None:
        side = "right" if flipped else "left"
        self.iface.statusBarIface().showMessage(
            f"Looking at the wall on your {side} (SPACE to swap)", 4000
        )

    def _on_trace(self, start, end, flipped) -> None:
        self.iface.mapCanvas().unsetMapTool(self.tool)
        self.tool = None

        try:
            line = SectionLine(
                (start.x(), start.y()), (end.x(), end.y()), name="Section",
                flipped=flipped,
            )
        except ValueError as exc:
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, str(exc), level=Qgis.Warning, duration=8
            )
            return

        dialog = SectionSetupDialog(line, self.iface, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return

        self._source_layer = dialog.su_layer_combo.currentLayer()
        self._begin_session(dialog)

    # -------------------------------------------------------------- session --

    def _begin_session(self, dialog: SectionSetupDialog) -> None:
        session = SectionSession(
            line=dialog.line,
            iface=self.iface,
            space_number=dialog.space_number(),
            source_layer=self._source_layer,
            style_qml=dialog.style_qml(),
        )
        try:
            session.begin()
        except Exception as exc:
            self._log(f"Could not start the session: {exc}")
            QMessageBox.critical(
                self.iface.mainWindow(), "Could not start the section", str(exc)
            )
            session.end(restore=True)
            return

        self.session = session

        if dialog.photo_path and dialog.fit is not None:
            try:
                session.attach_photo(dialog.photo_path, dialog.fit)
            except Exception as exc:
                self._log(f"Photo placement failed: {exc}")
                self.iface.messageBar().pushMessage(
                    PLUGIN_NAME,
                    f"The photo could not be placed ({exc}). Carrying on without it.",
                    level=Qgis.Warning, duration=10,
                )

        try:
            seeded = session.seed(dialog.result_candidates())
        except Exception as exc:
            self._log(f"Seeding failed: {exc}")
            seeded = 0

        session.zoom_to_section()
        session.start_editing()

        self.panel = SectionPanel(
            session, self.iface,
            source_layer=self._source_layer,
            parent=self.iface.mainWindow(),
        )
        self._connect_panel()
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.panel)

        self.iface.messageBar().pushMessage(
            PLUGIN_NAME,
            f"{seeded} units placed. You are now looking at the wall face - "
            "across is distance along the wall, up is height above sea level. "
            "Use Finish in the panel when you are done.",
            level=Qgis.Success, duration=12,
        )

    def _end_session(self, *, restore: bool) -> None:
        if self.panel is not None:
            try:
                # Removing the dock must not re-trigger its "are you done?"
                # prompt: this teardown is already the answer.
                self.panel._finishing = True
                # Independently guarded, and before the dock goes: the frame
                # tool owns rubber bands on the canvas and holds the session.
                # Reaching here without the panel's own Finish path is normal
                # (unload, or reopening a section over this one), and a tool
                # left behind would draw handles over the restored project.
                try:
                    self.panel.release_frame_tool()
                except Exception:
                    pass
                self.iface.removeDockWidget(self.panel)
                self.panel.deleteLater()
            except Exception:
                pass
            self.panel = None

        if self.session is not None:
            try:
                self.session.end(restore=restore)
            except Exception as exc:
                self._log(f"Teardown problem: {exc}")
            self.session = None
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, "Section closed and the project restored.",
                level=Qgis.Info, duration=5,
            )

    def _connect_panel(self) -> None:
        """Wire the dock panel. Shared by starting a new section and reopening
        a saved one, so the two cannot drift apart."""
        self.panel.finished.connect(lambda: self._end_session(restore=True))
        self.panel.exportDigitized.connect(
            lambda title: self._export(DIGITIZED, title)
        )
        self.panel.exportWireframe.connect(
            lambda title: self._export(WIREFRAME, title)
        )
        self.panel.saveRequested.connect(self._save_session)

    # ------------------------------------------------------------ save/open --

    def _save_session(self) -> None:
        if self.session is None:
            return
        default = self.session.saved_to or (
            f"{self.session.default_title()}.gpkg".replace(" ", "_")
        )
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Save the section", default,
            "GeoPackage (*.gpkg)",
        )
        if not path:
            return
        title = self.panel.title_edit.text() if self.panel else ""
        try:
            written = save_session(self.session, path, title=title)
        except Exception as exc:
            self._log(f"Save failed: {exc}")
            QMessageBox.critical(
                self.iface.mainWindow(), "Could not save the section", str(exc)
            )
            return
        self.session.saved_to = str(written)
        self.iface.messageBar().pushMessage(
            PLUGIN_NAME, f"Section saved to {written}",
            level=Qgis.Success, duration=8,
        )

    def open_section(self) -> None:
        """Reopen a saved section and carry on editing it."""
        if self.session is not None:
            answer = QMessageBox.question(
                self.iface.mainWindow(), "A section is already open",
                "Finish the current section before opening another?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
            self._end_session(restore=True)

        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(), "Open a saved section", "",
            "GeoPackage (*.gpkg);;All files (*)",
        )
        if not path:
            return

        try:
            meta = read_metadata(path)
            line = line_from_metadata(meta)
            features, fields = load_polygons(path)
        except Exception as exc:
            self._log(f"Open failed: {exc}")
            QMessageBox.critical(
                self.iface.mainWindow(), "Could not open the section", str(exc)
            )
            return

        session = SectionSession(
            line=line,
            iface=self.iface,
            space_number=meta.get("space_number") or None,
            source_layer=self._source_layer,
            style_qml=meta.get("style_qml") or None,
        )
        try:
            session.begin()
        except Exception as exc:
            self._log(f"Could not reopen the session: {exc}")
            QMessageBox.critical(
                self.iface.mainWindow(), "Could not open the section", str(exc)
            )
            session.end(restore=True)
            return

        self.session = session
        session.saved_to = str(path)

        # The placed raster is referenced, not stored, so a moved or offline
        # share costs the backdrop but not the drawing.
        placed = meta.get("photo_placed") or ""
        if placed and Path(placed).exists():
            try:
                session.reload_placed_photo(placed)
            except Exception as exc:
                self._log(f"Could not reload the photo: {exc}")
        elif placed:
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                f"The rectified photo is no longer at {placed}; the drawing "
                "opened without it.",
                level=Qgis.Warning, duration=10,
            )

        session.candidates = candidates_from_features(features)
        restored = session.restore_polygons(features, fields)

        self.panel = SectionPanel(
            session, self.iface,
            source_layer=self._source_layer,
            parent=self.iface.mainWindow(),
        )
        self._connect_panel()
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.panel)

        session.zoom_to_section()
        session.start_editing()
        self.iface.messageBar().pushMessage(
            PLUGIN_NAME,
            f"Reopened {Path(path).name} - {restored} polygons across "
            f"{len(session.candidates)} SUs.",
            level=Qgis.Success, duration=8,
        )

    # --------------------------------------------------------------- output --

    def _export(self, kind: str, title: str) -> None:
        if self.session is None:
            return

        options = ExportDialog(
            self.session, kind, title or self.session.default_title(),
            source_layer=self._source_layer,
            parent=self.iface.mainWindow(),
        )
        if options.exec_() != options.Accepted:
            return
        spec = options.spec()

        label = KIND_LABELS.get(kind, kind).lower().replace(" ", "_")
        default = f"{spec.title or 'section'}_{label}.png".replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            f"Export the {KIND_LABELS.get(kind, kind).lower()}", default,
            "PNG image (*.png);;PDF (*.pdf);;SVG (*.svg)",
        )
        if not path:
            return
        # A filter picked without typing an extension leaves the name bare.
        if not Path(path).suffix:
            path += ".png"

        try:
            ok, message = export_figure(
                self.session, spec, path, source_layer=self._source_layer
            )
        except Exception as exc:
            self._log(f"Export failed: {exc}")
            QMessageBox.critical(self.iface.mainWindow(), "Export failed", str(exc))
            return

        self.iface.messageBar().pushMessage(
            PLUGIN_NAME, message,
            level=Qgis.Success if ok else Qgis.Warning, duration=10,
        )

    def _log(self, message: str) -> None:
        QgsMessageLog.logMessage(message, LOG_TAG, Qgis.Warning)
