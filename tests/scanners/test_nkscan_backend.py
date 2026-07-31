"""NkscanBackend/NkscanSession: capability mapping, lazy prepare, frame index
translation (1-indexed ScanParams.frame -> nkscan's 0-indexed scan(index=)),
the progress/cancel adapter, and error translation.

A fake `nkscan` module is installed into sys.modules for the duration of each
test — NkscanBackend.__init__ and _as_scan_error both do their own `import
nkscan`, so the fake must be importable, not just attached to an instance.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pytest

from negpy.infrastructure.scanners.base import ScanMode, ScannerUnavailable, TransientScanError
from negpy.infrastructure.scanners.nkscan_backend import NkscanBackend, _progress_adapter
from negpy.infrastructure.scanners.params import ScanParams

_DEV_ID = "usb:001:002"
_PARAMS = ScanParams(dpi=1000, depth=16, capture_ir=False)


class _ScannerError(RuntimeError):
    pass


class _TransientError(_ScannerError):
    pass


class _TransportError(_TransientError):
    pass


class _DeviceBusy(_TransientError):
    pass


class _MediaError(_ScannerError):
    pass


class _ScanCancelled(_ScannerError):
    pass


class _UnsupportedError(_ScannerError):
    pass


class _DeviceNotFound(_ScannerError):
    pass


@dataclass
class FakeCapabilities:
    dpi: list[int] = field(default_factory=lambda: [4000, 1000])
    depths: list[int] = field(default_factory=lambda: [16])
    multisample: list[int] = field(default_factory=lambda: [1, 2, 4])
    ir_channel: bool = True
    max_area_mm: tuple[float, float] = (36.0, 24.0)
    auto_exposure: bool = True
    frame_control: bool = True
    detects_frames: bool = False
    senses_frames: bool = True
    single_line: bool = True
    can_eject: bool = True


@dataclass
class FakeDevice:
    id: str
    vendor: str
    model: str
    capabilities: FakeCapabilities


@dataclass
class FakeScanResult:
    rgb: np.ndarray
    ir: np.ndarray | None
    dpi: int
    device_model: str
    frame: int


class FakeSession:
    """Stand-in for nkscan.Session: records prepare()/scan()/lock_gain() calls."""

    def __init__(
        self,
        device_id: str,
        *,
        scan_error: Exception | None = None,
        rgb_shape: tuple[int, int, int] = (6, 5, 3),
        capabilities: FakeCapabilities | None = None,
        progress_steps: int = 2,
        sensed_frames: int | None = 6,
    ) -> None:
        self.device_id = device_id
        self._sensed_frames = sensed_frames
        self.model = "LS-50"
        self.capabilities = capabilities or FakeCapabilities()
        self.prepare_calls: list[dict[str, Any]] = []
        self.scan_calls: list[dict[str, Any]] = []
        self.progress_returns: list[bool] = []
        self.lock_gain_calls = 0
        self.eject_calls = 0
        self.close_calls = 0
        self._scan_error = scan_error
        self._rgb_shape = rgb_shape
        self._progress_steps = progress_steps

    def prepare(self, **kwargs: Any) -> int:
        self.prepare_calls.append(kwargs)
        return 1

    def sensed_frames(self) -> int | None:
        return self._sensed_frames

    def scan(
        self,
        index: int,
        *,
        dpi: int,
        ir: bool,
        focus: str,
        multisample: int,
        single_line: bool,
        window: tuple[float, float, float, float] | None,
        progress: Callable[[int, int], bool] | None = None,
    ) -> FakeScanResult:
        if progress is not None:
            for i in range(1, self._progress_steps + 1):
                self.progress_returns.append(progress(i, self._progress_steps))
        self.scan_calls.append(
            dict(index=index, dpi=dpi, ir=ir, focus=focus, multisample=multisample, single_line=single_line, window=window)
        )
        if self._scan_error is not None:
            raise self._scan_error
        h, w, c = self._rgb_shape
        rgb = np.zeros((h, w, c), dtype=np.uint16)
        ir_arr = np.zeros((h, w), dtype=np.uint16) if ir else None
        return FakeScanResult(rgb=rgb, ir=ir_arr, dpi=dpi, device_model=self.model, frame=index)

    def lock_gain(self) -> None:
        self.lock_gain_calls += 1

    def eject(self) -> bool:
        self.eject_calls += 1
        return True

    def close(self) -> None:
        self.close_calls += 1


class FakeNkscanModule:
    """Stand-in for the compiled `nkscan` extension module."""

    def __init__(self, *, devices: list[FakeDevice] | None = None, session: FakeSession | None = None) -> None:
        self.devices = devices if devices is not None else [FakeDevice(_DEV_ID, "Nikon", "LS-50", FakeCapabilities())]
        self._session = session
        self.opened: list[str] = []
        self.TransientError = _TransientError
        self.TransportError = _TransportError
        self.DeviceBusy = _DeviceBusy
        self.ScannerError = _ScannerError
        self.MediaError = _MediaError
        self.ScanCancelled = _ScanCancelled
        self.UnsupportedError = _UnsupportedError
        self.DeviceNotFound = _DeviceNotFound

    def list_devices(self) -> list[FakeDevice]:
        return self.devices

    def Session(self, device_id: str) -> FakeSession:
        self.opened.append(device_id)
        return self._session or FakeSession(device_id)


@pytest.fixture
def fake_nkscan(monkeypatch: pytest.MonkeyPatch) -> FakeNkscanModule:
    module = FakeNkscanModule()
    monkeypatch.setitem(sys.modules, "nkscan", module)
    return module


def test_backend_unavailable_when_nkscan_is_not_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "nkscan", None)  # forces ImportError on `import nkscan`

    with pytest.raises(ScannerUnavailable, match="uv sync --group nkscan"):
        NkscanBackend()


def test_list_devices_maps_capabilities(fake_nkscan: FakeNkscanModule) -> None:
    backend = NkscanBackend()
    devices = backend.list_devices()

    assert len(devices) == 1
    device = devices[0]
    assert device.id == _DEV_ID
    caps = device.capabilities
    # Dedicated film scanner with no source selector: all three modes, unconditionally.
    assert set(caps.sources) == {ScanMode.NEGATIVE, ScanMode.POSITIVE, ScanMode.TRANSPARENCY}
    assert caps.supported_dpi == (1000, 4000)  # sorted
    assert caps.supported_depths == (16,)
    assert caps.multisample == (1, 2, 4)
    assert caps.single_line is True
    assert caps.ir_channel is True
    assert caps.can_eject is True
    assert caps.supports_exposure_lock is True  # every nkscan session can lock_gain()
    assert caps.adapter_frame_control is True
    assert caps.adapter_frame_capacity == 6  # what the loaded carrier reported


def test_a_device_without_frame_control_offers_no_frame_range(fake_nkscan: FakeNkscanModule) -> None:
    fake_nkscan._session = FakeSession(_DEV_ID, capabilities=FakeCapabilities(frame_control=False))

    caps = NkscanBackend().list_devices()[0].capabilities

    assert caps.adapter_frame_capacity is None
    assert caps.adapter_frame_control is False


@pytest.mark.parametrize(
    ("sensed", "expected"),
    [
        (4, 4),  # a short strip
        (6, 6),
        # 1 is what the transport reports for a strip it has already passed once, so it cannot
        # be told from a genuine single frame — the carrier bound stands instead.
        (1, 6),
        (0, 6),  # nothing loaded
        (None, 6),  # a model that cannot be asked
    ],
)
def test_frame_capacity_follows_the_sensed_count(fake_nkscan: FakeNkscanModule, sensed: int | None, expected: int) -> None:
    fake_nkscan._session = FakeSession(_DEV_ID, sensed_frames=sensed)

    caps = NkscanBackend().list_devices()[0].capabilities

    assert caps.adapter_frame_capacity == expected


def test_capabilities_come_from_the_device_not_the_enumeration_table(fake_nkscan: FakeNkscanModule) -> None:
    """list_devices reports a static per-model table, so the film area it gives is the format
    rather than the loaded adapter's own boundaries. The session's answer wins."""
    fake_nkscan.devices = [FakeDevice(_DEV_ID, "Nikon", "LS-50", FakeCapabilities(max_area_mm=(25.1, 36.8)))]
    fake_nkscan._session = FakeSession(_DEV_ID, capabilities=FakeCapabilities(max_area_mm=(25.06, 37.84)))

    caps = NkscanBackend().list_devices()[0].capabilities

    assert caps.max_area_mm == pytest.approx((25.06, 37.84))


def test_an_unreachable_device_falls_back_to_the_enumeration_table(fake_nkscan: FakeNkscanModule) -> None:
    """A device already held by a scan answers nothing; enumeration must not fail over it."""

    def busy(_device_id: str) -> FakeSession:
        raise _DeviceBusy("held by something else")

    fake_nkscan.Session = busy  # type: ignore[method-assign]
    fake_nkscan.devices = [FakeDevice(_DEV_ID, "Nikon", "LS-50", FakeCapabilities(max_area_mm=(25.1, 36.8)))]

    caps = NkscanBackend().list_devices()[0].capabilities

    assert caps.max_area_mm == pytest.approx((25.1, 36.8))
    assert caps.adapter_frame_capacity == 6


@pytest.mark.parametrize(("auto_exposure", "expected"), [(True, True), (False, False)])
def test_white_balance_lock_follows_host_side_metering(fake_nkscan: FakeNkscanModule, auto_exposure: bool, expected: bool) -> None:
    """An LS-50 meters in firmware and takes the request silently, so the control must not show."""
    fake_nkscan._session = FakeSession(_DEV_ID, capabilities=FakeCapabilities(auto_exposure=auto_exposure))

    caps = NkscanBackend().list_devices()[0].capabilities

    assert caps.supports_white_balance_lock is expected


def test_scan_prepares_lazily_and_only_once_per_session(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    with backend.open_session(_DEV_ID) as session:
        session.scan(_PARAMS, lambda _: None, threading.Event())
        session.scan(_PARAMS, lambda _: None, threading.Event())

    assert len(session_impl.prepare_calls) == 1
    assert len(session_impl.scan_calls) == 2
    assert session_impl.close_calls == 1


def test_open_session_with_lock_white_balance_holds_it_during_prepare(fake_nkscan: FakeNkscanModule) -> None:
    """Independent of lock_exposure()/lock_gain() (which freezes the *gain* a
    metered frame settled on): this affects frame 1's own metering, so the film's
    orange base isn't neutralized away while auto-exposure is computing gains."""
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    with backend.open_session(_DEV_ID, lock_white_balance=True) as session:
        session.scan(_PARAMS, lambda _: None, threading.Event())

    assert session_impl.prepare_calls[0]["lock_white_balance"] is True


