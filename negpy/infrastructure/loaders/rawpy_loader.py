import os
from typing import Any, ContextManager, Optional, Tuple

import numpy as np
import rawpy
import tifffile

from negpy.domain.interfaces import IImageLoader
from negpy.infrastructure.loaders.helpers import NonStandardFileWrapper, read_orientation
from negpy.infrastructure.loaders.ir_planes import find_ir_plane
from negpy.kernel.system.logging import get_logger

logger = get_logger(__name__)

# DNG PhotometricInterpretation value for LinearRaw (TIFF/EP §6.10.4).
_LINEAR_RAW = 34892


def _find_linearraw_page(tif: "tifffile.TiffFile", samples: int) -> Optional[Any]:
    """Return the page carrying `samples`-sample LinearRaw data: page 0 itself (NegPy's own
    single-IFD DNGs) or one of its SubIFDs (VueScan/SilverFast-style thumbnail + SubIFD DNGs)."""
    page0 = tif.pages[0]
    for page in (page0, *(page0.pages or [])):
        tags = getattr(page, "tags", None)
        if tags is None:
            continue
        spp_tag = tags.get("SamplesPerPixel")
        photo_tag = tags.get("PhotometricInterpretation")
        spp = int(spp_tag.value) if spp_tag is not None else 0
        photo = int(photo_tag.value) if photo_tag is not None else 0
        if spp == samples and photo == _LINEAR_RAW:
            return page
    return None


def _is_dng(file_path: str) -> bool:
    return os.path.splitext(file_path)[1].lower() == ".dng"


def _peek_linearraw_4ch(file_path: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Inspect a DNG. If it carries 4 linear samples (RGB + IR), return (rgb, ir) as float32 [0,1].

    NegPy's own `write_dng_linear` produces a single-IFD DNG; VueScan and Adobe-style DNGs
    put the full-res data in a SubIFD behind a reduced-resolution thumbnail IFD0 — both are
    checked. Returns None for camera DNGs (Bayer, 3-channel, etc.) so rawpy can handle them.
    """
    if not _is_dng(file_path):
        return None
    try:
        with tifffile.TiffFile(file_path) as tif:
            page = _find_linearraw_page(tif, samples=4)
            if page is None:
                return None
            arr = page.asarray()  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning(f"DNG peek failed for {file_path}: {e}")
        return None

    if arr.ndim != 3 or arr.shape[2] != 4:
        return None

    if arr.dtype == np.uint16:
        scale = 1.0 / 65535.0
    elif arr.dtype == np.uint8:
        scale = 1.0 / 255.0
    else:
        scale = 1.0
    full = np.clip(arr.astype(np.float32) * scale, 0.0, 1.0)
    rgb = np.ascontiguousarray(full[:, :, :3])
    ir = np.ascontiguousarray(full[:, :, 3])
    return rgb, ir


def _peek_hdri_ir_page(file_path: str) -> Optional[np.ndarray]:
    """IR plane of a SilverFast HDRi DNG, or None.

    SilverFast writes the frame as a *3*-sample LinearRaw SubIFD behind a thumbnail IFD0 and
    puts the infrared record in its own full-resolution grayscale page (NewSubfileType=4) —
    the same layout as its HDRi TIFFs, so only the IR needs reading here: libraw decodes
    3-sample LinearRaw faithfully (a pass-through at `user_wb=[1,1,1,1]`), and keeping it on
    the rawpy path preserves the embedded-thumbnail splash and the half-size fast preview.
    """
    if not _is_dng(file_path):
        return None
    try:
        with tifffile.TiffFile(file_path) as tif:
            main = _find_linearraw_page(tif, samples=3)
            if main is None:
                return None
            main_h, main_w = int(main.shape[0]), int(main.shape[1])
            # Top-level pages are where SilverFast puts it; SubIFDs are searched too for
            # tools that nest it. The main page carries 3 samples, so a 2-D dims match
            # cannot select the image itself.
            candidates = [*tif.pages, *(tif.pages[0].pages or [])]
            return find_ir_plane(candidates, main_h, main_w)
    except Exception as e:
        logger.warning(f"DNG IR-page peek failed for {file_path}: {e}")
    return None


class RawpyLoader(IImageLoader):
    """
    Standard RAW loader (libraw). For LinearRaw 4-channel DNGs (RGB + IR), bypasses
    rawpy and reads via tifffile so the IR plane is preserved. SilverFast HDRi DNGs keep
    the libraw decode and get their IR from a separate grayscale page.
    """

    def load(self, file_path: str) -> Tuple[ContextManager[Any], dict]:
        peeked = _peek_linearraw_4ch(file_path)
        if peeked is not None:
            rgb, ir = peeked
            metadata = {
                "orientation": read_orientation(file_path),
                "raw_flip": 0,
                # Sensor-native linear samples; no ColorSpace names them.
                "color_space": None,
                "ir": ir,
            }
            return NonStandardFileWrapper(rgb), metadata

        raw = rawpy.imread(file_path)

        metadata = {
            "orientation": read_orientation(file_path),
            "raw_flip": 0,
            # Decoded output_color=raw, so the file's own tags characterise nothing here.
            "color_space": None,
            "ir": _peek_hdri_ir_page(file_path),
        }

        return raw, metadata
