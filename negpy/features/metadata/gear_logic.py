"""Resolve library selections into MetadataConfig updates."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Union

from negpy.features.metadata.gear_models import Camera, DevelopmentProcess, FilmStock, GearLibrary, Lens, ScanSetup
from negpy.features.metadata.models import PUSH_PULL_LABELS, MetadataConfig

GearItem = Union[Camera, Lens, FilmStock, DevelopmentProcess, ScanSetup]


def gear_search_text(item: GearItem) -> str:
    """Lowercase searchable text for substring filtering."""
    parts: list[str] = []

    if isinstance(item, Camera):
        parts = [item.display_name, item.make, item.model, item.notes]
    elif isinstance(item, Lens):
        parts = [item.display_name, item.make, item.lens_model, item.notes]
        if item.focal_length_mm is not None:
            parts.append(f"{item.focal_length_mm:g}")
        if item.max_aperture is not None:
            parts.append(f"{item.max_aperture:g}")
    elif isinstance(item, FilmStock):
        parts = [
            item.display_name,
            item.manufacturer,
            item.stock_name,
            item.notes,
            str(item.iso),
            item.format.value,
            item.color_type.value,
        ]
    elif isinstance(item, DevelopmentProcess):
        parts = [item.display_name, item.developer, item.dilution, item.notes, PUSH_PULL_LABELS.get(item.push_pull, "")]
    elif isinstance(item, ScanSetup):
        parts = [item.display_name, item.scanning, item.notes]

    return " ".join(p.strip() for p in parts if p and str(p).strip()).lower()


def matches_gear_filter(item: GearItem, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    return needle in gear_search_text(item)


def metadata_from_gear(
    config: MetadataConfig,
    library: GearLibrary,
    *,
    camera_id: Optional[str] = None,
    lens_id: Optional[str] = None,
    film_stock_id: Optional[str] = None,
) -> MetadataConfig:
    """Build updated MetadataConfig from gear library selections.

    Pass ``None`` (default) to leave an id unchanged; pass ``""`` to clear it.
    """
    cam_id = config.camera_id if camera_id is None else camera_id
    lens_id_val = config.lens_id if lens_id is None else lens_id
    film_id = config.film_stock_id if film_stock_id is None else film_stock_id

    camera_make = ""
    camera_model = ""
    lens_make = ""
    lens_model = ""
    focal_length: Optional[float] = None
    max_aperture: Optional[float] = None
    film = config.film
    film_manufacturer = ""
    film_iso: Optional[int] = None
    film_format = config.format
    film_color_type = ""

    if cam_id:
        cam = library.get_camera(cam_id)
        if cam:
            camera_make = cam.make
            camera_model = cam.model

    if lens_id_val:
        lens = library.get_lens(lens_id_val)
        if lens:
            lens_make = lens.make
            lens_model = lens.lens_model or lens.resolved_display_name
            focal_length = lens.focal_length_mm
            max_aperture = lens.max_aperture

    if film_id:
        stock = library.get_film_stock(film_id)
        if stock:
            film = stock.full_film_label
            film_manufacturer = stock.manufacturer
            film_iso = stock.iso
            film_format = stock.format.value
            film_color_type = stock.color_type.value

    return replace(
        config,
        camera_id=cam_id,
        lens_id=lens_id_val,
        film_stock_id=film_id,
        camera_make=camera_make,
        camera_model=camera_model,
        lens_make=lens_make,
        lens_model=lens_model,
        focal_length_mm=focal_length,
        max_aperture=max_aperture,
        film=film,
        film_manufacturer=film_manufacturer,
        film_iso=film_iso,
        format=film_format if film_id else config.format,
        film_color_type=film_color_type,
    )


def metadata_from_process(config: MetadataConfig, library: GearLibrary, process_id: str) -> MetadataConfig:
    """Apply a saved development recipe; an empty id clears the link, not the text."""
    process = library.get_process(process_id) if process_id else None
    if process is None:
        return replace(config, process_id="")
    return replace(
        config,
        process_id=process.id,
        developer=process.developer,
        process_dilution=process.dilution,
        push_pull=process.push_pull,
        process_time_seconds=process.time_seconds,
        process_temperature_c=process.temperature_c,
    )


def metadata_from_scan_setup(config: MetadataConfig, library: GearLibrary, scan_setup_id: str) -> MetadataConfig:
    """Apply a saved digitizing setup; an empty id clears the link, not the text."""
    setup = library.get_scan_setup(scan_setup_id) if scan_setup_id else None
    if setup is None:
        return replace(config, scanning_id="")
    return replace(config, scanning_id=setup.id, scanning=setup.scanning)
