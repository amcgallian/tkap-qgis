"""Export one phase plan per layer, driven by a print layout you designed.

The plugin never invents a layout. You build one in the QGIS layout designer --
north arrow, scale bar, legend, title block, whatever the publication needs --
and this walks the phase layers, showing one at a time, and exports a file each.
"""

from __future__ import annotations

import os

from contextlib import contextmanager

from qgis.core import (
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsProject,
    QgsRectangle,
    QgsSettings,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .dialog_layout import fit_to_screen, scrollable_body
from .phasing_core import (
    DEFAULT_TITLE_TEMPLATE,
    PhaseKey,
    format_title,
    match_companion_layers,
    parse_layer_name,
    phase_key_from_name,
    sanitise,
)

PREVIEW_DPI = 96

#: Default placeholder text. Put a label saying "Title" in your layout and it
#: gets swapped for the phase title on every export.
TITLE_MARKER_DEFAULT = "Title"

#: What an auto-updating legend should call the phase layer. Applied only for
#: the duration of the export; the real layer names are restored afterwards.
DEFAULT_LEGEND_NAME = "Stratigraphic Units"

#: Same idea for the optional companion feature layers.
DEFAULT_FEATURE_LEGEND_NAME = "Features"

SETTINGS_PREFIX = "tkap_phasing/export"

#: A layout label with this item id is always treated as a title, whatever its
#: text. Matching by text (see TITLE_MARKER_DEFAULT) is usually easier.
TITLE_ITEM_ID = "phase_title"

#: JPEG first -- it is the normal deliverable for TKAP phase plans.
FORMATS = (
    ("JPEG", "jpg"),
    ("PNG", "png"),
    ("PDF", "pdf"),
    ("SVG", "svg"),
)

DEFAULT_FORMAT = "jpg"


class PreviewDialog(QDialog):
    """Render the layout for one phase at a time, before committing to export.

    Uses the parent's ``_phase_session``, so the preview is produced by exactly
    the same code path as the real export.
    """

    def __init__(self, parent, layout, layers):
        super().__init__(parent)
        self.parent_dialog = parent
        self.layout_obj = layout
        self.layers = layers
        self.index = 0
        # Resolved once, on the real layer names, before any render renames them.
        self.pairs, _ = parent._feature_pairs(layers)

        self.setWindowTitle("Export preview")
        # Small enough to drop onto any screen; opens larger, see below.
        self.setMinimumSize(420, 340)
        self.setSizeGripEnabled(True)

        box = QVBoxLayout(self)

        self.caption = QLabel("")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setStyleSheet("QLabel { font-weight: bold; padding: 4px; }")
        box.addWidget(self.caption)

        self.canvas = QLabel("Rendering...")
        self.canvas.setAlignment(Qt.AlignCenter)
        self.canvas.setMinimumHeight(180)
        self.canvas.setStyleSheet(
            "QLabel { background: palette(alternate-base); border: 1px solid "
            "palette(mid); }"
        )
        box.addWidget(self.canvas, 1)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet("QLabel { color: palette(mid); }")
        box.addWidget(self.hint)

        nav = QHBoxLayout()
        self.prev_button = QPushButton("< Previous")
        self.next_button = QPushButton("Next >")
        self.counter = QLabel("")
        self.counter.setAlignment(Qt.AlignCenter)
        nav.addWidget(self.prev_button)
        nav.addWidget(self.counter, 1)
        nav.addWidget(self.next_button)
        box.addLayout(nav)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        box.addWidget(buttons)

        self.prev_button.clicked.connect(lambda: self._step(-1))
        self.next_button.clicked.connect(lambda: self._step(1))

        fit_to_screen(self, 760, 700)
        self._render()

    def _step(self, delta):
        self.index = (self.index + delta) % len(self.layers)
        self._render()

    def _render(self):
        layer = self.layers[self.index]
        # Read before _phase_session, which renames it for the legend.
        self.caption.setText(layer.name())
        self.counter.setText(f"{self.index + 1} of {len(self.layers)}")
        self.prev_button.setEnabled(len(self.layers) > 1)
        self.next_button.setEnabled(len(self.layers) > 1)

        count = layer.featureCount()
        bits = [f"{count} SU(s) in this phase" if count >= 0 else "SU count unavailable"]
        companion = self.pairs.get(layer.id())
        if companion is not None:
            # Read now, for the same reason as the caption above.
            bits.append(f"features from '{companion.name()}'")
        elif self.pairs:
            bits.append("no matching features layer")
        self.hint.setText("  -  ".join(bits))

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with self.parent_dialog._phase_session(self.layout_obj) as apply:
                apply(layer)
                exporter = QgsLayoutExporter(self.layout_obj)
                image = exporter.renderPageToImage(0, QSize(), PREVIEW_DPI)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            self.canvas.setText(f"Preview failed:\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        if image is None or image.isNull():
            self._image = None
            self.canvas.setText("Layout produced an empty page.")
            return

        self._image = image
        self._repaint()

    def _repaint(self):
        """Scale the cached render to the label. No re-render."""
        image = getattr(self, "_image", None)
        if image is None:
            return
        self.canvas.setPixmap(
            QPixmap.fromImage(image).scaled(
                self.canvas.width(),
                self.canvas.height(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def resizeEvent(self, event):  # noqa: N802 - Qt naming
        # Rescale only -- re-rendering the layout on every resize step would
        # make dragging the window unusable.
        super().resizeEvent(event)
        self._repaint()


class ExportPlansDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Export Phase Plans")
        # Width only. A minimum height would defeat the scroll area below.
        self.setMinimumWidth(520)
        self.setSizeGripEnabled(True)
        self._build_ui()
        self._connect()
        self._populate()
        self._restore_settings()
        fit_to_screen(self, 620, 900)

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        layout, footer = scrollable_body(self)

        # --- what to export -----------------------------------------------
        source_box = QGroupBox("Phase layers")
        source_form = QFormLayout(source_box)

        self.group_combo = QComboBox()
        source_form.addRow("Group:", self.group_combo)

        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QListWidget.NoSelection)
        self.layer_list.setMaximumHeight(150)
        source_form.addRow("Layers:", self.layer_list)

        button_row = QHBoxLayout()
        self.all_button = QPushButton("Select all")
        self.none_button = QPushButton("Select none")
        button_row.addWidget(self.all_button)
        button_row.addWidget(self.none_button)
        button_row.addStretch(1)
        source_form.addRow("", button_row)

        layout.addWidget(source_box)

        # --- companion feature layers ---------------------------------------
        feature_box = QGroupBox("Features (optional)")
        feature_form = QFormLayout(feature_box)

        self.feature_group_combo = QComboBox()
        self.feature_group_combo.setToolTip(
            "A group of feature layers, one per phase. Each is matched to the "
            "phase layer of the same name, shown with it, and drawn on top."
        )
        feature_form.addRow("Group:", self.feature_group_combo)

        self.feature_legend_edit = QLineEdit(DEFAULT_FEATURE_LEGEND_NAME)
        self.feature_legend_edit.setToolTip(
            "The matched feature layer is renamed to this for the export, the "
            "same way phase layers become 'Stratigraphic Units'. Leave blank to "
            "keep its real name."
        )
        feature_form.addRow("Legend name:", self.feature_legend_edit)

        self.feature_format = QLabel("")
        self.feature_format.setWordWrap(True)
        self.feature_format.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.feature_format.setStyleSheet(
            "QLabel { background: palette(alternate-base); border: 1px solid "
            "palette(mid); padding: 6px; }"
        )
        feature_form.addRow("Naming:", self.feature_format)

        self.feature_hint = QLabel("")
        self.feature_hint.setWordWrap(True)
        self.feature_hint.setStyleSheet("QLabel { color: palette(mid); }")
        feature_form.addRow("", self.feature_hint)

        layout.addWidget(feature_box)

        # --- layout --------------------------------------------------------
        layout_box = QGroupBox("Layout")
        layout_form = QFormLayout(layout_box)

        self.layout_combo = QComboBox()
        layout_form.addRow("Print layout:", self.layout_combo)

        self.layout_hint = QLabel("")
        self.layout_hint.setWordWrap(True)
        self.layout_hint.setStyleSheet("QLabel { color: palette(mid); }")
        layout_form.addRow("", self.layout_hint)

        self.zoom_check = QCheckBox("Zoom map to phase extent")
        layout_form.addRow("", self.zoom_check)

        self.title_check = QCheckBox("Replace title text")
        self.title_check.setChecked(True)
        layout_form.addRow("", self.title_check)

        self.marker_edit = QLineEdit(TITLE_MARKER_DEFAULT)
        self.marker_edit.setToolTip(
            "Any label in the layout whose text is exactly this gets replaced.\n"
            f"A label with the item id '{TITLE_ITEM_ID}' is always matched too."
        )
        layout_form.addRow("Label reading:", self.marker_edit)

        self.template_edit = QLineEdit(DEFAULT_TITLE_TEMPLATE)
        layout_form.addRow("Replace with:", self.template_edit)

        placeholders = QLabel(
            "{field}  {space}  {phase}  {phase_name}  {layer}"
            "     - use \\n for a line break"
        )
        placeholders.setStyleSheet("QLabel { color: palette(mid); }")
        layout_form.addRow("", placeholders)

        self.title_example = QLabel("")
        self.title_example.setWordWrap(True)
        self.title_example.setStyleSheet("QLabel { font-style: italic; }")
        layout_form.addRow("Example:", self.title_example)

        self.hide_others_check = QCheckBox("Hide other phase layers")
        self.hide_others_check.setChecked(True)
        layout_form.addRow("", self.hide_others_check)

        self.rename_check = QCheckBox("Rename layer for the legend")
        self.rename_check.setChecked(True)
        self.rename_check.setToolTip(
            "An auto-updating legend shows the layer's name. This swaps in a "
            "fixed name for the export so every plan's legend reads the same, "
            "then puts the real name back. File names are unaffected."
        )
        layout_form.addRow("", self.rename_check)

        self.legend_name_edit = QLineEdit(DEFAULT_LEGEND_NAME)
        layout_form.addRow("Legend name:", self.legend_name_edit)

        self.legend_add_check = QCheckBox("Add phase layer to the legend")
        self.legend_add_check.setChecked(True)
        self.legend_add_check.setToolTip(
            "For a legend with 'Auto update' off, inserts the current phase "
            "layer into the legend for each plan and takes it out again "
            "afterwards.\n"
            "Legends with Auto update on already follow the layer tree and are "
            "left alone."
        )
        layout_form.addRow("", self.legend_add_check)

        self.map_layers_check = QCheckBox("Set map layers per phase")
        self.map_layers_check.setChecked(True)
        self.map_layers_check.setToolTip(
            "Points the map frame at the current phase layer plus whatever else "
            "was already in the map, then restores it.\n"
            "Needed if the map frame has 'Lock layers' ticked, since a locked "
            "map ignores layer-tree visibility."
        )
        layout_form.addRow("", self.map_layers_check)

        layout.addWidget(layout_box)

        # --- output --------------------------------------------------------
        output_box = QGroupBox("Output")
        output_form = QFormLayout(output_box)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_button = QPushButton("Browse...")
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(self.folder_button)
        output_form.addRow("Folder:", folder_row)

        self.format_combo = QComboBox()
        for label, suffix in FORMATS:
            self.format_combo.addItem(label, suffix)
        output_form.addRow("Format:", self.format_combo)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 1200)
        self.dpi_spin.setValue(300)
        self.dpi_spin.setSuffix(" dpi")
        output_form.addRow("Resolution:", self.dpi_spin)

        layout.addWidget(output_box)

        layout.addStretch(1)

        # Outside the scroll area: these stay put no matter how short the
        # window is dragged.
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        footer.addWidget(self.progress)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.buttons.button(QDialogButtonBox.Ok).setText("Export")
        self.preview_button = self.buttons.addButton(
            "Preview...", QDialogButtonBox.ActionRole
        )
        self.preview_button.setToolTip(
            "Render the layout phase by phase before committing to the export"
        )
        footer.addWidget(self.buttons)

    def _connect(self):
        self.group_combo.currentIndexChanged.connect(self._phase_group_changed)
        self.feature_group_combo.currentIndexChanged.connect(self._describe_features)
        self.layout_combo.currentIndexChanged.connect(self._describe_layout)
        self.marker_edit.textChanged.connect(self._describe_layout)
        self.map_layers_check.toggled.connect(self._describe_layout)
        self.legend_add_check.toggled.connect(self._describe_layout)
        self.template_edit.textChanged.connect(self._update_title_example)
        self.layer_list.itemChanged.connect(self._update_title_example)
        self.folder_button.clicked.connect(self._pick_folder)
        self.all_button.clicked.connect(lambda: self._set_all(True))
        self.none_button.clicked.connect(lambda: self._set_all(False))
        self.buttons.accepted.connect(self._export)
        self.buttons.rejected.connect(self.reject)
        self.preview_button.clicked.connect(self._preview)

    # -- population --------------------------------------------------------

    def _populate(self):
        root = QgsProject.instance().layerTreeRoot()
        self.group_combo.clear()
        self.group_combo.addItem("<all layers in project>", None)
        for group in root.findGroups():
            self.group_combo.addItem(group.name(), group.name())
        # Prefer a group that looks like phasing output.
        for index in range(self.group_combo.count()):
            name = self.group_combo.itemData(index)
            if name and any(c.isdigit() for c in name):
                self.group_combo.setCurrentIndex(index)
                break
        self._populate_feature_groups()
        self._populate_layers()

        manager = QgsProject.instance().layoutManager()
        self.layout_combo.clear()
        for print_layout in manager.printLayouts():
            self.layout_combo.addItem(print_layout.name())
        self._describe_layout()

    def _phase_group_changed(self):
        # The features group must not be the phase group, so its options depend
        # on this choice.
        self._populate_feature_groups()
        self._populate_layers()

    def _populate_feature_groups(self):
        """Groups offerable as the features source: anything but the phase group."""
        previous = self.feature_group_combo.currentData()
        phase_group = self.group_combo.currentData()
        blocked = self.feature_group_combo.blockSignals(True)
        try:
            self.feature_group_combo.clear()
            self.feature_group_combo.addItem("<none>", None)
            for group in QgsProject.instance().layerTreeRoot().findGroups():
                if group.name() == phase_group:
                    continue
                self.feature_group_combo.addItem(group.name(), group.name())
            index = self.feature_group_combo.findData(previous)
            self.feature_group_combo.setCurrentIndex(max(index, 0))
        finally:
            self.feature_group_combo.blockSignals(blocked)
        self._describe_features()

    def _populate_layers(self):
        self.layer_list.clear()
        for layer in self._candidate_layers():
            item = QListWidgetItem(layer.name())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, layer.id())
            self.layer_list.addItem(item)
        self._update_title_example()
        self._describe_features()

    def _candidate_layers(self):
        root = QgsProject.instance().layerTreeRoot()
        group_name = self.group_combo.currentData()
        node = root.findGroup(group_name) if group_name else root
        if node is None:
            return []
        # With "<all layers in project>" chosen, the features group sits inside
        # the search too; exclude it so companions are never also treated as
        # phase layers.
        companion_ids = {layer.id() for layer in self._feature_layers()}
        layers = []
        for tree_layer in node.findLayers():
            layer = tree_layer.layer()
            if layer is not None and layer.id() not in companion_ids:
                layers.append(layer)
        return layers

    # -- companion feature layers -----------------------------------------

    def _feature_layers(self):
        """Layers in the chosen features group; empty if none is chosen."""
        combo = getattr(self, "feature_group_combo", None)
        if combo is None:
            return []
        group_name = combo.currentData()
        if not group_name:
            return []
        group = QgsProject.instance().layerTreeRoot().findGroup(group_name)
        if group is None:
            return []
        return [
            tree_layer.layer()
            for tree_layer in group.findLayers()
            if tree_layer.layer() is not None
        ]

    def _feature_pairs(self, phase_layers):
        """Match phase layers to companions. Returns (id -> layer, match report).

        Must be called before any renaming happens, since the pairing is done on
        the real layer names.
        """
        companions = self._feature_layers()
        if not companions or not phase_layers:
            return {}, None

        by_name = {}
        for companion in companions:
            # QGIS allows duplicate layer names; first one in tree order wins.
            by_name.setdefault(companion.name(), companion)

        match = match_companion_layers(
            [layer.name() for layer in phase_layers], list(by_name)
        )
        pairs = {}
        for layer in phase_layers:
            companion_name = match.pairs.get(layer.name())
            if companion_name is not None:
                pairs[layer.id()] = by_name[companion_name]
        return pairs, match

    def _naming_guide(self, phase_layers):
        """Spell out how to name the feature layers, using a real example."""
        example = ""
        for layer in phase_layers:
            key = phase_key_from_name(layer.name())
            if key is not None:
                example = layer.name()
                break
        if not example:
            example, key = "Field6_Sp34_Phase1_Construction", PhaseKey(34, 1)

        return (
            "One features layer per space-phase. Name it after its phase layer "
            f"({example}), or just tag it with the space and phase "
            f"(Features_Sp{key.space}_Phase{key.phase}). Case and punctuation are "
            "ignored; both numbers must match. Unmatched phases export SUs only."
        )

    def _describe_features(self):
        phase_layers = self._candidate_layers()
        self.feature_format.setText(self._naming_guide(phase_layers))

        companions = self._feature_layers()
        if not companions:
            self.feature_hint.setText("No features group - plans show SUs only.")
            return

        _, match = self._feature_pairs(phase_layers)
        if match is None:
            self.feature_hint.setText(f"{len(companions)} layer(s), nothing to match.")
            return

        bits = [f"{match.matched} of {len(phase_layers)} phase layer(s) matched."]
        if match.unmatched:
            preview = ", ".join(match.unmatched[:3])
            if len(match.unmatched) > 3:
                preview += f", +{len(match.unmatched) - 3} more"
            bits.append(f"No features for: {preview}")
        if match.unused:
            preview = ", ".join(match.unused[:3])
            if len(match.unused) > 3:
                preview += f", +{len(match.unused) - 3} more"
            bits.append(f"Unmatched features layer(s): {preview}")
        if match.ambiguous:
            phase, options = next(iter(match.ambiguous.items()))
            bits.append(
                f"{len(match.ambiguous)} ambiguous, e.g. {phase} -> "
                f"{', '.join(options)}; first is used."
            )
        # QGIS allows two layers to share a name, in which case only the first
        # is reachable by name and the rest would vanish without a word.
        seen, duplicates = set(), set()
        for companion in companions:
            if companion.name() in seen:
                duplicates.add(companion.name())
            seen.add(companion.name())
        if duplicates:
            bits.append(
                f"Duplicate name(s): {', '.join(sorted(duplicates))} "
                f"- only the first of each is used."
            )
        self.feature_hint.setText("\n".join(bits))

    def _describe_layout(self):
        layout = self._current_layout()
        if layout is None:
            self.layout_hint.setText(
                "No print layouts in this project. Create one in "
                "Project -> New Print Layout first."
            )
            return
        maps = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap)]
        labels = [i for i in layout.items() if isinstance(i, QgsLayoutItemLabel)]
        legends = [i for i in layout.items() if isinstance(i, QgsLayoutItemLegend)]
        has_title = any(i.id() == TITLE_ITEM_ID for i in labels)

        bits = [f"{len(maps)} map frame(s)"]
        if any(m.keepLayerSet() for m in maps) and not self.map_layers_check.isChecked():
            bits.append("map has locked layers - tick 'Set map layers per phase'")

        matched = self._find_title_items(layout)
        if matched:
            bits.append(f"{len(matched)} title label(s) matched")
        elif has_title:
            bits.append(f"'{TITLE_ITEM_ID}' label found")
        else:
            marker = self.marker_edit.text().strip()
            bits.append(f"no label reading '{marker}'")

        # The legend only tracks the phase if auto-update is on; and it only
        # drops unused symbol categories if it is also filtered by the map.
        for legend in legends:
            try:
                auto = legend.autoUpdateModel()
                filtered = legend.legendFilterByMapEnabled()
            except Exception:  # pragma: no cover - depends on QGIS build
                continue
            if not auto:
                bits.append(
                    "legend: Auto update off, phase layer will be inserted"
                    if self.legend_add_check.isChecked()
                    else "legend: Auto update off, phase layer will NOT appear"
                )
            elif not filtered:
                bits.append(
                    "legend: auto-update on, not filtered to map"
                )
            else:
                bits.append("legend: auto-update on, filtered to map")

        self.layout_hint.setText("\n".join(bits))

    def _update_title_example(self):
        """Show the title the first ticked phase would get."""
        layers = self._checked_layers()
        if not layers:
            self.title_example.setText("(tick a phase layer to preview the title)")
            return
        rendered = format_title(
            self.template_edit.text(), self._tokens_for(layers[0])
        )
        self.title_example.setText(rendered.replace("\n", " / "))

    def _current_layout(self):
        name = self.layout_combo.currentText()
        if not name:
            return None
        return QgsProject.instance().layoutManager().layoutByName(name)

    def _set_all(self, checked):
        for index in range(self.layer_list.count()):
            self.layer_list.item(index).setCheckState(
                Qt.Checked if checked else Qt.Unchecked
            )

    def _checked_layers(self):
        project = QgsProject.instance()
        layers = []
        for index in range(self.layer_list.count()):
            item = self.layer_list.item(index)
            if item.checkState() == Qt.Checked:
                layer = project.mapLayer(item.data(Qt.UserRole))
                if layer is not None:
                    layers.append(layer)
        return layers

    # -- settings ----------------------------------------------------------

    def _restore_settings(self):
        settings = QgsSettings()
        folder = settings.value(f"{SETTINGS_PREFIX}/folder", "")
        if not folder:
            folder = QgsProject.instance().homePath() or os.path.expanduser("~")
        self.folder_edit.setText(folder)
        self.dpi_spin.setValue(int(settings.value(f"{SETTINGS_PREFIX}/dpi", 300)))
        fmt = settings.value(f"{SETTINGS_PREFIX}/format", DEFAULT_FORMAT)
        index = self.format_combo.findData(fmt)
        if index >= 0:
            self.format_combo.setCurrentIndex(index)
        self.marker_edit.setText(
            settings.value(f"{SETTINGS_PREFIX}/marker", TITLE_MARKER_DEFAULT)
        )
        self.template_edit.setText(
            settings.value(f"{SETTINGS_PREFIX}/template", DEFAULT_TITLE_TEMPLATE)
        )
        self.legend_name_edit.setText(
            settings.value(f"{SETTINGS_PREFIX}/legend_name", DEFAULT_LEGEND_NAME)
        )
        self.map_layers_check.setChecked(
            settings.value(f"{SETTINGS_PREFIX}/set_map", True, type=bool)
        )
        self.legend_add_check.setChecked(
            settings.value(f"{SETTINGS_PREFIX}/legend_add", True, type=bool)
        )
        self.feature_legend_edit.setText(
            settings.value(
                f"{SETTINGS_PREFIX}/feature_legend_name", DEFAULT_FEATURE_LEGEND_NAME
            )
        )
        # Only reselect a features group that still exists in this project.
        index = self.feature_group_combo.findData(
            settings.value(f"{SETTINGS_PREFIX}/feature_group", None)
        )
        if index > 0:
            self.feature_group_combo.setCurrentIndex(index)

    def _store_settings(self):
        settings = QgsSettings()
        settings.setValue(f"{SETTINGS_PREFIX}/folder", self.folder_edit.text())
        settings.setValue(f"{SETTINGS_PREFIX}/dpi", self.dpi_spin.value())
        settings.setValue(
            f"{SETTINGS_PREFIX}/format", self.format_combo.currentData()
        )
        settings.setValue(f"{SETTINGS_PREFIX}/marker", self.marker_edit.text())
        settings.setValue(f"{SETTINGS_PREFIX}/template", self.template_edit.text())
        settings.setValue(
            f"{SETTINGS_PREFIX}/legend_name", self.legend_name_edit.text()
        )
        settings.setValue(
            f"{SETTINGS_PREFIX}/set_map", self.map_layers_check.isChecked()
        )
        settings.setValue(
            f"{SETTINGS_PREFIX}/legend_add", self.legend_add_check.isChecked()
        )
        settings.setValue(
            f"{SETTINGS_PREFIX}/feature_group", self.feature_group_combo.currentData()
        )
        settings.setValue(
            f"{SETTINGS_PREFIX}/feature_legend_name", self.feature_legend_edit.text()
        )

    def _pick_folder(self):
        start = self.folder_edit.text() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Output folder", start)
        if folder:
            self.folder_edit.setText(folder)

    # -- export ------------------------------------------------------------

    def _export(self):
        layout = self._current_layout()
        layers = self._checked_layers()
        folder = self.folder_edit.text().strip()
        suffix = self.format_combo.currentData()

        if layout is None:
            QMessageBox.warning(
                self,
                "No layout",
                "This project has no print layout. Create one in "
                "Project -> New Print Layout first.",
            )
            return
        if not layers:
            QMessageBox.warning(self, "No layers", "Tick at least one phase layer.")
            return
        if not folder:
            QMessageBox.warning(self, "No folder", "Choose an output folder.")
            return
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(self, "Bad folder", str(exc))
                return

        existing = [
            name
            for name in (f"{sanitise(l.name())}.{suffix}" for l in layers)
            if os.path.exists(os.path.join(folder, name))
        ]
        if existing:
            preview = "\n".join(existing[:8])
            if len(existing) > 8:
                preview += f"\n... and {len(existing) - 8} more"
            reply = QMessageBox.question(
                self,
                "Overwrite files?",
                f"{len(existing)} file(s) already exist in that folder and will "
                f"be overwritten:\n\n{preview}\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        written, errors = self._run_export(layout, layers, folder, suffix)

        self.progress.setVisible(False)
        if errors:
            preview = "\n".join(errors[:10])
            QMessageBox.critical(
                self,
                "Export finished with errors",
                f"Wrote {len(written)} file(s).\n\n{len(errors)} failed:\n{preview}",
            )
            return

        self._store_settings()
        QMessageBox.information(
            self,
            "Done",
            f"Exported {len(written)} phase plan(s) to\n{folder}",
        )
        self.accept()

    def _find_title_items(self, layout):
        """Labels to overwrite: matched by text, or by the reserved item id."""
        marker = self.marker_edit.text().strip().casefold()
        found = []
        for item in layout.items():
            if not isinstance(item, QgsLayoutItemLabel):
                continue
            if item.id() == TITLE_ITEM_ID:
                found.append(item)
            elif marker and item.text().strip().casefold() == marker:
                found.append(item)
        return found

    def _tokens_for(self, layer):
        """Title placeholders for one phase layer.

        Prefers the ph_space / ph_num / ph_name provenance columns, falling back
        to parsing the layer name if they were switched off during the split.
        """
        from qgis.core import QgsFeatureRequest

        tokens = parse_layer_name(layer.name())
        try:
            fields = layer.fields()
            names = ("ph_space", "ph_num", "ph_name")
            if all(fields.lookupField(n) >= 0 for n in names):
                request = QgsFeatureRequest()
                request.setLimit(1)
                request.setFlags(QgsFeatureRequest.NoGeometry)
                feature = next(iter(layer.getFeatures(request)), None)
                if feature is not None:
                    tokens["space"] = str(feature["ph_space"])
                    tokens["phase"] = str(feature["ph_num"])
                    tokens["phase_name"] = str(feature["ph_name"])
        except Exception:  # pragma: no cover - depends on provider
            pass
        return tokens

    @contextmanager
    def _phase_session(self, layout):
        """Snapshot project/layout state, yield an apply function, then restore.

        Preview and export drive exactly the same code, so what you see in the
        preview is what lands on disk.
        """
        root = QgsProject.instance().layerTreeRoot()
        hide_others = self.hide_others_check.isChecked()
        siblings = self._candidate_layers() if hide_others else []

        # Companions are paired here, before anything is renamed, for the same
        # reason filenames are: the matching is done on the real layer names.
        feature_layers = self._feature_layers()
        feature_pairs, _ = self._feature_pairs(self._candidate_layers())
        feature_rename_to = self.feature_legend_edit.text().strip()

        original_visibility = {}
        for layer in siblings + (feature_layers if hide_others else []):
            node = root.findLayer(layer.id())
            if node is not None:
                original_visibility[layer.id()] = node.isVisible()

        # Captured once, up front: the marker text is overwritten on the first
        # phase, so re-matching each iteration would find nothing.
        title_items = (
            [(item, item.text()) for item in self._find_title_items(layout)]
            if self.title_check.isChecked()
            else []
        )
        template = self.template_edit.text()

        map_item = layout.referenceMap()
        if map_item is None:
            maps = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap)]
            map_item = maps[0] if maps else None

        legends = [i for i in layout.items() if isinstance(i, QgsLayoutItemLegend)]

        # Captured before anything is renamed, so restoration is exact even if
        # the run aborts part-way.
        rename_to = (
            self.legend_name_edit.text().strip()
            if self.rename_check.isChecked()
            else ""
        )
        original_names = {}
        if rename_to:
            for candidate in self._candidate_layers():
                original_names[candidate.id()] = candidate.name()
        if feature_rename_to:
            for companion in feature_layers:
                original_names.setdefault(companion.id(), companion.name())

        # A legend with Auto update ON follows the project layer tree, and its
        # model root IS that tree -- inserting there would alter the project.
        # Only manual legends get a node pushed into them.
        manual_legends = []
        if self.legend_add_check.isChecked():
            for legend in legends:
                try:
                    if not legend.autoUpdateModel():
                        manual_legends.append(legend)
                except Exception:  # pragma: no cover - depends on QGIS build
                    continue
        # Parallel to manual_legends: the layer ids this plugin put in, so only
        # what we added is taken back out.
        inserted_layer_ids = [[] for _ in manual_legends]

        # Map frame: swap the phase layer in while keeping everything else that
        # was already in the map (basemaps, site outlines).
        set_map = self.map_layers_check.isChecked() and map_item is not None
        original_keep_layer_set = False
        original_map_layers = []
        background = []
        if set_map:
            # Both phase layers and companions are placed explicitly per phase,
            # so neither may be left sitting in the static background.
            managed_ids = {lyr.id() for lyr in self._candidate_layers()}
            managed_ids |= {lyr.id() for lyr in feature_layers}
            original_keep_layer_set = map_item.keepLayerSet()
            original_map_layers = list(map_item.layers())
            if original_keep_layer_set and original_map_layers:
                background = [
                    lyr for lyr in original_map_layers if lyr.id() not in managed_ids
                ]
            else:
                # Map follows the project; take the visible unmanaged layers in
                # layer-tree order so draw order is preserved.
                for tree_layer in root.findLayers():
                    lyr = tree_layer.layer()
                    if (
                        lyr is not None
                        and tree_layer.isVisible()
                        and lyr.id() not in managed_ids
                    ):
                        background.append(lyr)

        def apply(layer):
            companion = feature_pairs.get(layer.id())

            for sibling in siblings:
                node = root.findLayer(sibling.id())
                if node is not None:
                    node.setItemVisibilityChecked(sibling.id() == layer.id())
            if hide_others:
                for other in feature_layers:
                    node = root.findLayer(other.id())
                    if node is not None:
                        node.setItemVisibilityChecked(
                            companion is not None and other.id() == companion.id()
                        )

            # Tokens first: they can fall back to parsing the layer name, which
            # the legend rename below would otherwise have destroyed.
            if title_items:
                text = format_title(template, self._tokens_for(layer))
                for item, _ in title_items:
                    item.setText(text)

            if set_map:
                # Order is draw order, topmost first: features sit above the SUs
                # they were recorded in, and both above the background.
                stack = [layer]
                if companion is not None:
                    stack.insert(0, companion)
                map_item.setLayers(stack + background)
                map_item.setKeepLayerSet(True)

            if rename_to:
                layer.setName(rename_to)
            if companion is not None and feature_rename_to:
                companion.setName(feature_rename_to)

            # After the renames, so the legend nodes pick up the new names.
            wanted = [layer] + ([companion] if companion is not None else [])
            for index, legend in enumerate(manual_legends):
                try:
                    group = legend.model().rootGroup()
                except Exception:  # pragma: no cover - depends on QGIS build
                    continue
                for previous in inserted_layer_ids[index]:
                    node = group.findLayer(previous)
                    if node is not None:
                        group.removeChildNode(node)
                inserted_layer_ids[index] = []
                for position, entry in enumerate(wanted):
                    # Leave alone anything the operator curated in by hand.
                    if group.findLayer(entry.id()) is None:
                        group.insertLayer(position, entry)
                        inserted_layer_ids[index].append(entry.id())

            if self.zoom_check.isChecked() and map_item is not None:
                extent = layer.extent()
                if companion is not None and companion.crs() == layer.crs():
                    companion_extent = companion.extent()
                    if not companion_extent.isEmpty():
                        extent = QgsRectangle(extent)
                        extent.combineExtentWith(companion_extent)
                if not extent.isEmpty():
                    map_item.zoomToExtent(extent)

            # An auto-updating legend follows the layer tree, but it only
            # rebuilds when told to -- otherwise the exported legend can lag a
            # phase behind.
            for legend in legends:
                try:
                    if legend.autoUpdateModel():
                        legend.updateLegend()
                    if legend.legendFilterByMapEnabled():
                        legend.updateFilterByMap()
                except Exception:  # pragma: no cover - depends on QGIS build
                    pass

            layout.refresh()

        try:
            yield apply
        finally:
            for layer_id, visible in original_visibility.items():
                node = root.findLayer(layer_id)
                if node is not None:
                    node.setItemVisibilityChecked(visible)
            for index, legend in enumerate(manual_legends):
                for layer_id in inserted_layer_ids[index]:
                    try:
                        group = legend.model().rootGroup()
                        node = group.findLayer(layer_id)
                        if node is not None:
                            group.removeChildNode(node)
                    except Exception:  # pragma: no cover - depends on QGIS build
                        pass
                inserted_layer_ids[index] = []
            if set_map:
                map_item.setLayers(original_map_layers)
                map_item.setKeepLayerSet(original_keep_layer_set)
            for layer_id, original_name in original_names.items():
                restored = QgsProject.instance().mapLayer(layer_id)
                if restored is not None:
                    restored.setName(original_name)
            for item, original_text in title_items:
                item.setText(original_text)
            for legend in legends:
                try:
                    if legend.autoUpdateModel():
                        legend.updateLegend()
                except Exception:  # pragma: no cover - depends on QGIS build
                    pass
            layout.refresh()

    def _run_export(self, layout, layers, folder, suffix):
        """Show each phase layer in turn and export the layout."""
        written, errors = [], []
        # Resolved up front: the legend rename overwrites layer.name() during
        # the run, and reading it afterwards would name every file the same.
        names = {layer.id(): layer.name() for layer in layers}

        self.progress.setVisible(True)
        self.progress.setMaximum(len(layers))
        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            with self._phase_session(layout) as apply:
                for index, layer in enumerate(layers):
                    name = names[layer.id()]
                    self.progress.setValue(index + 1)
                    self.progress.setFormat(f"%v / %m  {name}")
                    QApplication.processEvents()

                    apply(layer)

                    path = os.path.join(folder, f"{sanitise(name)}.{suffix}")
                    error = self._export_one(layout, path, suffix)
                    if error:
                        errors.append(f"{name}: {error}")
                    else:
                        written.append(path)
        finally:
            QApplication.restoreOverrideCursor()

        return written, errors

    def _preview(self):
        layout = self._current_layout()
        layers = self._checked_layers()
        if layout is None:
            QMessageBox.warning(
                self, "No layout", "Choose a print layout to preview."
            )
            return
        if not layers:
            QMessageBox.warning(self, "No layers", "Tick at least one phase layer.")
            return
        PreviewDialog(self, layout, layers).exec_()

    def _export_one(self, layout, path, suffix):
        exporter = QgsLayoutExporter(layout)
        dpi = self.dpi_spin.value()

        if suffix == "pdf":
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = dpi
            result = exporter.exportToPdf(path, settings)
        elif suffix == "svg":
            settings = QgsLayoutExporter.SvgExportSettings()
            settings.dpi = dpi
            result = exporter.exportToSvg(path, settings)
        else:
            # JPEG and PNG both go through the image exporter; Qt picks the
            # encoder from the file extension.
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = dpi
            result = exporter.exportToImage(path, settings)

        if result != QgsLayoutExporter.Success:
            return f"exporter returned {result}"
        return ""
