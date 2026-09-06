#!/usr/bin/env python3
"""
test_primitives.py - Phase 7 vocabulary A: cone, sphere, torus, revolve
(ADR 0020, commands schema v0.6.0).

Runs the REAL executor (REGISTRY -> vocabulary functions -> undoable
transaction) against the fake FreeCAD (no running FreeCAD), plus the contract
checks that keep the schema, the executor and the brain catalog aligned.

Covers, per command:
  - create_cone   : plain cone, frustum (radius2), degenerate inputs rejected
                    (radius1==radius2 is a cylinder; zero/negative sizes).
  - create_sphere : radius + placement honoured; bad radius rejected.
  - create_torus  : ring/tube radii honoured; tube >= ring rejected.
  - revolve       : consumes a sketch (hidden afterwards), default angle 360,
                    axis letters resolved by the executor (ADR 0011), bad
                    angle/axis rejected; engine-side chaining hooks
                    (PROFILE_CONSUMERS) include revolve.

Runnable:
    python tests/test_primitives.py
    pytest tests/test_primitives.py
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "addon"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "engine"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "shared"))

import mock_freecad  # noqa: E402


def run_scenario():
    mod = mock_freecad.install()
    try:
        from ai_copilot import executor
        import fake_brain
        details = {}
        mod.newDocument("T")

        # 0) Contract alignment: every schema command has an implementation.
        cat = fake_brain.Catalog()
        # Introduced in v0.6.0; the exact current version is pinned by
        # test_vocab_b (the newest vocabulary test).
        major, minor = cat.version.split(".")[:2]
        assert (int(major), int(minor)) >= (0, 6), f"schema: {cat.version}"
        missing = [n for n in cat.names() if n not in executor.REGISTRY]
        assert not missing, f"schema commands without implementation: {missing}"
        extra = [n for n in executor.REGISTRY if cat.spec(n) is None]
        assert not extra, f"implemented commands missing from the schema: {extra}"
        for n in ("create_cone", "create_sphere", "create_torus", "revolve"):
            assert n in cat.names(), f"{n} missing from the schema"
        details["contract"] = f"{len(cat.names())} commands aligned"

        # 1) create_cone: pointed cone + frustum.
        res = executor.execute({"cmd": "create_cone",
                                "params": {"radius1": 10, "height": 25}})
        assert res["ok"], res
        cone = mod.ActiveDocument.getObject(res["created_ids"][0])
        assert cone.TypeId == "Part::Cone" and cone.Radius1 == 10 \
            and cone.Radius2 == 0 and cone.Height == 25, vars(cone)
        res2 = executor.execute({"cmd": "create_cone",
                                 "params": {"radius1": 10, "radius2": 4,
                                            "height": 25}})
        assert res2["ok"], res2
        frustum = mod.ActiveDocument.getObject(res2["created_ids"][0])
        assert frustum.Radius2 == 4, vars(frustum)
        bad = executor.execute({"cmd": "create_cone",
                                "params": {"radius1": 5, "radius2": 5,
                                           "height": 10}})
        assert not bad["ok"] and "cylinder" in bad["error"], bad
        bad2 = executor.execute({"cmd": "create_cone",
                                 "params": {"radius1": 0, "height": 10}})
        assert not bad2["ok"], bad2
        details["cone"] = f"cone+frustum ok, degenerate rejected"

        # 2) create_sphere: radius + placement.
        res = executor.execute({"cmd": "create_sphere",
                                "params": {"radius": 7,
                                           "placement": [1, 2, 3]}})
        assert res["ok"], res
        sph = mod.ActiveDocument.getObject(res["created_ids"][0])
        assert sph.TypeId == "Part::Sphere" and sph.Radius == 7, vars(sph)
        assert sph.Placement.Base == mod.Vector(1, 2, 3), sph.Placement.Base
        bad = executor.execute({"cmd": "create_sphere", "params": {"radius": 0}})
        assert not bad["ok"], bad
        details["sphere"] = "radius+placement ok"

        # 3) create_torus: ring/tube + the self-intersection guard.
        res = executor.execute({"cmd": "create_torus",
                                "params": {"radius1": 20, "radius2": 3}})
        assert res["ok"], res
        tor = mod.ActiveDocument.getObject(res["created_ids"][0])
        assert tor.TypeId == "Part::Torus" and tor.Radius1 == 20 \
            and tor.Radius2 == 3, vars(tor)
        bad = executor.execute({"cmd": "create_torus",
                                "params": {"radius1": 3, "radius2": 20}})
        assert not bad["ok"] and "SMALLER" in bad["error"], bad
        details["torus"] = "ring/tube ok, self-intersection rejected"

        # 4) revolve: sketch -> solid of revolution; profile hidden; defaults.
        res = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "rectangle", "width": 20,
                                           "height": 10}})
        assert res["ok"], res
        sketch_id = res["created_ids"][0]
        res = executor.execute({"cmd": "revolve",
                                "params": {"target": sketch_id, "axis": "X"}})
        assert res["ok"], res
        rev = mod.ActiveDocument.getObject(res["created_ids"][0])
        assert rev.TypeId == "Part::Revolution", rev.TypeId
        assert rev.Angle == 360, rev.Angle           # default full revolution
        assert rev.Axis == mod.Vector(1, 0, 0), rev.Axis  # letter resolved
        assert rev.Base == mod.Vector(0, 0, 0), rev.Base
        assert rev.Solid is True, rev.Solid
        sketch = mod.ActiveDocument.getObject(sketch_id)
        assert sketch.ViewObject.Visibility is False, \
            "the consumed profile must be hidden (like extrude)"
        details["revolve"] = "sketch consumed, angle 360, axis X resolved"

        # 5) revolve validation: bad axis / zero angle / missing target.
        res = executor.execute({"cmd": "create_sketch",
                                "params": {"shape": "circle", "radius": 5}})
        sid = res["created_ids"][0]
        bad = executor.execute({"cmd": "revolve",
                                "params": {"target": sid, "axis": "Q"}})
        assert not bad["ok"] and "axis" in bad["error"].lower(), bad
        bad2 = executor.execute({"cmd": "revolve",
                                 "params": {"target": sid, "angle": 0}})
        assert not bad2["ok"], bad2
        bad3 = executor.execute({"cmd": "revolve",
                                 "params": {"target": "Ghost"}})
        assert not bad3["ok"] and "not found" in bad3["error"], bad3
        # A partial revolution keeps its angle.
        ok = executor.execute({"cmd": "revolve",
                               "params": {"target": sid, "angle": 180,
                                          "axis": "Y"}})
        assert ok["ok"], ok
        rev2 = mod.ActiveDocument.getObject(ok["created_ids"][0])
        assert rev2.Angle == 180, rev2.Angle
        details["revolve_validation"] = "bad axis/angle/target rejected"

        # 6) engine-side chaining hooks: revolve consumes profiles (ADR 0010).
        import bridge_server
        assert "revolve" in bridge_server.PROFILE_CONSUMERS
        assert bridge_server.CONSUMING_PARAMS.get("revolve") == ("target",)
        assert "revolve" in bridge_server.Session._REF_FIELDS
        action = {"type": "command", "cmd": "revolve",
                  "params": {"target": "Sketch"}}
        rewritten, changed = bridge_server.Session._rewrite_extrude_target(
            action, "Sketch003")
        assert changed and rewritten["params"]["target"] == "Sketch003", rewritten
        details["chaining"] = "revolve chains a fresh sketch like extrude"

        return True, details
    finally:
        mock_freecad.uninstall()


def test_primitives():
    ok, details = run_scenario()
    assert ok, f"primitives scenario failed: {details}"


if __name__ == "__main__":
    print("== test_primitives: cone/sphere/torus/revolve (schema v0.6.0) ==")
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
    print("PASS - the Phase 7 primitives and revolve behave as specified.")
    sys.exit(0)
