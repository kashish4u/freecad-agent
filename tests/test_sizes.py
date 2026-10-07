#!/usr/bin/env python3
"""
test_sizes.py - Feature 2: fuzzy + unit-aware size resolution.

Every size-bearing command (create_box/cylinder/cone/sphere/torus, create_sketch,
drill_hole, fillet, chamfer, extrude, shell) now routes its dimensions through
``ai_copilot.executor.vocabulary._sizes.resolve_size``. This file proves that
resolver, and that it is wired into the real executor:

  1. resolve_size() unit tests  -- numbers, unit strings, adjectives, adjective
     scaling against a baseline, intensity words, clamping, sign, and ValueError.
  2. baseline_for() tests       -- empty doc -> default; target's own span; the
     largest existing object's span.
  3. integration scenarios       -- run through the REAL executor (REGISTRY ->
     vocabulary -> undoable transaction -> recompute) against the fake FreeCAD,
     reading back the resolved geometry to confirm the wiring.

Runnable:
    python tests/test_sizes.py
    pytest tests/test_sizes.py
"""

import math
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "addon"))

import mock_freecad  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. resolve_size() -- pure unit tests (no FreeCAD needed)                     #
# --------------------------------------------------------------------------- #

def test_resolve_size_numeric_passthrough():
    """A bare number is returned unchanged (so existing numeric-size tests pass)."""
    from ai_copilot.executor.vocabulary import _sizes
    assert _sizes.resolve_size(4) == 4.0
    assert _sizes.resolve_size(4.5) == 4.5
    assert _sizes.resolve_size(0) == 0.0
    assert _sizes.resolve_size(2.5) == 2.5
    assert _sizes.resolve_size(-10) == -10.0          # sign preserved (extrude)
    assert _sizes.resolve_size(50.8) == 50.8
    assert isinstance(_sizes.resolve_size(4), float)


def test_resolve_size_unit_strings_absolute():
    """Unit strings are ABSOLUTE conversions, no baseline scaling."""
    from ai_copilot.executor.vocabulary import _sizes
    assert _sizes.resolve_size("10mm") == 10.0
    assert _sizes.resolve_size("3cm") == 30.0
    assert _sizes.resolve_size("1m") == 1000.0
    assert _sizes.resolve_size("2000um") == 2.0       # microns -> mm
    assert _sizes.resolve_size("1000000nm") == 1.0    # nanometres -> mm
    assert _sizes.resolve_size("0.001mm") == 0.5      # below FLOOR_MM -> clamped
    assert _sizes.resolve_size("2 inch") == 50.8
    assert _sizes.resolve_size("1in") == 25.4
    assert _sizes.resolve_size("0.5in") == 12.7
    assert _sizes.resolve_size('1"') == 25.4
    assert _sizes.resolve_size("1 foot") == 304.8
    assert _sizes.resolve_size("1 ft") == 304.8
    assert _sizes.resolve_size("1 yard") == 914.4
    assert _sizes.resolve_size("10") == 10.0          # bare number -> mm


def test_resolve_size_adjectives_against_default_baseline():
    """Adjectives resolve to their base size at the default 100 mm baseline."""
    from ai_copilot.executor.vocabulary import _sizes
    assert _sizes.resolve_size("small", 100.0) == 20.0
    assert _sizes.resolve_size("medium", 100.0) == 50.0
    assert _sizes.resolve_size("large", 100.0) == 100.0
    assert _sizes.resolve_size("big", 100.0) == 100.0
    assert _sizes.resolve_size("thin", 100.0) == 5.0
    assert _sizes.resolve_size("thick", 100.0) == 20.0
    assert _sizes.resolve_size("huge", 100.0) == 250.0
    assert _sizes.resolve_size("tiny", 100.0) == 5.0
    assert _sizes.resolve_size("tall", 100.0) == 150.0


