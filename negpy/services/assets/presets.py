import json
import os
from typing import List, Dict, Any, Optional
from negpy.kernel.system.config import APP_CONFIG

# Preset-level data that is not a config field. Reserved keys are stripped before a preset
# reaches WorkspaceConfig.from_flat_dict, which would otherwise warn about them as unknown.
PRESET_NOTES_KEY = "__notes"
_RESERVED_PRESET_KEYS = frozenset({PRESET_NOTES_KEY})


def preset_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Just the config fields, without the preset's own bookkeeping."""
    return {k: v for k, v in data.items() if k not in _RESERVED_PRESET_KEYS}


def preset_notes(data: Dict[str, Any]) -> str:
    value = data.get(PRESET_NOTES_KEY)
    return str(value) if value else ""


def with_preset_notes(fields: Dict[str, Any], notes: str) -> Dict[str, Any]:
    """A preset payload ready to save: fields plus notes, which are dropped when empty."""
    out = preset_fields(fields)
    if notes.strip():
        out[PRESET_NOTES_KEY] = notes.strip()
    return out


class Presets:
    """
    JSON I/O for user presets.
    """

    # Subdirectory of presets_dir. Empty = the edit presets themselves.
    _SUBDIR = ""

    @classmethod
    def _dir(cls) -> str:
        return os.path.join(APP_CONFIG.presets_dir, cls._SUBDIR) if cls._SUBDIR else APP_CONFIG.presets_dir

    @classmethod
    def save_preset(cls, name: str, settings: Dict[str, Any]) -> None:
        directory = cls._dir()
        os.makedirs(directory, exist_ok=True)
        filepath = os.path.join(directory, f"{name}.json")
        with open(filepath, "w") as f_out:
            json.dump(settings, f_out, indent=4)

    @classmethod
    def load_preset(cls, name: str) -> Optional[Dict[str, Any]]:
        filepath = os.path.join(cls._dir(), f"{name}.json")
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r") as f_in:
            res = json.load(f_in)
            if isinstance(res, dict):
                return res
            return None

    @classmethod
    def list_presets(cls) -> List[str]:
        directory = cls._dir()
        if not os.path.exists(directory):
            return []
        return [f[:-5] for f in os.listdir(directory) if f.endswith(".json")]

    @classmethod
    def delete_preset(cls, name: str) -> bool:
        filepath = os.path.join(cls._dir(), f"{name}.json")
        if not os.path.exists(filepath):
            return False
        os.remove(filepath)
        return True


class MetadataPresets(Presets):
    """Named sets of Metadata-panel values, in their own namespace so the edit
    preset list stays a list of looks."""

    _SUBDIR = "metadata"
