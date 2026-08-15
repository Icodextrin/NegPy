# Windows USB setup (Plustek OpticFilm)

NegPy’s Plustek USB backend uses the external [pyopticfilm](https://github.com/jboneng/pyopticfilm) driver (libusb via PyUSB). The stock Plustek Windows driver must not own the device.

## Requirements

- Windows 10/11
- OpticFilm **8200i SE** (`07B3:1825`, GL128) — the only model validated for scan
- WinUSB (or libusbK) bound via [Zadig](https://zadig.akeo.ie/)
- NegPy with the `plustek` optional dependency (`uv sync --group plustek` or `pip install negpy[plustek]`); Windows also pulls `libusb-package` via pyopticfilm

## 1. Confirm the device

With the scanner powered and plugged in:

1. Open **Device Manager**
2. Look under imaging / USB devices for Plustek
3. Properties → Details → Hardware Ids should include `VID_07B3&PID_1825`

## 2. Bind WinUSB (Zadig)

1. Download [Zadig](https://zadig.akeo.ie/)
2. **Options → List All Devices**
3. Select the Plustek Film Scanner (`07B3:1825`)
4. Replace the driver with **WinUSB** (libusbK also works)
5. Keep the stock Plustek driver installer available if you need to restore VueScan/vendor software later

While WinUSB is bound, Plustek’s stock Windows scanning apps will not see the device.

## 3. Run NegPy and scan

From a source checkout:

```powershell
cd path\to\NegPy
uv sync --group plustek
make run
```

Or use a Windows release build (pyopticfilm, PyUSB, and libusb are bundled). In the Scan tab, Backend should be **pyOpticfilm (Plustek)**. Refresh the device list; the SE should appear when WinUSB is bound.

## 4. Restoring the vendor driver

1. Unplug the scanner
2. Device Manager → uninstall the WinUSB device (check “delete driver software” if offered)
3. Reinstall the Plustek / VueScan driver package
4. Replug the scanner

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Empty device list | Wrong PID, unplugged, or still on vendor driver |
| `DriverBindingError` / access denied | WinUSB not bound; another app has the handle |
| `UsbError` / link failures | Cable/hub; try a direct motherboard port |
| UI hints at missing USB / PyUSB | Install the plustek group: `uv sync --group plustek` |
| First scan at a DPI takes a few seconds | Normal — AFE + one dark + one white shading strip (same choreography as SilverFast), then cached per resolution |
| ASIC shading ready (preferred) | Log shows a colour white mean ~50–57k and `median_gain` ~1.1–1.7x, then `ASIC shading ready`, image `shading=True` / DVDSET on. Delete `plustek_calib` and rescan after driver changes so calib is remeasured |
| Colour white mean ~50–57k | Normal. At unity gain DVDSET returns `raw − dark`, so a bright strip reads near full scale. The 11–13k figures in the captures are the *gains* SilverFast computed from it (`0x2000` = 1.0), not white levels |
| Median gain outside 1.02–3.0x | Below the band the strip is already at target (nothing to flatten); above it the light path is too dim — check the lamp, the AFE gains, and that the head sits on clear home chrome |
| Carriage not at home after shading / scan abort | White strip arms `AGOHOME` during the measure; clearing SCAN parks. If park timed out, use SilverFast or power-cycle, then retry |
| Darker corners than SilverFast on Full window | Full window includes ~0.8 mm holder chrome; shading is per-column only, so residual Y falloff at those edges is expected vs a tighter SilverFast frame |
| Positive very dark until you crop (white edges on negative) | Holder chrome is brighter than film base; NegPy auto bounds latch onto it. Both paths clamp border highlights to the film inset (`border highlight clamp…`) — on the ASIC path this is essential, because DVDSET maps that same chrome to full scale by construction. Also raise Process → **Analysis Buffer**, or crop before auto |
| Rainbow vertical “barcode” stripes | The uploaded table was indexed differently from the image. Either the measurement went in the gain slot (gain must be `0xFFFF × 0x2000 / white`, which is proportional to `1/white`), or the blob was packed without its block padding — the AHB table is 512-byte blocks of 126 `(dark, gain)` pairs plus two `gain = 0` pad pairs, so contiguous records slide 8 bytes per block. Delete `plustek_calib` and rescan |
| Diamond / sheared scene (objects lean) | (1) Image X shrunk while USB still paced the full line — the shading table must cover every acquired column (Full window @1800 stays ~2592 px). (2) Odd crop USB width at 1800 (e.g. 2455) — output width must stay even (`optical_span_alignment` + even pixel count). Delete `plustek_calib` (cache v9+) and rescan — log should show even `pixels=` |
| Negative very dark / positive washed bright (vs SilverFast) | Both paths reference *home* chrome, which is brighter than the light at the scan position — at 1800 dpi the film base lands near 42% of full scale, ~1.2 stops down, and NegPy meters a thin negative. `expose_film_base` lifts it with one scalar gain keyed to the brightest channel (`… exposure makeup gain=…`); the gain must stay scalar or it neutralizes the orange mask that inversion needs |
| Strong orange/pink cast on the positive | Check `AFE search done gains=…` for a channel at or near `AFE_GAIN_MAX` (511). At the rail that channel's dark term clips to 0 (`dark0=(0, …)`), so it loses its blacks and tints the whole frame. The search now substitutes SF's session-04 code for any pegged channel; a persistent peg means the AFE gain target is unreachable — the target is an *AFE-strip* level, not a shading white, so do not raise it to match SF's ~50k probe mean |
| Strong green cast on the positive | The border clamp ceiling must be **per channel**. A single joint percentile flattens the margin to neutral grey at a level the dimmest channel never reaches in the film (session 004: joint 27432 vs green's own 19306), so auto Dmin meters green off chrome and lifts it 1.4x. Check `border highlight clamp peak_p99.7=(r,g,b)` — each figure should sit just above that channel's own film peak |
| `white clipped at the rail` | The white strip is pinned near `0xFFFF`, so it carries no shape to flatten — lower the AFE gains. DVDSET stays off and host stretch runs |
| Scan fails: white mean < 20000 | The post-unity strip is too dim to reach target within the 4x gain clamp. Often a stale AHB strip (dark/AFE wait until the buffer has data **at home**; motor-busy `0xa5` is not ready there). Also check the lamp is actually off for dark and the head is on clear home chrome. Cache v6+ ignores collapsed calib |
| Scan fails: colour ASIC shading / clear home field | Carriage not on the clear home sensor; park/power-cycle, then retry (film may stay loaded) |