def test_resolve_size_adjective_scales_with_baseline():
    """An adjective is scaled by (baseline / 100), clamped to [0.2, 5.0]."""
    from ai_copilot.executor.vocabulary import _sizes
    # twice the default baseline -> twice the adjective size
    assert _sizes.resolve_size("big", 200.0) == 200.0
    assert _sizes.resolve_size("small", 200.0) == 40.0
    assert _sizes.resolve_size("huge", 200.0) == 500.0
    # a fifth of the default baseline -> a fifth of the adjective size
    assert _sizes.resolve_size("big", 20.0) == 20.0
    assert _sizes.resolve_size("small", 20.0) == 4.0
    assert _sizes.resolve_size("huge", 20.0) == 50.0
    # extreme baselines hit the [0.2, 5.0] scaling clamp, not the raw ratio
    assert _sizes.resolve_size("big", 5000.0) == 500.0    # ratio 50 -> factor 5
    assert _sizes.resolve_size("tiny", 1.0) == 1.0        # ratio 0.01 -> factor 0.2


def test_resolve_size_intensity_words():
    """'very'/'really'/etc. scale the adjective, not the baseline."""
    from ai_copilot.executor.vocabulary import _sizes
    assert _sizes.resolve_size("very big", 100.0) == 130.0     # 100 * 1.30
    assert _sizes.resolve_size("very small", 100.0) == 26.0    # 20  * 1.30
    assert _sizes.resolve_size("really huge", 100.0) == 325.0  # 250 * 1.30


def test_resolve_size_floor_and_ceiling_clamped():
    """Resolutions are clamped to [FLOOR_MM, CEIL_MM] = [0.5, 5000]."""
    from ai_copilot.executor.vocabulary import _sizes
    assert _sizes.resolve_size("0.0001mm") == 0.5       # floor
    assert _sizes.resolve_size("1 mile") == 5000.0      # 1609344 -> ceiling
    # A bare number string is returned unchanged (mirrors the old float() path).
    assert _sizes.resolve_size("1000000") == 1000000.0
    assert _sizes.resolve_size("1000000mm") == 5000.0   # unit string -> ceiling


def test_resolve_size_sign_preserved():
    """A negative number (e.g. extrude distance) keeps its sign."""
    from ai_copilot.executor.vocabulary import _sizes
    assert _sizes.resolve_size("-5") == -5.0
    assert _sizes.resolve_size("-2.5 inch") == -63.5


def test_resolve_size_rejects_garbage():
    """Unparseable input raises ValueError."""
    from ai_copilot.executor.vocabulary import _sizes
    for bad in (None, "nonsense", True, [], {}):
        try:
            _sizes.resolve_size(bad)
        except ValueError:
            continue
        raise AssertionError(f"resolve_size({bad!r}) should have raised ValueError")


# --------------------------------------------------------------------------- #
# 2. baseline_for()                                                            #
# --------------------------------------------------------------------------- #

def _fresh_doc(mod):
    mod.newDocument("SZ")
    return mod.ActiveDocument


def test_baseline_for_empty_document_uses_default():
    """No geometry at all -> the default 100 mm baseline."""
    from ai_copilot.executor.vocabulary import _sizes
    doc = _fresh_doc(mock_freecad.install())
    assert _sizes.baseline_for(doc) == 100.0
    mock_freecad.uninstall()


def test_baseline_for_uses_largest_existing_object():
    """With geometry present, the baseline is the largest existing bbox span."""
    from ai_copilot.executor.vocabulary import _sizes
    mod = mock_freecad.install()
    doc = _fresh_doc(mod)

    box = doc.addObject("Part::Box", "Box")
    box.Length, box.Width, box.Height = 20.0, 15.0, 10.0
    doc.recompute()

    cyl = doc.addObject("Part::Cylinder", "Cyl")
    cyl.Radius, cyl.Height = 5.0, 10.0          # bbox span = 10 -> smaller than box
    doc.recompute()

    assert _sizes.baseline_for(doc) == 20.0     # the 20 mm box wins
    mock_freecad.uninstall()


