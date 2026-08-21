"""Modal dialog for managing the analog gear library."""

from __future__ import annotations

import qtawesome as qta
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from negpy.desktop.settings_catalog import NON_METADATA_SECTIONS, preset_config, preset_values, selected_flat_dict
from negpy.desktop.view.styles.templates import dialog_pane_qss, field_label, hint_label, pane_header_qss
from negpy.desktop.view.styles.theme import THEME
from negpy.desktop.view.widgets.granular_settings_dialog import GranularSettingsDialog
from negpy.features.metadata.gear_logic import matches_gear_filter
from negpy.features.metadata.capture import DEV_TIME_HINT, format_dev_time, format_temperature, parse_dev_time, parse_temperature
from negpy.features.metadata.gear_models import (
    Camera,
    DevelopmentProcess,
    FilmColorType,
    FilmFormat,
    FilmStock,
    GearLibrary,
    Lens,
    ScanSetup,
)
from negpy.features.metadata.models import PUSH_PULL_LABELS, PUSH_PULL_VALUES
from negpy.services.assets.gear import GearProfiles
from negpy.services.assets.presets import MetadataPresets

_CATEGORIES = [
    ("cameras", "Cameras"),
    ("lenses", "Lenses"),
    ("film_stocks", "Film Stocks"),
    ("processes", "Process"),
    ("scan_setups", "Scanning"),
    ("metadata_presets", "Presets"),
]

# Metadata presets are files of stored values, not library records: the form pane
# shows what one holds and the field picker edits it.
_PRESETS = "metadata_presets"

_CATEGORY_FIELDS: dict[str, frozenset[str]] = {
    "cameras": frozenset({"display_name", "make", "model", "notes"}),
    "lenses": frozenset({"display_name", "make", "lens_model", "focal", "aperture", "notes"}),
    "film_stocks": frozenset({"display_name", "manufacturer", "stock_name", "iso", "format", "color_type", "notes"}),
    "processes": frozenset({"display_name", "developer", "dilution", "push_pull", "dev_time", "dev_temp", "notes"}),
    "scan_setups": frozenset({"display_name", "scanning", "notes"}),
    _PRESETS: frozenset(),
}

_CATEGORY_SEARCH_PLACEHOLDER = {
    "cameras": "Search cameras…",
    "lenses": "Search lenses…",
    "film_stocks": "Search film stocks…",
    "processes": "Search processes…",
    "scan_setups": "Search scan setups…",
    _PRESETS: "Search presets…",
}


def _push_pull_index(value: int) -> int:
    return PUSH_PULL_VALUES.index(value) if value in PUSH_PULL_VALUES else PUSH_PULL_VALUES.index(0)