def test_open_session_without_lock_white_balance_leaves_it_unlocked(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    with backend.open_session(_DEV_ID) as session:
        session.scan(_PARAMS, lambda _: None, threading.Event())

    assert session_impl.prepare_calls[0]["lock_white_balance"] is False


def test_scan_frame_index_is_translated_from_1_indexed_to_0_indexed(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    with backend.open_session(_DEV_ID) as session:
        session.scan(ScanParams(dpi=1000, depth=16, capture_ir=False, frame=3), lambda _: None, threading.Event())
        session.scan(ScanParams(dpi=1000, depth=16, capture_ir=False, frame=None), lambda _: None, threading.Event())

    assert [c["index"] for c in session_impl.scan_calls] == [2, 0]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (ScanParams(dpi=1000, depth=16, capture_ir=False, frame=2, frame_count=6), 6),
        # No count declared: the table still has to reach the frame being scanned.
        (ScanParams(dpi=1000, depth=16, capture_ir=False, frame=3), 3),
        (ScanParams(dpi=1000, depth=16, capture_ir=False), None),  # let the transport place them
    ],
)
def test_prepare_declares_every_frame_the_pass_will_address(
    fake_nkscan: FakeNkscanModule, params: ScanParams, expected: int | None
) -> None:
    """One prepare covers the whole strip; a short table scans every later frame black."""
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    with backend.open_session(_DEV_ID) as session:
        session.scan(params, lambda _: None, threading.Event())

    assert session_impl.prepare_calls[0]["frames"] == expected


