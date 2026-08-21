"""One-time conversion of gear presets into metadata presets.

Gear presets were library records holding three ids (camera, lens, film stock).
Metadata presets store the resolved field values instead, so each one is read
through the library once and written to the metadata preset store under its
display name. Bundled presets convert too — the metadata store has no bundled
tier, so the shipped combinations become the user's own, editable copies.

Best-effort and idempotent: guarded by a done flag, an existing preset of the
same name is never overwritten, and nothing raises into app startup.
"""

from __future__ import annotations

import json
import os

from negpy.features.metadata.gear_logic import metadata_from_gear
from negpy.features.metadata.models import GEAR_FIELDS, MetadataConfig
from negpy.kernel.system.config import APP_CONFIG
from negpy.kernel.system.logging import get_logger
from negpy.kernel.system.paths import get_resource_path
from negpy.services.assets.gear import GearProfiles
from negpy.services.assets.presets import MetadataPresets

logger = get_logger(__name__)

_DONE_FLAG = "gear_presets_migrated"
_GEAR_PRESETS_FILE = "gear_presets.json"
# Windows and macOS both reject these in a filename, and a preset name is one.
_UNSAFE_NAME_CHARS = '/\\:*?"<>|'


def _read_presets(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _safe_name(name: str) -> str:
    return "".join(" " if c in _UNSAFE_NAME_CHARS else c for c in name).strip()


def migrate_gear_presets(repo) -> None:
    """Write every gear preset to the metadata preset store, once."""
    if repo.get_global_setting(_DONE_FLAG):
        return
    try:
        bundled = _read_presets(os.path.join(get_resource_path("gear"), _GEAR_PRESETS_FILE))
        user = _read_presets(os.path.join(APP_CONFIG.gear_dir, _GEAR_PRESETS_FILE))
        seen_ids = {str(p.get("id")) for p in bundled}
        presets = bundled + [p for p in user if str(p.get("id")) not in seen_ids]

        library = GearProfiles.load_library()
        existing = set(MetadataPresets.list_presets())
        for preset in presets:
            name = _safe_name(str(preset.get("displayName") or preset.get("display_name") or ""))
            if not name or name in existing:
                continue
            resolved = metadata_from_gear(
                MetadataConfig(),
                library,
                camera_id=str(preset.get("cameraId") or preset.get("camera_id") or ""),
                lens_id=str(preset.get("lensId") or preset.get("lens_id") or ""),
                film_stock_id=str(preset.get("filmStockId") or preset.get("film_stock_id") or ""),
            )
            MetadataPresets.save_preset(name, {f: getattr(resolved, f) for f in GEAR_FIELDS})
            existing.add(name)
    except Exception as e:  # startup must survive a broken gear file
        logger.warning("Gear preset migration skipped: %s", e)
        return
    repo.save_global_setting(_DONE_FLAG, True)