class GearLibraryDialog(QDialog):
    library_changed = pyqtSignal()
    presets_changed = pyqtSignal()

    def __init__(self, library: GearLibrary | None = None, parent=None, current_config=None):
        super().__init__(parent)
        self._library = library or GearProfiles.load_library()
        self._current_config = current_config
        self._category = "cameras"
        self._selected_idx = -1
        self._list_items: list = []
        self._updating = False

        self.setWindowTitle("Library")
        self.resize(820, 560)
        self._init_ui()
        self._select_category("cameras")

    def library(self) -> GearLibrary:
        return self._library

    def _init_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Category list
        left = QWidget()
        left.setFixedWidth(140)
        left.setStyleSheet(dialog_pane_qss())
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)

        cat_label = QLabel("LIBRARY")
        cat_label.setStyleSheet(pane_header_qss())
        left_layout.addWidget(cat_label)

        self.category_list = QListWidget()
        for key, label in _CATEGORIES:
            self.category_list.addItem(QListWidgetItem(label))
        self.category_list.setProperty("keys", [k for k, _ in _CATEGORIES])
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        left_layout.addWidget(self.category_list)
        root.addWidget(left)

        # Item list
        mid = QWidget()
        mid.setFixedWidth(220)
        mid.setStyleSheet(dialog_pane_qss())
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(8, 8, 8, 8)

        self.items_label = QLabel("ITEMS")
        self.items_label.setStyleSheet(pane_header_qss())
        mid_layout.addWidget(self.items_label)

        self.item_search = QLineEdit()
        self.item_search.setPlaceholderText("Search cameras…")
        self.item_search.textChanged.connect(self._on_item_search_changed)
        mid_layout.addWidget(self.item_search)

        self.item_list = QListWidget()
        self.item_list.currentRowChanged.connect(self._on_item_changed)
        mid_layout.addWidget(self.item_list)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton()
        self.add_btn.setIcon(qta.icon("fa5s.plus", color=THEME.text_primary))
        self.add_btn.setToolTip("Add item")
        self.add_btn.clicked.connect(self._add_item)
        self.dup_btn = QPushButton()
        self.dup_btn.setIcon(qta.icon("fa5s.copy", color=THEME.text_primary))
        self.dup_btn.setToolTip("Duplicate")
        self.dup_btn.clicked.connect(self._duplicate_item)
        self.edit_btn = QPushButton()
        self.edit_btn.setIcon(qta.icon("fa5s.pen", color=THEME.text_primary))
        self.edit_btn.setToolTip("Rename the preset, or change which fields it stores")
        self.edit_btn.clicked.connect(self._edit_preset)
        self.del_btn = QPushButton()
        self.del_btn.setIcon(qta.icon("fa5s.trash-alt", color=THEME.text_primary))
        self.del_btn.setToolTip("Delete")
        self.del_btn.clicked.connect(self._delete_item)
        for b in (self.add_btn, self.dup_btn, self.edit_btn, self.del_btn):
            b.setFixedWidth(36)
            btn_row.addWidget(b)
        btn_row.addStretch()
        mid_layout.addLayout(btn_row)

        root.addWidget(mid)

        # Form: a single layout, with rows shown and hidden per category, never removeRow.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 16, 16, 16)

        self.display_name_edit = QLineEdit()
        self.make_edit = QLineEdit()
        self.model_edit = QLineEdit()
        self.lens_model_edit = QLineEdit()
        self.focal_spin = QDoubleSpinBox()
        self.focal_spin.setRange(0, 2000)
        self.focal_spin.setSuffix(" mm")
        self.aperture_spin = QDoubleSpinBox()
        self.aperture_spin.setRange(0, 64)
        self.aperture_spin.setDecimals(1)
        self.aperture_spin.setPrefix("f/")
        self.manufacturer_edit = QLineEdit()
        self.stock_name_edit = QLineEdit()
        self.iso_spin = QSpinBox()
        self.iso_spin.setRange(1, 12800)
        self.format_combo = QComboBox()
        self.format_combo.addItems([e.value for e in FilmFormat])
        self.color_combo = QComboBox()
        self.color_combo.addItems([e.value for e in FilmColorType])
        self.developer_edit = QLineEdit()
        self.developer_edit.setPlaceholderText("e.g. D-76 1+1")
        self.push_pull_combo = QComboBox()
        self.push_pull_combo.addItems([PUSH_PULL_LABELS[v] for v in PUSH_PULL_VALUES])
        self.dilution_edit = QLineEdit()
        self.dilution_edit.setPlaceholderText("e.g. 1+50, stock")
        self.dev_time_edit = QLineEdit()
        self.dev_time_edit.setPlaceholderText(DEV_TIME_HINT)
        self.dev_temp_edit = QLineEdit()
        self.dev_temp_edit.setPlaceholderText("e.g. 20")
        self.scanning_edit = QLineEdit()
        self.scanning_edit.setPlaceholderText("e.g. DSLR copy-stand scan")
        self.notes_edit = QLineEdit()

        for w in (
            self.display_name_edit,
            self.make_edit,
            self.model_edit,
            self.lens_model_edit,
            self.manufacturer_edit,
            self.stock_name_edit,
            self.developer_edit,
            self.dilution_edit,
            self.dev_time_edit,
            self.dev_temp_edit,
            self.scanning_edit,
            self.notes_edit,
        ):
            w.textChanged.connect(self._on_form_changed)
        self.focal_spin.valueChanged.connect(self._on_form_changed)
        self.aperture_spin.valueChanged.connect(self._on_form_changed)
        self.iso_spin.valueChanged.connect(self._on_form_changed)
        self.format_combo.currentIndexChanged.connect(self._on_form_changed)
        self.color_combo.currentIndexChanged.connect(self._on_form_changed)
        self.push_pull_combo.currentIndexChanged.connect(self._on_form_changed)

        self.form_panel = QWidget()
        self.form_layout = QFormLayout(self.form_panel)
        self.form_layout.setSpacing(8)
        self._form_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._register_form_row("display_name", "Display name", self.display_name_edit)
        self._register_form_row("make", "Make", self.make_edit)
        self._register_form_row("model", "Model", self.model_edit)
        self._register_form_row("lens_model", "Lens model", self.lens_model_edit)
        self._register_form_row("focal", "Focal length", self.focal_spin)
        self._register_form_row("aperture", "Max aperture", self.aperture_spin)
        self._register_form_row("manufacturer", "Manufacturer", self.manufacturer_edit)
        self._register_form_row("stock_name", "Stock name", self.stock_name_edit)
        self._register_form_row("iso", "ISO", self.iso_spin)
        self._register_form_row("format", "Format", self.format_combo)
        self._register_form_row("color_type", "Color type", self.color_combo)
        self._register_form_row("developer", "Developer", self.developer_edit)
        self._register_form_row("dilution", "Dilution", self.dilution_edit)
        self._register_form_row("push_pull", "Push / Pull", self.push_pull_combo)
        self._register_form_row("dev_time", "Time", self.dev_time_edit)
        self._register_form_row("dev_temp", "Temperature (°C)", self.dev_temp_edit)
        self._register_form_row("scanning", "Scanning", self.scanning_edit)
        self._register_form_row("notes", "Notes", self.notes_edit)

        right_layout.addWidget(self.form_panel)

        self.preset_panel = QWidget()
        preset_layout = QVBoxLayout(self.preset_panel)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        preset_layout.setSpacing(8)
        self.preset_name_label = QLabel()
        self.preset_name_label.setStyleSheet(f"color: {THEME.text_primary}; font-weight: bold;")
        self.preset_fields_layout = QFormLayout()
        self.preset_fields_layout.setSpacing(8)
        self.preset_empty_label = QLabel("This preset stores nothing.")
        self.preset_empty_label.setStyleSheet(f"color: {THEME.text_secondary};")
        preset_layout.addWidget(self.preset_name_label)
        preset_layout.addLayout(self.preset_fields_layout)
        preset_layout.addWidget(self.preset_empty_label)
        preset_layout.addWidget(hint_label("Edit a value in the Metadata panel, then save over the preset."))
        self.preset_panel.setVisible(False)
        right_layout.addWidget(self.preset_panel)
        right_layout.addStretch()

        close_row = QHBoxLayout()
        close_row.addStretch()
        save_btn = QPushButton("Done")
        save_btn.clicked.connect(self.accept)
        close_row.addWidget(save_btn)
        right_layout.addLayout(close_row)

        root.addWidget(right)

    def _register_form_row(self, key: str, label_text: str, widget: QWidget) -> None:
        label = field_label(label_text)
        self.form_layout.addRow(label, widget)
        self._form_rows[key] = (label, widget)

    def _show_form_for_category(self, category: str) -> None:
        visible = _CATEGORY_FIELDS[category]
        for key, (label, widget) in self._form_rows.items():
            show = key in visible
            label.setVisible(show)
            widget.setVisible(show)

    def _current_items(self) -> list:
        if self._category == "cameras":
            return self._library.cameras
        if self._category == "lenses":
            return self._library.lenses
        if self._category == "film_stocks":
            return self._library.film_stocks
        if self._category == "processes":
            return self._library.processes
        if self._category == "scan_setups":
            return self._library.scan_setups
        return sorted(MetadataPresets.list_presets())

    def _set_current_items(self, items: list) -> None:
        if self._category == "cameras":
            self._library.cameras = items
        elif self._category == "lenses":
            self._library.lenses = items
        elif self._category == "film_stocks":
            self._library.film_stocks = items
        elif self._category == "processes":
            self._library.processes = items
        elif self._category == "scan_setups":
            self._library.scan_setups = items

    def _item_label(self, item) -> str:
        """A preset is its own name; a library record has a resolved one."""
        if isinstance(item, str):
            return item
        return item.resolved_display_name

    def _item_id(self, item) -> str:
        return item if isinstance(item, str) else item.id

    def _select_category(self, key: str) -> None:
        for i, (k, _) in enumerate(_CATEGORIES):
            if k == key:
                self.category_list.setCurrentRow(i)
                break

    def _on_category_changed(self, row: int) -> None:
        if row < 0:
            return
        self._category = _CATEGORIES[row][0]
        self.item_search.blockSignals(True)
        self.item_search.clear()
        self.item_search.setPlaceholderText(_CATEGORY_SEARCH_PLACEHOLDER.get(self._category, "Search…"))
        self.item_search.blockSignals(False)
        self._rebuild_item_list()
        self._show_form_for_category(self._category)
        is_presets = self._category == _PRESETS
        self.form_panel.setVisible(not is_presets)
        self.preset_panel.setVisible(is_presets)
        self.edit_btn.setVisible(is_presets)
        self.add_btn.setEnabled(not is_presets or self._current_config is not None)
        self.add_btn.setToolTip("Store the current frame's metadata as a preset" if is_presets else "Add item")

    def _on_item_search_changed(self, _text: str) -> None:
        self._rebuild_item_list()

    def _rebuild_item_list(self, *, select_id: str | None = None) -> None:
        all_items = self._current_items()
        selected_id = select_id
        if selected_id is None and 0 <= self._selected_idx < len(all_items):
            selected_id = self._item_id(all_items[self._selected_idx])

        query = self.item_search.text().strip()
        visible = [item for item in all_items if self._matches(item, query)]

        self._list_items = visible
        self.item_list.blockSignals(True)
        self.item_list.clear()
        for item in visible:
            self.item_list.addItem(QListWidgetItem(self._item_label(item)))

        row = -1
        if visible:
            if selected_id:
                row = next((i for i, item in enumerate(visible) if self._item_id(item) == selected_id), -1)
            if row < 0 and select_id is not None:
                row = next((i for i, item in enumerate(visible) if self._item_id(item) == select_id), 0)
            elif row < 0 and not query:
                row = 0
        self.item_list.setCurrentRow(row)
        self.item_list.blockSignals(False)

        if not visible and not query:
            self._selected_idx = -1
            self._clear_form()
        elif row >= 0:
            self._on_item_changed(row)

    def _matches(self, item, query: str) -> bool:
        if isinstance(item, str):
            return query.strip().casefold() in item.casefold()
        return matches_gear_filter(item, query)

    def _on_item_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._list_items):
            self._selected_idx = -1
            self._set_form_editable(True)
            self._clear_form()
            return
        item = self._list_items[row]
        item_id = self._item_id(item)
        all_items = self._current_items()
        self._selected_idx = next(i for i, candidate in enumerate(all_items) if self._item_id(candidate) == item_id)
        self._set_form_editable(isinstance(item, str) or not item.is_bundled)
        self._populate_form(item)

    def _set_form_editable(self, enabled: bool) -> None:
        for _label, widget in self._form_rows.values():
            widget.setEnabled(enabled)
        self.del_btn.setEnabled(enabled)

    def _populate_form(self, item) -> None:
        if isinstance(item, str):
            self._populate_preset(item)
            return
        self._updating = True
        try:
            if isinstance(item, Camera):
                self.display_name_edit.setText(item.display_name)
                self.make_edit.setText(item.make)
                self.model_edit.setText(item.model)
                self.notes_edit.setText(item.notes)
            elif isinstance(item, Lens):
                self.display_name_edit.setText(item.display_name)
                self.make_edit.setText(item.make)
                self.lens_model_edit.setText(item.lens_model)
                self.focal_spin.setValue(item.focal_length_mm or 0)
                self.aperture_spin.setValue(item.max_aperture or 0)
                self.notes_edit.setText(item.notes)
            elif isinstance(item, DevelopmentProcess):
                self.display_name_edit.setText(item.display_name)
                self.developer_edit.setText(item.developer)
                self.dilution_edit.setText(item.dilution)
                self.push_pull_combo.setCurrentIndex(_push_pull_index(item.push_pull))
                self.dev_time_edit.setText(format_dev_time(item.time_seconds))
                self.dev_temp_edit.setText(format_temperature(item.temperature_c))
                self.notes_edit.setText(item.notes)
            elif isinstance(item, ScanSetup):
                self.display_name_edit.setText(item.display_name)
                self.scanning_edit.setText(item.scanning)
                self.notes_edit.setText(item.notes)
            elif isinstance(item, FilmStock):
                self.display_name_edit.setText(item.display_name)
                self.manufacturer_edit.setText(item.manufacturer)
                self.stock_name_edit.setText(item.stock_name)
                self.iso_spin.setValue(item.iso)
                idx = self.format_combo.findText(item.format.value)
                if idx >= 0:
                    self.format_combo.setCurrentIndex(idx)
                idx = self.color_combo.findText(item.color_type.value)
                if idx >= 0:
                    self.color_combo.setCurrentIndex(idx)
                self.notes_edit.setText(item.notes)
        finally:
            self._updating = False

    def _populate_preset(self, name: str) -> None:
        while self.preset_fields_layout.rowCount():
            self.preset_fields_layout.removeRow(0)
        values = preset_values(MetadataPresets.load_preset(name) or {}, "metadata")
        self.preset_name_label.setText(name)
        for label, value in values:
            value_label = QLabel(value)
            value_label.setWordWrap(True)
            value_label.setStyleSheet(f"color: {THEME.text_secondary};")
            self.preset_fields_layout.addRow(field_label(label), value_label)
        self.preset_empty_label.setVisible(not values)

    def _clear_form(self) -> None:
        self.preset_name_label.setText("No preset selected")
        while self.preset_fields_layout.rowCount():
            self.preset_fields_layout.removeRow(0)
        self.preset_empty_label.setVisible(False)
        self._updating = True
        try:
            for w in (
                self.display_name_edit,
                self.make_edit,
                self.model_edit,
                self.lens_model_edit,
                self.manufacturer_edit,
                self.stock_name_edit,
                self.developer_edit,
                self.dilution_edit,
                self.dev_time_edit,
                self.dev_temp_edit,
                self.scanning_edit,
                self.notes_edit,
            ):
                w.clear()
            self.focal_spin.setValue(0)
            self.aperture_spin.setValue(0)
            self.iso_spin.setValue(100)
        finally:
            self._updating = False

    def _on_form_changed(self, *_args) -> None:
        # Presets have no form: their pane is a summary, and the picker writes the file.
        if self._updating or self._selected_idx < 0 or self._category == _PRESETS:
            return
        items = list(self._current_items())
        item = items[self._selected_idx]

        if isinstance(item, Camera):
            item.display_name = self.display_name_edit.text().strip()
            item.make = self.make_edit.text().strip()
            item.model = self.model_edit.text().strip()
            item.notes = self.notes_edit.text().strip()
        elif isinstance(item, Lens):
            item.display_name = self.display_name_edit.text().strip()
            item.make = self.make_edit.text().strip()
            item.lens_model = self.lens_model_edit.text().strip()
            item.focal_length_mm = self.focal_spin.value() or None
            item.max_aperture = self.aperture_spin.value() or None
            item.notes = self.notes_edit.text().strip()
        elif isinstance(item, DevelopmentProcess):
            item.display_name = self.display_name_edit.text().strip()
            item.developer = self.developer_edit.text().strip()
            item.dilution = self.dilution_edit.text().strip()
            item.push_pull = PUSH_PULL_VALUES[self.push_pull_combo.currentIndex()]
            item.time_seconds = parse_dev_time(self.dev_time_edit.text())
            item.temperature_c = parse_temperature(self.dev_temp_edit.text())
            item.notes = self.notes_edit.text().strip()
        elif isinstance(item, ScanSetup):
            item.display_name = self.display_name_edit.text().strip()
            item.scanning = self.scanning_edit.text().strip()
            item.notes = self.notes_edit.text().strip()
        elif isinstance(item, FilmStock):
            item.display_name = self.display_name_edit.text().strip()
            item.manufacturer = self.manufacturer_edit.text().strip()
            item.stock_name = self.stock_name_edit.text().strip()
            item.iso = self.iso_spin.value()
            item.format = FilmFormat(self.format_combo.currentText())
            item.color_type = FilmColorType(self.color_combo.currentText())
            item.notes = self.notes_edit.text().strip()

        items[self._selected_idx] = item
        self._set_current_items(items)
        list_row = next((i for i, visible in enumerate(self._list_items) if self._item_id(visible) == item.id), -1)
        list_entry = self.item_list.item(list_row) if list_row >= 0 else None
        if list_entry is not None:
            list_entry.setText(self._item_label(item))
        GearProfiles.save_library(self._library)
        self.library_changed.emit()

    def _selected_preset(self) -> str:
        items = self._current_items()
        if self._category != _PRESETS or not (0 <= self._selected_idx < len(items)):
            return ""
        return str(items[self._selected_idx])

    def _new_preset_from_frame(self) -> None:
        """A preset is the current frame's metadata, minus the fields left unticked."""
        if self._current_config is None:
            return
        dlg = GranularSettingsDialog(self, self._current_config, "current metadata", ask_name=True, exclude_sections=NON_METADATA_SECTIONS)
        dlg.setWindowTitle("New Metadata Preset")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        MetadataPresets.save_preset(dlg.name(), selected_flat_dict(self._current_config, dlg.selected()))
        self._rebuild_item_list(select_id=dlg.name())
        self.presets_changed.emit()

    def _edit_preset(self) -> None:
        name = self._selected_preset()
        data = MetadataPresets.load_preset(name) if name else None
        if not data:
            return
        cfg = preset_config(data)
        dlg = GranularSettingsDialog(self, cfg, name, ask_name=True, exclude_sections=NON_METADATA_SECTIONS)
        dlg.setWindowTitle("Edit Metadata Preset")
        dlg.set_name(name)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dlg.name()
        MetadataPresets.save_preset(new_name, selected_flat_dict(cfg, dlg.selected()))
        if new_name != name:
            MetadataPresets.delete_preset(name)
        self._rebuild_item_list(select_id=new_name)
        self.presets_changed.emit()

    def _add_item(self) -> None:
        if self._category == _PRESETS:
            self._new_preset_from_frame()
            return
        if self._category == "cameras":
            item = Camera(make="New", model="Camera")
        elif self._category == "lenses":
            item = Lens(lens_model="New lens")
        elif self._category == "processes":
            item = DevelopmentProcess(display_name="New process")
        elif self._category == "scan_setups":
            item = ScanSetup(display_name="New scan setup")
        else:
            item = FilmStock(stock_name="New stock")
        items = list(self._current_items())
        items.append(item)
        self._set_current_items(items)
        GearProfiles.save_library(self._library)
        self._rebuild_item_list(select_id=item.id)
        self.library_changed.emit()

    def _duplicate_item(self) -> None:
        if self._selected_idx < 0:
            return
        if self._category == _PRESETS:
            name = self._selected_preset()
            data = MetadataPresets.load_preset(name) if name else None
            if data is None:
                return
            existing = set(MetadataPresets.list_presets())
            copy_name = next(
                f"{name} copy{'' if i == 1 else f' {i}'}"
                for i in range(1, 100)
                if f"{name} copy{'' if i == 1 else f' {i}'}" not in existing
            )
            MetadataPresets.save_preset(copy_name, data)
            self._rebuild_item_list(select_id=copy_name)
            self.presets_changed.emit()
            return
        import copy

        items = list(self._current_items())
        dup = copy.deepcopy(items[self._selected_idx])
        from negpy.features.metadata.gear_models import _new_id

        dup.id = _new_id()
        dup.is_bundled = False
        items.append(dup)
        self._set_current_items(items)
        GearProfiles.save_library(self._library)
        self._rebuild_item_list(select_id=dup.id)
        self.library_changed.emit()

    def _delete_item(self) -> None:
        if self._selected_idx < 0:
            return
        if QMessageBox.question(self, "Delete", "Delete this item?") != QMessageBox.StandardButton.Yes:
            return
        if self._category == _PRESETS:
            name = self._selected_preset()
            if name:
                MetadataPresets.delete_preset(name)
                self._rebuild_item_list()
                self.presets_changed.emit()
            return
        items = list(self._current_items())
        del items[self._selected_idx]
        self._set_current_items(items)
        GearProfiles.save_library(self._library)
        self._rebuild_item_list()
        self.library_changed.emit()