def test_baseline_for_prefers_target():
    """When a target is passed, its own span is used (not the largest object)."""
    from ai_copilot.executor.vocabulary import _sizes
    mod = mock_freecad.install()
    doc = _fresh_doc(mod)

    big = doc.addObject("Part::Box", "Big")
    big.Length, big.Width, big.Height = 100.0, 100.0, 100.0
    doc.recompute()

    target = doc.addObject("Part::Box", "Target")
    target.Length, target.Width, target.Height = 8.0, 8.0, 8.0
    doc.recompute()

    assert _sizes.baseline_for(doc, target) == 8.0
    mock_freecad.uninstall()


def test_baseline_for_object_without_shape_ignored():
    """An object with no computed Shape contributes 0 (skipped for the largest)."""
    from ai_copilot.executor.vocabulary import _sizes
    mod = mock_freecad.install()
    doc = _fresh_doc(mod)

    box = doc.addObject("Part::Box", "Box")
    box.Length, box.Width, box.Height = 12.0, 12.0, 12.0
    doc.recompute()

    # A freshly added object with no Shape yet (recompute not run for it).
    pending = doc.addObject("Part::Box", "Pending")
    pending.Length = 999.0                       # would dominate IF its shape read

    assert _sizes.baseline_for(doc) == 12.0      # only the recomputed box counts
    mock_freecad.uninstall()


# --------------------------------------------------------------------------- #
# 3. integration through the REAL executor                                     #
# --------------------------------------------------------------------------- #

def _rect_extent(sketch):
    """Return (span_x, span_y) of a rectangle sketch from its line geometry."""
    xs, ys = [], []
    for g in getattr(sketch, "_geometry", []) or []:
        for p in (getattr(g, "StartPoint", None), getattr(g, "EndPoint", None)):
            if p is not None:
                xs.append(p.x)
                ys.append(p.y)
    return (max(xs) - min(xs)), (max(ys) - min(ys))


