"""Display-texture ring mechanics (issue #434 use-after-free guard).

The engine hands the main-thread canvas a GPU texture it keeps sampling until the
next frame. If that texture were pooled in `_tex_cache`, the next render would
overwrite it — or `cleanup()` on file load would destroy it — while the canvas
still reads it (a cross-thread use-after-free). The ring keeps display output
textures out of the pool so a rendered frame stays valid until it's replaced.

Tests exercise the pure ring logic with a fake texture so no GPU is required.
"""

import negpy.services.rendering.gpu_engine as ge


class _FakeTex:
    def __init__(self, width, height, usage=0):
        self.width, self.height, self.usage = width, height, usage
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def _bare_engine(monkeypatch):
    monkeypatch.setattr(ge, "GPUTexture", _FakeTex)
    eng = ge.GPUEngine.__new__(ge.GPUEngine)
    eng._tex_cache = {}
    eng._display_ring = [None, None, None]
    eng._display_idx = 0
    return eng


def test_consecutive_renders_hand_out_distinct_textures(monkeypatch):
    eng = _bare_engine(monkeypatch)
    frames = [ge.GPUEngine._get_display_texture(eng, 100, 50, 7) for _ in range(3)]
    # A new render must not target the slot(s) the canvas is still presenting.
    assert len({id(f) for f in frames}) == 3
    # Wraps around and reuses the oldest slot (same size) — by now off-screen.
    assert ge.GPUEngine._get_display_texture(eng, 100, 50, 7) is frames[0]


def test_resolution_change_reallocates_slot(monkeypatch):
    eng = _bare_engine(monkeypatch)
    a = ge.GPUEngine._get_display_texture(eng, 100, 50, 7)
    # Fill the ring, then wrap back to a's slot at a new size.
    ge.GPUEngine._get_display_texture(eng, 100, 50, 7)
    ge.GPUEngine._get_display_texture(eng, 100, 50, 7)
    b = ge.GPUEngine._get_display_texture(eng, 200, 100, 7)
    assert b is not a
    assert (b.width, b.height) == (200, 100)


def test_display_texture_survives_pool_cleanup(monkeypatch):
    eng = _bare_engine(monkeypatch)
    tex = ge.GPUEngine._get_display_texture(eng, 64, 64, 7)
    # It must never enter the shared pool that cleanup() destroys.
    assert tex not in eng._tex_cache.values()
    # Simulate GPUEngine.cleanup() nuking the pool (the load_file path).
    for t in eng._tex_cache.values():
        t.destroy()
    eng._tex_cache.clear()
    assert not tex.destroyed
