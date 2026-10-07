#!/usr/bin/env python3
"""
test_profile_validation.py - Phase 2: new profile primitives + closed-check.

Runs the REAL vocabulary functions against the fake FreeCAD/Part/Sketcher
(no running FreeCAD), verifying:
  - the five new profiles (lozenge, angle/L, T, tube, rtube) build the expected
    closed geometry (line/circle counts + bounding box);
  - the built-in shapes still work (rectangle / polygon / slot / polyline);
  - the closed-check (_validate_closed) rejects an OPEN wire (geometry-driven;
    it does not rely on Coincident constraints);
  - the defensive closed-check inside extrude accepts a closed profile but
    refuses to extrude an open one.

Runnable:
    python tests/test_profile_validation.py
    pytest tests/test_profile_validation.py
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "addon"))

import mock_freecad  # noqa: E402


def _lines(sk):
    return [g for g in sk._geometry if getattr(g, "kind", None) == "line"]


def _circles(sk):
    return [g for g in sk._geometry if getattr(g, "kind", None) == "circle"]


def _bbox(sk):
    xs = []
    ys = []
    for g in sk._geometry:
        if getattr(g, "kind", None) == "circle":
            continue
        for p in (g.StartPoint, g.EndPoint):
            if p is not None:
                xs.append(p.x)
                ys.append(p.y)
    return min(xs), min(ys), max(xs), max(ys)


def run_scenario():
    mod = mock_freecad.install()
    try:
        from ai_copilot.executor.vocabulary.create_sketch import create_sketch
        from ai_copilot.executor.vocabulary.extrude import extrude

        doc = mod.newDocument("T")
        details = {}

        # 0) every built-in shape still builds.
        sk = create_sketch(doc, {"shape": "rectangle", "width": 40,
                                 "height": 30})[0]
        assert len(_lines(sk)) == 4
        assert _bbox(sk) == (0.0, 0.0, 40.0, 30.0)
        details["rectangle"] = "4 lines, 0..40 x 0..30"

        sk = create_sketch(doc, {"shape": "circle", "radius": 12})[0]
        assert len(_circles(sk)) == 1 and _circles(sk)[0].Radius == 12
        details["circle"] = "r=12"

        sk = create_sketch(doc, {"shape": "polygon", "sides": 5,
                                 "radius": 10})[0]
        assert len(_lines(sk)) == 5, f"polygon needs 5 lines, got {len(_lines(sk))}"
        details["polygon"] = "5 lines"

        sk = create_sketch(doc, {"shape": "slot", "length": 60,
                                 "width": 20})[0]
        assert _lines(sk), "slot needs geometry"
        details["slot"] = "rounded slot"

        sk = create_sketch(doc, {"shape": "polyline",
                                 "points": [[0, 0], [10, 0], [10, 10]]})[0]
        assert len(_lines(sk)) >= 2, "polyline needs at least 2 edges"
        details["polyline"] = "closed wire"

        # 1) lozenge -> 4-line rhombus, diagonals = width x height, centred.
        sk = create_sketch(doc, {"shape": "lozenge", "width": 40,
                                 "height": 20})[0]
        assert len(_lines(sk)) == 4, f"lozenge needs 4 lines, got {len(_lines(sk))}"
        assert _bbox(sk) == (-20.0, -10.0, 20.0, 10.0), _bbox(sk)
        details["lozenge"] = "4 lines, -20..20 x -10..10"

        # 2) angle (shape='angle') -> 6-line L, thickness <= min(w,h), centred.
        sk = create_sketch(doc, {"shape": "angle", "width": 40, "height": 30,
                                 "thickness": 8})[0]
        assert len(_lines(sk)) == 6, f"angle needs 6 lines, got {len(_lines(sk))}"
        assert _bbox(sk) == (-20.0, -15.0, 20.0, 15.0), _bbox(sk)
        # 2b) shape='L' is an alias for 'angle'.
        skL = create_sketch(doc, {"shape": "L", "width": 40, "height": 30,
                                  "thickness": 8})[0]
        assert len(_lines(skL)) == 6
        details["angle"] = "6 lines; 'L' alias == 'angle'"

        # 3) T (shape='T') -> 8-line T, web/flange thicknesses, centred.
        sk = create_sketch(doc, {"shape": "T", "width": 40, "height": 30,
                                 "web_thickness": 8, "flange_thickness": 6})[0]
        assert len(_lines(sk)) == 8, f"T needs 8 lines, got {len(_lines(sk))}"
        assert _bbox(sk) == (-20.0, -15.0, 20.0, 15.0), _bbox(sk)
        # 3b) shape='t' is an alias for 'T'.
        skt = create_sketch(doc, {"shape": "t", "width": 40, "height": 30,
                                  "web_thickness": 8, "flange_thickness": 6})[0]
        assert len(_lines(skt)) == 8
        details["T"] = "8 lines; 't' alias == 'T'"

        # 4) tube -> two concentric circles (outer R, inner R - thickness).
        sk = create_sketch(doc, {"shape": "tube", "radius": 10,
                                 "thickness": 4})[0]
        circles = _circles(sk)
        assert len(circles) == 2, f"tube needs 2 circles, got {len(circles)}"
        radii = sorted(c.Radius for c in circles)
        assert radii == [6.0, 10.0], f"tube radii wrong: {radii}"
        details["tube"] = "2 circles, r=10 & r=6"

        # 5) rectangular tube -> two concentric rectangles (8 lines, 2 loops).
        sk = create_sketch(doc, {"shape": "rtube", "width": 40, "height": 30,
                                 "thickness": 4})[0]
        assert len(_lines(sk)) == 8, f"rtube needs 8 lines, got {len(_lines(sk))}"
        details["rtube"] = "8 lines (2 closed loops)"

        # 6) graceful failures (principle 7): clear ValueErrors for the new
        #    shapes when dimensions are missing or out of range.
        bad = [
            ({"shape": "lozenge"}, "lozenge needs width/height"),
            ({"shape": "lozenge", "width": 40, "height": 0}, "lozenge zero height"),
            ({"shape": "angle", "width": 40, "height": 30}, "angle missing thickness"),
            ({"shape": "angle", "width": 40, "height": 30, "thickness": 50},
             "angle thickness too big"),
            ({"shape": "T", "width": 40, "height": 30}, "T missing web_thickness"),
            ({"shape": "T", "width": 40, "height": 30, "web_thickness": 8},
             "T missing flange_thickness"),
            ({"shape": "T", "width": 40, "height": 30, "web_thickness": 8,
              "flange_thickness": 50}, "T flange too big"),
            ({"shape": "tube", "radius": 10}, "tube missing thickness"),
            ({"shape": "tube", "radius": 10, "thickness": 12},
             "tube thickness >= radius"),
            ({"shape": "rtube", "width": 40, "height": 30}, "rtube missing thickness"),
            ({"shape": "rtube", "width": 40, "height": 30, "thickness": 20},
             "rtube thickness too big"),
        ]
        for params, why in bad:
            try:
                create_sketch(doc, params)
                assert False, f"expected ValueError for: {why}"
            except ValueError:
                pass
        details["graceful_failures"] = "ok"

        # 7) the built-in closed-check (_validate_closed) rejects an OPEN wire.
        from ai_copilot.executor.vocabulary.create_sketch import _validate_closed
        sk = create_sketch(doc, {"shape": "lozenge", "width": 40,
                                 "height": 20})[0]
        # A lozenge has no Coincident constraints, so dropping one edge leaves an
        # open wire that only the geometry check can catch.
        sk._geometry.pop()
        try:
            _validate_closed(sk)
            assert False, "an open wire must raise"
        except ValueError:
            pass
        details["closed_check_rejects_open"] = "geometry-driven"

        # 8) extrude: a closed profile extrudes; an open one is refused by the
        #    defensive closed-check inside extrude (principle 7).
        closed = create_sketch(doc, {"shape": "lozenge", "width": 40,
                                     "height": 20})[0]
        ext = extrude(doc, {"target": closed.Name, "distance": 10})
        assert ext and getattr(ext[0], "TypeId", None) == "Part::Extrusion"
        assert ext[0].Shape is not None
        details["extrude_closed_ok"] = "Part::Extrusion built"

        open_sk = create_sketch(doc, {"shape": "lozenge", "width": 40,
                                      "height": 20})[0]
        open_sk._geometry.pop()  # make it open; no Coincident to fall back on
        try:
            extrude(doc, {"target": open_sk.Name, "distance": 10})
            assert False, "extrude must refuse an open profile"
        except ValueError:
            pass
        details["extrude_rejects_open"] = "closed-check in extrude"

        return True, details
    finally:
        mock_freecad.uninstall()


def test_profile_validation():
    ok, _ = run_scenario()
    assert ok


if __name__ == "__main__":
    print("== test_profile_validation: new profiles + closed-check (Phase 2) ==")
    try:
        ok, details = run_scenario()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    except Exception as exc:
        import traceback
        print(f"ERROR: {exc}\n{traceback.format_exc()}")
        sys.exit(1)
    for k, v in details.items():
        print(f"  [ok] {k}: {v}")
    print("PASS - new profiles build closed geometry and the closed-check "
          "rejects open wires (in create_sketch and in extrude).")
    sys.exit(0)