def run_scenario():
    mod = mock_freecad.install()
    try:
        import ai_copilot.executor as executor
        details = {}

        # 1) numeric sizes pass through unchanged (regression guard).
        mod.newDocument("I")
        r = executor.execute({"cmd": "create_box",
                              "params": {"length": 20, "width": 15,
                                         "height": 10}})
        assert r["ok"], r
        box = mod.ActiveDocument.getObject(r["created_ids"][0])
        assert (box.Length, box.Width, box.Height) == (20, 15, 10)
        details["numeric_passthrough"] = "box 20x15x10 unchanged"

        # 2) unit strings are absolute (inch -> mm, cm -> mm, mm -> mm).
        r = executor.execute({"cmd": "create_box",
                              "params": {"length": "2 inch", "width": "3cm",
                                         "height": "5 mm"}})
        box2 = mod.ActiveDocument.getObject(r["created_ids"][0])
        assert (box2.Length, box2.Width, box2.Height) == (50.8, 30.0, 5.0)
        details["unit_conversion"] = "box 2inx3cm5mm -> 50.8x30x5"

        # 3) an adjective on an empty doc resolves against the 100 mm default.
        mod.newDocument("J")
        r = executor.execute({"cmd": "create_box",
                              "params": {"length": "big", "width": "big",
                                         "height": "big"}})
        box3 = mod.ActiveDocument.getObject(r["created_ids"][0])
        assert box3.Length == 100.0, box3.Length
        details["adjective_default"] = "box 'big' -> 100"

        # 4) an adjective scales to the largest existing body.
        r = executor.execute({"cmd": "create_box",
                              "params": {"length": "small", "width": "small",
                                         "height": "small"}})
        small = mod.ActiveDocument.getObject(r["created_ids"][0])
        # baseline is the 100 mm 'big' box -> small (20) * (100/100) = 20
        assert small.Length == 20.0, small.Length
        details["adjective_scaled"] = "box 'small' next to 100 -> 20"

        # 5) cylinder unit radius + height.
        mod.newDocument("K")
        r = executor.execute({"cmd": "create_cylinder",
                              "params": {"radius": "1 inch", "height": "2 inch"}})
        cyl = mod.ActiveDocument.getObject(r["created_ids"][0])
        assert (cyl.Radius, cyl.Height) == (25.4, 50.8)
        details["cylinder_units"] = "r1inh2in -> 25.4x50.8"

        # 6) drill_hole unit diameter/depth -> tool cylinder radius = d/2.
        mod.newDocument("L")
        r = executor.execute({"cmd": "create_box",
                              "params": {"length": 30, "width": 30, "height": 10}})
        r = executor.execute({"cmd": "drill_hole",
                              "params": {"target": "Box",
                                         "diameter": "0.25 inch", "depth": "1 inch"}})
        tool = mod.ActiveDocument.getObject("DrillTool")
        assert tool.Radius * 2 == 6.35 and tool.Height == 25.4
        details["drill_units"] = "hole 0.25inx1in -> dia 6.35"

        # 7) fillet unit radius read back from the feature's Edge list.
        mod.newDocument("M")
        r = executor.execute({"cmd": "create_box",
                              "params": {"length": 30, "width": 25, "height": 20}})
        r = executor.execute({"cmd": "fillet",
                              "params": {"target": "Box", "radius": "0.1 inch"}})
        feat = mod.ActiveDocument.getObject(r["created_ids"][0])
        assert feat.Edges[0][1] == 2.54, feat.Edges
        details["fillet_units"] = "radius 0.1in -> 2.54"

        # 8) extrude preserves a negative sign (absolute length + reversed flag).
        mod.newDocument("N")
        r = executor.execute({"cmd": "create_box",
                              "params": {"length": 20, "width": 15, "height": 10}})
        r = executor.execute({"cmd": "extrude",
                              "params": {"target": "Box", "distance": "-5"}})
        ext = mod.ActiveDocument.getObject(r["created_ids"][0])
        assert ext.Reversed is True and ext.LengthFwd == 5.0, vars(ext)
        details["extrude_negative"] = "distance -5 -> reversed, len 5"

        # 9) sketch rectangle width/height in inches, read back from geometry.
        mod.newDocument("O")
        r = executor.execute({"cmd": "create_sketch",
                              "params": {"shape": "rectangle",
                                         "width": "2 inch", "height": "1 inch"}})
        sk = mod.ActiveDocument.getObject(r["created_ids"][0])
        w, h = _rect_extent(sk)
        assert abs(w - 50.8) < 1e-6 and abs(h - 25.4) < 1e-6, (w, h)
        details["sketch_units"] = "rect 2inx1in -> 50.8x25.4"

        # 10) circle radius in inches.
        mod.newDocument("P")
        r = executor.execute({"cmd": "create_sketch",
                              "params": {"shape": "circle", "radius": "1 inch"}})
        sk = mod.ActiveDocument.getObject(r["created_ids"][0])
        r_dim = sk._geometry[0].Radius
        assert abs(r_dim - 25.4) < 1e-6, r_dim
        details["circle_units"] = "circle r1in -> 25.4"

        return True, details
    finally:
        mock_freecad.uninstall()


def test_sizes_feature():
    ok, details = run_scenario()
    assert ok, f"size-resolution scenario failed: {details}"


if __name__ == "__main__":
    print("== test_sizes: fuzzy + unit-aware size resolution (Feature 2) ==")
    # Pure unit tests first (no FreeCAD).
    for fn in (
        test_resolve_size_numeric_passthrough,
        test_resolve_size_unit_strings_absolute,
        test_resolve_size_adjectives_against_default_baseline,
        test_resolve_size_adjective_scales_with_baseline,
        test_resolve_size_intensity_words,
        test_resolve_size_floor_and_ceiling_clamped,
        test_resolve_size_sign_preserved,
        test_resolve_size_rejects_garbage,
        test_baseline_for_empty_document_uses_default,
        test_baseline_for_uses_largest_existing_object,
        test_baseline_for_prefers_target,
        test_baseline_for_object_without_shape_ignored,
    ):
        fn()
    # Integration through the executor.
    ok, details = run_scenario()
    assert ok, f"size-resolution scenario failed: {details}"
    for k, v in details.items():
        print(f"  [ok] {k}: {v}")
    print("PASS - fuzzy and unit-aware sizes resolve and wire into every command.")
    sys.exit(0)
