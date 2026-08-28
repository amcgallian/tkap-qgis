"""Build a set of phase plans from a plan file, then export them.

The input end of *Export Phase Plans*. Instead of splitting the SU layer on
``space_phase`` and hoping the publication plans fall out of it, this reads the
plans straight from the write-up: one ``Map Title:`` per plan, with the query
that defines it. It builds the layers, checks every query against the layers it
was pointed at, and hands the result to the export dialog -- which then does
exactly what it does for a phase split: find the title label, drive the legend,
step through a preview, write one file per plan.
"""

from __future__ import annotations

import os

from qgis.core import QgsProject, QgsSettings
from qgis.gui import QgsMapLayerComboBox

try:
    from qgis.core import QgsMapLayerProxyModel
except ImportError:  # pragma: no cover - depends on QGIS build
    from qgis.gui import QgsMapLayerProxyModel

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .dialog_layout import fit_to_screen, scrollable_body
from .plan_file import read_plan_file, template_text, tokens_from_layer
from .plan_set_runner import (
    MODE_COPY,
    MODE_LIVE,
    build_plan_set,
    check_plans,
    clear_group,
    describe_query_problem,
    group_layer_count,
    group_names,
    summarise,
)

SETTINGS_PREFIX = "tkap_phasing/plan_set"

#: Title template handed to the export dialog. The plan file already wrote the
#: title out in full, so the default is to use it verbatim.
DEFAULT_PLAN_TEMPLATE = "{title}"

#: Plans inherit the source layer's symbology -- a plan that did not look like
#: the SU layer would not be publishable.
INHERIT_STYLE = True

#: What each mode is called in the dialog, and what it means for an edit.
MODES = (
    (MODE_COPY, "Editable copies"),
    (MODE_LIVE, "Live filtered layers"),
)

MODE_NOTES = {
    MODE_COPY: (
        "Each plan holds its own copy of the features its query selected. Edit "
        "them freely - reshape a polygon for a figure, fix a boundary - and "
        "nothing can reach the database, because a copy has no connection to "
        "it.\n"
        "The trade: a copy does not track the source, its filter is fixed once "
        "built, and it is gone when QGIS closes unless you save it somewhere."
    ),
    MODE_LIVE: (
        "Each plan is a live view of the source table, tracking the data, with "
        "its filter editable in the Query Builder.\n"
        "Editing is unlocked, which means an edit to a plan layer - including "
        "an accidental node drag - is written to the source table. The SU and "
        "Features layers themselves are still never touched by this tool."
    ),
}


class PlanSetDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.plan_file = None
        self.outcome = None
        self._loaded_path = ""
        self._replaced = 0
        self._seeded_groups = ()
        # Which combos the operator has set themselves. A combo they have not
        # touched stays free for the file's own queries to choose (_guess_layers).
        self._picked = set()
        #: Read by the caller after exec_(): the preselect for the export dialog,
        #: or None if the plans were only built.
        self.export_request = None
        self.setWindowTitle("Plans from a Plan File")
        self.setMinimumWidth(620)
        self.setSizeGripEnabled(True)
        self._build_ui()
        self._connect()
        self._restore_settings()
        fit_to_screen(self, 760, 860)

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        layout, footer = scrollable_body(self)

        # --- the file ------------------------------------------------------
        file_box = QGroupBox("Plan file")
        file_form = QFormLayout(file_box)

        file_row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("Field3PhasePlans2026_Queries.txt")
        self.file_button = QPushButton("Browse...")
        self.reload_button = QPushButton("Reload")
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(self.file_button)
        file_row.addWidget(self.reload_button)
        file_form.addRow("File:", file_row)

        self.format_hint = QLabel(
            "One 'Map Title:' per plan, then a block per layer -- a name ending "
            "in FeaturesPlan or SUsPlan, a colon, and the query under it. "
            "'-----' rules mark sections, '#' lines are notes. Queries are used "
            "exactly as written."
        )
        self.format_hint.setWordWrap(True)
        self.format_hint.setStyleSheet("QLabel { color: palette(mid); }")
        file_form.addRow("", self.format_hint)

        self.template_button = QPushButton("Save a template to fill in...")
        self.template_button.setToolTip(
            "Write a plan file with the format explained in it and two working "
            "example plans, ready to edit."
        )
        template_row = QHBoxLayout()
        template_row.addWidget(self.template_button)
        template_row.addStretch(1)
        file_form.addRow("", template_row)

        layout.addWidget(file_box)

        # --- the layers ----------------------------------------------------
        layer_box = QGroupBox("Layers the queries run against")
        layer_form = QFormLayout(layer_box)

        self.su_combo = QgsMapLayerComboBox()
        self.su_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.su_combo.setAllowEmptyLayer(True)
        self.su_combo.setToolTip("The layer the ...SUsPlan queries filter.")
        layer_form.addRow("SU layer:", self.su_combo)

        self.feature_combo = QgsMapLayerComboBox()
        self.feature_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.feature_combo.setAllowEmptyLayer(True)
        self.feature_combo.setToolTip(
            "The layer the ...FeaturesPlan queries filter. Leave empty to build "
            "SU plans only."
        )
        layer_form.addRow("Features layer:", self.feature_combo)

        self.match_hint = QLabel("")
        self.match_hint.setWordWrap(True)
        self.match_hint.setStyleSheet("QLabel { color: palette(mid); }")
        layer_form.addRow("", self.match_hint)

        layout.addWidget(layer_box)

        # --- what came out -------------------------------------------------
        self.summary = QLabel("Choose a plan file to begin.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setStyleSheet(
            "QLabel { background: palette(alternate-base); padding: 8px; "
            "border-radius: 4px; }"
        )
        self.summary.setMinimumHeight(64)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Plan", "SUs", "Features", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setMinimumHeight(220)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        select_row = QHBoxLayout()
        self.all_button = QPushButton("Select all")
        self.none_button = QPushButton("Select none")
        select_row.addWidget(self.all_button)
        select_row.addWidget(self.none_button)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        self.warnings_box = QPlainTextEdit()
        self.warnings_box.setReadOnly(True)
        self.warnings_box.setMaximumHeight(110)
        self.warnings_box.setPlaceholderText("File warnings appear here.")
        layout.addWidget(self.warnings_box)

        # --- where they go -------------------------------------------------
        group_box = QGroupBox("Layer groups")
        group_form = QFormLayout(group_box)

        self.mode_combo = QComboBox()
        for value, label in MODES:
            self.mode_combo.addItem(label, value)
        group_form.addRow("Plans are:", self.mode_combo)

        self.mode_note = QLabel("")
        self.mode_note.setWordWrap(True)
        self.mode_note.setTextFormat(Qt.PlainText)
        group_form.addRow("", self.mode_note)

        self.plans_group_edit = QLineEdit()
        group_form.addRow("Plans group:", self.plans_group_edit)

        self.features_group_edit = QLineEdit()
        group_form.addRow("Features group:", self.features_group_edit)

        note = QLabel(
            "Both layers of a plan take the plan's name, which is how the export "
            "dialog pairs them. Rebuilding replaces what is in these groups, "
            "never the layers chosen above.\n"
            "The SU and Features layers are only ever read. A plan is a separate "
            "live view of the same source with its own filter, and is read-only "
            "so an edit session cannot reach the database."
        )
        note.setWordWrap(True)
        note.setStyleSheet("QLabel { color: palette(mid); }")
        group_form.addRow("", note)

        layout.addWidget(group_box)
        layout.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        footer.addWidget(self.progress)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.buttons.button(QDialogButtonBox.Ok).setText("Build and Export...")
        self.build_button = self.buttons.addButton(
            "Build layers only", QDialogButtonBox.ActionRole
        )
        self.build_button.setToolTip(
            "Add the plan layers to the project without opening the export "
            "dialog. Export them later with Export Phase Plans."
        )
        footer.addWidget(self.buttons)

    def _connect(self):
        self.template_button.clicked.connect(self._save_template)
        self.file_button.clicked.connect(self._pick_file)
        self.reload_button.clicked.connect(lambda: self._load_file(force=True))
        self.file_edit.editingFinished.connect(self._load_file)
        self.su_combo.layerChanged.connect(lambda _: self._layer_picked("su"))
        self.feature_combo.layerChanged.connect(
            lambda _: self._layer_picked("feature")
        )
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.all_button.clicked.connect(lambda: self._set_all(True))
        self.none_button.clicked.connect(lambda: self._set_all(False))
        self.buttons.accepted.connect(self._build_and_export)
        self.buttons.rejected.connect(self.reject)
        self.build_button.clicked.connect(self._build_only)

    # -- the file ----------------------------------------------------------

    def _save_template(self):
        """Write a plan file to fill in, then load it so it can be checked."""
        from datetime import date

        home = QgsProject.instance().homePath() or os.path.expanduser("~")
        suggested = os.path.join(home, f"PhasePlans{date.today().year}_Queries.txt")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save plan file template",
            suggested,
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(template_text())
        except OSError as exc:
            QMessageBox.critical(self, "Could not write template", str(exc))
            return

        # Loaded straight away: the template is a working plan file, so the
        # table fills in and the format can be seen doing something before a
        # word of it is changed.
        self.file_edit.setText(path)
        self._load_file(force=True)

        opened = QMessageBox.question(
            self,
            "Template saved",
            f"Saved to\n{path}\n\nIt has two example plans in it, already "
            f"loaded below. Open it in your text editor now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if opened == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _pick_file(self):
        start = self.file_edit.text().strip() or QgsProject.instance().homePath()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Plan file",
            start or os.path.expanduser("~"),
            "Plan files (*.txt *.md *.plans);;All files (*)",
        )
        if path:
            self.file_edit.setText(path)
            self._load_file(force=True)

    def _load_file(self, force=False):
        """Read the file in the box and check it. ``force`` re-reads the same one.

        editingFinished fires on every focus change, so an unforced call for the
        file already loaded returns rather than re-counting the whole set.
        """
        path = self.file_edit.text().strip()
        if not path:
            return
        if not force and self._loaded_path == path and self.plan_file is not None:
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "No such file", f"Cannot read\n{path}")
            return

        try:
            self.plan_file = read_plan_file(path)
        except OSError as exc:
            QMessageBox.critical(self, "Cannot read plan file", str(exc))
            return
        except UnicodeDecodeError:
            QMessageBox.critical(
                self,
                "Cannot read plan file",
                "That file is not UTF-8 text. Save it as UTF-8 and try again.",
            )
            return

        self._loaded_path = path
        stem = os.path.splitext(os.path.basename(path))[0]
        plans_group, features_group = group_names(stem)
        # Seeded from the file name, but never over a name typed by hand: only
        # a box left blank or still holding the last file's suggestion moves.
        for edit, suggestion, previous in (
            (
                self.plans_group_edit,
                plans_group,
                (self._seeded_groups or ("", ""))[0],
            ),
            (
                self.features_group_edit,
                features_group,
                (self._seeded_groups or ("", ""))[1],
            ),
        ):
            current = edit.text().strip()
            if not current or current == previous:
                edit.setText(suggestion)
        self._seeded_groups = (plans_group, features_group)

        self.warnings_box.setPlainText("\n".join(self.plan_file.warnings))
        self._guess_layers()
        self._check()

    # -- the layers --------------------------------------------------------

    def _guess_layers(self):
        """Point the combos at the layers the file's own queries fit best.

        The queries name their columns -- ``"sunumber"``, ``"feature"`` -- so the
        file says which layer it wants without anyone having to name it. Only
        used to seed the combos; the operator can always override.
        """
        if self.plan_file is None:
            return
        candidates = [
            layer
            for layer in QgsProject.instance().mapLayers().values()
            if hasattr(layer, "fields") and hasattr(layer, "geometryType")
            # Never guess a layer this tool built: a plan layer has the source's
            # fields, so it scores just as well, and picking one would AND a
            # second plan's filter onto every query.
            and not tokens_from_layer(layer)
        ]
        if not candidates:
            return

        taken = set()
        for which, combo, queries in (
            ("su", self.su_combo, [p.su_query for p in self.plan_file.usable]),
            (
                "feature",
                self.feature_combo,
                [p.feature_query for p in self.plan_file.usable],
            ),
        ):
            queries = [query for query in queries if query]
            if not queries or which in self._picked:
                # A combo left to the guess must not be handed the layer the
                # other one already has: the two query sets are for two layers.
                taken.update(
                    layer.id()
                    for layer in (combo.currentLayer(),)
                    if layer is not None
                )
                continue
            best, best_score = None, 0
            for layer in candidates:
                if layer.id() in taken:
                    continue
                # Two points per query the layer could run, and one for being
                # unfiltered -- between two layers that fit, the one without a
                # filter of its own is the one the queries were written against.
                score = 2 * sum(
                    1
                    for query in queries
                    if not describe_query_problem(layer, query)
                )
                if not score:
                    continue
                if not layer.subsetString():
                    score += 1
                if score > best_score:
                    best, best_score = layer, score
            if best is not None and best_score:
                taken.add(best.id())
                blocked = combo.blockSignals(True)
                try:
                    combo.setLayer(best)
                finally:
                    combo.blockSignals(blocked)

    def _layer_picked(self, which):
        """A combo the operator changed themselves; guessing leaves it alone."""
        self._picked.add(which)
        self._check()

    # -- what a plan is ----------------------------------------------------

    def _mode_changed(self):
        """Explain the chosen mode, then rebuild -- it changes what gets built."""
        mode = self.mode_combo.currentData()
        self.mode_note.setText(MODE_NOTES.get(mode, ""))
        # Live mode writes through to the source table. That is worth more than
        # grey helper text, and worth less than a modal nobody reads twice.
        self.mode_note.setStyleSheet(
            "QLabel { color: palette(mid); }"
            if mode == MODE_COPY
            else "QLabel { color: rgb(150, 60, 20); font-weight: bold; }"
        )
        self._check()

    # -- checking ----------------------------------------------------------

    def _check(self):
        """Build every plan's layers and count what they select."""
        self.outcome = None
        self.table.setRowCount(0)
        if self.plan_file is None:
            return

        su_layer = self.su_combo.currentLayer()
        feature_layer = self.feature_combo.currentLayer()
        if su_layer is None and feature_layer is None:
            self.summary.setText(
                f"{len(self.plan_file.usable)} plan(s) read. Choose the layers "
                f"the queries run against."
            )
            self.match_hint.setText("")
            return
        if (
            su_layer is not None
            and feature_layer is not None
            and su_layer.id() == feature_layer.id()
        ):
            self.summary.setText(
                "The SU layer and the Features layer are the same layer. Pick "
                "the two different layers the queries were written for."
            )
            return

        plans = self.plan_file.usable
        self.progress.setVisible(True)
        self.progress.setMaximum(len(plans))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.outcome = check_plans(
                self.plan_file,
                su_layer,
                feature_layer,
                inherit_style=INHERIT_STYLE,
                mode=self.mode_combo.currentData(),
                progress=self._progress,
            )
        finally:
            QApplication.restoreOverrideCursor()
            self.progress.setVisible(False)

        self._fill_table()
        self.summary.setText(summarise(self.outcome, self.plan_file))
        self._describe_layers(su_layer, feature_layer)

    def _progress(self, done, total, label):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.progress.setFormat(f"%v / %m  {label}")
        QApplication.processEvents()

    def _describe_layers(self, su_layer, feature_layer):
        bits = []
        for layer, role in ((su_layer, "SU"), (feature_layer, "Features")):
            if layer is not None and layer.providerType() == "memory":
                # A plan is a second layer over the same source. A scratch layer
                # has no source to reopen -- every plan would come out empty,
                # and the counts below would be the only clue why.
                bits.append(
                    f"The {role} layer is a scratch layer, which cannot be "
                    f"filtered into plans. Save it to a GeoPackage or the "
                    f"database and pick that instead."
                )
            if layer is not None and layer.subsetString():
                # Not an error, but it silently narrows every plan, so say it.
                bits.append(
                    f"The {role} layer already has a filter of its own; every "
                    f"plan query is ANDed onto it."
                )
        if su_layer is None:
            bits.append("No SU layer: the SU half of every plan is skipped.")
        if feature_layer is None:
            bits.append("No Features layer: plans will show SUs only.")
        if self.outcome is not None:
            broken = [check for check in self.outcome.checks if not check.ok]
            if broken:
                first = broken[0]
                bits.append(
                    f"e.g. {first.plan.title}: {first.status()}. Check the two "
                    f"combo boxes are the right way round."
                )
        self.match_hint.setText("\n".join(bits))

    def _fill_table(self):
        if self.outcome is None:
            return
        checks = self.outcome.checks
        self.table.setRowCount(len(checks))
        red = QBrush(QColor(180, 40, 40))

        for row, check in enumerate(checks):
            item = QTableWidgetItem(check.plan.title)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # A plan whose query will not run is unticked, not hidden: the row
            # still says why, and can be ticked once the file is fixed.
            item.setCheckState(Qt.Checked if check.ok else Qt.Unchecked)
            item.setData(Qt.UserRole, row)
            item.setToolTip(
                f"Layer name: {check.layer_name}\n"
                f"Section: {check.plan.section or '-'}\n\n"
                f"SUs:\n{check.plan.su_query or '-'}\n\n"
                f"Features:\n{check.plan.feature_query or '-'}"
            )
            self.table.setItem(row, 0, item)

            for column, count in ((1, check.su_count), (2, check.feature_count)):
                cell = QTableWidgetItem("-" if count is None else str(count))
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if count == 0:
                    cell.setForeground(red)
                self.table.setItem(row, column, cell)

            status = QTableWidgetItem(check.status())
            if not check.ok:
                status.setForeground(red)
            status.setToolTip(check.status())
            self.table.setItem(row, 3, status)

    def _set_all(self, checked):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _checked(self):
        if self.outcome is None:
            return []
        out = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                out.append(self.outcome.checks[row])
        return out

    # -- building ----------------------------------------------------------

    def _build(self):
        """Publish the ticked plans. Returns the group names, or ``None``."""
        if self.outcome is None:
            QMessageBox.warning(
                self,
                "Nothing to build",
                "Load a plan file and choose the layers its queries run against.",
            )
            return None

        checks = self._checked()
        if not checks:
            QMessageBox.warning(self, "Nothing ticked", "Tick at least one plan.")
            return None

        broken = [check for check in checks if not check.ok]
        if broken:
            preview = "\n".join(f"{c.plan.title}: {c.status()}" for c in broken[:6])
            QMessageBox.warning(
                self,
                "Some queries will not run",
                f"{len(broken)} ticked plan(s) have a query that failed:\n\n"
                f"{preview}\n\nUntick them or fix the plan file.",
            )
            return None

        plans_group = self.plans_group_edit.text().strip() or "Plans"
        features_group = self.features_group_edit.text().strip() or "Features"
        if plans_group == features_group:
            QMessageBox.warning(
                self,
                "Same group twice",
                "The plans and the features need two different groups -- the "
                "export dialog tells them apart by which group they are in.",
            )
            return None

        # Building empties both groups first, and a group is just a name -- so
        # it may be one the operator filled themselves. Removing layers from the
        # project is not something to do quietly on the strength of a text box.
        protect = {
            layer.id()
            for layer in (
                self.su_combo.currentLayer(),
                self.feature_combo.currentLayer(),
            )
            if layer is not None
        }
        standing = {
            name: group_layer_count(name, protect)
            for name in (plans_group, features_group)
            if group_layer_count(name, protect)
        }
        if standing:
            listed = "\n".join(
                f"{name}: {count} layer(s)" for name, count in standing.items()
            )
            reply = QMessageBox.question(
                self,
                "Replace what is in those groups?",
                f"Building empties these groups first:\n\n{listed}\n\n"
                f"Those layers are removed from the project. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return None

        self._replaced = sum(
            clear_group(name, protect) for name in (plans_group, features_group)
        )

        build_plan_set(
            self.outcome,
            plans_group,
            features_group,
            checks=checks,
            replace=False,
        )
        self._store_settings()
        return plans_group, features_group

    def _build_only(self):
        checks = self._checked()
        groups = self._build()
        if groups is None:
            return
        plans_group, features_group = groups
        where = f"'{plans_group}'"
        if any(check.feature_layer is not None for check in checks):
            where += f" and '{features_group}'"
        replaced = (
            f"\n\n{self._replaced} layer(s) already in those groups were replaced."
            if self._replaced
            else ""
        )
        QMessageBox.information(
            self,
            "Built",
            f"{len(checks)} plan(s) built into {where}.{replaced}\n\n"
            f"Export them with Export Phase Plans, or edit any plan's query in "
            f"Layer Properties -> Source first.",
        )
        self.accept()

    def _any_features(self):
        return any(check.feature_layer is not None for check in self._checked())

    def _build_and_export(self):
        """Build, then ask the caller to open the export dialog on the result.

        Handed back rather than opened here: this dialog is modal, and opening
        the next one inside its own event loop would nest them.
        """
        has_features = self._any_features()
        groups = self._build()
        if groups is None:
            return
        plans_group, features_group = groups

        from .export_dialog import EXTENT_COMBINED

        self.export_request = {
            "group": plans_group,
            "feature_group": features_group if has_features else None,
            "template": DEFAULT_PLAN_TEMPLATE,
            "extent_mode": EXTENT_COMBINED,
        }
        self.accept()

    # -- settings ----------------------------------------------------------

    def _restore_settings(self):
        settings = QgsSettings()
        self.file_edit.setText(settings.value(f"{SETTINGS_PREFIX}/file", ""))
        self.plans_group_edit.setText(
            settings.value(f"{SETTINGS_PREFIX}/plans_group", "")
        )
        self.features_group_edit.setText(
            settings.value(f"{SETTINGS_PREFIX}/features_group", "")
        )
        index = self.mode_combo.findData(
            settings.value(f"{SETTINGS_PREFIX}/mode", MODE_COPY)
        )
        blocked = self.mode_combo.blockSignals(True)
        try:
            self.mode_combo.setCurrentIndex(max(index, 0))
        finally:
            self.mode_combo.blockSignals(blocked)
        # Paints the note without triggering the rebuild the signal would.
        self.mode_note.setText(MODE_NOTES.get(self.mode_combo.currentData(), ""))
        self.mode_note.setStyleSheet(
            "QLabel { color: palette(mid); }"
            if self.mode_combo.currentData() == MODE_COPY
            else "QLabel { color: rgb(150, 60, 20); font-weight: bold; }"
        )
        remembered = self.file_edit.text().strip()
        if remembered and os.path.isfile(remembered):
            self._load_file()

    def _store_settings(self):
        settings = QgsSettings()
        settings.setValue(f"{SETTINGS_PREFIX}/file", self.file_edit.text().strip())
        settings.setValue(
            f"{SETTINGS_PREFIX}/plans_group", self.plans_group_edit.text().strip()
        )
        settings.setValue(
            f"{SETTINGS_PREFIX}/features_group",
            self.features_group_edit.text().strip(),
        )
        settings.setValue(f"{SETTINGS_PREFIX}/mode", self.mode_combo.currentData())
