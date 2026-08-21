"""Metadata presets: catalog coverage, the JSON namespace, and the panel's load path."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import fields, replace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtWidgets import QDialog

from conftest import FakeController
from negpy.desktop.settings_catalog import CATALOG, apply_selected_fields, preset_config, rows_for_keys, selected_flat_dict
from negpy.desktop.view.sidebar import metadata as metadata_module
from negpy.desktop.view.sidebar.metadata import MetadataSidebar
from negpy.desktop.view.widgets.gear_library_dialog import GearLibraryDialog
from negpy.domain.models import WorkspaceConfig
from negpy.features.metadata.gear_models import Camera, DevelopmentProcess, FilmStock, GearLibrary, ScanSetup
from negpy.features.metadata.models import GEAR_FIELDS, MetadataConfig
from negpy.features.metadata.payload import build_metadata_payload
from negpy.services.assets.search import facts_for, match, parse_query
from negpy.kernel.system.config import APP_CONFIG
from negpy.services.assets import gear_preset_migration
from negpy.services.assets.gear import GearProfiles
from negpy.services.assets.gear_preset_migration import migrate_gear_presets
from negpy.services.assets.presets import MetadataPresets, Presets

# A frame number belongs to one frame, so it is not offered as a preset field.
_UNPRESETABLE = {"capture_frame"}


class _FakeRepo:
    def __init__(self):
        self._settings: dict = {}

    def get_global_setting(self, key, default=None):
        return self._settings.get(key, default)

    def save_global_setting(self, key, value):
        self._settings[key] = value


def _metadata_rows():
    return [r for title, rows in CATALOG if title == "Metadata" for r in rows]


def test_catalog_covers_every_metadata_field():
    listed = [f for r in _metadata_rows() for f in r.fields]
    assert sorted(listed) == sorted(set(listed)), "a field is listed in two rows"
    assert set(listed) == {f.name for f in fields(MetadataConfig)} - _UNPRESETABLE


def test_gear_row_travels_as_one_unit():
    base = WorkspaceConfig()
    source = replace(
        base,
        metadata=replace(
            base.metadata,
            camera_id="c1",
            camera_make="Nikon",
            camera_model="F3",
            film_stock_id="f1",
            film="Kodak Portra 400",
            film_iso=400,
        ),
    )
    data = selected_flat_dict(source, [r for r in _metadata_rows() if r.label == "Gear"])
    assert data["camera_id"] == "c1" and data["camera_make"] == "Nikon"
    assert data["film_iso"] == 400
    # Nothing outside the gear pick rides along.
    assert "developer" not in data and "scanning" not in data


def test_presets_and_metadata_presets_are_separate_namespaces():
    Presets.save_preset("Portra", {"density": 1.5})
    MetadataPresets.save_preset("Portra", {"developer": "D-76"})

    assert Presets.list_presets() == ["Portra"]
    assert MetadataPresets.list_presets() == ["Portra"]
    assert Presets.load_preset("Portra") == {"density": 1.5}
    assert MetadataPresets.load_preset("Portra") == {"developer": "D-76"}
    assert MetadataPresets.delete_preset("Portra") is True
    assert Presets.load_preset("Portra") == {"density": 1.5}


@pytest.fixture(autouse=True)
def presets_dir(monkeypatch, tmp_path):
    """Never touch the user's own preset store."""
    monkeypatch.setattr(APP_CONFIG, "presets_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def sidebar(monkeypatch) -> MetadataSidebar:
    monkeypatch.setattr(metadata_module.GearProfiles, "load_library", staticmethod(GearLibrary))
    controller = FakeController()
    controller.session.update_config = lambda config, **_kwargs: setattr(controller.state, "config", config)
    return MetadataSidebar(controller)


def test_load_writes_only_the_stored_fields(sidebar: MetadataSidebar) -> None:
    state = sidebar.state
    state.config = replace(state.config, metadata=replace(state.config.metadata, scanning="Flextight", developer="Rodinal"))
    MetadataPresets.save_preset("HP5", {"developer": "D-76 1+1", "push_pull": 1})
    sidebar._refresh_metadata_presets()
    sidebar.metadata_preset_combo.set_selected_id("HP5")

    sidebar._on_metadata_preset_load()

    assert state.config.metadata.developer == "D-76 1+1"
    assert state.config.metadata.push_pull == 1
    assert state.config.metadata.scanning == "Flextight"


def test_load_restores_gear_ids_with_resolved_values(sidebar: MetadataSidebar) -> None:
    base = WorkspaceConfig()
    source = replace(base, metadata=replace(base.metadata, camera_id="c1", camera_make="Nikon", camera_model="F3"))
    MetadataPresets.save_preset("F3", selected_flat_dict(source, [r for r in _metadata_rows() if r.label == "Gear"]))
    sidebar._refresh_metadata_presets()
    sidebar.metadata_preset_combo.set_selected_id("F3")

    sidebar._on_metadata_preset_load()

    meta = sidebar.state.config.metadata
    assert (meta.camera_id, meta.camera_make, meta.camera_model) == ("c1", "Nikon", "F3")


def test_manage_saves_the_current_frame_as_a_preset(sidebar: MetadataSidebar) -> None:
    state = sidebar.state
    state.config = replace(state.config, metadata=replace(state.config.metadata, developer="D-76", scanning="Flextight"))
    dlg = MagicMock()
    dlg.exec.return_value = QDialog.DialogCode.Accepted
    dlg.name.return_value = "Dev only"
    dlg.selected.return_value = [r for r in _metadata_rows() if r.label == "Process"]
    library = GearLibraryDialog(GearLibrary(), current_config=state.config)
    library._select_category("metadata_presets")
    with patch("negpy.desktop.view.widgets.gear_library_dialog.GranularSettingsDialog", return_value=dlg):
        library._add_item()

    assert MetadataPresets.load_preset("Dev only") == {
        "developer": "D-76",
        "process_dilution": "",
        "push_pull": 0,
        "process_time_seconds": None,
        "process_temperature_c": None,
        "process_id": "",
    }


def test_manage_edit_renames_and_keeps_values() -> None:
    MetadataPresets.save_preset(
        "Old",
        {
            "developer": "D-76",
            "process_dilution": "",
            "push_pull": 1,
            "process_time_seconds": None,
            "process_temperature_c": None,
            "process_id": "",
        },
    )
    library = GearLibraryDialog(GearLibrary())
    library._select_category("metadata_presets")
    dlg = MagicMock()
    dlg.exec.return_value = QDialog.DialogCode.Accepted
    dlg.name.return_value = "New"
    dlg.selected.return_value = [r for r in _metadata_rows() if r.label == "Process"]
    with patch("negpy.desktop.view.widgets.gear_library_dialog.GranularSettingsDialog", return_value=dlg):
        library._edit_preset()

    assert MetadataPresets.list_presets() == ["New"]
    assert MetadataPresets.load_preset("New") == {
        "developer": "D-76",
        "process_dilution": "",
        "push_pull": 1,
        "process_time_seconds": None,
        "process_temperature_c": None,
        "process_id": "",
    }


def test_manage_lists_presets_and_summarizes_the_selected_one() -> None:
    MetadataPresets.save_preset("HP5", {"developer": "D-76 1+1", "push_pull": 1, "scanning": "DSLR copy-stand"})
    library = GearLibraryDialog(GearLibrary())
    library._select_category("metadata_presets")

    assert [library.item_list.item(i).text() for i in range(library.item_list.count())] == ["HP5"]
    assert library.preset_name_label.text() == "HP5"
    shown = {
        library.preset_fields_layout.itemAt(i, library.preset_fields_layout.ItemRole.LabelRole).widget().text(): (
            library.preset_fields_layout.itemAt(i, library.preset_fields_layout.ItemRole.FieldRole).widget().text()
        )
        for i in range(library.preset_fields_layout.rowCount())
    }
    assert shown == {"Process": "D-76 1+1 · Push +1", "Scanning": "DSLR copy-stand"}
    assert library.form_panel.isVisible() is False


def test_preset_fields_reach_other_frames_unchanged():
    """The rows a preset stores are the rows applied, whatever else the target holds."""
    data = {"developer": "D-76", "scanning": "Flextight"}
    base = WorkspaceConfig()
    target = replace(base, metadata=replace(base.metadata, developer="Rodinal", capture_roll="Roll042"))
    merged = apply_selected_fields(preset_config(data), target, rows_for_keys(data, "metadata"))
    assert merged.metadata.developer == "D-76"
    assert merged.metadata.scanning == "Flextight"
    assert merged.metadata.capture_roll == "Roll042"


def test_gear_presets_migrate_to_metadata_presets(monkeypatch, tmp_path):
    """A gear preset's three ids become the resolved gear fields, once."""
    monkeypatch.setattr(APP_CONFIG, "gear_dir", str(tmp_path / "gear"))
    os.makedirs(APP_CONFIG.gear_dir, exist_ok=True)
    with open(os.path.join(APP_CONFIG.gear_dir, "gear_presets.json"), "w", encoding="utf-8") as f:
        json.dump([{"id": "p1", "displayName": "FM2 combo", "cameraId": "c1", "filmStockId": "f1"}], f)

    library = GearLibrary(
        cameras=[Camera(id="c1", make="Nikon", model="FM2")],
        film_stocks=[FilmStock(id="f1", manufacturer="Ilford", stock_name="HP5+", iso=400)],
    )
    monkeypatch.setattr(gear_preset_migration.GearProfiles, "load_library", staticmethod(lambda: library))
    monkeypatch.setattr(gear_preset_migration, "get_resource_path", lambda _p: str(tmp_path / "bundled"))

    repo = _FakeRepo()
    migrate_gear_presets(repo)

    stored = MetadataPresets.load_preset("FM2 combo")
    assert stored is not None
    assert stored["camera_id"] == "c1" and stored["camera_make"] == "Nikon"
    assert stored["film"] == "Ilford HP5+" and stored["film_iso"] == 400
    assert set(stored) == set(GEAR_FIELDS)

    # Second run is a no-op, and never overwrites a preset the user has since edited.
    MetadataPresets.save_preset("FM2 combo", {"developer": "D-76"})
    migrate_gear_presets(repo)
    assert MetadataPresets.load_preset("FM2 combo") == {"developer": "D-76"}


def test_migration_skips_a_name_already_taken(monkeypatch, tmp_path):
    monkeypatch.setattr(APP_CONFIG, "gear_dir", str(tmp_path / "gear"))
    os.makedirs(APP_CONFIG.gear_dir, exist_ok=True)
    with open(os.path.join(APP_CONFIG.gear_dir, "gear_presets.json"), "w", encoding="utf-8") as f:
        json.dump([{"id": "p1", "displayName": "Mine", "cameraId": "c1"}], f)
    monkeypatch.setattr(gear_preset_migration.GearProfiles, "load_library", staticmethod(GearLibrary))
    monkeypatch.setattr(gear_preset_migration, "get_resource_path", lambda _p: str(tmp_path / "bundled"))
    MetadataPresets.save_preset("Mine", {"scanning": "Flextight"})

    migrate_gear_presets(_FakeRepo())

    assert MetadataPresets.load_preset("Mine") == {"scanning": "Flextight"}


def test_process_and_scan_setups_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(APP_CONFIG, "gear_dir", str(tmp_path / "gear"))
    GearProfiles._library_cache = None
    GearProfiles.save_library(
        GearLibrary(
            processes=[DevelopmentProcess(id="p1", display_name="D-76 1+1, push +1", developer="D-76 1+1", push_pull=1)],
            scan_setups=[ScanSetup(id="s1", display_name="Copy-stand · Z7", scanning="DSLR copy-stand")],
        )
    )
    loaded = GearProfiles.load_library()

    assert loaded.get_process("p1").developer == "D-76 1+1"
    assert loaded.get_process("p1").push_pull == 1
    assert loaded.get_scan_setup("s1").scanning == "DSLR copy-stand"


def _library_with_process() -> GearLibrary:
    return GearLibrary(
        processes=[DevelopmentProcess(id="p1", display_name="D-76 1+1, push +1", developer="D-76 1+1", push_pull=1)],
        scan_setups=[ScanSetup(id="s1", display_name="Copy-stand · Z7", scanning="DSLR copy-stand")],
    )


def test_picking_a_process_fills_developer_and_push(monkeypatch) -> None:
    library = _library_with_process()
    monkeypatch.setattr(metadata_module.GearProfiles, "load_library", staticmethod(lambda: library))
    controller = FakeController()
    controller.session.update_config = lambda config, **_kwargs: setattr(controller.state, "config", config)
    sidebar = MetadataSidebar(controller)

    sidebar.process_combo.set_selected_id("p1")
    sidebar._on_process_selected()

    meta = sidebar.state.config.metadata
    assert (meta.process_id, meta.developer, meta.push_pull) == ("p1", "D-76 1+1", 1)

    sidebar.scan_setup_combo.set_selected_id("s1")
    sidebar._on_scan_setup_selected()
    assert sidebar.state.config.metadata.scanning_id == "s1"
    assert sidebar.state.config.metadata.scanning == "DSLR copy-stand"


def test_typing_over_a_filled_value_unlinks_the_saved_entry(monkeypatch) -> None:
    library = _library_with_process()
    monkeypatch.setattr(metadata_module.GearProfiles, "load_library", staticmethod(lambda: library))
    controller = FakeController()
    controller.session.update_config = lambda config, **_kwargs: setattr(controller.state, "config", config)
    sidebar = MetadataSidebar(controller)
    sidebar.process_combo.set_selected_id("p1")
    sidebar._on_process_selected()

    sidebar.developer_edit.setText("Rodinal 1+50")
    sidebar._persist_all_metadata_settings()

    meta = sidebar.state.config.metadata
    assert meta.process_id == ""
    assert meta.developer == "Rodinal 1+50"


class TestDevelopmentTime:
    def test_picking_a_process_fills_time_and_temperature(self, monkeypatch) -> None:
        library = GearLibrary(
            processes=[
                DevelopmentProcess(id="p1", display_name="D-76 1+1", developer="D-76", dilution="1+1", time_seconds=570, temperature_c=20.0)
            ]
        )
        monkeypatch.setattr(metadata_module.GearProfiles, "load_library", staticmethod(lambda: library))
        controller = FakeController()
        controller.session.update_config = lambda config, **_kwargs: setattr(controller.state, "config", config)
        sidebar = MetadataSidebar(controller)

        sidebar.process_combo.set_selected_id("p1")
        sidebar._on_process_selected()

        assert sidebar.state.config.metadata.process_time_seconds == 570
        assert sidebar.state.config.metadata.process_temperature_c == 20.0
        assert sidebar.state.config.metadata.process_dilution == "1+1"
        assert sidebar.dev_time_edit.text() == "9:30"
        assert sidebar.dilution_edit.text() == "1+1"

    def test_typed_time_persists_as_seconds(self, sidebar: MetadataSidebar) -> None:
        sidebar.dev_time_edit.setText("11:15")
        sidebar.dev_temp_edit.setText("24.5")
        sidebar._persist_all_metadata_settings()

        assert sidebar.state.config.metadata.process_time_seconds == 675
        assert sidebar.state.config.metadata.process_temperature_c == 24.5

    def test_unreadable_time_is_flagged_and_not_persisted(self, sidebar: MetadataSidebar) -> None:
        sidebar.state.config = replace(
            sidebar.state.config,
            metadata=replace(sidebar.state.config.metadata, process_time_seconds=570),
        )
        sidebar.dev_time_edit.setText("1:75")
        assert sidebar.dev_time_edit.styleSheet() != ""
        sidebar._persist_all_metadata_settings()

        assert sidebar.state.config.metadata.process_time_seconds == 570

    def test_search_matches_a_time_range(self):
        base = WorkspaceConfig()
        cfg = replace(
            base,
            metadata=replace(
                base.metadata,
                developer="D-76",
                process_dilution="1+1",
                process_time_seconds=570,
                process_temperature_c=20.0,
            ),
        )
        facts = facts_for({"name": "roll1-04.tif", "path": "/x/roll1-04.tif"}, cfg)

        assert match(parse_query("devtime:>=9"), facts)
        assert match(parse_query("devtime:<=10 temp:20"), facts)
        assert not match(parse_query("devtime:>12"), facts)
        assert match(parse_query("developer:d-76 devtime:>=9"), facts)
        assert match(parse_query("dilution:1+1"), facts)
        assert not match(parse_query("dilution:1+50"), facts)

    def test_search_skips_a_frame_with_no_time(self):
        facts = facts_for({"name": "x.tif", "path": "/x.tif"}, WorkspaceConfig())
        assert not match(parse_query("devtime:>=1"), facts)
        assert not match(parse_query("temp:20"), facts)

    def test_time_reaches_the_export_payload(self):
        base = WorkspaceConfig()
        meta = replace(
            base.metadata,
            developer="D-76",
            process_dilution="1+1",
            process_time_seconds=570,
            process_temperature_c=20.0,
        )
        payload = build_metadata_payload(meta, GearLibrary(), None)

        assert payload.development_time == "9:30"
        assert payload.development_temperature == "20 °C"
        rows = dict(next(rows for title, rows in payload.to_preview_sections() if title == "Process"))
        assert rows["Development time"] == "9:30"
        assert rows["Temperature"] == "20 °C"
        assert rows["Dilution"] == "1+1"

    def test_dilution_joins_the_developer_in_the_image_description(self):
        base = WorkspaceConfig()
        meta = replace(
            base.metadata,
            developer="D-76",
            process_dilution="1+1",
            description_fields=("developer",),
        )
        payload = build_metadata_payload(meta, GearLibrary(), None)

        assert payload.developer_display() == "D-76 1+1"
        assert "D-76 1+1" in payload.image_description


class TestDilution:
    def test_picking_a_process_fills_dilution(self, monkeypatch) -> None:
        library = GearLibrary(processes=[DevelopmentProcess(id="p1", display_name="HC-110 B", developer="HC-110", dilution="1+31")])
        monkeypatch.setattr(metadata_module.GearProfiles, "load_library", staticmethod(lambda: library))
        controller = FakeController()
        controller.session.update_config = lambda config, **_kwargs: setattr(controller.state, "config", config)
        sidebar = MetadataSidebar(controller)

        sidebar.process_combo.set_selected_id("p1")
        sidebar._on_process_selected()

        assert sidebar.state.config.metadata.process_dilution == "1+31"
        assert sidebar.dilution_edit.text() == "1+31"

    def test_typed_dilution_persists_and_unlinks(self, sidebar: MetadataSidebar) -> None:
        sidebar.dilution_edit.setText("1+50")
        sidebar._persist_all_metadata_settings()

        assert sidebar.state.config.metadata.process_dilution == "1+50"
        assert sidebar.state.config.metadata.process_id == ""

    def test_search_matches_a_dilution(self):
        base = WorkspaceConfig()
        cfg = replace(base, metadata=replace(base.metadata, developer="HC-110", process_dilution="1+31"))
        facts = facts_for({"name": "a.tif", "path": "/a.tif"}, cfg)

        assert match(parse_query("dilution:1+31"), facts)
        assert match(parse_query("developer:hc-110 dilution:1+31"), facts)
        assert not match(parse_query("dilution:1+50"), facts)

    def test_dilution_reaches_the_export_payload(self):
        base = WorkspaceConfig()
        meta = replace(base.metadata, developer="D-76", process_dilution="1+1")
        payload = build_metadata_payload(meta, GearLibrary(), None)

        assert payload.dilution == "1+1"
        assert payload.developer_display() == "D-76 1+1"
        rows = dict(next(rows for title, rows in payload.to_preview_sections() if title == "Process"))
        assert rows["Developer"] == "D-76 1+1"