def test_per_frame_offsets_are_passed_through_as_a_table(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()
    params = ScanParams(
        dpi=1000,
        depth=16,
        capture_ir=False,
        frame=1,
        frame_count=3,
        frame_offset_mm=0.5,
        frame_offsets_mm=(0.5, 0.7, 0.9),
    )

    with backend.open_session(_DEV_ID) as session:
        session.scan(params, lambda _: None, threading.Event())

    assert session_impl.prepare_calls[0]["offsets_mm"] == [0.5, 0.7, 0.9]


def test_without_a_table_the_offset_stays_a_single_value(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()
    params = ScanParams(dpi=1000, depth=16, capture_ir=False, frame_offset_mm=0.4)

    with backend.open_session(_DEV_ID) as session:
        session.scan(params, lambda _: None, threading.Event())

    assert session_impl.prepare_calls[0]["offsets_mm"] is None
    assert session_impl.prepare_calls[0]["offset_mm"] == 0.4


def test_one_shot_scan_returns_a_well_formed_result(fake_nkscan: FakeNkscanModule) -> None:
    backend = NkscanBackend()

    result = backend.scan(_DEV_ID, _PARAMS, lambda _: None, threading.Event())

    assert result.rgb.shape == (6, 5, 3)
    assert result.dpi == _PARAMS.dpi
    assert result.device_model == "LS-50"
    assert result.ir is None
    assert result.ir_valid_mask is None


def test_ir_valid_mask_is_set_when_ir_is_captured(fake_nkscan: FakeNkscanModule) -> None:
    backend = NkscanBackend()
    params = ScanParams(dpi=1000, depth=16, capture_ir=True)

    result = backend.scan(_DEV_ID, params, lambda _: None, threading.Event())

    assert result.ir is not None
    assert result.ir_valid_mask is not None
    assert result.ir_valid_mask.shape == result.ir.shape
    assert result.ir_valid_mask.all()


def test_progress_adapter_reports_fraction_and_honours_cancel() -> None:
    """Unit-tested directly: a scan() call with cancel already set never reaches
    the adapter at all (see test_scan_honours_a_precancelled_call below) — this
    is what nkscan's mid-read callback sees once a read is actually underway."""
    seen: list[float] = []
    cancel = threading.Event()
    adapter = _progress_adapter(seen.append, cancel)

    assert adapter(1, 2) is True
    assert adapter(2, 2) is True
    assert seen == pytest.approx([0.5, 1.0])

    cancel.set()
    assert adapter(1, 2) is False  # mid-read cancel: tell nkscan to stop


def test_scan_honours_a_precancelled_call(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(RuntimeError, match="[Cc]ancel"):
        backend.scan(_DEV_ID, _PARAMS, lambda _: None, cancel)

    assert session_impl.scan_calls == []  # never reached the hardware


def test_transient_error_is_translated(fake_nkscan: FakeNkscanModule) -> None:
    fake_nkscan._session = FakeSession(_DEV_ID, scan_error=fake_nkscan.TransientError("USB glitch"))
    backend = NkscanBackend()

    with pytest.raises(TransientScanError):
        backend.scan(_DEV_ID, _PARAMS, lambda _: None, threading.Event())


def test_media_error_is_not_translated_to_transient(fake_nkscan: FakeNkscanModule) -> None:
    fake_nkscan._session = FakeSession(_DEV_ID, scan_error=fake_nkscan.MediaError("no film"))
    backend = NkscanBackend()

    with pytest.raises(Exception) as excinfo:
        backend.scan(_DEV_ID, _PARAMS, lambda _: None, threading.Event())
    assert not isinstance(excinfo.value, TransientScanError)


def test_scan_cancelled_propagates_with_a_cancel_shaped_message(fake_nkscan: FakeNkscanModule) -> None:
    fake_nkscan._session = FakeSession(_DEV_ID, scan_error=fake_nkscan.ScanCancelled("the pass was cancelled"))
    backend = NkscanBackend()

    with pytest.raises(Exception, match="[Cc]ancel"):
        backend.scan(_DEV_ID, _PARAMS, lambda _: None, threading.Event())


def test_lock_exposure_calls_lock_gain(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    with backend.open_session(_DEV_ID) as session:
        session.scan(_PARAMS, lambda _: None, threading.Event())
        session.lock_exposure()

    assert session_impl.lock_gain_calls == 1


def test_eject_closes_the_session(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID)
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    assert backend.eject(_DEV_ID) is True
    assert session_impl.eject_calls == 1
    assert session_impl.close_calls == 1


def test_eject_is_capability_gated_but_still_releases(fake_nkscan: FakeNkscanModule) -> None:
    session_impl = FakeSession(_DEV_ID, capabilities=FakeCapabilities(can_eject=False))
    fake_nkscan._session = session_impl
    backend = NkscanBackend()

    assert backend.eject(_DEV_ID) is False
    assert session_impl.eject_calls == 0
    assert session_impl.close_calls == 1
