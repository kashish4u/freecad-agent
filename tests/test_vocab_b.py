#!/usr/bin/env python3
"""
test_vocab_b.py - Phase 7 vocabulary B: rich sketch profiles + loft/sweep/shell
(ADR 0021, commands schema v0.8.0).

Runs the REAL executor against the fake FreeCAD (no running FreeCAD), plus the
pure engine helpers of the multi-profile chaining.

Covers:
  - polygon  : `sides` line segments closing a loop; sides < 3 rejected.
  - slot     : 2 lines + 2 ARCS (the executor-side arc support); length <=
               width rejected.
  - polyline : closed wire from [x,y] points; a [x,y,bulge] point produces an
               arc; fewer than 3 points rejected.
  - loft     : Part::Loft through 2 sketches, both hidden; < 2 profiles or an
               unknown profile rejected.
  - sweep    : Part::Sweep with Sections+Spine, profile and path hidden; same
               sketch for both rejected.
  - shell    : Part::Thickness, open face resolved from where='top' (never a
               model-chosen index, principle 7), inward (negative) value;
               zero thickness rejected.
  - engine   : _RunState pending-profile pool + _rewrite_profile_lists
               (loft list rewrite, sweep profile/path rewrite, order, no
               double-use), _record_consumed with a list param.

Runnable:
    python tests/test_vocab_b.py
    pytest tests/test_vocab_b.py
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "addon"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "engine"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "shared"))

import mock_freecad  # noqa: E402


def _geoms(doc, oid, kind):
    obj = doc.getObject(oid)
    return [g for g in obj._geometry if getattr(g, "kind", None) == kind]


def run_scenario():
    mod = mock_freecad.install()
    try:
        from ai_copilot import executor
        import fake_brain
        details = {}
        doc = mod.newDocument("T")

        # 0) Contract alignment (schema v0.8.0 <-> REGISTRY).
        cat = fake_brain.Catalog()
        assert cat.version == "0.8.0", f"schema version: {cat.version}"
        missing = [n for n in cat.names() if n not in executor.REGISTRY]
        assert not missing, f"schema commands without implementation: {missing}"
        extra = [n for n in executor.REGISTRY if cat.spec(n) is None]
        assert not extra, f"implemented commands missing from the schema: {extra}"
        for n in ("loft", "sweep", "shell"):
            assert n in cat.names(), f"{n} missing from the schema"
        shapes = cat.spec("create_sketch")["params"]["properties"]["shape"]["enum"]
        assert set(shapes) == {"rectangle", "circle", "polygon", "slot",
                               "polyline", "lozenge", "angle", "l", "t",
                               "tube", "rtube"}, shapes
        details["contract"] = f"{len(cat.names())} commands aligned"

        # 1) polygon: hexagon = 6 line segments.
        res = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "polygon", "sides": 6,
                                           "radius": 15}})
        assert res["ok"], res
        hexa = res["created_ids"][0]
        assert len(_geoms(doc, hexa, "line")) == 6, "hexagon needs 6 segments"
        bad = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "polygon", "sides": 2,
                                           "radius": 15}})
        assert not bad["ok"] and "sides" in bad["error"], bad
        details["polygon"] = "6 segments, sides<3 rejected"

        # 2) slot: 2 lines + 2 arcs; length must exceed width.
        res = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "slot", "length": 30,
                                           "width": 10}})
        assert res["ok"], res
        slot_id = res["created_ids"][0]
        assert len(_geoms(doc, slot_id, "line")) == 2, "slot needs 2 lines"
        arcs = _geoms(doc, slot_id, "arc")
        assert len(arcs) == 2, "slot needs 2 arc ends"
        assert all(abs(a.Radius - 5.0) < 1e-9 for a in arcs), \
            [a.Radius for a in arcs]  # width/2
        bad = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "slot", "length": 10,
                                           "width": 10}})
        assert not bad["ok"] and "length > width" in bad["error"], bad
        details["slot"] = "2 lines + 2 arcs (r=width/2)"

        # 3) polyline: closed wire; bulge point makes an arc.
        res = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "polyline",
                                           "points": [[0, 0], [40, 0],
                                                      [40, 20], [0, 20]]}})
        assert res["ok"], res
        poly = res["created_ids"][0]
        assert len(_geoms(doc, poly, "line")) == 4, "closed = 4 edges"
        res = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "polyline",
                                           "points": [[0, 0], [40, 0, 8],
                                                      [40, 20], [0, 20]]}})
        assert res["ok"], res
        poly2 = res["created_ids"][0]
        assert len(_geoms(doc, poly2, "line")) == 3, "3 straight edges"
        assert len(_geoms(doc, poly2, "arc")) == 1, "1 arc edge (bulge)"
        bad = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "polyline",
                                           "points": [[0, 0], [10, 0]]}})
        assert not bad["ok"], bad
        details["polyline"] = "closed wire; bulge -> arc; <3 points rejected"

        # 4) loft: square -> circle, both sketches consumed and hidden.
        r1 = executor.execute({"cmd": "create_sketch",
                               "params": {"shape": "rectangle", "width": 40,
                                          "height": 40}})
        r2 = executor.execute({"cmd": "create_sketch",
                               "params": {"shape": "circle", "radius": 10,
                                          "placement": [0, 0, 30]}})
        s1, s2 = r1["created_ids"][0], r2["created_ids"][0]
        res = executor.execute({"cmd": "loft",
                                "params": {"profiles": [s1, s2]}})
        assert res["ok"], res
        lofted = doc.getObject(res["created_ids"][0])
        assert lofted.TypeId == "Part::Loft", lofted.TypeId
        assert [s.Name for s in lofted.Sections] == [s1, s2], lofted.Sections
        assert lofted.Solid is True and lofted.Ruled is False
        assert doc.getObject(s1).ViewObject.Visibility is False
        assert doc.getObject(s2).ViewObject.Visibility is False
        bad = executor.execute({"cmd": "loft", "params": {"profiles": [s1]}})
        assert not bad["ok"] and "at least 2" in bad["error"], bad
        bad2 = executor.execute({"cmd": "loft",
                                 "params": {"profiles": [s1, "Ghost"]}})
        assert not bad2["ok"] and "not found" in bad2["error"], bad2
        details["loft"] = "2 sections, consumed profiles hidden"

        # 5) sweep: circle profile along a polyline path.
        rp = executor.execute({"cmd": "create_sketch",
                               "params": {"shape": "circle", "radius": 4,
                                          "plane": "XZ"}})
        rw = executor.execute({"cmd": "create_sketch",
                               "params": {"shape": "polyline",
                                          "points": [[0, 0], [50, 0, 10],
                                                     [50, 40], [0, 40]]}})
        prof, path = rp["created_ids"][0], rw["created_ids"][0]
        res = executor.execute({"cmd": "sweep",
                                "params": {"profile": prof, "path": path}})
        assert res["ok"], res
        swept = doc.getObject(res["created_ids"][0])
        assert swept.TypeId == "Part::Sweep", swept.TypeId
        assert [s.Name for s in swept.Sections] == [prof], swept.Sections
        assert swept.Spine[0].Name == path, swept.Spine
        assert swept.Solid is True and swept.Frenet is True
        assert doc.getObject(prof).ViewObject.Visibility is False
        assert doc.getObject(path).ViewObject.Visibility is False
        bad = executor.execute({"cmd": "sweep",
                                "params": {"profile": prof, "path": prof}})
        assert not bad["ok"] and "DIFFERENT" in bad["error"], bad
        details["sweep"] = "sections+spine set, both inputs hidden"

        # 6) shell: hollow a box, open face resolved from where='top'.
        rb = executor.execute({"cmd": "create_box",
                               "params": {"length": 40, "width": 30,
                                          "height": 20}})
        box_id = rb["created_ids"][0]
        res = executor.execute({"cmd": "shell",
                                "params": {"target": box_id, "thickness": 2}})
        assert res["ok"], res
        hollow = doc.getObject(res["created_ids"][0])
        assert hollow.TypeId == "Part::Thickness", hollow.TypeId
        base, refs = hollow.Faces
        assert base.Name == box_id and refs[0].startswith("Face"), hollow.Faces
        assert hollow.Value == -2, hollow.Value        # inward walls
        assert doc.getObject(box_id).ViewObject.Visibility is False
        bad = executor.execute({"cmd": "shell",
                                "params": {"target": box_id, "thickness": 0}})
        assert not bad["ok"], bad
        details["shell"] = f"open face {refs[0]}, value {hollow.Value} (inward)"

        # 7) engine helpers: multi-profile chaining (pure, ADR 0021).
        import bridge_server
        S = bridge_server.Session
        # loft: two unknown refs -> the two pending sketches, in order.
        action = {"type": "command", "cmd": "loft",
                  "params": {"profiles": ["Sketch", "Sketch001"]}}
        out, changed = S._rewrite_profile_lists(
            action, ["SkA", "SkB"], {"Box"})
        assert changed and out["params"]["profiles"] == ["SkA", "SkB"], out
        assert action["params"]["profiles"] == ["Sketch", "Sketch001"], \
            "input must not be mutated"
        # known refs are left alone; pending entries already referenced are
        # not reused for another slot.
        action2 = {"type": "command", "cmd": "loft",
                   "params": {"profiles": ["SkA", "Sketch001"]}}
        out2, changed2 = S._rewrite_profile_lists(
            action2, ["SkA", "SkB"], {"SkA"})
        assert out2["params"]["profiles"] == ["SkA", "SkB"], out2
        # sweep: profile gets the OLDEST pending, path the next.
        action3 = {"type": "command", "cmd": "sweep",
                   "params": {"profile": "Sketch", "path": "Sketch001"}}
        out3, changed3 = S._rewrite_profile_lists(
            action3, ["SkP", "SkW"], set())
        assert out3["params"]["profile"] == "SkP" \
            and out3["params"]["path"] == "SkW", out3
        # pending pool bookkeeping: sketches enter, consumers remove.
        pend = S._update_pending(
            [], {"type": "command", "cmd": "create_sketch", "params": {}},
            {"ok": True, "created_ids": ["SkA"]})
        pend = S._update_pending(
            pend, {"type": "command", "cmd": "create_sketch", "params": {}},
            {"ok": True, "created_ids": ["SkB"]})
        assert pend == ["SkA", "SkB"], pend
        pend2 = S._update_pending(
            pend, {"type": "command", "cmd": "loft",
                   "params": {"profiles": ["SkA", "SkB"]}},
            {"ok": True, "created_ids": ["Loft"]})
        assert pend2 == [], pend2
        pend3 = S._update_pending(
            pend, {"type": "command", "cmd": "extrude",
                   "params": {"target": "SkA", "distance": 5}},
            {"ok": True, "created_ids": ["Ex"]})
        assert pend3 == ["SkB"], pend3
        # consumed-map records every entry of a LIST param (ADR 0016 ext).
        rep = S._record_consumed(
            {"type": "command", "cmd": "loft",
             "params": {"profiles": ["SkA", "SkB"]}},
            {"ok": True, "created_ids": ["Loft"]}, {})
        assert rep == {"SkA": "Loft", "SkB": "Loft"}, rep
        details["engine"] = "pending pool + list rewrite + consumed map ok"

        return True, details
    finally:
        mock_freecad.uninstall()


def test_vocab_b():
    ok, details = run_scenario()
    assert ok, f"vocab B scenario failed: {details}"


if __name__ == "__main__":
    print("== test_vocab_b: rich profiles + loft/sweep/shell (schema v0.8.0) ==")
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
    print("PASS - vocabulary B (rich profiles, loft, sweep, shell) behaves "
          "as specified.")
    sys.exit(0)
